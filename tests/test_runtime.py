"""AsyncRuntime tests (wave 3 + A7): rollover, firing, gates, restart
recovery, grounded intents, timing feedback, delivery latency.

A tiny TimeScale (1 virtual hour = 1 ms real) runs full multi-day proactive
cycles in milliseconds while the short poll sleep keeps the event loop
responsive. All proactive activity is driven by schedule rows injected via
the store (seam A-1) and by agenda items seeded for the event hours, so
quiet-hours / cooldown / expired / recovery cases are fully deterministic
even though the planner itself never places events in quiet hours.

The A2 store seam has not landed in this repo, so the runtime tests run on
the seam-faithful SeamStore (test_proactive) and inject a real
IntentResolver over it; the sleeper is always injected (recorded, never
real seconds).
"""

import asyncio

import numpy as np

import engine.rng as rng_mod
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.behavior import derive_behavior
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import DailyAgenda
from harness.judge import ScriptedJudge
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import (
    REASON_SCHEDULE,
    ProactiveSchedule,
    day_scores,
)
from harness.session import Session
from tests.helpers import SeamStore, agenda_item, ground_agenda, make_session, rows, suppressed_codes

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345


def _session(*, replies=None):
    """Local 3-tuple wrapper over the shared make_session (store, clock,
    session) — this file's call sites unpack all three and use the
    module's PERSONA/TIMING/VARIANT/SEED constants."""
    store = SeamStore()
    clock = VirtualClock()
    session = make_session(
        store,
        clock=clock,
        client=FakeClient(responses=replies or ["ok!"]),
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
    )
    return store, clock, session

#: 1 virtual hour = 2 ms real, so a day rolls over in 48 ms; poll
#: sleeps are ~0.1 ms and per-event overhead stays below the inter-event sleeps.
FAST = TimeScale(seconds_per_virtual_hour=0.002)

#: Cooldown scale: the 10.0 → 10.1 pair is 0.1 h apart (window 0.25 h),
#: which is 0.2 ms at FAST and 50 ms at 0.5 s/vh.
SLOW = TimeScale(seconds_per_virtual_hour=0.5)


def _run(store, session, schedule, channel, *, max_hours, scale=FAST,
         resolver=None, sleeper=None, seed=SEED):
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=seed,
        time_scale=scale, max_virtual_hours=max_hours,
        resolver=resolver if resolver is not None else IntentResolver(
            store, rng=rng_mod.stream_rng(seed)
        ),
        sleeper=sleeper if sleeper is not None else record,
    )
    asyncio.run(runtime.run())
    runtime._delays = delays  # recorded response_delay_s values
    return runtime


class TraceChannel(FakeChannel):
    """FakeChannel that also records send events into a shared trace."""

    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    async def send(self, message):
        self.trace.append(("send", message.text))
        await super().send(message)


# --- gates at fire time (quiet hours, cooldown) with grounded intents ---


def test_quiet_hours_and_cooldown_events_suppressed():
    """(a)+(b) events landing in quiet hours or inside the cooldown gap are
    suppressed + logged, and only the passing event produces a proactive
    OutboundMessage with its reason."""
    store, clock, session = _session()
    ground_agenda(store, 0.5, 3.5, item_id="night")      # covers 02:00
    ground_agenda(store, 9.5, 10.6, item_id="morning")   # covers 10:00/10:06
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
    assert suppressed_codes(store) == {"quiet_hours", "cooldown"}

# all three slots consumed (no pending rows left for them)
    schedule_rows = rows(store, SEED)
    for t_h in (2.0, 10.0, 10.1):
        assert schedule_rows[t_h]["status"] == "fired", f"row {t_h} not consumed"
    assert schedule_rows[10.0]["fired_t_h"] == 10.0
# intent statuses recorded for both the fired and suppressed intents
    assert {i.id for i in store.list_proactive_intents(status="fired")}
    assert {i.id for i in store.list_proactive_intents(status="suppressed")}


def test_restart_does_not_refire():
    """(c) ProactiveSchedule.restore from the same store must not re-fire an
    event that already fired in a previous runtime instance."""
    store, clock, session = _session()
    ground_agenda(store, 0.0, 24.0, item_id="allday")
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
    _run(store, session2, ProactiveSchedule.restore(SEED, store), ch2,
         max_hours=12.0)
    assert ch2.sent == []  # no duplicate fire


def test_rollover_advances_day_and_persists():
    """(d) rollover advances the virtual day and re-plans + persists new
    pending events for the CURRENT day (A7: day 1+ rows appear only after
    the rollover)."""
    store, clock, session = _session()
    ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    pending_before = len(store.pending_schedule_events(SEED))
    assert pending_before > 0

    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=50.0)

    assert clock.now_h() >= 24.0          # clock crossed a midnight
    assert session.current_day is not None and session.current_day >= 1  # session rolled over
    assert store.load_daily_state(1) is not None  # new day persisted
    pending = store.pending_schedule_events(SEED)
