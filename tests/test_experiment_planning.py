"""A1 (WS-A): experiment-only state-aware day-0 plan + fired-event integrity.

Regression for the it3 defect: the experiment matrix planned day-0
contacts UP-FRONT at neutral state (``plan_and_persist`` with
``scores=None``) before the day-0 mood row existed, and INSERT OR IGNORE
never revised them — FULL cells planned their first day at state factor
exactly 1.0. Production ``runtime._replan`` is state-aware; the defect
lived in the two experiment copies (``run_cell`` in cvs_common,
``build_runtime`` in live_companion).

Covered:
1. Day-0 plan is state-aware through BOTH entry points: the day-0 mood row
   is drawn BEFORE planning (``session.ensure_day(0)`` — the same slow
   step the midnight rollover uses) and the plan is made with real
   ``day_scores`` — the persisted day-0 rows match the state-aware
   composition and differ from the neutral one the defect produced.
2. STRUCTURED_NO_STATE ablation intact: its day-0 plan stays identical to
   the neutral composition (the B5 state channel collapses to 1.0 under
   the condition patch), even though the mood row exists (the ablation is
   read-side).
3. Fired-event integrity (parametrized over both entry points): after some
   day-0 events have FIRED, a re-plan with different scores leaves the
   fired rows untouched (same t_h/reason, status stays fired) and never
   resurrects a fired hour as pending; unfired future events may be
   regenerated (the re-plan actually produces new candidate hours).
4. Timing rationale preserved: the day-0 plan exists before the firing
   loop starts — a 1-day cell actually FIRES a day-0 event (a row first
   planned at midnight day 1 would be born expired and could never fire).

All tests use a real SQLiteStore on a tmp dir and a fixed seed (5001);
fake client + scripted judge keep everything deterministic and offline.
"""

import numpy as np
import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from experiments.cvs_common import (
    BLOCK_END_D,
    BLOCK_START_D,
    GATE2_USER_INTERESTS,
    REASON_SCHEDULE,
    DeterministicClient,
    DeterministicJudge,
    VirtualClock,
    make_session,
    run_cell,
)
from experiments.live_companion import bootstrap, build_runtime
from harness.bootstrap import ensure_companion_initialized
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.domain import UserProfile
from harness.scheduler import (
    ProactiveSchedule,
    day_scores,
    plan_proactive_events,
    state_factors_for_plan,
)
from harness.store import SQLiteStore

SEED = 5001
PERSONA = PersonaParams()
TIMING = TimingParams()
#: Non-neutral prior-day adjustment for the integrity re-plans: a real
#: (non-zero) score array that would revise rows if the store allowed it.
DIFF_SCORES = np.array([0.35, 0.0, 0.0])


def _boot(store: SQLiteStore) -> None:
    ensure_companion_initialized(
        store, seed=SEED,
        user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
        day=0,
    )


def _make_session(store: SQLiteStore):
    client = DeterministicClient(SEED)
    judge = DeterministicJudge(SEED, block_start=BLOCK_START_D, block_end=BLOCK_END_D)
    return make_session("FULL", SEED, store, VirtualClock(0.0), client, judge,
                        PERSONA, TIMING, MoodVariant.DECOUPLED_OFFSETS)


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


# --------------------------------------------------------------------------- #
# 1. state-aware day-0 plan through both entry points
# --------------------------------------------------------------------------- #


def test_matrix_entry_day0_plan_is_state_aware(tmp_path):
    """run_cell: the day-0 mood row exists and the persisted day-0 rows
    contain the state-aware up-front candidates (which differ from the
    neutral candidates the defect produced)."""
    records = run_cell("FULL", SEED, tmp_path / "cell", days=1,
                       fake=True, perturb=True)
    store = SQLiteStore(records["db"], audit_mode=True)
    try:
        assert store.load_daily_state(0) is not None, (
            "day-0 mood must be drawn before the day-0 plan"
        )
        aware = _state_aware_day0_plan(store)
        neutral = _neutral_day0_plan()
        assert aware != neutral, "seed must discriminate state-aware vs neutral"
        rows = _day0_rows(store)
        assert set(aware) <= set(rows), (
            f"day-0 rows {rows} lack the state-aware up-front plan {aware}"
        )
    finally:
        store.close()


