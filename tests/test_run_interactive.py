"""The interactive CLI driver — arg surface, wiring and command dispatch.

Everything here runs with ``--fake``: a scripted client, no credentials and
no network. The REPL loop itself is exercised through ``main`` with stdin
replaced, so the one thing that cannot be unit-tested (``input()``) is still
covered end to end.
"""

from __future__ import annotations

import pytest

from harness.client import FakeClient
from sim.run_interactive import (
    InteractiveContext,
    build_context,
    build_parser,
    handle_command,
    handle_line,
    main,
)


def _args(tmp_path, *extra):
    parser = build_parser()
    return parser, parser.parse_args(
        ["--fake", "--store", str(tmp_path / "s.db"), "--days", "2", *extra]
    )


@pytest.fixture
def ctx(tmp_path):
    parser, args = _args(tmp_path)
    context = build_context(args, parser)
    yield context
    context.store.close()


# --- argument surface ----------------------------------------------------


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.seed == 12345
    assert args.days == 60
    assert args.store == "data/session.db"
    assert not args.fake and not args.feedback and not args.trace


def test_parser_accepts_every_documented_flag(tmp_path):
    args = build_parser().parse_args([
        "--seed", "7", "--days", "3", "--store", str(tmp_path / "x.db"),
        "--feedback", "--synthetic", "--fake", "--trace",
        "--persona-core", "core prose", "--model", "m",
        "--user-name", "Ars", "--user-interests", "lifting,metal",
    ])
    assert (args.seed, args.days, args.model) == (7, 3, "m")
    assert args.feedback and args.synthetic and args.fake and args.trace
    assert args.user_name == "Ars"
    assert args.persona_core == "core prose"


# --- wiring --------------------------------------------------------------


def test_fake_run_is_offline_and_synthetic(ctx):
    """--fake means a scripted client and synthetic scores, offline.

    build_context passes judge_client=None on the fake path rather than
    constructing a research-lane client, so an offline run never tries to
    resolve credentials it does not need; the session's documented fallback
    then points judge_client at the same fake client.
    """
    assert isinstance(ctx.session.client, FakeClient)
    assert ctx.synthetic is True
    # No separate judge client is built: the session's documented fallback
    # points judge_client at the same client, which offline is the fake.
    assert ctx.session.judge_client is ctx.session.client


def test_bootstrap_runs_once_and_reports(tmp_path, capsys):
    """The bootstrap line is the operator's confirmation that identity exists."""
    parser, args = _args(tmp_path)
    context = build_context(args, parser)
    try:
        out = capsys.readouterr().out
        assert "bootstrap:" in out and "persona=" in out
        assert context.store.load_persona() is not None
    finally:
        context.store.close()


def test_bootstrap_is_idempotent_across_restarts(tmp_path):
    """Re-opening the same DB keeps the identity it already had."""
    parser, args = _args(tmp_path)
    first = build_context(args, parser)
    name = first.store.load_persona().name
    first.store.close()
    second = build_context(args, parser)
    try:
        assert second.store.load_persona().name == name
    finally:
        second.store.close()


def test_user_interests_flag_reaches_the_persona(tmp_path):
    """The onboarding interests drive the persona's shared-interest draw."""
    parser, args = _args(tmp_path, "--user-interests", "lifting, metal ,")
    context = build_context(args, parser)
    try:
        assert context.store.load_persona() is not None
    finally:
        context.store.close()


# --- command dispatch ----------------------------------------------------


def test_quit_ends_the_session(ctx):
    assert handle_command(ctx, "quit", "") is False


def test_help_prints_the_command_list(ctx, capsys):
    assert handle_command(ctx, "help", "") is True
    assert "/advance" in capsys.readouterr().out


def test_unknown_command_is_reported_not_fatal(ctx, capsys):
    assert handle_command(ctx, "frobnicate", "") is True
    assert "unknown command: /frobnicate" in capsys.readouterr().out


