"""Seeded 30/60/120-day life runs over the real persistence lane: active life never
dies, arcs end but not all complete, new arcs appear, agendas vary, CurrentActivity
is never ambiguous, and a day-60 restart reproduces the straight run exactly.
"""

from __future__ import annotations


import pytest


from harness import domain
from harness.life import (
    AWAKE_END_H,
    AWAKE_START_H,
    DAY_HOURS,
    current_activity_now,
)
from tests.helpers.life import life_persona, run_life_days

#: Fixed seeds per horizon.
HORIZONS = ((30, 12345), (60, 999), (120, 777))
#: Seed for the SQLite restart-trajectory check.
RESTART_SEED = 777
RESTART_DAYS = 120
RESTART_SPLIT = 60


class _MemoryStore:
    """Seam-faithful in-memory store (no persistence, no audit log)."""

    def __init__(self) -> None:
        self._arcs: dict[str, domain.LifeArc] = {}
        self._agendas: dict[int, dict[str, domain.AgendaItem]] = {}

    def upsert_life_arc(self, arc: domain.LifeArc) -> None:
        self._arcs[arc.id] = arc

    def get_life_arc(self, arc_id: str) -> domain.LifeArc | None:
        return self._arcs.get(arc_id)

    def list_life_arcs(self, status: str | None = None) -> list[domain.LifeArc]:
        arcs = list(self._arcs.values())
        if status is not None:
            arcs = [a for a in arcs if a.status == status]
        return arcs

    def update_life_arc_status(self, arc_id: str, status: str) -> None:
        import dataclasses

        self._arcs[arc_id] = dataclasses.replace(self._arcs[arc_id], status=status)

    def save_agenda(self, day: int, agenda: domain.DailyAgenda) -> None:
        self._agendas[day] = {it.id: it for it in agenda.items}

    def load_agenda(self, day: int) -> domain.DailyAgenda | None:
        items = self._agendas.get(day)
        if items is None:
            return None
        return domain.DailyAgenda(day=day, items=tuple(items.values()))

    def update_agenda_item_status(self, item_id: str, status: str) -> None:
        import dataclasses

        for items in self._agendas.values():
            if item_id in items:
                items[item_id] = dataclasses.replace(items[item_id], status=status)
                return
        raise KeyError(item_id)

    def list_agenda_items(
        self, day: int | None = None, status: str | None = None
    ) -> list[domain.AgendaItem]:
        items: list[domain.AgendaItem] = []
        for d, its in self._agendas.items():
            if day is not None and d != day:
                continue
            items.extend(its.values())
        if status is not None:
            items = [it for it in items if it.status == status]
        return items


def _agenda_signature(agenda: domain.DailyAgenda | None) -> tuple:
    if agenda is None:
        return ()
    return tuple(sorted((it.start_t_h, it.end_t_h, it.activity) for it in agenda.items))


# 30/60/120-day property checks

@pytest.mark.parametrize("days,seed", HORIZONS)
def test_horizon_active_life_never_dies(days, seed):
    """No day ends with zero active arcs; the horizon ends with life alive."""
    persona = life_persona()
    store = _MemoryStore()
    arcs, active_counts, _ = run_life_days(seed, persona, store, days)
    assert len(active_counts) == days
    assert 0 not in active_counts, f"seed {seed}: active life died on some day"
    assert active_counts[-1] >= 1, "horizon ended with a dead life"
    assert any(a.status == "active" for a in arcs)


@pytest.mark.parametrize("days,seed", HORIZONS)
def test_horizon_not_every_arc_remains_and_not_every_arc_completes(days, seed):
    """Some arcs end (completed/abandoned) and some do not complete."""
    persona = life_persona()
    store = _MemoryStore()
    _, _, _ = run_life_days(seed, persona, store, days)
    arcs = store.list_life_arcs()
    assert any(a.status in {"completed", "abandoned"} for a in arcs), (
        "every arc still active — nothing ever ends"
    )
    assert any(a.status != "completed" for a in arcs), (
        "every arc completed — nothing survives or is abandoned"
    )
    assert all(a.status in {"active", "completed", "abandoned"} for a in arcs)


@pytest.mark.parametrize("days,seed", HORIZONS)
def test_horizon_new_arcs_appear(days, seed):
    """Replenishment spawns replacement arcs over every horizon."""
    persona = life_persona()
    store = _MemoryStore()
    arcs, _, _ = run_life_days(seed, persona, store, days)
    spawned = [a for a in arcs if a.id.endswith("_s0")]
    assert spawned, "no replacement arc appeared"
    assert len(spawned) == len({a.id for a in spawned})
    for s in spawned:
        assert s.started_day <= days
        assert s.status in {"active", "completed", "abandoned"}
        assert s.next_intention
        assert s.interest in {i.name for i in persona.interests}


