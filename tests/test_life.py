"""Tests for harness/life.py — A4 persistent life simulation (vertical-slice Wave 1).

Covers: deterministic arc seeding tied to the persona's real interests and
persisted through the store seam; daily agenda generation predominantly from
persona sources inside the awake window; step_life arc progress/status and
item-status deviation with persistence; the 30-day core simulation (arcs
survive day boundaries, progress happens, activities recur, days differ,
interests dominate, statuses stay valid); restart/reload reproducing the same
persistent state (including a deterministic continuation); and the CRITICAL
RNG rule — life.py never draws from day_rng(seed, t), only from the reserved
LIFE stream stream_rng(seed, 4[, day]).

A2's real SQLite store seam (wip/vslice-a2) had not landed when A4 ran, so the
module tests use ``FakeLifeStore``: an in-memory store implementing exactly the
§15 store subset life.py uses, persisted to a JSON file on every write so a
reopened instance reproduces the same state (the behaviour A2's store will
provide). Reported to the orchestrator.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from engine.rng import day_rng, stream_rng

from harness import domain
from harness.life import (
    LIFE_STREAM,
    AWAKE_END_H,
    AWAKE_START_H,
    DAY_HOURS,
    generate_agenda,
    init_life,
    step_life,
)

#: Fixed seed for the 30-day core simulation (deterministic, documented).
#: Chosen so the trajectory exhibits every arc transition — completion,
#: abandonment and surviving active arcs — plus day-to-day variety.
CORE_SEED = 14


# ---------------------------------------------------------------------------
# Seam-faithful fake store (JSON-persisted so reopen reproduces state)
# ---------------------------------------------------------------------------

class FakeLifeStore:
    """§15 store-seam subset for life.py, flushed to a JSON file per write.

    ``list_life_arcs`` / ``load_agenda`` on a reopened instance (same file)
    reproduce the same persistent state — the behaviour A2's SQLiteStore will
    provide. No business logic lives here (seam rule).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._arcs: dict[str, domain.LifeArc] = {}
        self._agendas: dict[int, dict[str, domain.AgendaItem]] = {}
        if self.path.exists():
            self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        data = json.loads(self.path.read_text())
        for raw in data.get("arcs", []):
            arc = domain.LifeArc(**raw)
            self._arcs[arc.id] = arc
        for day_str, items in data.get("agendas", {}).items():
            self._agendas[int(day_str)] = {
                it["id"]: domain.AgendaItem(**it) for it in items
            }

    def _flush(self) -> None:
        payload = {
            "arcs": [dataclasses.asdict(a) for a in self._arcs.values()],
            "agendas": {
                str(day): [dataclasses.asdict(it) for it in items.values()]
                for day, items in self._agendas.items()
            },
        }
        self.path.write_text(json.dumps(payload, sort_keys=True))

    # -- arc ops (seam) ----------------------------------------------------
    def upsert_life_arc(self, arc: domain.LifeArc) -> None:
        self._arcs[arc.id] = arc
        self._flush()

    def get_life_arc(self, arc_id: str) -> domain.LifeArc | None:
        return self._arcs.get(arc_id)

    def list_life_arcs(self, status: str | None = None) -> list[domain.LifeArc]:
        arcs = list(self._arcs.values())
        if status is not None:
            arcs = [a for a in arcs if a.status == status]
        return arcs

    def update_life_arc_status(self, arc_id: str, status: str) -> None:
        self._arcs[arc_id] = dataclasses.replace(self._arcs[arc_id], status=status)
        self._flush()

    # -- agenda ops (seam) -------------------------------------------------
    def save_agenda(self, day: int, agenda: domain.DailyAgenda) -> None:
        self._agendas[day] = {it.id: it for it in agenda.items}
        self._flush()

    def load_agenda(self, day: int) -> domain.DailyAgenda | None:
        items = self._agendas.get(day)
        if items is None:
            return None
        return domain.DailyAgenda(day=day, items=tuple(items.values()))

    def update_agenda_item_status(self, item_id: str, status: str) -> None:
        for items in self._agendas.values():
            if item_id in items:
                items[item_id] = dataclasses.replace(items[item_id], status=status)
                self._flush()
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

    def close(self) -> None:
        self._flush()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _persona() -> domain.PersonaProfile:
    interests = (
        domain.Interest(name="photography", bucket="exact", salience=0.9),
        domain.Interest(name="pottery", bucket="exact", salience=0.7),
        domain.Interest(name="novels", bucket="adjacent", salience=0.6),
        domain.Interest(name="running", bucket="adjacent", salience=0.5),
        domain.Interest(name="plants", bucket="independent", salience=0.4),
        domain.Interest(name="cooking", bucket="independent", salience=0.55),
    )
    routines = (
        domain.Routine(name="morning coffee", start_frac=8.5 / 24.0, duration_h=0.5,
                       cadence=1.0, salience=0.3),
        domain.Routine(name="evening walk", start_frac=20.0 / 24.0, duration_h=0.75,
                       cadence=0.6, salience=0.4),
    )
    return domain.PersonaProfile(
        name="Luna",
        core="A curious soul who collects hobbies.",
        interests=interests,
        routines=routines,
    )