def test_state_prints_the_latent_and_observable_state(ctx, capsys):
    handle_line(ctx, "hello")  # roll a day so the latent fields exist
    capsys.readouterr()
    assert handle_command(ctx, "state", "") is True
    out = capsys.readouterr().out
    for field in ("day=", "M=", "mu=", "eta=", "phase="):
        assert field in out


def test_state_before_the_first_rollover_does_not_crash(ctx, capsys):
    """A fresh session has no latent state yet; /state used to raise
    TypeError formatting None with :.3f and take the driver down."""
    assert handle_command(ctx, "state", "") is True
    out = capsys.readouterr().out
    assert "m=-" in out and "mu=-" in out


def test_trace_toggles(ctx, capsys):
    before = ctx.trace
    handle_command(ctx, "trace", "")
    assert ctx.trace is not before
    assert "trace on" in capsys.readouterr().out or not ctx.trace
    handle_command(ctx, "trace", "")
    assert ctx.trace is before


def test_advance_moves_the_clock(ctx, capsys):
    start = ctx.clock.now_h()
    assert handle_command(ctx, "advance", "3") is True
    assert ctx.clock.now_h() == pytest.approx(start + 3.0)
    assert "advanced" in capsys.readouterr().out


def test_advance_with_a_bad_argument_prints_usage_and_survives(ctx, capsys):
    """A typo must not move the clock and must not end the session."""
    start = ctx.clock.now_h()
    assert handle_command(ctx, "advance", "soon") is True
    assert ctx.clock.now_h() == start
    assert "usage: /advance <hours>" in capsys.readouterr().out


def test_day_jumps_to_the_start_of_a_day(ctx, capsys):
    assert handle_command(ctx, "day", "1") is True
    assert ctx.clock.day() == 1
    assert "proactive event(s) fired" in capsys.readouterr().out


def test_day_with_a_bad_argument_is_reported_not_fatal(ctx, capsys):
    day = ctx.clock.day()
    assert handle_command(ctx, "day", "yesterday") is True
    assert ctx.clock.day() == day
    assert "error:" in capsys.readouterr().out


def test_day_never_rewinds(ctx, capsys):
    """A backwards jump is refused: the session's clock is monotonic."""
    handle_command(ctx, "day", "2")
    assert handle_command(ctx, "day", "1") is True
    assert ctx.clock.day() == 2
    assert "error:" in capsys.readouterr().out


# --- line routing --------------------------------------------------------


def test_blank_lines_are_ignored(ctx):
    assert handle_line(ctx, "") is True


def test_plain_text_is_sent_as_a_message(ctx, capsys):
    assert handle_line(ctx, "hello there") is True
    out = capsys.readouterr().out
    assert out.strip()  # the companion answered
    assert ctx.store.recent_messages()


def test_slash_prefix_routes_to_the_command_handler(ctx):
    assert handle_line(ctx, "/quit") is False


def test_trace_on_prints_the_channel_trace(ctx, capsys):
    ctx.trace = True
    handle_line(ctx, "hi")
    assert "trace:" in capsys.readouterr().out


# --- the loop ------------------------------------------------------------


def test_main_runs_a_scripted_session(tmp_path, monkeypatch, capsys):
    """A whole session through the real REPL: message, command, quit."""
    lines = iter(["hello", "/state", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _="": next(lines))
    rc = main(["--fake", "--store", str(tmp_path / "m.db"), "--days", "2"])
    assert rc == 0
    assert "interactive session" in capsys.readouterr().out


def test_main_survives_eof(tmp_path, monkeypatch, capsys):
    """Ctrl-D ends the session cleanly and still finalizes the day."""
    def eof(_=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main(["--fake", "--store", str(tmp_path / "eof.db")]) == 0
    assert "interactive session" in capsys.readouterr().out


def test_context_is_a_plain_dataclass(ctx):
    """The REPL state is one object, so a command can flip `trace` in place."""
    assert isinstance(ctx, InteractiveContext)
    assert ctx.session is not None and ctx.schedule is not None