@pytest.mark.parametrize("days,seed", HORIZONS)
def test_horizon_schedules_not_identical(days, seed):
    """Agendas are not identical day over day (routines recur but item sets
    and times vary), and no day repeats its next five days."""
    persona = life_persona()
    store = _MemoryStore()
    _, _, agendas = run_life_days(seed, persona, store, days)
    sigs = {d: _agenda_signature(agendas.get(d)) for d in range(1, days + 1)}
    assert len(set(sigs.values())) >= 10, "agendas nearly identical across the horizon"
    for d in range(1, days - 4):
        window = {sigs[d + k] for k in range(1, 6)}
        assert sigs[d] not in window or len(window) > 1, (
            f"day {d} agenda identical to the next 5 days"
        )


@pytest.mark.parametrize("days,seed", HORIZONS)
def test_horizon_no_overlapping_current_activities(days, seed):
    """CurrentActivity is single-valued and in progress across a fine t_h grid inside
    the awake window."""
    persona = life_persona()
    store = _MemoryStore()
    _, _, agendas = run_life_days(seed, persona, store, days)
    resolved = 0
    for day in range(1, days + 1):
        agenda = agendas[day]
        base = day * DAY_HOURS
        t_h = base + AWAKE_START_H
        while t_h < base + AWAKE_END_H:
            current = current_activity_now(agenda, t_h)
            if current is None:
                # gap: no planned/completed item covers t_h
                assert not any(
                    it.start_t_h <= t_h < it.end_t_h
                    and it.status not in {"skipped", "shifted"}
                    for it in agenda.items
                )
            else:
                resolved += 1
                assert current.t_h == t_h  # now, not the item's start
                item = current.item
                assert item.start_t_h <= t_h < item.end_t_h  # really in progress
                assert item.status != "skipped"
                # single-valued: highest salience among items in progress
                contenders = [
                    it for it in agenda.items
                    if it.start_t_h <= t_h < it.end_t_h
                    and it.status not in {"skipped", "shifted"}
                ]
                assert current.item == max(
                    contenders, key=lambda it: (it.salience, it.start_t_h)
                )
            t_h += 0.5
    assert resolved > 0, "the sweep never found an active moment"


# restart preserves the seeded trajectory

def _arc_state(store) -> dict:
    return {a.id: (a.progress, a.status, a.next_intention, a.started_day)
            for a in store.list_life_arcs()}


def test_restart_preserves_seeded_trajectory_120(tmp_path):
    """A 120-day run split by a day-60 restart on the real SQLite lane reproduces the
    straight run exactly: same persisted state, arcs, agendas and active counts."""
    from harness.store import SQLiteStore

    persona = life_persona()

    # straight run: days 1..60 then 61..120 on the same store
    straight = SQLiteStore(tmp_path / "straight.db")
    arcs_a, counts_a, _ = run_life_days(RESTART_SEED, persona, straight, RESTART_SPLIT)
    state_60 = _arc_state(straight)
    arcs_b, counts_b, agendas_b = run_life_days(
        RESTART_SEED, persona, straight, RESTART_DAYS - RESTART_SPLIT,
        start_day=RESTART_SPLIT + 1, arcs=arcs_a,
    )
    state_final = _arc_state(straight)
    straight.close()

    # split run: days 1..60, restart, days 61..120
    split = SQLiteStore(tmp_path / "split.db")
    _, counts1, _ = run_life_days(RESTART_SEED, persona, split, RESTART_SPLIT)
    split.close()

    split2 = SQLiteStore(tmp_path / "split.db")
    reloaded = sorted(split2.list_life_arcs(), key=lambda a: a.id)
    # Normalize order like a restarted session does (id order); the
    # straight run's in-memory order is already id-ordered.
    assert _arc_state(split2) == state_60, "day-60 persisted state diverged"
    arcs2, counts2, agendas2 = run_life_days(
        RESTART_SEED, persona, split2, RESTART_DAYS - RESTART_SPLIT,
        start_day=RESTART_SPLIT + 1, arcs=reloaded,
    )
    state_split_final = _arc_state(split2)
    split2.close()

    assert state_split_final == state_final  # final state identical
    assert counts1 == counts_a and counts2 == counts_b, (
        "per-day active counts diverged after restart"
    )
    for day in range(RESTART_SPLIT + 1, RESTART_DAYS + 1):
        assert agendas2[day] == agendas_b[day], f"agenda for day {day} diverged"
    # Id lineage identical as a set; list order may differ for non-active
    # arcs (reload sorts by id, the live run keeps creation order).
    assert {a.id for a in arcs2} == {a.id for a in arcs_b}, (
        "arc id lineage diverged after restart"
    )