def _store(tmp_path: Path) -> FakeLifeStore:
    return FakeLifeStore(tmp_path / "life_store.json")


def _simulate(seed: int, persona: domain.PersonaProfile, store: FakeLifeStore,
              days: int = 30, start_day: int = 1):
    """Run init_life + days of generate_agenda/step_life; return live state."""
    arcs = init_life(seed, persona, store, start_day=start_day)
    agendas: dict[int, domain.DailyAgenda] = {}
    results: dict[int, object] = {}
    for day in range(start_day, start_day + days):
        rng = stream_rng(seed, LIFE_STREAM, day)
        agenda = generate_agenda(day, persona, arcs, store, rng)
        result = step_life(day, persona, arcs, agenda, store, rng)
        arcs = result.updated_arcs
        agendas[day] = result.agenda
        results[day] = result
    return arcs, agendas, results


def _day_start(day: int) -> float:
    return day * DAY_HOURS


# ---------------------------------------------------------------------------
# init_life
# ---------------------------------------------------------------------------

def test_init_life_deterministic_and_persisted(tmp_path):
    persona = _persona()
    store1 = _store(tmp_path)
    arcs1 = init_life(CORE_SEED, persona, store1)
    store2 = _store(tmp_path)
    arcs2 = init_life(CORE_SEED, persona, store2)

    assert arcs1 == arcs2  # deterministic per seed
    assert 2 <= len(arcs1) <= 4
    assert [a.id for a in arcs1] == [f"arc_{i}" for i in range(1, len(arcs1) + 1)]
    interest_names = {i.name for i in persona.interests}
    for arc in arcs1:
        assert arc.status == "active"
        assert arc.interest in interest_names  # every arc tied to a real interest
        assert arc.next_intention
        assert 0.0 <= arc.progress <= 1.0
        assert arc.started_day >= 1
    # persisted through the seam, readable back
    assert store1.list_life_arcs() == arcs1
    assert store1.get_life_arc(arcs1[0].id) == arcs1[0]
    assert store1.list_life_arcs(status="active") == arcs1


def test_init_life_variation_across_seeds(tmp_path):
    persona = _persona()
    signatures = set()
    for seed in (1, 2, 3):
        arcs = init_life(seed, persona, _store(tmp_path))
        signatures.add(tuple((a.id, a.name, a.interest, a.next_intention) for a in arcs))
    assert len(signatures) >= 2  # different seeds create variation


# ---------------------------------------------------------------------------
# generate_agenda
# ---------------------------------------------------------------------------

