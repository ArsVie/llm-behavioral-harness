"""Command semantics tests (Wave 2, W-commands): harness/commands.py.

Unit-level tests of ``handle_command`` — a pure function of
(ControlCommand, CommandContext). The context is built against a real
SQLiteStore + VirtualClock (runtime facts only) with recorder hooks, so
every assertion pins the command's reply AND its side effects (hooks
called / not called, store writes, refusal semantics). No channel, no
session, no network.

The end-to-end channel path (FakeApplication-driven) lives in
tests/test_commands_channel.py.
"""

from harness.channels.telegram import ControlCommand
from harness.clock import VirtualClock
from harness.commands import CommandContext, handle_command
from harness.store import SQLiteStore


def _store(tmp_path, name="cmd.db"):
    return SQLiteStore(tmp_path / name)


def _cmd(name, args="", sender_id=42):
    return ControlCommand(name=name, args=args, sender_id=sender_id)


def _ctx(store, clock, **overrides):
    """CommandContext with recorder hooks; overrides win."""
    recorded = {"tz": [], "mute": [], "setup": []}

    def _record(key):
        def _hook(*a):
            recorded[key].append(a)

        return _hook

    ctx = CommandContext(
        store=store,
        clock=clock,
        anchor=None,
        persona_exists=None,  # derived from the store
        pending_proactive_count=0,
        flags={},
        request_tz_change=_record("tz"),
        request_mute=_record("mute"),
        seed=5001,
        commit_sha="abc1234",
        request_setup=_record("setup"),
        state_summary=None,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx, recorded


# --------------------------------------------------------------------------- #
# /help, /ping, unknown
# --------------------------------------------------------------------------- #


def test_help_lists_every_command(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    ctx, _ = _ctx(store, clock)
    reply = handle_command(_cmd("help"), ctx)
    for name in (
        "/help", "/ping", "/setup", "/tz", "/status", "/state", "/mute",
        "/version",
    ):
        assert name in reply
    assert "usage" in reply.lower() or "—" in reply


def test_ping_reports_alive_with_runtime_facts(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=24.0 * 2 + 8.5)  # day 2, 08:30 local
    ctx, _ = _ctx(store, clock)
    reply = handle_command(_cmd("ping"), ctx)
    assert reply.startswith("pong")
    assert "day 2" in reply
    assert "08:30" in reply


def test_unknown_command_points_to_help(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, _ = _ctx(store, clock=VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("reset"), ctx)
    assert "unknown command '/reset'" in reply
    assert "/help" in reply


def test_leading_slash_in_name_is_tolerated(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, _ = _ctx(store, clock=VirtualClock(t_h=8.0))
    assert handle_command(_cmd("/ping"), ctx).startswith("pong")


# --------------------------------------------------------------------------- #
# /setup — pre-bootstrap only
# --------------------------------------------------------------------------- #


def test_setup_refuses_once_persona_exists(tmp_path) -> None:
    """/setup must refuse after any bootstrap created a persona row."""
    store = _store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    from harness.bootstrap import OnboardingConfig, ensure_companion_initialized

    ensure_companion_initialized(store, seed=5001, config=OnboardingConfig(), day=0)
    assert store.load_persona() is not None
    ctx, recorded = _ctx(store, clock)
    reply = handle_command(_cmd("setup"), ctx)
    assert "refused" in reply
    assert "pre-bootstrap" in reply
    assert recorded["setup"] == []  # the hook must never run


def test_setup_runs_the_hook_on_a_blank_db(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    assert store.load_persona() is None
    ctx, recorded = _ctx(store, clock)
    reply = handle_command(_cmd("setup"), ctx)
    assert reply.startswith("setup complete")
    assert recorded["setup"] == [()]


def test_setup_without_hook_gives_launcher_guidance(tmp_path) -> None:
    store = _store(tmp_path)
    ctx = CommandContext(store=store, clock=VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("setup"), ctx)
    assert "--defer-bootstrap" in reply


def test_setup_hook_failure_is_reported_not_raised(tmp_path) -> None:
    store = _store(tmp_path)

    def _boom():
        raise RuntimeError("disk full")

    ctx = CommandContext(
        store=store, clock=VirtualClock(t_h=8.0), request_setup=_boom
    )
    reply = handle_command(_cmd("setup"), ctx)
    assert reply.startswith("/setup failed")
    assert "disk full" in reply


# --------------------------------------------------------------------------- #
# /tz — IANA validation, applied at the next rollover
# --------------------------------------------------------------------------- #


def test_tz_valid_name_records_the_change(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("tz", "America/Mexico_City"), ctx)
    assert recorded["tz"] == [("America/Mexico_City",)]
    assert "recorded" in reply
    assert "next rollover" in reply


def test_tz_invalid_name_is_rejected_without_calling_the_hook(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("tz", "Not/AZone"), ctx)
    assert "unknown timezone" in reply
    assert recorded["tz"] == []


def test_tz_without_args_prints_usage(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("tz"), ctx)
    assert reply.startswith("usage: /tz")
    assert recorded["tz"] == []


def test_tz_without_hook_still_validates_and_reports(tmp_path) -> None:
    store = _store(tmp_path)
    ctx = CommandContext(store=store, clock=VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("tz", "UTC"), ctx)
    assert "UTC" in reply  # validated; no hook -> informational


# --------------------------------------------------------------------------- #
# /status — runtime facts only
# --------------------------------------------------------------------------- #


def test_status_reports_day_local_hour_pending_and_age(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=24.0 * 3 + 20.25)  # day 3, 20:15 local
    store.add_message(role="user", content="hi", t_h=24.0 * 3 + 17.0, day=3)
    ctx, _ = _ctx(store, clock, pending_proactive_count=2)
    reply = handle_command(_cmd("status"), ctx)
    assert "day 3" in reply
    assert "20:15 local" in reply
    assert "2 proactive(s) pending" in reply
    assert "3.2 h ago" in reply  # 20.25 - 17.0 = 3.25


def test_status_without_exchanges_says_so(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, _ = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("status"), ctx)
    assert "no exchanges yet" in reply


def test_status_age_under_an_hour_is_in_minutes(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=24.0 + 12.0)
    store.add_message(role="user", content="hi", t_h=24.0 + 11.5, day=1)
    ctx, _ = _ctx(store, clock)
    reply = handle_command(_cmd("status"), ctx)
    assert "30 min ago" in reply


# --------------------------------------------------------------------------- #
# /state — mood internals, BEHIND the debug flag
# --------------------------------------------------------------------------- #


def test_state_is_refused_without_the_debug_flag(tmp_path) -> None:
    store = _store(tmp_path)
    store.save_daily_state(
        0,
        {
            "day": 0, "M": 3, "m": 1.2, "g": 0.8, "p": 0.5, "arg": 0.0,
            "mu": 0.4, "eta": 0.6, "cycle_day": 2.0, "phase_label": "luteal",
            "seed": 5001, "score": None,
        },
    )
    ctx, _ = _ctx(store, VirtualClock(t_h=8.0), flags={})  # debug OFF
    reply = handle_command(_cmd("state"), ctx)
    assert "disabled" in reply
    assert "0.4" not in reply  # no internals leak through the refusal


def test_state_with_debug_flag_renders_persisted_mood_internals(tmp_path) -> None:
    store = _store(tmp_path)
    store.save_daily_state(
        0,
        {
            "day": 0, "M": 3, "m": 1.2, "g": 0.8, "p": 0.5, "arg": 0.0,
            "mu": 0.4, "eta": 0.6, "cycle_day": 2.0, "phase_label": "luteal",
            "seed": 5001, "score": None,
        },
    )
    ctx, _ = _ctx(store, VirtualClock(t_h=8.0), flags={"debug": True})
    reply = handle_command(_cmd("state"), ctx)
    assert "mu=0.400" in reply
    assert "eta=0.600" in reply
    assert "M=3" in reply
    assert "luteal" in reply


def test_state_with_debug_uses_the_session_summary_provider_when_given(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    ctx = CommandContext(
        store=store,
        clock=VirtualClock(t_h=8.0),
        flags={"debug": True},
        state_summary=lambda: {
            "day": 1, "M": 5, "m": 1.5, "g": 0.9, "mu": 0.7, "eta": 0.3,
            "phase": "follicular", "cycle_day": 4.0,
        },
    )
    reply = handle_command(_cmd("state"), ctx)
    assert "mu=0.700" in reply
    assert "follicular" in reply


def test_state_debug_on_empty_store_reports_nothing_recorded(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, _ = _ctx(store, VirtualClock(t_h=8.0), flags={"debug": True})
    reply = handle_command(_cmd("state"), ctx)
    assert "no state recorded yet" in reply


# --------------------------------------------------------------------------- #
# /mute — defer, never consume
# --------------------------------------------------------------------------- #


def test_mute_records_hours_and_promises_deferral(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("mute", "4"), ctx)
    assert recorded["mute"] == [(4.0,)]
    assert "muted for 4 h" in reply
    assert "deferred, never consumed" in reply


def test_mute_accepts_fractional_hours(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("mute", "2.5"), ctx)
    assert recorded["mute"] == [(2.5,)]
    assert "muted for 2.5 h" in reply


def test_mute_rejects_garbage_and_non_positive_values(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    assert "invalid duration" in handle_command(_cmd("mute", "abc"), ctx)
    assert "invalid duration" in handle_command(_cmd("mute", "-3"), ctx)
    assert "invalid duration" in handle_command(_cmd("mute", "0"), ctx)
    assert recorded["mute"] == []


def test_mute_without_args_prints_usage(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, recorded = _ctx(store, VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("mute"), ctx)
    assert reply.startswith("usage: /mute")
    assert recorded["mute"] == []


# --------------------------------------------------------------------------- #
# /version — commit sha, seed, active flags
# --------------------------------------------------------------------------- #


def test_version_renders_sha_seed_and_flags(tmp_path) -> None:
    store = _store(tmp_path)
    ctx, _ = _ctx(
        store, VirtualClock(t_h=8.0),
        flags={"debug": True, "typing": False},
    )
    reply = handle_command(_cmd("version"), ctx)
    assert "abc1234" in reply
    assert "seed=5001" in reply
    assert "debug=on" in reply
    assert "typing=off" in reply


def test_version_degrades_gracefully_without_extras(tmp_path) -> None:
    store = _store(tmp_path)
    ctx = CommandContext(store=store, clock=VirtualClock(t_h=8.0))
    reply = handle_command(_cmd("version"), ctx)
    assert "unknown" in reply  # sha and seed both unknown
    assert "flags: none" in reply
