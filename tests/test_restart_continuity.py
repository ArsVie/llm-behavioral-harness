"""What a restart must carry over: yesterday's mood (momentum)."""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.behavior import derive_behavior
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.store import SQLiteStore


def _session(path):
    return Session(
        SQLiteStore(path), persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=3,
        client=FakeClient(responses=["ok!"]), clock=VirtualClock(),
        judge=ScriptedJudge(0.5).judge_day,
    )


def _momentum(session, day):
    return derive_behavior(
        session._records[day], TimingParams(), hour=12.0,
        previous=session._records.get(day - 1),
    ).momentum


def test_momentum_survives_a_restart(tmp_path):
    lived = _session(tmp_path / "s.db")
    for day in range(3):
        lived.ensure_day(day)
    wanted = [_momentum(lived, day) for day in range(1, 3)]
    assert wanted[-1] != 0.0, "precondition: the mood moved into day 2"
    lived.store.close()
    resumed = _session(tmp_path / "s.db")
    assert _momentum(resumed, 2) == wanted[-1]
    resumed.store.close()


def test_a_stop_mid_day_leaves_the_day_for_its_midnight(tmp_path):
    """Cancelling the run (Ctrl-C, SIGTERM) must not judge the day in
    progress; reaching the horizon still closes it."""
    import asyncio

    from harness.channels.base import FakeChannel
    from harness.runtime import AsyncRuntime, TimeScale
    from harness.scheduler import ProactiveSchedule

    def _runtime(session, horizon):
        session.ensure_day(0)
        session.store.add_message("user", "hi", 1.0, 0)
        return AsyncRuntime(
            session, ProactiveSchedule.restore(3, session.store), FakeChannel(),
            store=session.store, timing=TimingParams(), seed=3,
            time_scale=TimeScale(seconds_per_virtual_hour=0.01),
            max_virtual_hours=horizon, sleeper=lambda _d: asyncio.sleep(0),
        )

    async def _stop_soon(runtime):
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    stopped = _session(tmp_path / "a.db")
    asyncio.run(_stop_soon(_runtime(stopped, None)))
    assert stopped.store.load_judgement(0) is None

    ran = _session(tmp_path / "b.db")
    asyncio.run(_runtime(ran, 2.0).run())
    assert ran.store.load_judgement(0) is not None
