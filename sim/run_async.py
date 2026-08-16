"""Async proactive driver — real-time runtime entrypoint (wave 3, seam A-6).

Mirrors sim/run_interactive's flags (--seed --days --store --feedback
--synthetic --fake --trace --persona-core --model) and adds channel selection
and clock pacing:

    --channel NAME   'cli' | 'telegram' | 'fake'. Default: DEFAULT_CHANNEL,
                     overridden by the HARNESS_CHANNEL env var when the flag
                     is not given.
    --time-scale S   real seconds per virtual hour (default 3600.0 = real
                     time; smaller values accelerate — e.g. 0.001 runs one
                     virtual day in 24 ms). The flag maps 1:1 onto
                     TimeScale.seconds_per_virtual_hour, the runtime's
                     semantic unit.

The schedule is restored from the store when pending events exist
(restart-resume) and planned + persisted otherwise. The run lasts
``--days * 24`` virtual hours: --days is both the schedule horizon and the
run length.

This module never imports sim/run_interactive (it copies the Session
construction pattern instead).

Wave-2 command flags (worker W-commands; ALL OFF by default — existing
invocations behave exactly as before):

    --defer-bootstrap  On a BLANK database (no persona row) the bootstrap is
                       SKIPPED at startup: the companion stays uninitialized
                       and only /setup initializes it (pre-bootstrap only).
                       Once /setup has run, ``ensure_companion_initialized``
                       + the schedule horizon are applied exactly as the
                       normal startup path would have. DEFAULT (flag off) =
                       unconditional bootstrap at startup, unchanged. A
                       non-blank DB is bootstrapped as usual (idempotent).
    --tz <IANA>        IANA timezone for the real-time anchor (S2), e.g.
                       America/Mexico_City. HARNESS_TZ env is the fallback.
                       Default (neither given) = no anchor = today's
                       behavior. A bad IANA name is a launcher error (exit
                       2). The anchor is passed to AsyncRuntime when it
                       accepts one (W-runtime seam); before that merge it is
                       informational for the run.
    --enable-commands  Register the S3 slash-command handler. Default OFF →
                       ``start(on_message=..., on_command=None)`` → commands
                       are dropped, exactly like today. When ON, the channel
                       is wrapped in :class:`CommandBridgeChannel`, which
                       injects the launcher's command callback into
                       ``Channel.start`` — an interim dispatch that builds
                       the S3 :class:`CommandContext` and calls
                       ``harness.commands.handle_command``; a future
                       runtime's OWN ``on_command`` (its locked
                       ``_on_command`` dispatch) wins over the bridge's.

    /state is gated behind the debug flag: set HARNESS_DEBUG_COMMANDS=1 to
    expose mood internals (it would contaminate the perceptual experiment
    otherwise).

    Launcher-side hook persistence (interim; namespaced keys the W-runtime
    dispatch may consume): /tz records ``cmd.tz.pending`` (applied at the
    next rollover — the virtual clock never jumps backwards); /mute records
    ``cmd.mute.until_t_h`` (defer, never consume).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfoNotFoundError

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor, anchor_for_fresh_start
from harness.assembler import DEFAULT_PERSONA_CORE
from harness.bootstrap import (
    DEFAULT_USER_INTERESTS,
    DEFAULT_USER_NAME,
    OnboardingConfig,
    ensure_companion_initialized,
)
from harness.channels.base import OutboundMessage
from harness.channels.telegram import ControlCommand
from harness.client import FakeClient, OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.commands import CommandContext, handle_command
from harness.config import DEFAULT_CHANNEL, select_channel
from harness.judge import judge_day
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

WAKE_HOUR = 8.0


def _env_bool(name: str, default: bool = False) -> bool:
    """Env bool with the harness convention (mirrors tools._env_bool):
    unset/empty -> default; truthy = 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _onboarding_config(args) -> OnboardingConfig:
    """Onboarding config from CLI args (shared by the startup bootstrap and
    the /setup hook so both initialize identically)."""
    user_interests = tuple(
        s.strip()
        for s in (args.user_interests or ",".join(DEFAULT_USER_INTERESTS)).split(",")
        if s.strip()
    )
    return OnboardingConfig(
        user_name=args.user_name or DEFAULT_USER_NAME,
        user_interests=user_interests,
    )


