"""The live launcher's day-0 proactive plan is state-aware, and a re-plan
never resurrects a fired event."""

import numpy as np

from engine.types import PersonaParams, TimingParams
from experiments.live_companion import bootstrap, build_runtime, build_session
from harness.bootstrap import ensure_companion_initialized
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import UserProfile
from harness.scheduler import (
    REASON_SCHEDULE,
    ProactiveSchedule,
    day_scores,
    plan_proactive_events,
    state_factors_for_plan,
)
from harness.store import SQLiteStore
from tests.helpers.judges import DeterministicJudge

SEED = 5001
PERSONA = PersonaParams()
TIMING = TimingParams()
#: Non-neutral prior-day adjustment scores for the integrity re-plans.
DIFF_SCORES = np.array([0.35, 0.0, 0.0])


def _boot(store: SQLiteStore) -> None:
    ensure_companion_initialized(
        store, seed=SEED,
        user=UserProfile(name="User", interests=(
            "mathematics", "lifting", "movies", "metal music")),
        day=0,
    )


def _make_session(store: SQLiteStore):
    return build_session(store, SEED, VirtualClock(0.0), FakeClient(),
                         DeterministicJudge(SEED), PERSONA, TIMING)


def _day0_rows(store: SQLiteStore) -> list[float]:
    return sorted(
        float(r["t_h"])
        for r in store.schedule_events_for_seed(SEED)
        if int(r["day"]) == 0
    )


def _state_aware_day0_plan(store: SQLiteStore) -> list[float]:
    """The day-0 plan the fixed entry points must persist: real state
    factors (the day-0 daily_state row) + the real day_scores array."""
    return sorted(float(h) for h in plan_proactive_events(
        1, SEED, PERSONA, TIMING,
        scores=day_scores(store, 0, TIMING),
        state_factors=state_factors_for_plan(store, 1, TIMING),
    ))


def _neutral_day0_plan() -> list[float]:
    return sorted(float(h) for h in plan_proactive_events(
        1, SEED, PERSONA, TIMING, scores=None, state_factors=None
    ))


# ---- State-aware day-0 plan through both entry points ----


def test_live_entry_day0_plan_is_state_aware(tmp_path):
    """build_runtime: the day-0 mood is drawn before planning; the rows match the plan."""
    store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
    bootstrap(store, seed=SEED)
    try:
        assert store.load_daily_state(0) is None
        build_runtime(store, SEED, FakeChannel(), client=FakeClient(),
                      judge=DeterministicJudge(SEED))
        assert store.load_daily_state(0) is not None, (
            "day-0 mood must be drawn before the day-0 plan"
        )
        aware = _state_aware_day0_plan(store)
        assert aware != _neutral_day0_plan()
        assert _day0_rows(store) == aware, (
            "day-0 rows must match the state-aware plan exactly"
        )
    finally:
        store.close()


# ---- Fired-event integrity under re-plan with different scores ----


def _assert_fired_rows_intact(store: SQLiteStore, fired_t: list[float]) -> None:
    by_t = {float(r["t_h"]): r for r in store.schedule_events_for_seed(SEED)}
    pending = {float(r["t_h"]) for r in store.schedule_events_for_seed(SEED)
               if r["status"] == "pending"}
    for t in fired_t:
        row = by_t[t]
        assert row["status"] == "fired", f"fired row {t} was resurrected"
        assert float(row["t_h"]) == t and row["reason"] == REASON_SCHEDULE
        assert row["fired_t_h"] is not None
    assert not (set(fired_t) & pending), (
        "a fired hour must never reappear as pending"
    )


def test_replan_with_different_scores_regenerates_unfired_events(tmp_path):
    """A re-plan with different scores regenerates unfired events; fired rows stay
    untouched."""
    store = SQLiteStore(tmp_path / "cell.db", audit_mode=True)
    _boot(store)
    session = _make_session(store)
    session.ensure_day(0)
    ProactiveSchedule.plan_and_persist(
        1, SEED, PERSONA, TIMING, store,
        reason=REASON_SCHEDULE, scores=day_scores(store, 0, TIMING),
    )
    day0 = _day0_rows(store)
    assert day0, "the day-0 plan must produce rows"
    fired_t = [day0[0]]
    store.mark_schedule_fired(SEED, fired_t[0], fired_t[0])
    scores = DIFF_SCORES[:2]
    candidates = plan_proactive_events(
        2, SEED, PERSONA, TIMING, scores=scores,
        state_factors=state_factors_for_plan(store, 2, TIMING),
    )
    existing = {float(r["t_h"]) for r in store.schedule_events_for_seed(SEED)}
    new_hours = [float(h) for h in candidates if float(h) not in existing]
    assert new_hours, "the different-scores re-plan must regenerate candidates"
    ProactiveSchedule.plan_and_persist(
        2, SEED, PERSONA, TIMING, store,
        reason=REASON_SCHEDULE, scores=scores,
    )
    _assert_fired_rows_intact(store, fired_t)
    pending = {float(r["t_h"]) for r in store.schedule_events_for_seed(SEED)
               if r["status"] == "pending"}
    assert set(new_hours) <= pending, (
        "the re-plan must persist the regenerated (pending) events"
    )
    store.close()
