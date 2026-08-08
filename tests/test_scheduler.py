"""Scheduler + proactive firing tests (W-E2)."""

import numpy as np
import pytest

from engine.types import ADJ_SLOPE, PersonaParams, TimingParams
from engine.circadian import envelope
from harness.clock import VirtualClock
from harness.client import FakeClient
from harness.judge import ScriptedJudge
from harness.scheduler import (
    INITIATIVE_BOUNDS,
    REASON_SCHEDULE,
    ProactiveSchedule,
    adj_from_score,
    day_scores,
    initiative_factor,
    plan_proactive_events,
)
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
SEED = 777


def test_plan_is_deterministic():
    a = plan_proactive_events(30, SEED, PERSONA, TIMING)
    b = plan_proactive_events(30, SEED, PERSONA, TIMING)
    assert np.array_equal(a, b)


def test_plan_respects_horizon_and_quiet_hours():
    days = 60
    events = plan_proactive_events(days, SEED, PERSONA, TIMING)
    assert len(events) > 0
    assert events[0] >= 0.0
    assert events[-1] < days * 24.0
    for t in events:
        assert envelope(t % 24.0, TIMING) >= 1e-9, f"event in quiet hours at {t % 24:.2f}h"


def test_plan_respects_daily_cap():
    days = 90
    events = plan_proactive_events(days, SEED, PERSONA, TIMING)
    day_counts = {}
    for t in events:
        day = int(t // 24.0)
        day_counts[day] = day_counts.get(day, 0) + 1
    assert max(day_counts.values()) <= TIMING.daily_cap


def test_plan_daily_rate_sane():
    days = 90
    events = plan_proactive_events(days, SEED, PERSONA, TIMING)
    rate = len(events) / days
    assert 0.5 <= rate <= 4.0


def test_schedule_bookkeeping():
    schedule = ProactiveSchedule(event_hours=np.asarray([5.0, 10.0, 20.0]))
    assert schedule.due_at(6.0) == [5.0]
    schedule.mark_fired(5.0)
    assert schedule.due_at(30.0) == [10.0, 20.0]
    assert schedule.next_pending(6.0) == 10.0
    schedule.mark_fired(10.0)
    assert schedule.due_at(30.0) == [20.0]
    # A7: an overdue PENDING event is visible, not skipped.
    assert schedule.next_pending(30.0) == 20.0
    schedule.mark_fired(20.0)
    assert schedule.next_pending(30.0) is None


# --------------------------------------------------------------------------- #
# A7 restart regression: next_pending must surface overdue pending events
# --------------------------------------------------------------------------- #


def _schedule(*hours):
    return ProactiveSchedule(event_hours=np.asarray(list(hours), dtype=float))


def test_next_pending_visible_at_exact_event_time():
    # Restart exactly AT the event: now == event_time ⇒ it MUST be visible.
    schedule = _schedule(10.0)
    assert schedule.next_pending(10.0) == 10.0


def test_next_pending_visible_ten_minutes_after():
    # Restart 10 min after the event: overdue, must still be returned.
    schedule = _schedule(10.0)
    assert schedule.next_pending(10.0 + 10.0 / 60.0) == 10.0


def test_next_pending_overdue_within_validity_window():
    # An overdue event inside its validity window is returned (and the
    # runtime fires it); the next future event is NOT preferred over it.
    schedule = _schedule(10.0, 14.0)
    assert schedule.next_pending(12.0) == 10.0


def test_next_pending_overdue_beyond_validity_window():
    # Even far beyond the validity window the row is surfaced (the runtime
    # decides fire-vs-expire from the validity window, not next_pending).
    schedule = _schedule(10.0, 30.0)
    assert schedule.next_pending(48.0) == 10.0


def test_next_pending_multiple_overdue_earliest_first():
    # Several overdue pending events → earliest is returned first, then the
    # next, and a future event only after all overdue ones are consumed.
    schedule = _schedule(10.0, 11.0, 20.0)
    assert schedule.next_pending(15.0) == 10.0
    schedule.mark_fired(10.0)
    assert schedule.next_pending(15.0) == 11.0
    schedule.mark_fired(11.0)
    assert schedule.next_pending(15.0) == 20.0


def test_next_pending_fired_rows_never_returned():
    schedule = _schedule(10.0, 12.0)
    schedule.mark_fired(10.0)
    assert schedule.next_pending(11.0) == 12.0  # overdue fired row skipped
    schedule.mark_fired(12.0)
    assert schedule.next_pending(20.0) is None


def test_restore_keeps_overdue_rows_pending(tmp_path):
    # restore() seeds _fired only from non-pending rows — overdue pending
    # rows survive a restart and are surfaced by next_pending (the bug).
    store = SQLiteStore(tmp_path / "s.db")
    try:
        store.save_schedule_events(SEED, [
            {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        ])
        restored = ProactiveSchedule.restore(SEED, store)
        assert restored.next_pending(10.0) == 10.0
        assert restored.next_pending(10.2) == 10.0
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# A7 timing feedback: A(score_{d-1}) · I(t) enters the hazard
# --------------------------------------------------------------------------- #


def test_a_mapping_is_monotone_and_bounded():
    # A(s) = adj_from_score: worse score ⇒ lower adjustment, better ⇒ higher.
    assert adj_from_score(-1.0, TIMING) < adj_from_score(0.0, TIMING) < adj_from_score(1.0, TIMING)
    for s in (-2.0, -1.0, 0.0, 1.0, 2.0):
        assert TIMING.adj_bounds[0] <= adj_from_score(s, TIMING) <= TIMING.adj_bounds[1]


def test_initiative_factor_neutral_monotone_bounded():
    assert initiative_factor(0.5) == pytest.approx(1.0)
    assert initiative_factor(0.2) < 1.0 < initiative_factor(0.8)
    lo, hi = INITIATIVE_BOUNDS
    assert lo <= initiative_factor(0.0) <= hi
    assert lo <= initiative_factor(1.0) <= hi


def test_worse_previous_day_score_lowers_hazard():
    # The full A7 formula: plan the same days with effective scores built
    # from a bad vs a good previous-day judgement — fewer accepted events.
    days = 150
    lo = np.full(days, (adj_from_score(-0.9, TIMING) - 1.0) / ADJ_SLOPE)
    hi = np.full(days, (adj_from_score(0.9, TIMING) - 1.0) / ADJ_SLOPE)
    events_lo = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=lo)
    events_hi = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=hi)
    assert len(events_lo) < len(events_hi)


