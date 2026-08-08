"""Scheduler + proactive firing tests (W-E2)."""

import numpy as np
import pytest

from engine.types import PersonaParams, TimingParams
from engine.circadian import envelope
from harness.clock import VirtualClock
from harness.client import FakeClient
from harness.judge import ScriptedJudge
from harness.scheduler import ProactiveSchedule, plan_proactive_events
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
    assert schedule.next_pending(30.0) is None


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
