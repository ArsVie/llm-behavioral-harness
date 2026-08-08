"""AsyncRuntime tests (wave 3): rollover, firing, gates, restart, reactive.

A tiny TimeScale (1 virtual hour = 1 ms real) runs full multi-day proactive
cycles in milliseconds while the short poll sleep keeps the event loop
responsive. All proactive activity is driven by schedule rows injected via
the store (seam A-1), so quiet-hours / cooldown / expired cases are fully
deterministic even though the planner itself never places events in quiet
hours.
"""

import asyncio

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

#: 1 virtual hour = 20 ms real → a day rolls over in 480 ms; poll sleeps are
#: ~1 ms. Small enough for sub-second days, large enough that per-event
#: processing overhead (store writes, worker threads) never overtakes the
#: inter-event sleeps, keeping the rollover-vs-firing race deterministic.
FAST = TimeScale(seconds_per_virtual_hour=0.02)

#: Robust scale for the cooldown test. The 10.0 → 10.1 pair is only 0.1 h
#: apart (the cooldown window is 0.25 h), which is 2 ms at FAST — under load
#: that sleep can stall past the rollover loop's wake (12 h → 240 ms), the
#: clock jumps, and the event fires late (gates legitimately pass at the new
#: time). At 0.5 s/vh the gap is 50 ms and the rollover wake is ~1 s after
#: the last event, so the gate decision happens at the planned time.
SLOW = TimeScale(seconds_per_virtual_hour=0.5)


def _session(tmp_path, *, replies=None):
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    client = FakeClient(responses=replies or ["ok!"])
    judge = ScriptedJudge(score=0.5)
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client,
        clock=clock,
        judge=judge.judge_day,
    )
    return store, clock, session


def _run(store, session, schedule, channel, *, max_hours, scale=FAST) -> AsyncRuntime:
    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=scale, max_virtual_hours=max_hours,
    )
    asyncio.run(runtime.run())
    return runtime


def _rows(store):
    return {abs(r["t_h"]): r for r in store.schedule_events_for_seed(SEED)}


def _suppressed_codes(store):
    return {
        e["detail"]
        for e in store.events_since(0)
        if e["event"] == "proactive_suppressed"
    }


def test_quiet_hours_and_cooldown_events_suppressed(tmp_path):
    """(a)+(b) events landing in quiet hours or inside the cooldown gap are
    suppressed + logged, and only the passing event produces a proactive
    OutboundMessage with its reason."""
    store, clock, session = _session(tmp_path)
    store.save_schedule_events(SEED, [
        {"t_h": 2.0, "day": 0, "reason": REASON_SCHEDULE},    # 02:00 → quiet hours
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},   # awake → fires
        {"t_h": 10.1, "day": 0, "reason": REASON_SCHEDULE},   # 6 min after → cooldown
    ])
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=12.0, scale=SLOW)

    # exactly one proactive message: the 10:00 event, with its reason
    assert len(channel.sent) == 1
    msg = channel.sent[0]
    assert msg.proactive is True and msg.reason == REASON_SCHEDULE
    assert store.proactive_count(0) == 1

    # both suppressions logged with their gate codes
    assert _suppressed_codes(store) == {"quiet_hours", "cooldown"}

    # all three slots consumed (no pending rows left for them)
    rows = _rows(store)
    for t_h in (2.0, 10.0, 10.1):
        assert rows[t_h]["status"] == "fired", f"row {t_h} not consumed"
    assert rows[10.0]["fired_t_h"] == 10.0
    store.close()


def test_restart_does_not_refire(tmp_path):
    """(c) ProactiveSchedule.restore from the same store must not re-fire an
    event that already fired in a previous runtime instance."""
    store, clock, session = _session(tmp_path)
    ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    schedule = ProactiveSchedule.restore(SEED, store)

    ch1 = FakeChannel()
    _run(store, session, schedule, ch1, max_hours=12.0)
    fired_rows = [r for r in store.schedule_events_for_seed(SEED) if r["status"] == "fired"]
    assert len(ch1.sent) == len(fired_rows) > 0
    assert all(m.proactive and m.reason == REASON_SCHEDULE for m in ch1.sent)

    # restart: fresh session (resumes state), schedule rebuilt from store,
    # fresh channel — run past the fired event's time again
    session2 = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=["ok!"]),
        clock=VirtualClock(),
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    ch2 = FakeChannel()
    _run(store, session2, ProactiveSchedule.restore(SEED, store), ch2, max_hours=12.0)
    assert ch2.sent == []  # no duplicate fire
    store.close()


def test_rollover_advances_day_and_persists(tmp_path):
    """(d) rollover advances the virtual day and re-plans + persists new
    pending events for the extended horizon."""
    store, clock, session = _session(tmp_path)
    ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    pending_before = len(store.pending_schedule_events(SEED))
    assert pending_before > 0

    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel, max_hours=50.0)

    assert clock.now_h() >= 24.0          # clock crossed a midnight
    assert session.current_day is not None and session.current_day >= 1  # session rolled over
    assert store.load_daily_state(1) is not None  # new day persisted
    pending_after = len(store.pending_schedule_events(SEED))
    assert pending_after > pending_before  # re-plan extended the horizon
    store.close()


def test_reactive_inbound_reply_via_channel(tmp_path):
    """Reactive path: FakeChannel.feed → session reply via channel.send with
    proactive=False."""
    store, clock, session = _session(tmp_path, replies=["hello!"])
    channel = FakeChannel()

    async def driver():
        feed = asyncio.create_task(channel.feed("hi", t_h=0.5))
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=1.0,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    assert len(channel.sent) == 1
    msg = channel.sent[0]
    assert msg.text == "hello!"
    assert msg.proactive is False and msg.reason is None
    store.close()


def test_expired_event_marked_expired(tmp_path):
    """An inbound message carrying a virtual t_h advances the clock; a planned
    event whose validity window has elapsed is consumed as 'expired' (no
    message sent for it)."""
    store, clock, session = _session(tmp_path, replies=["hi back"])
    ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    # guarantee a known day-0 event: the planner's first event may land past
    # max_virtual_hours, which would skip gating entirely
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()

    async def driver():
        async def delayed_feed():
            await asyncio.sleep(0.05)  # let the firing loop pick nxt first
            await channel.feed("hi", t_h=28.5)  # jump past day 0's validity
        feed = asyncio.create_task(delayed_feed())
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=30.0,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())

    # only the reactive reply was sent — nothing proactive fired
    assert len(channel.sent) == 1
    assert channel.sent[0].proactive is False
    # the first day-0 event elapsed its validity window → expired + logged
    rows = store.schedule_events_for_seed(SEED)
    assert any(r["status"] == "expired" for r in rows)
    assert "expired" in _suppressed_codes(store)
    store.close()
