"""Slash-command semantics for the live companion (Wave 2, worker W-commands, seam S3).

This module is the S3 command seam's semantics side: it owns what every
command MEANS and what it replies, nothing else. The channel side
(``harness.channels.telegram``) parses updates into :class:`ControlCommand`
objects; the runtime (W-runtime) or the launcher dispatches them here.

Purity contract
---------------
``handle_command(cmd, ctx)`` is a PURE function of its two arguments:

- it performs NO I/O of its own — every fact it renders comes from the
  injected ``CommandContext`` (store reads, clock reads, pre-computed
  counts) and every EFFECT flows through the context's narrow hooks
  (``request_tz_change`` / ``request_mute`` / ``request_setup``);
- it NEVER touches ``Session`` — no ``session.on_message``, no closing
  draws, no memory writes. The runtime's ``_on_command`` dispatch calls it
  under the runtime lock; the launcher's interim dispatch calls it on the
  event loop. Both are safe because this module is inert by construction;
- it always returns a reply string and never raises: hook failures are
  caught and reported in the reply (a command reply is a string, no
  exceptions escape).

Command set (the plan's list; NO /reset — destructive operations stay at
the launcher):

    /help              list + usage
    /ping              alive check
    /setup             initialize a fresh database — REFUSES once a persona
                       row exists (pre-bootstrap only)
    /tz <IANA>         change timezone via ``request_tz_change`` — applied
                       at the next rollover (the virtual clock never jumps
                       backwards)
    /status            day, local hour, pending-proactive count,
                       last-exchange age — runtime facts only, no internals
    /state             mood internals — BEHIND a debug flag: it would
                       contaminate the perceptual experiment otherwise
    /mute <hours>      pause proactive messages via ``request_mute`` —
                       deferred, never consumed
    /version           commit sha, seed, active flags

Unknown commands get a pointer to /help (the channel routes every
slash-command here; rejecting unknown names is this module's job).

Debug gating (/state)
---------------------
``/state`` renders when the context's ``flags`` dict has ``debug`` (or the
alias ``debug_commands``) set. The launcher maps the new env var
``HARNESS_DEBUG_COMMANDS`` (default OFF) onto ``flags["debug"]``; the
W-runtime dispatch may pass any of its own flags the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from harness.channels.telegram import ControlCommand

if TYPE_CHECKING:  # typing-only: the anchor is a pure value, never touched
    from harness.anchor import RealTimeAnchor


@dataclass
class CommandContext:
    """Read-only session facts + narrow hooks handed to ``handle_command``.

    The first five fields are the frozen S3 seam contract (the W-runtime
    dispatch constructs exactly these); every other field is an OPTIONAL
    extension with a safe default, so a seam-faithful context works
    unchanged. ``handle_command`` never mutates the context and never
    reaches past it into the session.
    """

    store: Any  # read-only store facts (load_persona, latest_interaction_t_h, latest_daily_state, kv)
    clock: Any  # VirtualClock (now_h / day / local_hour)
    anchor: Any | None = None  # RealTimeAnchor when the run is anchored, else None
    persona_exists: bool | None = None  # None -> derived from store.load_persona()
    pending_proactive_count: int = 0  # runtime-computed pending schedule rows
    flags: dict[str, bool] = field(default_factory=dict)  # name -> enabled
    request_tz_change: Callable[[str], None] | None = None  # applied at next rollover
    request_mute: Callable[[float], None] | None = None  # defer, never consume
    # --- optional extensions (safe defaults; the runtime may pass them) ---
    seed: int | None = None
    commit_sha: str | None = None
    request_setup: Callable[[], str | None] | None = None  # pre-bootstrap only
    state_summary: Callable[[], dict] | None = None  # dev snapshot (session.state_summary)


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #


def _fmt_hour(h: float) -> str:
    """Local hour -> "HH:MM", exact to the minute (no float drift)."""
    total_min = int(round(h * 60.0)) % (24 * 60)
    hh, mm = divmod(total_min, 60)
    return f"{hh:02d}:{mm:02d}"


def _fmt_age(age_h: float) -> str:
    """Age in virtual hours -> human string (minutes under 1 h)."""
    if age_h < 1.0:
        return f"{max(1, int(round(age_h * 60.0)))} min ago"
    return f"{age_h:.1f} h ago"


def _persona_exists(ctx: CommandContext) -> bool:
    """Seam field wins; a minimal context falls back to the store."""
    pe = getattr(ctx, "persona_exists", None)
    if pe is not None:
        return bool(pe)
    store = getattr(ctx, "store", None)
    loader = getattr(store, "load_persona", None)
    return loader is not None and loader() is not None


def _debug_enabled(ctx: CommandContext) -> bool:
    """/state gate: the debug flag (``debug`` or alias ``debug_commands``)."""
    flags = getattr(ctx, "flags", None) or {}
    return bool(flags.get("debug") or flags.get("debug_commands"))


# --------------------------------------------------------------------------- #
# per-command handlers (all pure: render facts, call hooks, return a string)
# --------------------------------------------------------------------------- #


def _cmd_help(cmd: ControlCommand, ctx: CommandContext) -> str:
    lines = [
        "Available commands:",
        "/help — this list",
        "/ping — alive check",
        "/setup — initialize a fresh database (pre-bootstrap only)",
        "/tz <IANA> — change timezone, applied at the next rollover",
        "/status — day, local hour, pending proactives, last-exchange age",
        "/state — mood internals (debug-only)",
        "/mute <hours> — pause proactive messages (deferred, never consumed)",
        "/version — commit, seed, active flags",
    ]
    return "\n".join(lines)


def _cmd_ping(cmd: ControlCommand, ctx: CommandContext) -> str:
    clock = getattr(ctx, "clock", None)
    if clock is None:
        return "pong — alive."
    return f"pong — day {clock.day()}, {_fmt_hour(clock.local_hour())} local."


def _cmd_setup(cmd: ControlCommand, ctx: CommandContext) -> str:
    if _persona_exists(ctx):
        return (
            "/setup refused — the companion is already initialized "
            "(a persona row exists). /setup is pre-bootstrap only."
        )
    hook = getattr(ctx, "request_setup", None)
    if hook is None:
        return (
            "/setup is only available on a fresh database — start the "
            "launcher with --defer-bootstrap to initialize via /setup."
        )
    try:
        detail = hook()
    except Exception as exc:  # noqa: BLE001 - a command reply is a string
        return f"/setup failed: {exc}"
    if detail:
        return f"setup complete — {detail}"
    return "setup complete — companion initialized."


def _cmd_tz(cmd: ControlCommand, ctx: CommandContext) -> str:
    name = cmd.args.strip()
    if not name:
        return "usage: /tz <IANA timezone> — e.g. /tz America/Mexico_City"
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError et al.
        return (
            f"unknown timezone '{name}' — /tz takes an IANA name "
            "(e.g. America/Mexico_City)."
        )
    hook = getattr(ctx, "request_tz_change", None)
    if hook is None:
        return f"timezone change to {name} requested — no tz hook in this context."
    try:
        hook(name)
    except Exception as exc:  # noqa: BLE001 - a command reply is a string
        return f"/tz failed to record the change: {exc}"
    return (
        f"timezone change to {name} recorded — it applies at the next "
        "rollover (the virtual clock never jumps backwards)."
    )


def _cmd_status(cmd: ControlCommand, ctx: CommandContext) -> str:
    clock = getattr(ctx, "clock", None)
    store = getattr(ctx, "store", None)
    if clock is None or store is None:
        return "/status — unavailable (no clock/store in context)."
    day = clock.day()
    hour = _fmt_hour(clock.local_hour())
    pending = int(getattr(ctx, "pending_proactive_count", 0))
    age_s = "no exchanges yet"
    latest = None
    if hasattr(store, "latest_interaction_t_h"):
        latest = store.latest_interaction_t_h()
    if latest is not None:
        age_s = _fmt_age(max(0.0, clock.now_h() - latest))
    return (
        f"day {day} · {hour} local · {pending} proactive(s) pending · "
        f"last exchange {age_s}"
    )


def _cmd_state(cmd: ControlCommand, ctx: CommandContext) -> str:
    if not _debug_enabled(ctx):
        return (
            "/state is disabled — it is debug-only (it would contaminate "
            "the perceptual experiment)."
        )
    provider = getattr(ctx, "state_summary", None)
    summary: dict | None = None
    if callable(provider):
        try:
            raw = provider()
            summary = raw if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001 - degraded to the persisted row
            summary = None
    if not summary:
        store = getattr(ctx, "store", None)
        latest = getattr(store, "latest_daily_state", None)
        if callable(latest):
            raw = latest()
            summary = raw if isinstance(raw, dict) else None
    if not summary:
        return "/state — no state recorded yet."
    parts = []
    for key in ("day", "M", "m", "g", "mu", "eta", "phase", "phase_label",
                "cycle_day"):
        if key in summary and summary[key] is not None:
            value = summary[key]
            if isinstance(value, float):
                parts.append(f"{key}={value:.3f}")
            else:
                parts.append(f"{key}={value}")
    return "/state — " + " · ".join(parts)


def _cmd_mute(cmd: ControlCommand, ctx: CommandContext) -> str:
    raw = cmd.args.strip()
    if not raw:
        return "usage: /mute <hours> — pauses proactive messages for that many hours"
    try:
        hours = float(raw)
    except ValueError:
        return (
            f"invalid duration '{raw}' — /mute takes hours as a number "
            "(e.g. /mute 4)."
        )
    if not math.isfinite(hours) or hours <= 0:
        return (
            f"invalid duration '{raw}' — /mute takes a positive number of hours."
        )
    hook = getattr(ctx, "request_mute", None)
    if hook is None:
        return f"mute for {hours:g} h requested — no mute hook in this context."
    try:
        hook(hours)
    except Exception as exc:  # noqa: BLE001 - a command reply is a string
        return f"/mute failed to record: {exc}"
    return (
        f"proactive messages muted for {hours:g} h — pending events are "
        "deferred, never consumed."
    )


def _cmd_version(cmd: ControlCommand, ctx: CommandContext) -> str:
    sha = getattr(ctx, "commit_sha", None) or "unknown"
    seed = getattr(ctx, "seed", None)
    seed_s = "unknown" if seed is None else str(seed)
    flags = getattr(ctx, "flags", None) or {}
    flag_s = (
        " ".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(flags.items()))
        or "none"
    )
    return f"llm-behavioral-harness {sha} · seed={seed_s} · flags: {flag_s}"


def _cmd_unknown(cmd: ControlCommand) -> str:
    return f"unknown command '/{cmd.name}' — /help lists what I understand."


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

_COMMANDS: dict[str, Callable[[ControlCommand, CommandContext], str]] = {
    "help": _cmd_help,
    "ping": _cmd_ping,
    "setup": _cmd_setup,
    "tz": _cmd_tz,
    "status": _cmd_status,
    "state": _cmd_state,
    "mute": _cmd_mute,
    "version": _cmd_version,
}


def handle_command(cmd: ControlCommand, ctx: CommandContext) -> str:
    """Dispatch one parsed command to its handler and return the reply.

    Pure by construction (see the module docstring): all effects flow
    through the context's hooks, the reply is always a string, and nothing
    here touches ``Session``. ``cmd.name`` carries no leading slash (the
    channel strips it); a stray slash is tolerated defensively.
    """
    name = cmd.name[1:] if cmd.name.startswith("/") else cmd.name
    handler = _COMMANDS.get(name)
    if handler is None:
        return _cmd_unknown(cmd)
    return handler(cmd, ctx)