def test_live_entry_day0_plan_is_state_aware(tmp_path):
    """build_runtime: on a fresh store the day-0 mood is drawn BEFORE
    planning (bootstrap leaves no mood row) and the persisted day-0 rows
    match the state-aware plan exactly."""
    store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
    bootstrap(store, seed=SEED)
    try:
        assert store.load_daily_state(0) is None
        build_runtime(store, SEED, "FULL", FakeChannel(),
                      client=FakeClient(),
                      judge=DeterministicJudge(SEED, block_start=BLOCK_START_D,
                                               block_end=BLOCK_END_D))
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


def test_structured_no_state_day0_plan_stays_neutral(tmp_path):
    """The B5 state channel ablates under STRUCTURED_NO_STATE: the day-0
    plan stays identical to the neutral composition even though the mood
    row exists (the ablation is read-side, via the condition patch)."""
    records = run_cell("STRUCTURED_NO_STATE", SEED, tmp_path / "cell",
                       days=1, fake=True, perturb=True)
    store = SQLiteStore(records["db"], audit_mode=True)
    try:
        assert store.load_daily_state(0) is not None
        neutral = _neutral_day0_plan()
        rows = _day0_rows(store)
        assert set(neutral) <= set(rows), (
            f"SNS day-0 rows {rows} lost the neutral plan {neutral}"
        )
    finally:
        store.close()


def test_day0_plan_preexists_firing_loop(tmp_path):
    """Timing rationale preserved: the day-0 plan exists before the firing
    loop starts — a 1-day cell actually FIRES a day-0 event (a row first
    planned at midnight day 1 would be born expired and could never fire)."""
    records = run_cell("FULL", SEED, tmp_path / "cell", days=1,
                       fake=True, perturb=True)
    store = SQLiteStore(records["db"], audit_mode=True)
    try:
        fired = [r for r in store.schedule_events_for_seed(SEED)
                 if int(r["day"]) == 0 and r["status"] == "fired"]
        assert fired, "the day-0 plan must pre-exist the firing loop"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 3. fired-event integrity under re-plan with different scores
# --------------------------------------------------------------------------- #


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


@pytest.mark.parametrize("entry", ["matrix", "live"],
                         ids=["matrix-run_cell", "live-build_runtime"])
def test_fired_rows_survive_replan_with_different_scores(tmp_path, entry):
    """Both experiment paths: after day-0 events have FIRED, a re-plan with
    different scores leaves the fired rows untouched (same t_h/reason,
    status stays fired) and never resurrects a fired hour as pending."""
    if entry == "matrix":
        records = run_cell("FULL", SEED, tmp_path / "cell", days=2,
                           fake=True, perturb=True)
        store = SQLiteStore(records["db"], audit_mode=True)
        # rows fired by the run itself (day 0 and day 1)
        fired_t = sorted(
            float(r["t_h"]) for r in store.schedule_events_for_seed(SEED)
            if r["status"] == "fired"
        )
        assert fired_t, "the cell must fire events before the re-plan"
        horizon = 3
        scores = DIFF_SCORES
    else:
        store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
        bootstrap(store, seed=SEED)
        build_runtime(store, SEED, "FULL", FakeChannel(),
                      client=FakeClient(),
                      judge=DeterministicJudge(SEED, block_start=BLOCK_START_D,
                                               block_end=BLOCK_END_D))
        day0 = _day0_rows(store)
        assert day0, "the live day-0 plan must produce rows"
        fired_t = [day0[0]]
        store.mark_schedule_fired(SEED, fired_t[0], fired_t[0])
        horizon = 2
        scores = DIFF_SCORES[:2]
    try:
        ProactiveSchedule.plan_and_persist(
            horizon, SEED, PERSONA, TIMING, store,
            reason=REASON_SCHEDULE, scores=scores,
        )
        _assert_fired_rows_intact(store, fired_t)
    finally:
        store.close()


def test_replan_with_different_scores_regenerates_unfired_events(tmp_path):
    """The matrix plan-entry shape (fresh store, day-0 plan via the same
    sequence run_cell uses): a re-plan with different scores inserts NEW
    pending rows for unfired future events (regeneration) while fired rows
    stay untouched — the INSERT OR IGNORE integrity contract."""
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