def test_generate_agenda_shape_sources_and_persistence(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    day = 5
    rng = stream_rng(CORE_SEED, LIFE_STREAM, day)
    agenda = generate_agenda(day, persona, arcs, store, rng)

    assert agenda.day == day
    assert agenda.items, "agenda must never be empty"
    assert len({it.id for it in agenda.items}) == len(agenda.items)  # unique ids
    assert all(it.status == "planned" for it in agenda.items)
    assert all(it.source_type in {"arc", "interest", "routine"} for it in agenda.items)

    arc_by_id = {a.id: a for a in arcs}
    interest_names = {i.name for i in persona.interests}
    routine_names = {r.name for r in persona.routines}
    for item in agenda.items:
        local_start = item.start_t_h - _day_start(day)
        local_end = item.end_t_h - _day_start(day)
        assert AWAKE_START_H <= local_start < AWAKE_END_H
        assert local_end <= AWAKE_END_H
        assert item.end_t_h > item.start_t_h
        if item.source_type == "interest":
            assert item.source_id in interest_names
        elif item.source_type == "arc":
            assert item.source_id in arc_by_id
            assert arc_by_id[item.source_id].interest in interest_names
        else:
            assert item.source_id in routine_names
    # items sorted by start
    starts = [it.start_t_h for it in agenda.items]
    assert starts == sorted(starts)

    # persisted via the seam
    assert store.load_agenda(day) == agenda


def test_generate_agenda_deterministic_per_seed_day(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    rng1 = stream_rng(CORE_SEED, LIFE_STREAM, 7)
    rng2 = stream_rng(CORE_SEED, LIFE_STREAM, 7)
    assert generate_agenda(7, persona, arcs, store, rng1) == generate_agenda(
        7, persona, arcs, store, rng2
    )


def test_agenda_variation_across_days(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    signatures = set()
    for day in range(1, 11):
        agenda = generate_agenda(day, persona, arcs, store,
                                 stream_rng(CORE_SEED, LIFE_STREAM, day))
        signatures.add(tuple((it.source_type, it.source_id, it.start_t_h) for it in agenda.items))
    assert len(signatures) >= 3  # stochastic variation: not every day identical


def test_routine_cadence_boundaries(tmp_path):
    persona = dataclasses.replace(
        _persona(),
        routines=(
            domain.Routine(name="always", start_frac=0.35, duration_h=0.5,
                           cadence=1.0, salience=0.2),
            domain.Routine(name="never", start_frac=0.5, duration_h=0.5,
                           cadence=0.0, salience=0.2),
        ),
    )
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    for day in range(1, 8):
        agenda = generate_agenda(day, persona, arcs, store,
                                 stream_rng(CORE_SEED, LIFE_STREAM, day))
        names = {it.source_id for it in agenda.items if it.source_type == "routine"}
        assert "always" in names
        assert "never" not in names


# ---------------------------------------------------------------------------
# step_life
# ---------------------------------------------------------------------------

def test_step_life_statuses_and_persistence(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    day = 5
    agenda = generate_agenda(day, persona, arcs, store,
                             stream_rng(CORE_SEED, LIFE_STREAM, day))
    result = step_life(day, persona, arcs, agenda, store,
                       stream_rng(CORE_SEED, LIFE_STREAM, day))

    assert {it.status for it in result.agenda.items} <= {
        "planned", "completed", "skipped", "shifted",
    }
    assert any(it.status == "completed" for it in result.agenda.items)
    # persisted per item through the seam
    stored = {it.id: it.status for it in store.list_agenda_items(day=day)}
    assert stored == {it.id: it.status for it in result.agenda.items}
    assert store.load_agenda(day) == result.agenda


def test_step_life_arc_progress_and_persistence(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    day = 6
    agenda = generate_agenda(day, persona, arcs, store,
                             stream_rng(CORE_SEED, LIFE_STREAM, day))
    result = step_life(day, persona, arcs, agenda, store,
                       stream_rng(CORE_SEED, LIFE_STREAM, day))

    by_id = {a.id: a for a in arcs}
    for arc in result.updated_arcs:
        assert arc.progress >= by_id[arc.id].progress  # monotone
    assert sum(a.progress for a in result.updated_arcs) > sum(
        a.progress for a in arcs
    ), "some progress must happen on a day with an active arc"
    # persisted through the seam
    assert store.list_life_arcs() == result.updated_arcs


def test_step_life_current_activity_main(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    day = 8
    agenda = generate_agenda(day, persona, arcs, store,
                             stream_rng(CORE_SEED, LIFE_STREAM, day))
    result = step_life(day, persona, arcs, agenda, store,
                       stream_rng(CORE_SEED, LIFE_STREAM, day))

    activity = result.current_activity
    assert activity is not None
    completed = [it for it in result.agenda.items if it.status == "completed"]
    candidates = completed or list(result.agenda.items)
    expected = max(candidates, key=lambda it: (it.salience, it.start_t_h))
    assert activity.item == expected
    assert activity.description == expected.activity
    assert activity.t_h == expected.start_t_h


def test_step_life_arcs_can_complete_or_abandon(tmp_path):
    persona = _persona()
    store = _store(tmp_path)
    _, _, results = _simulate(CORE_SEED, persona, store, days=30)
    arcs = store.list_life_arcs()
    assert any(a.status in {"completed", "abandoned"} for a in arcs), (
        "over 30 days at least one arc must complete or be abandoned"
    )
    assert all(a.status in {"active", "completed", "abandoned"} for a in arcs)
    assert any(a.status == "completed" for a in arcs)  # the oldest arc completes


# ---------------------------------------------------------------------------
# Core 30-day simulation
# ---------------------------------------------------------------------------

def test_30_day_simulation_core(tmp_path):
    """Core test: 30 days, fixed seed, real persistence semantics.

    Active arcs survive day boundaries, progress happens, activities recur,
    days differ, activities come predominantly from the persona's actual
    interests, and item statuses stay valid with modest deviation.
    """
    persona = _persona()
    store = _store(tmp_path)
    arcs, agendas, _ = _simulate(CORE_SEED, persona, store, days=30)

    assert len(agendas) == 30
    interest_names = {i.name for i in persona.interests}
    routine_names = {r.name for r in persona.routines}
    arc_by_id = {a.id: a for a in arcs}

    # 1. active arcs survive day boundaries (persisted in the store)
    assert store.list_life_arcs(status="active"), "at least one arc stays active"
    assert len(store.list_life_arcs()) == len(arcs)

    # 2. some progress happens (monotone overall, strictly on some day)
    day1_total = sum(a.progress for a in init_life(CORE_SEED, persona, _store(tmp_path)))
    day30_total = sum(a.progress for a in arcs)
    assert day30_total > day1_total
    assert any(a.progress > 0.0 for a in arcs)

    # 3. some activities recur across days
    activity_counts: dict[str, int] = {}
    for agenda in agendas.values():
        for item in agenda.items:
            activity_counts[item.activity] = activity_counts.get(item.activity, 0) + 1
    assert any(count >= 2 for count in activity_counts.values())

    # 4. not every day identical
    signatures = {
        tuple((it.source_type, it.source_id, it.start_t_h) for it in agenda.items)
        for agenda in agendas.values()
    }
    assert len(signatures) >= 5

    # 5. predominantly from the persona's actual interests
    interest_traced = 0
    total_items = 0
    for agenda in agendas.values():
        for item in agenda.items:
            total_items += 1
            if item.source_type == "interest":
                assert item.source_id in interest_names
                interest_traced += 1
            elif item.source_type == "arc":
                assert item.source_id in arc_by_id
                assert arc_by_id[item.source_id].interest in interest_names
                interest_traced += 1
            else:
                assert item.source_id in routine_names
    assert total_items > 0
    assert interest_traced / total_items >= 0.5, (
        f"interest-traced share {interest_traced / total_items:.2f} below 0.5"
    )

    # 6. statuses valid + modest deviation; some completed, some skipped/shifted
    all_statuses = {
        it.status for agenda in agendas.values() for it in agenda.items
    }
    assert all_statuses <= {"planned", "completed", "skipped", "shifted"}
    assert "completed" in all_statuses
    assert all_statuses & {"skipped", "shifted"}, "a person deviates from plans"

    # 7. persisted statuses match the in-memory trajectory
    for day in (1, 15, 30):
        stored = {it.id: it.status for it in store.list_agenda_items(day=day)}
        assert stored == {it.id: it.status for it in agendas[day].items}

    # 8. agenda items always inside the awake window
    for day, agenda in agendas.items():
        for item in agenda.items:
            assert AWAKE_START_H <= item.start_t_h - _day_start(day) < AWAKE_END_H
            assert item.end_t_h - _day_start(day) <= AWAKE_END_H
            assert item.end_t_h > item.start_t_h


def test_reload_reproduces_persistent_state(tmp_path):
    """Restart/reload: close store, reopen, reload → identical state, and the
    continuation (day 31) matches a from-scratch run of the same seed."""
    persona = _persona()
    path = tmp_path / "life_store.json"

    store1 = FakeLifeStore(path)
    arcs, agendas, results = _simulate(CORE_SEED, persona, store1, days=30)
    store1.close()

    # --- restart: new store instance over the same file ---
    store2 = FakeLifeStore(path)
    reloaded_arcs = store2.list_life_arcs()
    assert reloaded_arcs == arcs  # same persistent state
    assert store2.load_agenda(30) == agendas[30]
    for day in (1, 15, 30):
        stored_items = {it.id: it for it in store2.list_agenda_items(day=day)}
        assert stored_items == {it.id: it for it in agendas[day].items}

    # --- continuation determinism: day 31 from reloaded state == fresh run ---
    rng_reloaded = stream_rng(CORE_SEED, LIFE_STREAM, 31)
    agenda31_reloaded = generate_agenda(31, persona, reloaded_arcs, store2, rng_reloaded)
    step31_reloaded = step_life(31, persona, reloaded_arcs, agenda31_reloaded, store2,
                                stream_rng(CORE_SEED, LIFE_STREAM, 31))

    store3 = FakeLifeStore(tmp_path / "fresh.json")
    fresh_arcs, _, _ = _simulate(CORE_SEED, persona, store3, days=30)
    agenda31_fresh = generate_agenda(31, persona, fresh_arcs, store3,
                                     stream_rng(CORE_SEED, LIFE_STREAM, 31))
    step31_fresh = step_life(31, persona, fresh_arcs, agenda31_fresh, store3,
                             stream_rng(CORE_SEED, LIFE_STREAM, 31))

    assert agenda31_reloaded == agenda31_fresh
    assert step31_reloaded.updated_arcs == step31_fresh.updated_arcs
    assert step31_reloaded.agenda == step31_fresh.agenda
    assert step31_reloaded.current_activity == step31_fresh.current_activity


# ---------------------------------------------------------------------------
# RNG stream isolation (CRITICAL replay rule)
# ---------------------------------------------------------------------------

def test_life_stream_does_not_touch_daily_stream(tmp_path):
    """life.py must never draw from day_rng(seed, t); the daily stream is the
    Session's own generator and any extra draw would desync replay."""
    seed = 424242
    persona = _persona()
    store = _store(tmp_path)

    expected = [float(x) for x in day_rng(seed, 3).random(7)]
    arcs = init_life(seed, persona, store)
    agenda = generate_agenda(3, persona, arcs, store,
                             stream_rng(seed, LIFE_STREAM, 3))
    step_life(3, persona, arcs, agenda, store, stream_rng(seed, LIFE_STREAM, 3))
    actual = [float(x) for x in day_rng(seed, 3).random(7)]

    assert actual == expected  # daily stream untouched by life ops
    # and life's own stream is reproducible
    assert stream_rng(seed, LIFE_STREAM, 3).random(5).tolist() == \
        stream_rng(seed, LIFE_STREAM, 3).random(5).tolist()


def test_module_uses_no_random_or_clock():
    """No `random` module, no real clocks, no unseeded numpy rng in life.py."""
    src = Path(__file__).resolve().parent.parent / "harness" / "life.py"
    text = src.read_text()
    for forbidden in ("import random", "from random", "time.", "datetime",
                      "np.random.seed", "default_rng("):
        assert forbidden not in text, f"forbidden pattern {forbidden!r} in life.py"
