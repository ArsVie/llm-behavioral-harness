"""S1 clock-advance + sent_at (WS-A, D4): anchored inbound messages advance
the virtual clock to the REAL arrival instant and ``messages.sent_at`` is
stamped from real arrival; the unanchored path is byte-identical (no
advance, sent_at NULL — the documented frozen-clock fallback).

The anchored path: ``AsyncRuntime._on_inbound`` advances the session clock
to ``anchor.t_h_at(msg.received_at)`` (never backwards); the store then
resolves ``sent_at = real_at(t_h)`` — the anchor's exact inverse — to the
true arrival epoch. Two trial messages arriving at different wall times get
DIFFERENT sent_at values (previously the frozen clock shared one timestamp).

No run() is invoked: the tests drive ``_on_inbound`` directly (the
registered channel handler) with a ManualWallClock + no-op sleeper, exactly
the test_runtime_anchor pattern.
"""

import asyncio
from pathlib import Path

import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor
from harness.channels.base import FakeChannel, InboundMessage
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

#: Fixed anchor: wall epoch T0 maps to virtual hour 8.0 (t_h0), tz UTC — the
#: t_h math in these tests is exact.
T0 = 1_000_000.0
ANCHOR = RealTimeAnchor(epoch0_s=T0, t_h0=8.0, tz="UTC")

HOUR = 3600.0


class ManualWallClock:
    """Injectable wall clock (anchor mode): a fake ``time.time()`` the test
    advances explicitly."""

    def __init__(self, t0: float = T0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class NoopSleeper:
    """Sleeper that records the requested delay and returns immediately —
    the runtime never waits real seconds in tests."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _runtime(tmp_path: Path, *, anchored: bool):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "s.db")
    if anchored:
        store.attach_anchor(ANCHOR)
    clock = VirtualClock(0.0)
    client = FakeClient(responses=["ok!"])
    judge = ScriptedJudge(score=0.5)
    session = Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=client, clock=clock, judge=judge.judge_day,
    )
    channel = FakeChannel()
    sleeper = NoopSleeper()
    wall = ManualWallClock()
    rt = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=TimeScale(0.02), max_virtual_hours=24.0,
        sleeper=sleeper, now=wall,
        anchor=ANCHOR if anchored else None,
    )
    return store, clock, session, channel, rt, sleeper, wall


def _user_rows(store) -> list[dict]:
    return [m for m in store.recent_messages(50) if m["role"] == "user"]


def _feed(rt, wall, *received_ats: float) -> None:
    """Deliver one inbound message per real arrival instant, in order."""

    async def scenario() -> None:
        for at in received_ats:
            await rt._on_inbound(  # noqa: SLF001 - direct handler drive
                InboundMessage(text="hi", sender_id="u", received_at=at)
            )
        rt._executor.shutdown()

    asyncio.run(scenario())


def test_anchored_sent_at_is_real_arrival_and_clock_advances(tmp_path) -> None:
    """Two messages at different wall times: sent_at = real arrival for each
    and the virtual clock advances mid-conversation (no more shared
    timestamp)."""
    store, clock, session, channel, rt, sleeper, wall = _runtime(
        tmp_path, anchored=True
    )
    _feed(rt, wall, wall.t, wall.t + 3 * HOUR)
    rows = _user_rows(store)
    assert len(rows) == 2
    # sent_at stamped from REAL arrival (anchor exact inverse), distinct
    assert rows[0]["sent_at"] == pytest.approx(T0, abs=1e-6)
    assert rows[1]["sent_at"] == pytest.approx(T0 + 3 * HOUR, abs=1e-6)
    assert rows[1]["sent_at"] != rows[0]["sent_at"]
    # the virtual clock advanced mid-conversation: 0.0 -> 8.0 -> 11.0
    assert clock.now_h() == pytest.approx(ANCHOR.t_h_at(T0 + 3 * HOUR), abs=1e-9)
    # and the rows' virtual hours track the arrivals too
    assert rows[0]["t_h"] == pytest.approx(8.0, abs=1e-9)
    assert rows[1]["t_h"] == pytest.approx(11.0, abs=1e-9)
    store.close()


def test_anchored_never_rewinds_the_clock(tmp_path) -> None:
    """A late-delivered message whose real arrival maps to an EARLIER t_h
    than the current clock position does NOT rewind; sent_at still resolves
    from the clock position (the arrival is already represented)."""
    store, clock, session, channel, rt, sleeper, wall = _runtime(
        tmp_path, anchored=True
    )
    _feed(rt, wall, T0, T0 - 2 * HOUR)  # second arrival maps to t_h 6.0
    rows = _user_rows(store)
    assert len(rows) == 2
    assert clock.now_h() == pytest.approx(8.0, abs=1e-9)  # never below 8.0
    assert rows[0]["t_h"] == pytest.approx(8.0, abs=1e-9)
    assert rows[1]["t_h"] == pytest.approx(8.0, abs=1e-9)
    store.close()


def test_unanchored_inbound_frozen_clock_and_null_sent_at(tmp_path) -> None:
    """No anchor: received_at is ignored, the clock stays frozen (the
    documented fallback) and sent_at stays NULL — byte-identical replay."""
    store, clock, session, channel, rt, sleeper, wall = _runtime(
        tmp_path, anchored=False
    )
    _feed(rt, wall, 1_234_567.0, 1_234_567.0 + 2 * HOUR)
    rows = _user_rows(store)
    assert len(rows) == 2
    assert all(r["sent_at"] is None for r in rows)
    assert clock.now_h() == 0.0  # no advance: received_at is ignored
    assert rows[0]["t_h"] == rows[1]["t_h"] == 0.0  # frozen clock
    store.close()


def test_anchored_clock_advance_is_deterministic(tmp_path) -> None:
    """Two identical anchored runs produce byte-identical message rows
    (t_h + sent_at) — seeded determinism holds on the anchored path."""
    schedule = (T0, T0 + HOUR, T0 + 4 * HOUR)

    def run_once(path: Path) -> list[tuple]:
        store, clock, session, channel, rt, sleeper, wall = _runtime(
            path, anchored=True
        )
        _feed(rt, wall, *schedule)
        rows = [
            (float(r["t_h"]), float(r["sent_at"]))
            for r in _user_rows(store)
        ]
        store.close()
        return rows

    assert run_once(tmp_path / "a") == run_once(tmp_path / "b")