"""Async proactive driver — real-time runtime entrypoint (wave 3, seam A-6).

Mirrors sim/run_interactive's flags (--seed --days --store --feedback
--synthetic --fake --trace --persona-core --model) and adds channel selection
and clock pacing:

    --channel NAME   'cli' | 'telegram' | 'fake'. Default: DEFAULT_CHANNEL,
                     overridden by the HARNESS_CHANNEL env var when the flag
                     is not given.
    --time-scale S   real seconds per virtual hour (default 3600.0 = real
                     time; smaller values accelerate — e.g. 0.001 runs one
                     virtual day in 86 ms). The flag maps 1:1 onto
                     TimeScale.seconds_per_virtual_hour, the runtime's
                     semantic unit.

The schedule is restored from the store when pending events exist
(restart-resume) and planned + persisted otherwise. The run lasts
``--days * 24`` virtual hours: --days is both the schedule horizon and the
run length.

This module never imports sim/run_interactive (it copies the Session
construction pattern instead).
"""

from __future__ import annotations

import argparse
import asyncio
import os

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import DEFAULT_PERSONA_CORE
from harness.client import FakeClient, OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.config import DEFAULT_CHANNEL, select_channel
from harness.judge import judge_day
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

WAKE_HOUR = 8.0


def _restore_or_plan(
    store: SQLiteStore, seed: int, persona: PersonaParams, timing: TimingParams,
    days: int,
) -> ProactiveSchedule:
    """Restart-resume: restore the persisted schedule when pending events
    exist, otherwise plan + persist a fresh horizon."""
    if store.pending_schedule_events(seed):
        return ProactiveSchedule.restore(seed, store)
    return ProactiveSchedule.plan_and_persist(days, seed, persona, timing, store)


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
        "--channel", type=str, default=None,
        help="active channel: cli | telegram | fake (default: HARNESS_CHANNEL env or cli)",
    )
    parser.add_argument(
        "--time-scale", type=float, default=3600.0,
        help="real seconds per virtual hour (default 3600.0 = real time; "
             "smaller values accelerate, e.g. 0.001 runs one virtual day in 86 ms)",
    )
    args = parser.parse_args(argv)

    store = SQLiteStore(args.store)
    clock = VirtualClock(t_h=0.0 + WAKE_HOUR)
    if args.fake:
        client = FakeClient(echo=True)
        synthetic = True
    else:
        client = OpenAICompatibleClient(model=args.model)
        synthetic = args.synthetic
    persona = PersonaParams()
    timing = TimingParams()

    schedule = _restore_or_plan(store, args.seed, persona, timing, args.days)
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

    channel_name = args.channel or os.environ.get("HARNESS_CHANNEL") or DEFAULT_CHANNEL
    channel = select_channel(channel_name)
    time_scale = TimeScale(seconds_per_virtual_hour=args.time_scale)

    state = session.state_summary()
    print("llm-behavioral-harness — async runtime")
    print(f"seed={args.seed} day={state['day']} M={state['M']} phase={state['phase']} "
          f"channel={channel_name} time_scale={args.time_scale} s/vh "
          f"feedback={args.feedback} synthetic={synthetic}")
    print(f"running {args.days} virtual days "
          f"({args.days * 24.0 * args.time_scale / 3600.0:.2f} real hours at this scale)\n")

    runtime = AsyncRuntime(
        session,
        schedule,
        channel,
        store=store,
        timing=timing,
        seed=args.seed,
        time_scale=time_scale,
        max_virtual_hours=args.days * 24.0,
    )
    try:
        asyncio.run(runtime.run())
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