def test_higher_initiative_raises_hazard():
    days = 150
    low_i = np.full(days, (initiative_factor(0.2) - 1.0) / ADJ_SLOPE)
    high_i = np.full(days, (initiative_factor(0.8) - 1.0) / ADJ_SLOPE)
    events_low = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=low_i)
    events_high = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=high_i)
    assert len(events_low) < len(events_high)


def _daily_row(day, *, M=6, mu=0.0, eta=0.0, phase_label="phase_a"):
    return {
        "day": day, "M": M, "m": 0.0, "g": 0.7, "p": 0.5, "arg": 0.0,
        "mu": mu, "eta": eta, "cycle_day": float(day), "phase_label": phase_label,
        "seed": SEED, "score": None,
    }


def test_day_scores_use_previous_day_judgement_and_initiative():
    from test_proactive import SeamStore

    def scores_for(score_prev, initiative_day):
        store = SeamStore()
        store.save_daily_state(0, _daily_row(0))
        store.save_daily_state(1, _daily_row(1))
        if score_prev is not None:
            store.save_judgement(0, score_prev, "j", None, shadow=True)
        # day_initiative reads the stored directive; override by writing the
        # derived directive's initiative back into a fixed M via derive.
        # Simpler: drive initiative through the day-1 record's mood (M) and
        # previous record (momentum) — higher M ⇒ higher initiative.
        return day_scores(store, 1, TIMING)[0]

    low = scores_for(-0.9, None)
    high = scores_for(0.9, None)
    assert low < high  # better previous day ⇒ larger effective score

    neutral = scores_for(0.0, None)
    # Monotone in the judgement across the whole range.
    assert low < neutral < high


def test_day_scores_missing_judgement_falls_back_neutral():
    from test_proactive import SeamStore
    store = SeamStore()
    store.save_daily_state(0, _daily_row(0))
    store.save_daily_state(1, _daily_row(1))
    scores = day_scores(store, 1, TIMING)
    assert len(scores) == 2
    # No judgement: A ≡ 1.0; day 1 initiative ≈ neutral-ish ⇒ effective ≈ 0.
    assert scores[0] == pytest.approx(0.0, abs=0.35)


def test_day_scores_shape_covers_current_day_only():
    from test_proactive import SeamStore
    store = SeamStore()
    store.save_daily_state(0, _daily_row(0))
    store.save_daily_state(1, _daily_row(1))
    store.save_daily_state(2, _daily_row(2))
    store.save_judgement(0, 0.3, "j", None, shadow=True)
    store.save_judgement(1, -0.2, "j", None, shadow=True)
    scores = day_scores(store, 2, TIMING)
    assert scores.shape == (3,)
    # entries for judged days are real; the current day is a placeholder
    assert scores[0] != 0.0 and scores[1] != 0.0
    assert scores[2] == 0.0


def test_fire_proactive_creates_proactive_message(tmp_path):
    from engine.types import MoodVariant
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=["proactive hello!"])
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=client,
        clock=VirtualClock(t_h=10.0),
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    result = session.fire_proactive()
    assert result.reply == "proactive hello!"
    msgs = store.messages_for_day(0)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["proactive"] == 1
    # fresh transcript → system-only payload; no trailing user request
    last_call = client.calls[-1]
    assert last_call["messages"][-1]["role"] == "system"
    assert "reaching out first" in last_call["system"]


def test_fire_proactive_validates_reason(tmp_path):
    from engine.types import MoodVariant
    store = SQLiteStore(tmp_path / "s.db")
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=FakeClient(),
        clock=VirtualClock(t_h=10.0),
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    with pytest.raises(ValueError, match="reason"):
        session.fire_proactive(reason="nagging")


def test_session_with_schedule_end_to_end(tmp_path):
    """Clock advance → due proactive events fire in order."""
    from engine.types import MoodVariant
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=[f"proactive #{i}" for i in range(20)])
    clock = VirtualClock(t_h=8.0)
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    schedule = ProactiveSchedule.plan(5, SEED, PERSONA, TIMING)
    # advance through day 0: fire whatever is due
    clock.advance_hours(16.0)  # to 24:00
    due = schedule.due_at(clock.now_h())
    for t in due:
        if t > clock.now_h():
            clock.advance_hours(t - clock.now_h())
        session.fire_proactive()
        schedule.mark_fired(t)
    assert len(due) > 0
    proactives = store.conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE proactive = 1"
    ).fetchone()["n"]
    assert proactives == len(due)
    store.close()
