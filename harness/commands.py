"""Slash-command semantics for the live companion.

``handle_command(cmd, ctx)`` is PURE in its two arguments: no I/O of its own,
every fact comes from the injected context, every effect flows through its
narrow hooks, and it always returns a reply string (never raises). It never
touches ``Session``. The channel (``harness.channels.telegram``) parses updates
into :class:`ControlCommand` objects; the runtime or the launcher dispatches
them here. Commands: /help, /ping, /setup, /tz, /status, /state (debug-only),
/mute, /version, /commitment, /today, /here, /leave; unknown names get a
pointer to /help.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

from harness.channels.telegram import ControlCommand
from harness.clock import hhmm


@dataclass
class CommandContext:
    """Read-only session facts + narrow hooks handed to ``handle_command``.

    The first five fields are the frozen seam contract; every other field is an
    OPTIONAL extension with a safe default, so a seam-faithful context works
    unchanged. ``handle_command`` never mutates the context and never reaches
    past it into the session.
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
    request_setup: Callable[..., str | None] | None = None  # pre-bootstrap only
    state_summary: Callable[[], dict] | None = None  # dev snapshot (session.state_summary)
    # (mode, kind, hours, start_h) -> reply line; applies from tomorrow
    request_commitment: Callable[..., str] | None = None
    # (off, start_h) -> reply line; today's commitment only
    request_day_edit: Callable[..., str] | None = None
    # () -> reply line ("" = no reply); rings her, never waits for the call
    request_call: Callable[[], str] | None = None
    # () -> reply line; ends the call in progress
    request_hangup: Callable[[], str] | None = None
    # (mode) -> reply line; "together" while he is there, "apart" after he goes
    request_presence: Callable[[str], str] | None = None


# formatting helpers


def _fmt_age(age_h: float) -> str:
    """Age in virtual hours -> human string (minutes under 1 h)."""
    if age_h < 1.0:
        return f"{max(1, int(round(age_h * 60.0)))} min ago"
    return f"{age_h:.1f} h ago"


def _persona_exists(ctx: CommandContext) -> bool:
    """Seam field wins; a minimal context falls back to the store."""
    if ctx.persona_exists is not None:
        return bool(ctx.persona_exists)
    loader = getattr(ctx.store, "load_persona", None)
    return loader is not None and loader() is not None


def _debug_enabled(ctx: CommandContext) -> bool:
    """/state gate: the debug flag (``debug`` or alias ``debug_commands``)."""
    flags = ctx.flags or {}
    return bool(flags.get("debug") or flags.get("debug_commands"))


# per-command handlers (pure: render facts, call hooks, return a string)


def _cmd_help(cmd: ControlCommand, ctx: CommandContext) -> str:
    lines = [
        "Available commands:",
        "/call — call her (voice, on the computer)",
        "/hangup — end the call",
        "/here — he is here now (scenes in person)",
        "/leave — he left (back to texting)",
        "/help — this list",
        "/ping — alive check",
        "/setup [commitment] — onboard a fresh database (pre-bootstrap only)",
        "/commitment job|school|home <hours> [HH:MM] | light | persona — "
        "her fixed commitment, from tomorrow",
        "/today off | HH:MM — today's commitment off, or moved",
        "/tz <IANA> — change timezone, applied at the next rollover",
        "/status — day, local hour, pending proactives, last-exchange age",
        "/state — mood internals (debug-only)",
        "/mute <hours> — pause proactive messages (deferred, never consumed)",
        "/version — commit, seed, active flags",
    ]
    return "\n".join(lines)


def _cmd_ping(cmd: ControlCommand, ctx: CommandContext) -> str:
    clock = ctx.clock
    if clock is None:
        return "pong — alive."
    return f"pong — day {clock.day()}, {hhmm(clock.local_hour())} local."


_COMMITMENT_KINDS = ("job", "school", "home")
COMMITMENT_USAGE = (
    "job|school|home <hours a day> [HH:MM start], light (no big blocks), "
    "or persona (read it from her persona)"
)


def _parse_hhmm(raw: str) -> float | None:
    try:
        hours, minutes = raw.split(":")
        value = int(hours) + int(minutes) / 60.0
    except ValueError:
        return None
    return value if 0.0 <= value < 24.0 else None


def parse_commitment(args: str):
    """``(mode, kind, hours, start_h)`` from command arguments, or a usage
    error string. Empty arguments are ``None`` (nothing asked)."""
    words = args.lower().split()
    if not words:
        return None
    if words == ["light"]:
        return ("light", None, 0.0, None)
    if words == ["persona"]:
        return ("persona", None, 0.0, None)
    if words[0] not in _COMMITMENT_KINDS or len(words) not in (2, 3):
        return f"usage: {COMMITMENT_USAGE}"
    try:
        hours = float(words[1])
    except ValueError:
        return f"usage: {COMMITMENT_USAGE}"
    if not 1.0 <= hours <= 10.0:
        return "hours a day must be between 1 and 10"
    start = None
    if len(words) == 3:
        start = _parse_hhmm(words[2])
        if start is None or start + hours > 23.0 or start < 6.0:
            return "the start must be HH:MM, from 06:00, ending by 23:00"
    return ("user", words[0], hours, start)


