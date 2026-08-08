"""Interactive CLI driver — the e2e slice front door (W-E2).

Runs a live session under the virtual clock: chat, time control, proactive
firing, and dev-facing state/trace introspection.

    MPLBACKEND=Agg .venv/bin/python -m sim.run_interactive --seed 12345 \
        --days 60 [--feedback] [--fake] [--store data/session.db] [--trace]

Commands:
    <text>       send a message to the companion
    /advance N   advance the clock N hours (fires due proactive events)
    /day N       jump to the start of day N
    /proactive   fire one proactive message now
    /state       show the current latent + observable state
    /trace       toggle directive channel trace on replies
    /help        list commands
    /quit        finalize the current day and exit

Live mode requires LLM_API_KEY (+ optional LLM_BASE_URL / LLM_MODEL).
`--fake` runs offline with a scripted client (no credentials needed).
Judge runs in shadow mode unless --feedback is given.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import DEFAULT_PERSONA_CORE
from harness.client import FakeClient, OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.judge import judge_day
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

WAKE_HOUR = 8.0


def _fire_due(session: Session, schedule: ProactiveSchedule, trace: bool) -> int:
    """Fire every planned proactive event whose hour has passed."""
    fired = 0
    while True:
        due = schedule.due_at(session.clock.now_h())
        if not due:
            break
        for t_h in due:
            if t_h > session.clock.now_h():
                session.clock.advance_hours(t_h - session.clock.now_h())
            result = session.fire_proactive(REASON_SCHEDULE)
            schedule.mark_fired(t_h)
            print(f"\n[proactive @ day {result.day}, hour {result.hour:.0f}:00]")
            print(result.reply)
            if trace:
                _print_trace(result.directive)
            fired += 1
    return fired


def _print_trace(directive) -> None:
    print(
        "  trace: "
        f"phase={directive.trace.phase_label} g={directive.trace.hormonal_gain:.2f} "
        f"mu={directive.trace.event_memory:.2f} eta={directive.trace.endogenous_tone:.2f} "
        f"valence={directive.valence:.2f} energy={directive.energy:.2f} "
        f"playfulness={directive.playfulness:.2f} reflectiveness={directive.reflectiveness:.2f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_interactive",
        description="Interactive CLI driver — the e2e slice front door (W-E2).",
    )
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--days", type=int, default=60, help="proactive schedule horizon")
    parser.add_argument("--store", type=str, default="data/session.db")
    parser.add_argument("--feedback", action="store_true", help="enable judge → mu feedback")
    parser.add_argument("--synthetic", action="store_true", help="synthetic scores (run_daily parity)")
    parser.add_argument("--fake", action="store_true", help="offline scripted client (no API key)")
    parser.add_argument("--trace", action="store_true", help="show directive channels on replies")
    parser.add_argument("--persona-core", type=str, default=None)
    parser.add_argument("--model", type=str, default=None, help="LLM_MODEL override")
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

    schedule = ProactiveSchedule.plan(args.days, args.seed, persona, timing)
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

    state = session.state_summary()
    print("llm-behavioral-harness — interactive session")
    print(f"seed={args.seed} day={state['day']} M={state['M']} phase={state['phase']} "
          f"feedback={args.feedback} synthetic={synthetic}")
    print("type /help for commands; plain text sends a message.\n")

    trace = args.trace
    try:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                cmd, _, arg = line[1:].partition(" ")
                arg = arg.strip()
                if cmd == "quit":
                    break
                elif cmd == "help":
                    print(parser.format_help())
                    print("commands: <text> | /advance N | /day N | /proactive | /state | /trace | /help | /quit")
                elif cmd == "advance":
                    try:
                        clock.advance_hours(float(arg))
                    except ValueError:
                        print("usage: /advance <hours>")
                        continue
                    n = _fire_due(session, schedule, trace)
                    print(f"[day {clock.day()}, hour {clock.local_hour():.0f}:00] "
                          f"advanced; {n} proactive event(s) fired")
                elif cmd == "day":
                    try:
                        clock.advance_to_day(int(arg))
                    except ValueError as exc:
                        print(f"error: {exc}")
                        continue
                    n = _fire_due(session, schedule, trace)
                    print(f"[day {clock.day()}] {n} proactive event(s) fired")
                elif cmd == "proactive":
                    result = session.fire_proactive(REASON_SCHEDULE)
                    print(result.reply)
                    if trace:
                        _print_trace(result.directive)
                elif cmd == "state":
                    s = session.state_summary()
                    print(
                        f"day={s['day']} M={s['M']} m={s['m']:.3f} g={s['g']:.3f} "
                        f"mu={s['mu']:.3f} eta={s['eta']:.3f} phase={s['phase']} "
                        f"cycle_day={s['cycle_day']:.1f} hour={s['hour']:.1f} "
                        f"feedback={s['feedback']}"
                    )
                elif cmd == "trace":
                    trace = not trace
                    print(f"trace {'on' if trace else 'off'}")
                else:
                    print(f"unknown command: /{cmd} (try /help)")
            else:
                result = session.on_message(line)
                print(result.reply)
                if trace:
                    _print_trace(result.directive)
    finally:
        session.finalize_current()  # persist the current day's judgement on quit
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
