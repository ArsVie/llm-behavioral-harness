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

Live mode requires the product-lane token (LILY_TOKEN, sourced from the
repo-root .env) plus optional LLM_BASE_URL / LLM_MODEL.
`--fake` runs offline with a scripted client (no credentials needed).
Judge runs in shadow mode unless --feedback is given.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import DEFAULT_PERSONA_CORE
from harness.bootstrap import (
    DEFAULT_USER_INTERESTS,
    DEFAULT_USER_NAME,
    OnboardingConfig,
    ensure_companion_initialized,
)
from harness.client import FakeClient, OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.judge import judge_day
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

WAKE_HOUR = 8.0


def _bootstrap_and_report(store: SQLiteStore, seed: int, args) -> None:
    """Idempotent clean-start initialization (Iteration-2 A1b): blank DB →
    persona → user-relative interests → life arcs → today's agenda, then a
    one-line summary. Safe to call on every start (no-op once initialized)."""
    user_interests = tuple(
        s.strip()
        for s in (args.user_interests or ",".join(DEFAULT_USER_INTERESTS)).split(",")
        if s.strip()
    )
    config = OnboardingConfig(
        user_name=args.user_name or DEFAULT_USER_NAME,
        user_interests=user_interests,
    )
    boot = ensure_companion_initialized(
        store, seed=seed, config=config, day=0
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


def _format_state(s: dict) -> str:
    """Render the state line, tolerating a session that has not rolled a day.

    ``Session.state_summary`` returns None for every latent field until the
    first rollover, so formatting them with ``:.3f`` raised TypeError — /state
    as the first command of a fresh session used to crash the driver.
    """
    def num(key: str, places: int = 3) -> str:
        value = s.get(key)
        return "-" if value is None else f"{value:.{places}f}"

    return (
        f"day={s['day']} M={s['M']} m={num('m')} g={num('g')} "
        f"mu={num('mu')} eta={num('eta')} phase={s['phase']} "
        f"cycle_day={num('cycle_day', 1)} hour={num('hour', 1)} "
        f"feedback={s['feedback']}"
    )


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Separate from main() so the flags can be tested
    without standing up a store or a client."""
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
    parser.add_argument(
        "--user-name", type=str, default=None,
        help="onboarding user display name (default: bootstrap default)",
    )
    parser.add_argument(
        "--user-interests", type=str, default=None,
        help="comma-separated user interests for the user-relative 40/40/20 "
             "portfolio (default: mathematics,metal,lifting,movies)",
    )
    return parser


@dataclass
class InteractiveContext:
    """Everything the REPL needs, built once before the loop starts."""

    store: SQLiteStore
    clock: VirtualClock
    session: Session
    schedule: ProactiveSchedule
    parser: argparse.ArgumentParser
    trace: bool
    synthetic: bool


def build_context(args, parser: argparse.ArgumentParser) -> InteractiveContext:
    """Wire the store, clock, clients and session for one interactive run.

    ``--fake`` runs fully offline: a scripted client, synthetic scores, and
    no judge-lane client at all (the session default stands). A live run
    gets its own RESEARCH-lane client for judging, so judge spend is never
    attributed to the product lane.
    """
    store = SQLiteStore(args.store)
    clock = VirtualClock(t_h=0.0 + WAKE_HOUR)
    _bootstrap_and_report(store, args.seed, args)
    if args.fake:
        client = FakeClient(echo=True)
        synthetic = True
        judge_client = None
    else:
        client = OpenAICompatibleClient(model=args.model, lane="product")
        synthetic = args.synthetic
        judge_client = OpenAICompatibleClient(model=args.model, lane="research")
    persona = PersonaParams()
    timing = TimingParams()
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
        judge_client=judge_client,
    )
    return InteractiveContext(
        store=store, clock=clock, session=session,
        schedule=ProactiveSchedule.plan(args.days, args.seed, persona, timing),
        parser=parser, trace=args.trace, synthetic=synthetic,
    )


def handle_command(ctx: InteractiveContext, cmd: str, arg: str) -> bool:
    """Run one slash command. Returns False when the session should end.

    A malformed argument prints usage and is otherwise a no-op — the REPL
    must survive a typo, not exit on one.
    """
    if cmd == "quit":
        return False
    if cmd == "help":
        print(ctx.parser.format_help())
        print("commands: <text> | /advance N | /day N | /proactive | /state | /trace | /help | /quit")
    elif cmd == "advance":
        try:
            ctx.clock.advance_hours(float(arg))
        except ValueError:
            print("usage: /advance <hours>")
            return True
        n = _fire_due(ctx.session, ctx.schedule, ctx.trace)
        print(f"[day {ctx.clock.day()}, hour {ctx.clock.local_hour():.0f}:00] "
              f"advanced; {n} proactive event(s) fired")
    elif cmd == "day":
        try:
            ctx.clock.advance_to_day(int(arg))
        except ValueError as exc:
            print(f"error: {exc}")
            return True
        n = _fire_due(ctx.session, ctx.schedule, ctx.trace)
        print(f"[day {ctx.clock.day()}] {n} proactive event(s) fired")
    elif cmd == "proactive":
        result = ctx.session.fire_proactive(REASON_SCHEDULE)
        print(result.reply)
        if ctx.trace:
            _print_trace(result.directive)
    elif cmd == "state":
        print(_format_state(ctx.session.state_summary()))
    elif cmd == "trace":
        ctx.trace = not ctx.trace
        print(f"trace {'on' if ctx.trace else 'off'}")
    else:
        print(f"unknown command: /{cmd} (try /help)")
    return True


def handle_line(ctx: InteractiveContext, line: str) -> bool:
    """Route one input line. Returns False when the session should end."""
    if not line:
        return True
    if line.startswith("/"):
        cmd, _, arg = line[1:].partition(" ")
        return handle_command(ctx, cmd, arg.strip())
    result = ctx.session.on_message(line)
    print(result.reply)
    if ctx.trace:
        _print_trace(result.directive)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = build_context(args, parser)

    state = ctx.session.state_summary()
    print("llm-behavioral-harness — interactive session")
    print(f"seed={args.seed} day={state['day']} M={state['M']} phase={state['phase']} "
          f"feedback={args.feedback} synthetic={ctx.synthetic}")
    print("type /help for commands; plain text sends a message.\n")

    try:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                break
            if not handle_line(ctx, line):
                break
    finally:
        ctx.session.finalize_current()  # persist the current day's judgement on quit
        ctx.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