def _cmd_setup(cmd: ControlCommand, ctx: CommandContext) -> str:
    commitment = parse_commitment(cmd.args or "")
    if isinstance(commitment, str):
        return commitment
    if _persona_exists(ctx):
        return (
            "/setup refused — the companion is already initialized "
            "(a persona row exists). /setup is pre-bootstrap only."
        )
    hook = ctx.request_setup
    if hook is None:
        return (
            "/setup is not available on this launcher — it did not wire the "
            "setup hook. Initialize at startup instead."
        )
    try:
        detail = hook(commitment) if commitment is not None else hook()
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
    hook = ctx.request_tz_change
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
    clock, store = ctx.clock, ctx.store
    if clock is None or store is None:
        return "/status — unavailable (no clock/store in context)."
    day = clock.day()
    hour = hhmm(clock.local_hour())
    pending = int(ctx.pending_proactive_count)
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
    provider = ctx.state_summary
    summary: dict | None = None
    if callable(provider):
        try:
            raw = provider()
            summary = raw if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001 - degraded to the persisted row
            summary = None
    if not summary:
        latest = getattr(ctx.store, "latest_daily_state", None)
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
    hook = ctx.request_mute
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
    sha = ctx.commit_sha or "unknown"
    seed_s = "unknown" if ctx.seed is None else str(ctx.seed)
    flags = ctx.flags or {}
    flag_s = (
        " ".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(flags.items()))
        or "none"
    )
    return f"llm-behavioral-harness {sha} · seed={seed_s} · flags: {flag_s}"


def _cmd_commitment(cmd: ControlCommand, ctx: CommandContext) -> str:
    parsed = parse_commitment(cmd.args or "")
    if parsed is None:
        return f"usage: /commitment {COMMITMENT_USAGE}"
    if isinstance(parsed, str):
        return parsed
    return _call_hook(ctx.request_commitment, "commitment", *parsed)


def _cmd_today(cmd: ControlCommand, ctx: CommandContext) -> str:
    arg = (cmd.args or "").strip().lower()
    if arg == "off":
        off, start = True, None
    else:
        off, start = False, _parse_hhmm(arg)
        if start is None:
            return "usage: /today off, or /today HH:MM to move today's commitment"
    return _call_hook(ctx.request_day_edit, "today", off, start)


def _cmd_call(cmd: ControlCommand, ctx: CommandContext) -> str:
    return _call_hook(ctx.request_call, "call")


def _cmd_hangup(cmd: ControlCommand, ctx: CommandContext) -> str:
    return _call_hook(ctx.request_hangup, "hangup")


def _cmd_here(cmd: ControlCommand, ctx: CommandContext) -> str:
    return _call_hook(ctx.request_presence, "here", "together")


def _cmd_leave(cmd: ControlCommand, ctx: CommandContext) -> str:
    return _call_hook(ctx.request_presence, "leave", "apart")


def _call_hook(hook: Callable[..., str] | None, command: str, *args) -> str:
    """A hook's reply line, or why it could not give one."""
    if hook is None:
        return f"/{command} is not available on this launcher."
    try:
        return hook(*args)
    except Exception as exc:  # noqa: BLE001 - a command reply is a string
        return f"/{command} failed: {exc}"


# dispatch

_COMMANDS: dict[str, Callable[[ControlCommand, CommandContext], str]] = {
    "help": _cmd_help,
    "ping": _cmd_ping,
    "setup": _cmd_setup,
    "tz": _cmd_tz,
    "status": _cmd_status,
    "state": _cmd_state,
    "mute": _cmd_mute,
    "version": _cmd_version,
    "commitment": _cmd_commitment,
    "today": _cmd_today,
    "call": _cmd_call,
    "hangup": _cmd_hangup,
    "here": _cmd_here,
    "leave": _cmd_leave,
}


def handle_command(cmd: ControlCommand, ctx: CommandContext) -> str:
    """Dispatch one parsed command to its handler and return the reply.

    All effects flow through the context's hooks; the reply is always a string and
    nothing here touches ``Session``. ``cmd.name`` carries no leading slash (the
    channel strips it); a stray slash is tolerated defensively.
    """
    name = cmd.name[1:] if cmd.name.startswith("/") else cmd.name
    handler = _COMMANDS.get(name)
    if handler is None:
        return f"unknown command '/{cmd.name}' — /help lists what I understand."
    return handler(cmd, ctx)
