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
    LifeStore,
    current_activity_now,
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


def _simulate(seed: int, persona: domain.PersonaProfile, store: "LifeStore",
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


#: NOW-semantics resolver under test (alias keeps the T2 tests readable).
life_now = current_activity_now


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
    # Day 8: every arc has started (max started_day = 8 for this seed), so the
    # per-day draw sequence is the canonical one; earlier days skip draws for
    # not-yet-started arcs (start-time respect) and may legitimately deviate.
    day = 8
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


def test_reload_reproduces_persistent_state_real_store(tmp_path):
    """Gate check: the SAME restart/reload contract against A2's real SQLiteStore.

    The fake-store variant above proves semantics; this one proves the seam against
    the production persistence layer (schema v2, migrations, WAL reopen).
    """
    from harness.store import SQLiteStore

    persona = _persona()
    path = tmp_path / "life_real.db"

    store1 = SQLiteStore(path)
    arcs, agendas, _ = _simulate(CORE_SEED, persona, store1, days=30)
    store1.close()

    # --- restart: new store instance over the same file ---
    store2 = SQLiteStore(path)
    # No ORDER BY contract in the store seam: compare state as id-keyed maps and
    # normalize by id before continuation runs (persistence preserves STATE, not
    # list order).
    reloaded_arcs = sorted(store2.list_life_arcs(), key=lambda a: a.id)
    arcs_sorted = sorted(arcs, key=lambda a: a.id)
    assert {a.id: a for a in reloaded_arcs} == {a.id: a for a in arcs_sorted}
    assert store2.load_agenda(30) == agendas[30]
    for day in (1, 15, 30):
        stored_items = {it.id: it for it in store2.list_agenda_items(day=day)}
        assert stored_items == {it.id: it for it in agendas[day].items}

    # --- continuation determinism: day 31 from reloaded state == fresh run ---
    agenda31_reloaded = generate_agenda(31, persona, reloaded_arcs, store2,
                                        stream_rng(CORE_SEED, LIFE_STREAM, 31))
    step31_reloaded = step_life(31, persona, reloaded_arcs, agenda31_reloaded, store2,
                                stream_rng(CORE_SEED, LIFE_STREAM, 31))
    store2.close()

    store3 = SQLiteStore(tmp_path / "life_fresh.db")
    fresh_arcs, _, _ = _simulate(CORE_SEED, persona, store3, days=30)
    fresh_arcs = sorted(fresh_arcs, key=lambda a: a.id)
    agenda31_fresh = generate_agenda(31, persona, fresh_arcs, store3,
                                     stream_rng(CORE_SEED, LIFE_STREAM, 31))
    step31_fresh = step_life(31, persona, fresh_arcs, agenda31_fresh, store3,
                             stream_rng(CORE_SEED, LIFE_STREAM, 31))
    store3.close()

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


# ---------------------------------------------------------------------------
# Iteration 2 (A2): arc start-time respect (plan §5-A2 T1)
# ---------------------------------------------------------------------------

def _late_arc() -> domain.LifeArc:
    """An arc that does not start until day 15 (used by the T1 tests)."""
    return domain.LifeArc(
        id="arc_late",
        name="learning pottery",
        interest="pottery",
        started_day=15,
        progress=0.1,
        status="active",
        next_intention="practice the fundamentals",
    )


def test_arc_start_time_respected_agenda(tmp_path):
    """An arc with started_day=15 must not generate activities on day 8."""
    persona = _persona()
    store = _store(tmp_path)
    early = domain.LifeArc(
        id="arc_early", name="learning novels", interest="novels",
        started_day=2, progress=0.2, status="active",
        next_intention="try a new variation",
    )
    late = _late_arc()
    store.upsert_life_arc(early)
    store.upsert_life_arc(late)

    for day in (3, 8, 14):  # before the arc's start day
        agenda = generate_agenda(day, persona, [early, late], store,
                                 stream_rng(CORE_SEED, LIFE_STREAM, day))
        late_items = [it for it in agenda.items if it.source_id == "arc_late"]
        assert not late_items, (
            f"arc not started until day 15 but generated items on day {day}: "
            f"{[it.activity for it in late_items]}"
        )

    # from its start day onward the arc can contribute (0.8 per-day chance;
    # over a window it must land at least once for this seed)
    seen = False
    for day in range(15, 23):
        agenda = generate_agenda(day, persona, [early, late], store,
                                 stream_rng(CORE_SEED, LIFE_STREAM, day))
        if any(it.source_id == "arc_late" for it in agenda.items):
            seen = True
            break
    assert seen, "started arc never generated an agenda item in 8 days"


def test_arc_start_time_respected_progress(tmp_path):
    """step_life must not advance progress of an arc that has not started."""
    persona = _persona()
    store = _store(tmp_path)
    late = _late_arc()
    store.upsert_life_arc(late)

    agenda = generate_agenda(8, persona, [late], store,
                             stream_rng(CORE_SEED, LIFE_STREAM, 8))
    result = step_life(8, persona, [late], agenda, store,
                       stream_rng(CORE_SEED, LIFE_STREAM, 8))
    after = {a.id: a for a in result.updated_arcs}
    assert after["arc_late"].progress == 0.1  # untouched before start
    assert after["arc_late"].status == "active"
    assert store.get_life_arc("arc_late").progress == 0.1

    # once started, the arc advances
    agenda16 = generate_agenda(16, persona, [late], store,
                               stream_rng(CORE_SEED, LIFE_STREAM, 16))
    result16 = step_life(16, persona, [late], agenda16, store,
                         stream_rng(CORE_SEED, LIFE_STREAM, 16))
    after16 = {a.id: a for a in result16.updated_arcs}
    assert after16["arc_late"].progress > 0.1


def test_arc_start_time_survives_reload(tmp_path):
    """started_day is persisted; the start-time gate applies after a restart."""
    persona = _persona()
    path = tmp_path / "life_store.json"
    store1 = FakeLifeStore(path)
    late = _late_arc()
    store1.upsert_life_arc(late)
    store1.close()

    store2 = FakeLifeStore(path)
    reloaded = store2.list_life_arcs()
    assert [a.started_day for a in reloaded] == [15]
    agenda = generate_agenda(8, persona, reloaded, store2,
                             stream_rng(CORE_SEED, LIFE_STREAM, 8))
    assert not [it for it in agenda.items if it.source_id == "arc_late"]


# ---------------------------------------------------------------------------
# Iteration 2 (A2): CurrentActivity = active NOW (plan §5-A2 T2, invariant 8)
# ---------------------------------------------------------------------------

def test_current_activity_now_future_plan_is_not_current():
    """A 7 PM plan is NOT what she is doing at 10 AM: nothing active -> None,
    and the plan stays in the DailyAgenda."""
    day = 10
    base = _day_start(day)
    items = (
        domain.AgendaItem("ag_1", base + 10.0, base + 11.0, "morning pottery",
                          "arc", "arc_1", 0.7, "planned"),
        domain.AgendaItem("ag_2", base + 19.0, base + 20.0, "evening run",
                          "interest", "running", 0.5, "planned"),
    )
    agenda = domain.DailyAgenda(day=day, items=items)

    # 10:00-11:00: the morning item is current while actually in progress
    assert life_now(agenda, base + 10.0).item.id == "ag_1"
    assert life_now(agenda, base + 10.75).item.id == "ag_1"
    # 10 AM "now" is never the 7 PM plan
    assert life_now(agenda, base + 10.0).item.id != "ag_2"
    # gap at 15:00: nothing active -> None
    assert life_now(agenda, base + 15.0) is None
    # before the first item and after the last: None
    assert life_now(agenda, base + 8.5) is None
    assert life_now(agenda, base + 21.0) is None
    # the plan itself is current only while actually happening
    assert life_now(agenda, base + 19.5).item.id == "ag_2"


def test_current_activity_now_skipped_item_not_current():
    """A skipped item is not happening at its planned slot."""
    day = 10
    base = _day_start(day)
    skipped = domain.AgendaItem("ag_1", base + 10.0, base + 11.0, "morning pottery",
                                "arc", "arc_1", 0.7, "skipped")
    other = domain.AgendaItem("ag_2", base + 19.0, base + 20.0, "evening run",
                              "interest", "running", 0.5, "planned")
    agenda = domain.DailyAgenda(day=day, items=(skipped, other))
    assert life_now(agenda, base + 10.5) is None
    assert life_now(agenda, base + 19.5).item.id == "ag_2"


def test_current_activity_now_overlap_single():
    """Even with overlapping items, current activity is single-valued: the
    highest-salience item in progress at t_h, never a pair."""
    day = 3
    base = _day_start(day)
    items = (
        domain.AgendaItem("pottery", base + 14.0, base + 16.0, "pottery class",
                          "interest", "drawing", 0.5, "planned"),
        domain.AgendaItem("run", base + 15.0, base + 17.0, "evening run",
                          "interest", "outdoors", 0.4, "planned"),
    )
    agenda = domain.DailyAgenda(day=day, items=items)
    current = life_now(agenda, base + 15.5)
    assert current is not None
    assert current.item.id == "pottery"  # higher salience wins the overlap
    assert current.item.start_t_h <= base + 15.5 < current.item.end_t_h


def test_step_life_current_activity_with_t_h(tmp_path):
    """step_life(t_h=...) resolves the NOW activity (item in progress at t_h,
    None in gaps); the legacy no-t_h call keeps the day-level main-activity
    contract."""
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(CORE_SEED, persona, store)
    day = 8
    agenda = generate_agenda(day, persona, arcs, store,
                             stream_rng(CORE_SEED, LIFE_STREAM, day))
    result = step_life(day, persona, arcs, agenda, store,
                       stream_rng(CORE_SEED, LIFE_STREAM, day), t_h=_day_start(day) + 9.0)
    if result.current_activity is not None:
        item = result.current_activity.item
        assert item.start_t_h <= _day_start(day) + 9.0 < item.end_t_h
        assert item.status != "skipped"
        assert result.current_activity.t_h == _day_start(day) + 9.0
    # after the awake window nothing can be active
    late = step_life(day, persona, arcs, agenda, store,
                     stream_rng(CORE_SEED, LIFE_STREAM, day),
                     t_h=_day_start(day) + AWAKE_END_H + 0.5)
    assert late.current_activity is None


# ---------------------------------------------------------------------------
# Iteration 2 (A2): arc replenishment (plan §5-A2 T3, invariant 9)
# ---------------------------------------------------------------------------

def _run_days(seed: int, persona, store, days: int):
    """Session-faithful daily loop (fresh per-day rng per call); returns the
    final arc list and per-day post-step active counts."""
    arcs = init_life(seed, persona, store)
    active_counts: list[int] = []
    for day in range(1, days + 1):
        agenda = generate_agenda(day, persona, arcs, store,
                                 stream_rng(seed, LIFE_STREAM, day))
        result = step_life(day, persona, arcs, agenda, store,
                           stream_rng(seed, LIFE_STREAM, day))
        arcs = result.updated_arcs
        active_counts.append(sum(1 for a in arcs if a.status == "active"))
    return arcs, active_counts


def test_replenishment_life_never_permanently_dies(tmp_path):
    """60 days: active arcs never reach 0 (the post-step spawn is certain),
    and replacement arcs actually appear with fresh ids."""
    persona = _persona()
    store = _store(tmp_path)
    arcs, active_counts = _run_days(12345, persona, store, 60)
    assert 0 not in active_counts, "active life must never die"
    assert active_counts[-1] >= 1
    spawned = [a for a in arcs if a.id.endswith("_s0")]
    assert spawned, "replenishment must spawn replacement arcs"
    assert all(a.status in {"active", "completed", "abandoned"} for a in arcs)
    assert len({a.id for a in arcs}) == len(arcs)  # ids stay unique


def test_replenishment_not_every_completion_spawns(tmp_path):
    """Some below-minimum days roll and fail: replacements do not mirror
    completions one-for-one (NOT every completed arc creates another)."""
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(777, persona, store)
    below_min_days = 0
    spawn_days = 0
    for day in range(1, 121):
        agenda = generate_agenda(day, persona, arcs, store,
                                 stream_rng(777, LIFE_STREAM, day))
        result = step_life(day, persona, arcs, agenda, store,
                           stream_rng(777, LIFE_STREAM, day))
        # pre-spawn active count: today's spawned arc (if any) is excluded
        pre_spawn_active = [
            a for a in result.updated_arcs
            if a.status == "active" and not (a.id.endswith("_s0") and a.started_day == day)
        ]
        if len(pre_spawn_active) < 2:  # N_MIN_ACTIVE
            below_min_days += 1
        if any(a.id.endswith("_s0") and a.started_day == day for a in result.updated_arcs):
            spawn_days += 1
        arcs = result.updated_arcs
    assert below_min_days > spawn_days, (
        "every below-minimum day spawned — no failed rolls"
    )


def test_replenishment_candidates_descendant_and_fresh(tmp_path):
    """Replacement arcs originate from the persona's world: some are
    descendants of prior completed arcs (same interest), some are fresh
    interests; an interest with an active arc is never duplicated."""
    persona = _persona()
    store = _store(tmp_path)
    arcs = init_life(999, persona, store)
    for day in range(1, 61):
        agenda = generate_agenda(day, persona, arcs, store,
                                 stream_rng(999, LIFE_STREAM, day))
        result = step_life(day, persona, arcs, agenda, store,
                           stream_rng(999, LIFE_STREAM, day))
        arcs = result.updated_arcs
        active_interests = [a.interest for a in arcs if a.status == "active"]
        assert len(active_interests) == len(set(active_interests)), (
            f"day {day}: two active arcs on the same interest"
        )
    all_arcs = store.list_life_arcs()
    persona_interests = {i.name for i in persona.interests}
    spawned = [a for a in all_arcs if a.id.endswith("_s0")]
    assert spawned
    for s in spawned:
        assert s.interest in persona_interests  # never off-persona
        assert s.status in {"active", "completed", "abandoned"}
        assert s.next_intention and s.started_day <= 60
    completed_interests = {a.interest for a in all_arcs if a.status == "completed"}
    assert any(s.interest in completed_interests for s in spawned), (
        "expected at least one descendant of a completed arc"
    )
    assert any(s.interest not in completed_interests for s in spawned), (
        "expected at least one fresh-interest replacement"
    )


def test_replenishment_spawn_survives_reload(tmp_path):
    """Replacement arcs are persisted: reopening the store reproduces them."""
    persona = _persona()
    path = tmp_path / "life_store.json"
    store1 = FakeLifeStore(path)
    arcs, _ = _run_days(12345, persona, store1, 60)
    store1.close()

    store2 = FakeLifeStore(path)
    reloaded = store2.list_life_arcs()
    assert {a.id for a in reloaded} == {a.id for a in arcs}
    spawned = [a for a in reloaded if a.id.endswith("_s0")]
    assert spawned
    for s in spawned:
        assert store2.get_life_arc(s.id) == s
        assert s.started_day <= 60


def test_recent_good_days_counts_meaningful_events(tmp_path):
    """Source-4 helper: meaningful recent companion events (day_finalized
    with score >= 0.7 inside the look-back window) count; low scores, other
    event kinds and stale events do not; seam-less stores contribute 0."""
    from harness.life import _recent_good_days
    from harness.store import SQLiteStore

    store = SQLiteStore(tmp_path / "events.db")
    assert _recent_good_days(store, 20) == 0  # empty audit log
    store.log_event(15, 15 * 24.0, "day_finalized", "score=0.800 shadow=False")
    store.log_event(16, 16 * 24.0, "day_finalized", "score=0.600 shadow=False")  # below threshold
    store.log_event(17, 17 * 24.0, "life_step", "arcs=3 items=5")  # not a day
    store.log_event(18, 18 * 24.0, "day_finalized", "score=0.750 shadow=True")
    assert _recent_good_days(store, 20) == 2  # days 15 and 18 in window
    assert _recent_good_days(store, 22) == 2
    assert _recent_good_days(store, 23) == 1  # day 15 fell out of the window
    store.close()
    # a seam-less store (no audit log) contributes 0, keeping the policy alive
    assert _recent_good_days(_store(tmp_path / "fake"), 20) == 0


def test_replenishment_meaningful_events_change_trajectory(tmp_path):
    """Meaningful recent companion events (plan §5-A2 T3 source 4) raise the
    spawn probability: the same seed with a streak of good recent days lands
    a different spawn trajectory than without."""
    from harness.store import SQLiteStore

    persona = _persona()

    def run(good_events: bool) -> list[int]:
        store = SQLiteStore(tmp_path / f"boost_{good_events}.db")
        arcs = init_life(42, persona, store)
        spawn_days: list[int] = []
        for day in range(1, 61):
            if good_events and day > 1:
                store.log_event(day - 1, (day - 1) * 24.0 + 23.0, "day_finalized",
                                "score=0.750 shadow=False")
            agenda = generate_agenda(day, persona, arcs, store,
                                     stream_rng(42, LIFE_STREAM, day))
            result = step_life(day, persona, arcs, agenda, store,
                               stream_rng(42, LIFE_STREAM, day))
            arcs = result.updated_arcs
            for a in result.updated_arcs:
                if a.id.endswith("_s0") and a.started_day == day:
                    spawn_days.append(day)
        store.close()
        return spawn_days

    plain = run(False)
    boosted = run(True)
    assert plain and boosted
    assert plain != boosted, "good recent days must change the spawn trajectory"
