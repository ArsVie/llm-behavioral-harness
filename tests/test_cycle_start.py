"""Each character's cycle starts at her own point (random_cycle_start)."""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.session import CYCLE_START_KEY, Session
from harness.store import SQLiteStore


def _session(path, seed, *, random_start=True, persona=None):
    return Session(
        SQLiteStore(path), persona=persona or PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=seed,
        client=FakeClient(responses=["ok!"]), clock=VirtualClock(),
        judge=ScriptedJudge(0.5).judge_day, random_cycle_start=random_start,
    )


def test_characters_start_their_cycles_at_different_points(tmp_path):
    starts = []
    for seed in range(8):
        session = _session(tmp_path / f"s{seed}.db", seed)
        state = session.cycle_state
        assert 0.0 <= state.cycle_day < state.L_current
        assert session.store.get_kv(CYCLE_START_KEY) == repr(state.cycle_day)
        starts.append(state.cycle_day)
        session.store.close()
    assert len({round(start, 6) for start in starts}) == len(starts)


def test_the_start_is_kept_across_a_restart(tmp_path):
    first = _session(tmp_path / "s.db", 7)
    first.ensure_day(3)
    day_three = first.cycle_state.cycle_day
    first.store.close()
    again = _session(tmp_path / "s.db", 7)
    assert again.cycle_state.cycle_day == day_three
    again.store.close()


def test_a_run_that_began_before_the_change_keeps_its_start(tmp_path):
    legacy = _session(tmp_path / "s.db", 7, random_start=False)
    legacy.ensure_day(2)
    fixed = legacy.cycle_state.cycle_day
    legacy.store.close()
    resumed = _session(tmp_path / "s.db", 7)
    assert resumed.cycle_state.cycle_day == fixed
    assert resumed.store.get_kv(CYCLE_START_KEY) == repr(0.0)
    resumed.store.close()


def test_an_explicit_phi_and_the_default_off_are_left_alone(tmp_path):
    pinned = _session(tmp_path / "p.db", 7, persona=PersonaParams(phi=10.0))
    assert pinned.cycle_state.cycle_day == 10.0
    off = _session(tmp_path / "o.db", 7, random_start=False)
    assert off.cycle_state.cycle_day == 0.0
    assert off.store.get_kv(CYCLE_START_KEY) is None


def test_the_proactive_plan_follows_her_real_phase(tmp_path):
    """The hazard's phase term replays the cycle from the kept start, so the
    plan's phase for each day is the phase the session recorded for it."""
    import sim.run_events as run_events
    from harness.scheduler import with_cycle_start

    session = _session(tmp_path / "s.db", 11)
    session.ensure_day(12)
    lived = [str(session.store.load_daily_state(day)["phase_label"]) for day in range(13)]
    planned = run_events._precompute_phase_labels(
        13, 11, with_cycle_start(PersonaParams(), session.store))
    assert planned == lived
    assert run_events._precompute_phase_labels(13, 11, PersonaParams()) != lived
    session.store.close()