def _bootstrap_and_report(store: SQLiteStore, seed: int, args) -> None:
    """Idempotent clean-start initialization (Iteration-2 A1b): blank DB →
    persona → user-relative interests → life arcs → today's agenda, then a
    one-line summary. Safe to call on every start (no-op once initialized)."""
    boot = ensure_companion_initialized(
        store, seed=seed, config=_onboarding_config(args), day=0
    )
    counts: dict[str, int] = {}
    for interest in boot.persona.interests:
        counts[interest.bucket] = counts.get(interest.bucket, 0) + 1
    print(
        f"bootstrap: user={boot.user_profile.name} persona={boot.persona.name} "
        f"interests={len(boot.persona.interests)} "
        f"(exact {counts.get('exact', 0)} / adjacent {counts.get('adjacent', 0)} / "
        f"independent {counts.get('independent', 0)}) arcs={len(boot.life_arcs)} "
        f"agenda[0]={len(boot.today_agenda.items) if boot.today_agenda else 0}"
    )


def _restore_or_plan(
    store: SQLiteStore, seed: int, persona: PersonaParams, timing: TimingParams,
    days: int,
) -> ProactiveSchedule:
    """Restart-resume: restore the persisted schedule when pending events
    exist, otherwise plan + persist a fresh horizon."""
    if store.pending_schedule_events(seed):
        return ProactiveSchedule.restore(seed, store)
    return ProactiveSchedule.plan_and_persist(days, seed, persona, timing, store)


def resolve_tz(
    tz_flag: str | None, env: Mapping[str, str] | None = None
) -> tuple[str | None, RealTimeAnchor | None]:
    """Resolve --tz / HARNESS_TZ to (IANA name, RealTimeAnchor).

    No explicit tz (flag and env both absent/empty) -> (None, None): today's
    behavior, no anchor. A bad IANA name raises ZoneInfoNotFoundError (the
    launcher turns it into an argparse error, exit 2).
    """
    env = os.environ if env is None else env
    tz = (tz_flag or "").strip() or (env.get("HARNESS_TZ") or "").strip() or None
    if tz is None:
        return None, None
    return tz, anchor_for_fresh_start(time.time(), tz)


def _commit_sha() -> str | None:
    """Short HEAD sha of the harness repo, or None when unavailable (best
    effort — never fails the launcher)."""
    try:
        root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:  # noqa: BLE001 - best effort
        return None


def _make_request_setup(store: SQLiteStore, args, persona, timing) -> Callable[[], str]:
    """The /setup hook: initialize identity, then plan the schedule horizon.

    Pre-bootstrap only — handle_command refuses to call it once a persona
    row exists. The runtime picks the planned rows up at its next midnight
    replan (restart-safe, INSERT OR IGNORE). Returns a human summary for
    the /setup reply.
    """

    def _request_setup() -> str:
        boot = ensure_companion_initialized(
            store, seed=args.seed, config=_onboarding_config(args), day=0
        )
        _restore_or_plan(store, args.seed, persona, timing, args.days)
        agenda = boot.today_agenda
        return (
            f"persona={boot.persona.name} interests={len(boot.persona.interests)} "
            f"arcs={len(boot.life_arcs)} "
            f"agenda[0]={len(agenda.items) if agenda else 0}"
        )

    return _request_setup


