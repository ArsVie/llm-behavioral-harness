"""Life-lane day loop (consolidated from test_life/_run_days and
test_life_long_horizon/_run_days).

The two original helpers had different signatures and return shapes:

- test_life._run_days(seed, persona, store, days) -> (arcs, active_counts)
- test_life_long_horizon._run_days(seed, persona, store, days, *,
  start_day=1, arcs=None) -> (arcs, active_counts, agendas)

``run_life_days`` is the superset (long-horizon signature + agendas dict);
the two test_life call sites only unpack two values, so the third return
value is harmless there.
"""

from __future__ import annotations

from harness.life import LIFE_STREAM, generate_agenda, init_life, step_life
from engine.rng import stream_rng


def run_life_days(
    seed: int, persona, store, days: int,
    start_day: int = 1, arcs=None,
):
    """Session-faithful loop (fresh per-day rng per call): returns the final
    arc list, post-step active counts per day, and the day agendas."""
    if arcs is None:
        arcs = init_life(seed, persona, store)
    active_counts: list[int] = []
    agendas: dict[int, object] = {}
    for day in range(start_day, start_day + days):
        agenda = generate_agenda(day, persona, arcs, store,
                                 stream_rng(seed, LIFE_STREAM, day))
        result = step_life(day, persona, arcs, agenda, store,
                           stream_rng(seed, LIFE_STREAM, day))
        arcs = result.updated_arcs
        agendas[day] = result.agenda
        active_counts.append(sum(1 for a in arcs if a.status == "active"))
    return arcs, active_counts, agendas
