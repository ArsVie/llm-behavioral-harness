"""The AFK bomb has to actually fire.

Live, 2026-09-08: she was told at 21:42 that ``read history`` started at
21:50, the AFK bomb armed for 21:52, and he stopped replying at 21:48. The
bomb never fired. The next wake of any kind was the 23:00 quiet-hours
conversation close, by which time the window had shut, so the backstop
force-skipped the item with a server-drawn reason. She was informed of an
event and then never asked about it.

The cause: the rollover loop surveys its park instants ONCE and then sleeps
the whole interval. The heads-up armed the 21:52 deadline AFTER the loop had
already committed to sleeping to 23:00, and nothing re-surveyed. In anchor
mode — paced against real wall-clock time — that means the bomb can
essentially never fire during a conversation, because every deadline it arms
is created mid-sleep. ``next_negotiation_trigger_t_h`` computed the right
instant all along; nobody was listening.

So a turn now announces that the wake set may have changed
(``request_retarget``), which cuts the park short; the loop re-surveys and
parks at whichever instant is earliest.

DELIBERATELY NOT CHANGED: the window-close backstop still wins when the
runtime arrives after ``end_t_h`` (``test_the_backstop_still_wins_...``).
Running the missed leg retroactively would give her the decision she was
owed, but it reverses the documented G0 backstop and the eight tests that
pin it, and it would fire a model call for every window an offline stretch
skipped. That is a contract decision, not a bug fix.
"""

from __future__ import annotations

import asyncio

import pytest

import engine.rng as rng_mod
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.negotiation_contract import NegotiationPhase
from harness.negotiation_state import (
    NegotiationState,
    decide_status_at,
    next_trigger_t_h,
)
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from tests.helpers import AnchorManualClock, SeamStore, make_session

SEED = 8001
T0 = 1_000_000.0


def _state(**kw) -> NegotiationState:
    """The live negotiation: window 21.84-22.59, informed at 21.715, bomb
    armed for 21.88."""
    base = {
        "item_id": "ag1", "activity": "read history", "source_type": "routine",
        "start_t_h": 21.84, "end_t_h": 22.59, "salience": 0.81,
        "phase": NegotiationPhase.DECIDE.value, "informed": True,
        "turns_to_decide": 0, "afk_deadline_t_h": 21.88,
        "last_decide_at_t_h": 21.715,
    }
    base.update(kw)
    return NegotiationState(**base)


# --- the deadline the live run never reached ---


def test_the_bomb_is_due_the_moment_its_deadline_arrives():
    """Nothing was ever wrong with the machine — it was never asked."""
    st = _state()
    assert decide_status_at(st, now=21.88, companion_turn=False) == "due"


def test_the_loop_had_two_park_instants_it_never_used():
    """From the heads-up the next park is the window OPENING, and from
    there it is the bomb. The live loop was asleep to 23:00 and used
    neither."""
    st = _state()
    assert next_trigger_t_h(st, 21.715) == pytest.approx(21.84)
    assert next_trigger_t_h(st, 21.84) == pytest.approx(21.88)


def test_nothing_is_due_before_the_window_opens():
    """The turn at 21:48 came before the 21:50 window and correctly did not
    decide — which is why the 21:52 wake was the only remaining chance."""
    st = _state()
    assert decide_status_at(st, now=21.80, companion_turn=True) == "waiting"


def test_the_backstop_still_wins_after_the_window_closes():
    """Pinned on purpose: see the module docstring. A late wake forces the
    skip; it does not retroactively run the missed leg."""
    st = _state()
    assert decide_status_at(st, now=23.0, companion_turn=False) == "forced"


# --- the park sleep has to notice a newly armed deadline ---


