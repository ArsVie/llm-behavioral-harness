"""Runtime builders shared by the runtime/adversarial test family.

``run_runtime`` is test_runtime's bounded AsyncRuntime driver (test_proactive_it2
uses it at its own default scale). ``ground_agenda`` and ``schedule_rows`` are
test_adversarial_runtime/_proactivity's positional agenda seed (source id
``arc1``) and schedule-row map. test_adversarial_runtime/_proactivity keep their
own ``_run``: theirs builds the IntentResolver without a seeded rng.
"""

from __future__ import annotations

import asyncio

import engine.rng as rng_mod
from engine.types import TimingParams
from harness.domain import AgendaItem, DailyAgenda
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale

TIMING = TimingParams()
SEED = 12345

FAST = TimeScale(seconds_per_virtual_hour=0.002)


def ground_agenda(store, start_t_h, end_t_h, *, item_id="g1", salience=0.8,
                  activity="pottery class"):
    item = AgendaItem(item_id, start_t_h, end_t_h, activity, "arc", "arc1",
                      salience, "planned")
    # Adds to the day: a second call must not wipe the first item.
    existing = store.load_agenda(0)
    kept = tuple(i for i in (existing.items if existing else ()) if i.id != item.id)
    store.save_agenda(0, DailyAgenda(0, kept + (item,)))
    return item


def run_runtime(store, session, schedule, channel, *, max_hours, scale=FAST,
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


def schedule_rows(store, seed=SEED):
    return {abs(float(r["t_h"])): r for r in store.schedule_events_for_seed(seed)}