def build_command_callback(
    store: SQLiteStore,
    clock: VirtualClock,
    *,
    seed: int,
    channel,
    anchor: RealTimeAnchor | None = None,
    flags: dict[str, bool] | None = None,
    commit_sha: str | None = None,
    request_setup: Callable[[], str | None] | None = None,
    request_tz_change: Callable[[str], None] | None = None,
    request_mute: Callable[[float], None] | None = None,
) -> Callable[[ControlCommand], object]:
    """Build the launcher-side ``on_command`` callback (interim dispatch).

    Returns an async callback for ``Channel.start(on_command=...)``: it
    constructs the S3 :class:`CommandContext` from read-only facts, calls
    ``harness.commands.handle_command``, and sends the reply as a
    non-proactive :class:`OutboundMessage`. It NEVER touches
    ``Session.on_message`` or the closing machinery — the same purity
    contract the W-runtime's locked ``_on_command`` dispatch honors (its
    own callback, when the runtime passes one, supersedes this one).
    """

    async def _on_command(cmd: ControlCommand) -> None:
        ctx = CommandContext(
            store=store,
            clock=clock,
            anchor=anchor,
            persona_exists=store.load_persona() is not None,
            pending_proactive_count=len(store.pending_schedule_events(seed)),
            flags=dict(flags) if flags else {},
            request_tz_change=request_tz_change,
            request_mute=request_mute,
            seed=seed,
            commit_sha=commit_sha,
            request_setup=request_setup,
        )
        reply = handle_command(cmd, ctx)
        await channel.send(OutboundMessage(text=reply, proactive=False))

    return _on_command