def _armed_runtime(max_hours=24.0, t_h=21.7, arm_after=None):
    """An anchor-mode runtime on a fake wall clock. ``arm_after`` requests a
    retarget once that many sleep slices have elapsed — which is what a turn
    does in production: it lands between two slices of the park."""
    store = SeamStore()
    clock = VirtualClock(t_h=t_h)
    session = make_session(
        store, clock=clock, client=FakeClient(responses=["ok!"]),
        persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=SEED,
    )
    manual = AnchorManualClock(T0)
    slices: list[float] = []
    holder: dict[str, AsyncRuntime] = {}

    async def sleeper(delay: float) -> None:
        slices.append(delay)
        await manual.sleep(delay)
        if arm_after is not None and len(slices) == arm_after:
            holder["runtime"].request_retarget()

    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), FakeChannel(),
        store=store, timing=TimingParams(), seed=SEED,
        time_scale=TimeScale(), max_virtual_hours=max_hours,
        resolver=IntentResolver(store, rng=rng_mod.stream_rng(SEED)),
        sleeper=sleeper,
        anchor=RealTimeAnchor(epoch0_s=T0, t_h0=t_h, tz="UTC"),
        now=manual,
    )
    holder["runtime"] = runtime
    runtime._test_slices = slices
    return store, session, runtime


def test_the_park_sleep_returns_early_when_a_nearer_wake_is_armed():
    """The whole defect in one assertion: something armed mid-park must be
    able to cut it short, or the loop cannot re-survey in time."""
    _store, _session, runtime = _armed_runtime(arm_after=2)

    async def scenario():
        # A five-hour park; the retarget lands two slices in.
        return await runtime._sleep_until_t_h(
            21.7 + 5.0, 21.7, interruptible=True
        )

    assert asyncio.run(scenario()) is False


def test_the_park_is_sliced_so_a_retarget_is_noticed_within_a_slice():
    """A long park must not be one uninterruptible sleep — that is exactly
    how the live 23:00 wake happened."""
    from harness.runtime import RETARGET_SLICE_S

    _store, _session, runtime = _armed_runtime()

    async def scenario():
        return await runtime._sleep_until_t_h(
            21.7 + 5.0, 21.7, interruptible=True
        )

    assert asyncio.run(scenario()) is True
    assert max(runtime._test_slices) <= RETARGET_SLICE_S
    assert len(runtime._test_slices) == 5 * 3600 / RETARGET_SLICE_S


def test_an_uninterruptible_sleep_is_still_one_request():
    """The firing loop's sleeps are unsliced: one request for the whole
    interval, so a scheduled event is gated at its own instant."""
    _store, _session, runtime = _armed_runtime()

    async def scenario():
        return await runtime._sleep_until_t_h(21.7 + 5.0, 21.7)

    assert asyncio.run(scenario()) is True
    assert runtime._test_slices == [5 * 3600.0]


def test_an_uninterrupted_park_sleep_still_reaches_its_target():
    _store, _session, runtime = _armed_runtime()

    async def scenario():
        return await runtime._sleep_until_t_h(
            21.7 + 1.0, 21.7, interruptible=True
        )

    assert asyncio.run(scenario()) is True


def test_a_pending_request_is_consumed_by_exactly_one_park():
    """Otherwise the flag would spin the loop instead of parking it."""
    _store, _session, runtime = _armed_runtime()
    runtime.request_retarget()

    async def scenario():
        first = await runtime._sleep_until_t_h(21.7 + 1.0, 21.7,
                                               interruptible=True)
        second = await runtime._sleep_until_t_h(21.7 + 1.0, 21.7,
                                                interruptible=True)
        return first, second

    assert asyncio.run(scenario()) == (False, True)


def test_the_firing_loops_sleeps_are_never_interruptible():
    """Only the rollover PARK re-surveys. A firing-loop sleep that returned
    early would gate a scheduled event at the wrong instant."""
    _store, _session, runtime = _armed_runtime()
    runtime.request_retarget()

    async def scenario():
        return await runtime._sleep_until_t_h(21.7 + 1.0, 21.7)

    assert asyncio.run(scenario()) is True


def test_a_turn_requests_a_retarget():
    """The reactive path must announce it: a heads-up arms its deadline
    inside the turn, while the loop is already asleep."""
    _store, _session, runtime = _armed_runtime()
    assert not runtime._retarget.is_set()
    runtime.request_retarget()
    assert runtime._retarget.is_set()
