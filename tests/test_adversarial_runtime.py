"""A9 Iteration-2 adversarial wave — RUNTIME attack class (plan §5-A9 R1).

Attacks on the accelerated-time runtime (plan §16 invariants 3 and 17):
rollover must never jump the virtual clock past a pending event (fast-clock
events near midnight fire at their own time, never spurious-expire), the
quiet-hours deferral must not consume still-valid events, a send exception
must not corrupt the process (terminate + no orphan threads), and
cancellation during a sleep must shut down cleanly.

Deterministic only: injected sleeper (recorded, never real seconds) except
the one bounded real sleep that deterministically triggers the rollover-vs-
firing race; virtual clock, fixed seeds, no LLM.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel, OutboundMessage
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import DailyAgenda, EpisodicMemory, MemoryKind
from harness.judge import ScriptedJudge
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import (
    REASON_CHECK_IN,
    REASON_SCHEDULE,
    REASON_SHARED_INTEREST,
    ProactiveSchedule,
)
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

FAST = TimeScale(seconds_per_virtual_hour=0.02)
SLOW = TimeScale(seconds_per_virtual_hour=0.5)

#: quiet hours: 23:00-08:00 + 1h ramps — fully awake from 09:00 local.
QUIET_FIN_AWAKE_H = 33.0  # day-1 09:00 in absolute hours


def _store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


def _session(store, clock: VirtualClock | None = None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=["ok!"]),
        clock=clock or VirtualClock(),
        judge=ScriptedJudge(score=0.5).judge_day,
    )


def _ground_agenda(store, start_t_h, end_t_h, *, item_id="g1", salience=0.8,
                   activity="pottery class"):
    from harness.domain import AgendaItem

    item = AgendaItem(item_id, start_t_h, end_t_h, activity, "arc", "arc1",
                      salience, "planned")
    store.save_agenda(0, DailyAgenda(0, (item,)))
    return item


def _rows(store):
    return {abs(float(r["t_h"])): r for r in store.schedule_events_for_seed(SEED)}


def _run(store, session, schedule, channel, *, max_hours, scale=FAST,
         sleeper=None, resolver=None, clock_start_h=None):
    """Run the real AsyncRuntime for a bounded horizon; the sleeper is the
    injected recording sleeper unless a blocking one is given."""
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    if clock_start_h is not None and session.clock.now_h() < clock_start_h:
        session.clock.advance_hours(clock_start_h - session.clock.now_h())
    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=scale, max_virtual_hours=max_hours,
        resolver=resolver if resolver is not None else IntentResolver(store),
        sleeper=sleeper if sleeper is not None else record,
    )
    asyncio.run(runtime.run())
    runtime._delays = delays
    return runtime


def _no_llh_threads() -> bool:
    return not any(n.startswith("llh-runtime") for n in
                   [t.name for t in threading.enumerate()])


# R1-a: the clock does not jump past a pending event


def test_r1a_fast_clock_events_near_midnight_fire_not_expire(tmp_path):
    """E0 stress variant closer to midnight than the Iteration-1 regression:
    events at 21.5 and 22.5 (the 22.5 one inside the envelope ramp-down,
    validity 3h → still valid at midnight) under FAST accelerated time with
    a blocking sleeper that holds the firing loop long enough for the
    rollover's midnight sleep to complete. The rollover must PARK at the
    pending event: both fire AT THEIR OWN TIMES (fired_t_h == event hour),
    never spuriously expired."""
    store = _store(tmp_path, "r1a.db")
    try:
        _ground_agenda(store, 21.0, 22.0, item_id="slot_a", activity="evening a")
        _ground_agenda(store, 22.0, 23.0, item_id="slot_b", activity="evening b")
        store.save_schedule_events(SEED, [
            {"t_h": 21.5, "day": 0, "reason": REASON_SCHEDULE},
            {"t_h": 22.5, "day": 0, "reason": REASON_SCHEDULE},
        ])
        channel = FakeChannel()

        async def blocking_sleeper(delay: float) -> None:
            # bounded real wait: a deterministic rollover-vs-firing race trigger
            # (the 24:00 sleep completes during event A's response delay)
            await asyncio.sleep(0.3)

        _run(store, _session(store), ProactiveSchedule.restore(SEED, store),
             channel, max_hours=25.5, scale=FAST, sleeper=blocking_sleeper)

        rows = _rows(store)
        assert rows[21.5]["status"] == "fired"
        assert rows[22.5]["status"] == "fired", (
            "near-midnight event spuriously expired by the midnight rollover"
        )
        assert rows[22.5]["fired_t_h"] == pytest.approx(22.5), (
            "event not gated at its own time"
        )
        suppressed = {
            e["detail"] for e in store.events_since(0)
            if e["event"] == "proactive_suppressed"
        }
        assert "expired" not in suppressed
        assert len([m for m in channel.sent if m.proactive]) == 2
    finally:
        store.close()


def test_r1c_three_events_before_midnight_all_fire_at_own_times(tmp_path):
    """A denser near-midnight cluster (21.0/21.5/22.0, all within validity
    at midnight, 3 == the daily proactive cap) under FAST time: EVERY event
    fires at its own hour — the rollover parks at each in turn and never
    jumps the clock past a pending one; the run still crosses midnight."""
    store = _store(tmp_path, "r1c.db")
    try:
        _ground_agenda(store, 20.5, 21.5, item_id="s1", activity="evening one")
        _ground_agenda(store, 21.0, 22.0, item_id="s2", activity="evening two")
        _ground_agenda(store, 21.5, 22.5, item_id="s3", activity="evening three")
        store.save_schedule_events(SEED, [
            {"t_h": 21.0, "day": 0, "reason": REASON_SCHEDULE},
            {"t_h": 21.5, "day": 0, "reason": REASON_SCHEDULE},
            {"t_h": 22.0, "day": 0, "reason": REASON_SCHEDULE},
        ])
        channel = FakeChannel()

        async def blocking_sleeper(delay: float) -> None:
            await asyncio.sleep(0.3)

        _run(store, _session(store), ProactiveSchedule.restore(SEED, store),
             channel, max_hours=25.5, scale=FAST, sleeper=blocking_sleeper)

        rows = _rows(store)
        for h in (21.0, 21.5, 22.0):
            assert rows[h]["status"] == "fired", f"event at {h} did not fire"
            assert rows[h]["fired_t_h"] == pytest.approx(h)
        assert len([m for m in channel.sent if m.proactive]) == 3
        assert "expired" not in {
            e["detail"] for e in store.events_since(0)
            if e["event"] == "proactive_suppressed"
        }
    finally:
        store.close()


# R1-b: a quiet-hours event whose validity outlives the window is deferred;
# it fires at the next awake instant and the run ends at max_virtual_hours.


def test_r1b_quiet_deferral_of_parked_event_terminates_and_delivers(tmp_path):
    """A still-valid shared-interest event at 23:30 (inside quiet hours,
    validity 12h → outlives the window) is parked by the rollover and
    deferred by the firing loop. The run must terminate and the event must
    fire at the first fully-awake instant (day-1 09:00) — never consumed,
    never spuriously expired, never livelocked.

    CURRENT STATUS (findings R1-F1): the deferral sleep does not advance the
    virtual clock while the rollover is parked at the pending event, so the
    firing loop re-defers forever and the run NEVER terminates. The test
    bounds the wait so the suite fails fast instead of hanging.
    """
    store = _store(tmp_path, "r1b.db")
    try:
        store.save_schedule_events(SEED, [
            {"t_h": 23.5, "day": 0, "reason": REASON_SHARED_INTEREST},
        ])
        # ground the shared-interest event: episode tagged with a persona
        # interest (12h validity → still valid at 09:00 next day)
        from harness.bootstrap import ensure_companion_initialized
        from harness.domain import UserProfile

        ensure_companion_initialized(
            store, seed=SEED, user=UserProfile(name="u", interests=("pottery",))
        )
        # g8b: register the episode's source session
        store.open_session("day-0", 22.0)
        store.close_session("day-0", 22.7)
        store.insert_episode(EpisodicMemory(
            "ep_si", "user talked about pottery class", MemoryKind.SHARED_EPISODE,
            22.5, 22.6, 0.8, 0, None, None,
            "day-0", (1,), ("pottery class is fun",), ("pottery",),
        ))
        channel = FakeChannel()
        session = _session(store)
        schedule = ProactiveSchedule.restore(SEED, store)

        async def noop_sleeper(_delay: float) -> None:
            return None

        runtime = AsyncRuntime(
            session, schedule, channel,
            store=store, timing=TIMING, seed=SEED,
            time_scale=FAST, max_virtual_hours=34.0,
            resolver=IntentResolver(store),
            sleeper=noop_sleeper,
        )

        # bound the run: healthy execution needs < 2s wall time
        try:
            asyncio.run(asyncio.wait_for(runtime.run(), timeout=20.0))
        except asyncio.TimeoutError:
            runtime._executor.shutdown()
            runtime._registry.close()
            pytest.fail(
                "R1-F1 LIVELOCK: a still-valid event parked inside quiet "
                "hours never terminates the run — the deferral sleep does "
                "not advance the virtual clock while the rollover is parked "
                "at the pending event (violates invariants 3/17; owner A3/A6)"
            )
        rows = _rows(store)
        assert rows[23.5]["status"] == "fired", (
            f"deferred event consumed as {rows[23.5]['status']!r}"
        )
        assert rows[23.5]["fired_t_h"] == pytest.approx(QUIET_FIN_AWAKE_H), (
            "deferred event did not fire at the first fully-awake instant"
        )
        assert len([m for m in channel.sent if m.proactive]) == 1
        assert "expired" not in {
            e["detail"] for e in store.events_since(0)
            if e["event"] == "proactive_suppressed"
        }
    finally:
        store.close()


# R1-c: send exceptions and cancellation — clean termination, no leaks


class _BoomChannel(FakeChannel):
    """Channel whose send() raises for proactive messages."""

    def __init__(self):
        super().__init__()
        self.raises = 0

    async def send(self, message: OutboundMessage) -> None:
        if message.proactive:
            self.raises += 1
            raise RuntimeError("send exploded")
        self.sent.append(message)


def test_r1d_send_exception_propagates_but_terminates_cleanly(tmp_path):
    """A channel.send exception must never hang the process or leak the
    owned executor: the run raises (the exception is surfaced, not silently
    swallowed), the executor is shut down, no llh-runtime threads remain,
    and the store stays usable."""
    store = _store(tmp_path, "r1d.db")
    try:
        schedule = ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING,
                                                      store)
        h = next(float(x) for x in schedule.event_hours if x < 20.0)
        _ground_agenda(store, h - 0.5, h + 0.5)
        channel = _BoomChannel()
        with pytest.raises(RuntimeError, match="send exploded"):
            _run(store, _session(store), schedule, channel, max_hours=h + 2.0)
        assert channel.raises >= 1
        # no orphan runtime threads survive the exception path
        assert _no_llh_threads(), "orphan llh-runtime thread after send failure"
        # the store connection remains usable by its owner
        assert store.conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    finally:
        store.close()


def test_r1e_cancellation_during_sleep_shuts_down_cleanly(tmp_path):
    """Cancelling the runtime while it sleeps (mid-rollover) must unwind
    cleanly: CancelledError propagates, the owned executor shuts down, no
    llh-runtime threads remain, and the store stays usable."""
    store = _store(tmp_path, "r1e.db")
    try:
        schedule = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING,
                                                      store)
        session = _session(store)
        runtime = AsyncRuntime(
            session, schedule, FakeChannel(),
            store=store, timing=TIMING, seed=SEED,
            time_scale=SLOW, max_virtual_hours=100.0,
            resolver=IntentResolver(store),
        )

        async def record(delay: float) -> None:
            await asyncio.sleep(0.01)

        runtime.sleeper = record

        async def main() -> None:
            task = asyncio.create_task(runtime.run())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        assert not runtime._executor.is_running, "executor leaked after cancel"
        assert _no_llh_threads(), "orphan llh-runtime thread after cancel"
        assert store.conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    finally:
        store.close()
