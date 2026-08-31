"""Runtime/session builders shared across adversarial + runtime tests.

Consolidated from test_adversarial_runtime, test_adversarial_proactivity,
test_runtime and test_proactive_it2 — all four files carried byte-identical
module constants (PERSONA/TIMING/VARIANT/SEED/FAST) and identical
``_session``/``_ground_agenda``/``_run``/``_rows`` builders. Nothing changes
semantics: the builders are exactly the same code the four files already ran.
"""

from __future__ import annotations

import asyncio

import engine.rng as rng_mod
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.judge import ScriptedJudge
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.session import Session

# make_session lives in tests.helpers.store (the canonical superset); re-export
# it here so the runtime module remains the single import surface for the
# adversarial/runtime test family.
from tests.helpers.store import make_session  # noqa: F401

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

FAST = TimeScale(seconds_per_virtual_hour=0.002)


def ground_agenda(store, start_t_h, end_t_h, *, item_id="g1", salience=0.8,
                  activity="pottery class"):
    item = AgendaItem(item_id, start_t_h, end_t_h, activity, "arc", "arc1",
                      salience, "planned")
    store.save_agenda(0, DailyAgenda(0, (item,)))
    return item


def run(store, session, schedule, channel, *, max_hours, scale=FAST,
        resolver=None, sleeper=None, seed=SEED, clock_start_h=None):
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    if clock_start_h is not None and session.clock.now_h() < clock_start_h:
        session.clock.advance_hours(clock_start_h - session.clock.now_h())
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


def rows(store, seed=SEED):
    return {abs(float(r["t_h"])): r for r in store.schedule_events_for_seed(seed)}