# The re-plan covers the current day — new rows exist for day 1+
    assert any(r["day"] >= 1 for r in pending)
    store.close()


def test_reactive_inbound_reply_via_channel():
    """Reactive path: FakeChannel.feed → session reply via channel.send with
    proactive=False, after the recorded response delay."""
    store, clock, session = _session(replies=["hello!"])
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


def test_expired_event_marked_expired():
    """An inbound message carrying a virtual t_h advances the clock; a planned
    event whose OPPORTUNITY validity window has elapsed is consumed as
    'expired' (no message sent for it)."""
    store, clock, session = _session(replies=["hi back"])
    ground_agenda(store, 9.5, 10.5, item_id="slot")
    ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()

    async def driver():
        async def delayed_feed():
            await asyncio.sleep(FAST.seconds_per_virtual_hour * 2.5)  # let the firing loop pick nxt first
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
# the day-0 event elapsed its validity window → expired + logged
    rows = store.schedule_events_for_seed(SEED)
    assert any(r["status"] == "expired" for r in rows)
    assert "expired" in suppressed_codes(store)
    store.close()


# --- restart recovery: overdue pending events are evaluated, not stranded ---


def test_restart_exactly_at_event_fires():
    store, clock, session = _session(replies=["proactive!"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    clock.advance_hours(10.0)  # restart exactly at the event time
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=10.5)
    assert len(channel.sent) == 1 and channel.sent[0].proactive
    assert rows(store, SEED)[10.0]["status"] == "fired"


def test_restart_ten_minutes_after_fires():
    store, clock, session = _session(replies=["proactive!"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    clock.advance_hours(10.0 + 10.0 / 60.0)  # 10 min late — inside validity
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=10.5)
    assert len(channel.sent) == 1 and channel.sent[0].proactive
    assert rows(store, SEED)[10.0]["status"] == "fired"


def test_restart_within_validity_window_fires():
    store, clock, session = _session(replies=["proactive!"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    clock.advance_hours(12.0)  # 2 h late — still inside the 3 h window
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=12.5)
    assert len(channel.sent) == 1 and channel.sent[0].proactive
    assert rows(store, SEED)[10.0]["status"] == "fired"


def test_restart_beyond_validity_window_expires():
    store, clock, session = _session(replies=["proactive!"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    clock.advance_hours(13.5)  # 3.5 h late — past the 3 h validity window
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=14.0)
    assert channel.sent == []  # expired, NOT fired
    assert rows(store, SEED)[10.0]["status"] == "expired"
    assert "expired" in suppressed_codes(store)


def test_restart_multiple_overdue_all_evaluated():
    """Several overdue events are surfaced in order and none is stranded:
    the first fires, the second is consumed by the cooldown gate."""
    store, clock, session = _session(replies=["proactive!", "second!"])
    ground_agenda(store, 9.5, 10.5, item_id="slot_a")
    ground_agenda(store, 10.6, 11.4, item_id="slot_b")
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 11.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    clock.advance_hours(12.0)
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=12.5)
# first overdue event fires; the second is delivered at the same now →
# inside the cooldown gap → suppressed (consumed, not stranded)
    assert len(channel.sent) == 1
    schedule_rows = rows(store, SEED)
    assert schedule_rows[10.0]["status"] == "fired"
    assert schedule_rows[11.0]["status"] == "fired"
    assert store.pending_schedule_events(SEED) == []
    assert "cooldown" in suppressed_codes(store)


# --- groundedness: SUPPRESS when no grounded reason ---


def test_no_grounded_reason_suppresses():
    """A contact opportunity with NO grounded candidate is suppressed with
    no_grounded_reason and the row is consumed — nothing is hallucinated."""
    store, clock, session = _session(replies=["proactive!"])
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=11.0)
    assert channel.sent == []
    assert "no_grounded_reason" in suppressed_codes(store)
    assert rows(store, SEED)[10.0]["status"] == "fired"  # consumed
    assert store.list_proactive_intents() == []  # not persisted


class DeleteAfterResolveResolver(IntentResolver):
    """A9-style attack: the intent resolves against a source that is deleted
    before the content gate runs."""

    def resolve(self, opportunity_t_h):
        intent = super().resolve(opportunity_t_h)
        if intent is not None:
            self.store._agenda_items.clear()
        return intent


def test_grounding_attack_deleted_source_suppressed():
    store, clock, session = _session(replies=["proactive!"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()
    resolver = DeleteAfterResolveResolver(store, rng=rng_mod.stream_rng(SEED))
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=11.0, resolver=resolver)
    assert channel.sent == []  # suppressed, not hallucinated
    assert "no_source" in suppressed_codes(store)
    assert rows(store, SEED)[10.0]["status"] == "fired"


# --- delivery latency: injectable sleeper between LLM and channel.send ---


def _expected_delay(session, hour):
    return derive_behavior(
        session.current_record, TIMING, hour=hour
    ).response_delay_s


def test_proactive_latency_sleeps_before_send():
    store, clock, session = _session(replies=["proactive hi"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    trace = []

    async def sleeper(delay):
        trace.append(("sleep", delay))

    channel = TraceChannel(trace)
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=11.0, scale=SLOW, sleeper=sleeper)
    expected = _expected_delay(session, hour=10.0)
    assert expected > 0.0
# sleep(LLM-done → send) happens before the send, with the directive's
# wall-clock delay; suppressed events reach no LLM, so no delay.
    assert trace == [("sleep", expected), ("send", "proactive hi")]


def test_reactive_latency_sleeps_before_send():
    store, clock, session = _session(replies=["hello!"])
    trace = []

    async def sleeper(delay):
        trace.append(("sleep", delay))

    channel = TraceChannel(trace)

    async def driver():
        feed = asyncio.create_task(channel.feed("hi", t_h=0.5))
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=1.0,
                sleeper=sleeper,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    expected = _expected_delay(session, hour=0.5)
    assert trace == [("sleep", expected), ("send", "hello!")]
    store.close()


def test_suite_runs_without_real_waits():
    """The default sleeper is asyncio.sleep — but every runtime test injects
    a recorder, so the suite must never wait real seconds. The recorded
    delays are the directive's wall-clock seconds (NOT scaled by TimeScale)."""
    store, clock, session = _session(replies=["proactive hi"])
    ground_agenda(store, 9.5, 10.5)
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()
    runtime = _run(store, session, ProactiveSchedule.restore(SEED, store),
                   channel, max_hours=11.0, scale=SLOW)
    expected = _expected_delay(session, hour=10.0)
    assert runtime._delays == [expected]
    assert expected > 1.0  # a REAL asyncio.sleep here would take seconds


# --- conversation lifecycle in the live runtime (SeamStore path) ---


def test_quiet_hours_closes_open_conversation_at_boundary():
    """A5: a conversation open at 22:50 is closed with quiet_hours AT the
    23:00 boundary (the rollover parks at the quiet-hours start), and no
    companion turn fires inside quiet hours."""
    store, clock, session = _session(replies=["night reply"])
    channel = FakeChannel()

    async def driver():
        feed = asyncio.create_task(channel.feed("good evening", t_h=22.833))
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=23.5,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    assert session.open_conversation_id() is None  # closed at the boundary
    closes = [
        e for e in store.events_since(0)
        if e["event"] == "conversation_closed"
    ]
    assert len(closes) == 1
    assert closes[0]["detail"] == "id=conv-0 reason=quiet_hours turns=2"
# the reactive reply was delivered; no companion turn inside quiet hours
    assert [m.text for m in channel.sent] == ["night reply"]
    assert all(
        m["role"] != "assistant" or m["t_h"] < 23.0
        for m in store.messages_for_day(0)
    )
    store.close()


def test_user_left_closes_open_conversation_at_deadline():
    """user_left: the rollover parks at the silence deadline (last user
    turn 10:00 + USER_LEFT_THRESHOLD_H = 22:00) and records the close
    there, not lazily at the next turn."""
    store, clock, session = _session(replies=["morning"])
    channel = FakeChannel()

    async def driver():
        feed = asyncio.create_task(channel.feed("morning", t_h=10.0))
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=22.5,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    assert session.open_conversation_id() is None
    closes = [
        e for e in store.events_since(0)
        if e["event"] == "conversation_closed"
    ]
    assert len(closes) == 1
    assert "reason=user_left" in closes[0]["detail"]
    store.close()


# --- timing feedback in the live runtime ---


def test_replan_never_passes_scores_none(monkeypatch):
    store, clock, session = _session()
    ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    calls: list = []
    orig = ProactiveSchedule.plan_and_persist.__func__

    def spy(cls, *args, **kwargs):
        calls.append(kwargs.get("scores"))
        return orig(cls, *args, **kwargs)

    monkeypatch.setattr(ProactiveSchedule, "plan_and_persist", classmethod(spy))
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=50.0)
    assert len(calls) >= 2  # one re-plan per rollover
    assert all(s is not None for s in calls)  # scores are non-None live


def test_replan_plans_current_day_with_real_scores():
    store, clock, session = _session()
    ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=50.0)
# Replanning on the finished store changes nothing — INSERT OR IGNORE
# is idempotent and the schedule is stable.
    rows = store.schedule_events_for_seed(SEED)
    day1 = {r["t_h"] for r in rows if r["day"] == 1}
    assert len(day1) > 0
    before = sorted((float(r["t_h"]), r["status"]) for r in rows)
    scores = day_scores(store, 1, TIMING)
    ProactiveSchedule.plan_and_persist(
        2, SEED, PERSONA, TIMING, store,
        reason=REASON_SCHEDULE, scores=scores,
    )
    after = sorted((float(r["t_h"]), r["status"])
                   for r in store.schedule_events_for_seed(SEED))
    assert after == before, "replan on the finished store drifted the schedule"