class CommandBridgeChannel:
    """Channel adapter that injects ``on_command`` into ``Channel.start``.

    The runtime calls ``channel.start(on_message)`` (today's signature).
    This bridge forwards that call with the launcher's command callback
    attached (seam S3: ``start(on_message, on_command=None)``), so
    ``--enable-commands`` works even before W-runtime's own ``_on_command``
    dispatch merges. When a future runtime passes its OWN ``on_command``,
    the runtime's callback wins — no double wiring. Channels without the S3
    seam (CLI) keep their plain signature; commands are dropped there
    (matching today). Everything else (send, stop, typing_context, ...)
    delegates to the wrapped channel.
    """

    def __init__(self, channel, on_command):
        self._channel = channel
        self._on_command = on_command

    async def start(self, on_message, on_command=None):
        cb = on_command if on_command is not None else self._on_command
        if cb is None or "on_command" not in inspect.signature(
            self._channel.start
        ).parameters:
            await self._channel.start(on_message)
            return
        await self._channel.start(on_message, on_command=cb)

    async def stop(self):
        await self._channel.stop()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._channel, name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_async",
        description="Async proactive driver — real-time runtime entrypoint (wave 3).",
    )
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument(
        "--days", type=int, default=60,
        help="schedule horizon AND run length in virtual days",
    )
    parser.add_argument("--store", type=str, default="data/session.db")
    parser.add_argument("--feedback", action="store_true", help="enable judge → mu feedback")
    parser.add_argument("--synthetic", action="store_true", help="synthetic scores (run_daily parity)")
    parser.add_argument("--fake", action="store_true", help="offline scripted client (no API key)")
    parser.add_argument("--trace", action="store_true", help="show directive channels on replies")
    parser.add_argument("--persona-core", type=str, default=None)
    parser.add_argument("--model", type=str, default=None, help="LLM_MODEL override")
    parser.add_argument(
        "--user-name", type=str, default=None,
        help="onboarding user display name (default: bootstrap default)",
    )
    parser.add_argument(
        "--user-interests", type=str, default=None,
        help="comma-separated user interests for the user-relative 40/40/20 "
             "portfolio (default: mathematics,metal,lifting,movies)",
    )
    parser.add_argument(
        "--channel", type=str, default=None,
        help="active channel: cli | telegram | fake (default: HARNESS_CHANNEL env or cli)",
    )
    parser.add_argument(
        "--time-scale", type=float, default=3600.0,
        help="real seconds per virtual hour (default 3600.0 = real time; "
             "smaller values accelerate, e.g. 0.001 runs one virtual day in 24 ms)",
    )
    parser.add_argument(
        "--defer-bootstrap", action="store_true",
        help="on a BLANK database, skip the startup bootstrap: the companion "
             "stays uninitialized until /setup (pre-bootstrap only; requires "
             "--enable-commands). Default: unconditional bootstrap, unchanged.",
    )
    parser.add_argument(
        "--tz", type=str, default=None,
        help="IANA timezone for the real-time anchor, e.g. America/Mexico_City "
             "(default: HARNESS_TZ env; neither = no anchor = today's behavior)",
    )
    parser.add_argument(
        "--enable-commands", action="store_true",
        help="register the slash-command handler (S3). Default OFF -> "
             "start(on_message=..., on_command=None) -> commands are dropped, "
             "exactly like today.",
    )
    args = parser.parse_args(argv)

    store = SQLiteStore(args.store)
    clock = VirtualClock(t_h=0.0 + WAKE_HOUR)
    persona = PersonaParams()
    timing = TimingParams()

    blank = store.load_persona() is None
    if args.defer_bootstrap and blank:
        print(
            "defer-bootstrap: blank DB — companion NOT initialized; send /setup "
            "to initialize (pre-bootstrap only; requires --enable-commands)",
            flush=True,
        )
        schedule = ProactiveSchedule.restore(args.seed, store)
    else:
        _bootstrap_and_report(store, args.seed, args)
        schedule = _restore_or_plan(store, args.seed, persona, timing, args.days)

    if args.fake:
        client = FakeClient(echo=True)
        synthetic = True
    else:
        client = OpenAICompatibleClient(model=args.model)
        synthetic = args.synthetic

    session = Session(
        store,
        persona=persona,
        timing=timing,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=args.seed,
        client=client,
        clock=clock,
        judge=judge_day,
        feedback=args.feedback,
        persona_core=args.persona_core or DEFAULT_PERSONA_CORE,
        synthetic_score=synthetic,
    )

    try:
        tz_name, anchor = resolve_tz(args.tz)
    except ZoneInfoNotFoundError as exc:
        parser.error(f"invalid timezone: {exc}")
    # S1 write path: resolve real timestamps from the anchor at row
    # creation; without an anchor all *_at columns stay NULL.
    if anchor is not None:
        store.attach_anchor(anchor)

    channel_name = args.channel or os.environ.get("HARNESS_CHANNEL") or DEFAULT_CHANNEL
    channel = select_channel(channel_name)
    if args.enable_commands:
        if "on_command" not in inspect.signature(channel.start).parameters:
            print(
                "WARNING: --enable-commands given but the selected channel has no "
                "command seam (S3) — commands will be dropped.",
                flush=True,
            )
        else:
            channel = CommandBridgeChannel(
                channel,
                build_command_callback(
                    store,
                    clock,
                    seed=args.seed,
                    channel=channel,
                    anchor=anchor,
                    flags={"debug": _env_bool("HARNESS_DEBUG_COMMANDS")},
                    commit_sha=_commit_sha(),
                    request_setup=_make_request_setup(store, args, persona, timing),
                    request_tz_change=lambda name: store.set_kv("cmd.tz.pending", name),
                    request_mute=lambda hours: store.set_kv(
                        "cmd.mute.until_t_h", f"{clock.now_h() + hours:.3f}"
                    ),
                ),
            )
    time_scale = TimeScale(seconds_per_virtual_hour=args.time_scale)

    state = session.state_summary()
    print("llm-behavioral-harness — async runtime")
    print(f"seed={args.seed} day={state['day']} M={state['M']} phase={state['phase']} "
          f"channel={channel_name} time_scale={args.time_scale} s/vh "
          f"feedback={args.feedback} synthetic={synthetic}"
          + (f" tz={tz_name}" if tz_name else "")
          + (" commands=on" if args.enable_commands else ""))
    print(f"running {args.days} virtual days "
          f"({args.days * 24.0 * args.time_scale / 3600.0:.2f} real hours at this scale)\n")

    runtime_kwargs: dict[str, Any] = dict(
        session=session,
        schedule=schedule,
        channel=channel,
        store=store,
        timing=timing,
        seed=args.seed,
        time_scale=time_scale,
        max_virtual_hours=args.days * 24.0,
    )
    if anchor is not None:
        if "anchor" in inspect.signature(AsyncRuntime.__init__).parameters:
            runtime_kwargs["anchor"] = anchor
        else:
            print(
                "WARNING: --tz/HARNESS_TZ given but this AsyncRuntime has no anchor "
                "support (W-runtime not merged) — tz is informational for this run.",
                flush=True,
            )
    runtime = AsyncRuntime(**runtime_kwargs)
    try:
        asyncio.run(runtime.run())
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
