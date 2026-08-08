"""Persistent life simulation — life arcs, daily agenda, current activity (A4).

Owned by track A4 (vertical-slice Wave 1). Binding API shapes: §7 (A4) and
§15 (life seam + store seam) of plans/companion-vertical-slice-2026-08.md.
The three public entry points are ``init_life``, ``generate_agenda`` and
``step_life``; all take the injected store (persistence) and a seeded rng.

RNG contract (CRITICAL)
-----------------------
All stochastic draws come from ``engine.rng`` seeded streams. The LIFE stream
uses the reserved stream key 4 (plan §15: 4 = LIFE, 5 = PERSONA; the key
cannot live in engine/rng.py because that module is frozen, so the convention
is documented here):

* ``stream_rng(seed, 4)``        — init draws (arc selection in ``init_life``).
* ``stream_rng(seed, 4, day)``   — per-day draws (``generate_agenda`` and
                                   ``step_life``; the caller passes this
                                   generator in as ``rng``).

This stream NEVER draws from ``day_rng(seed, t)`` (DAILY_STREAM 0): the Session
holds the SAME day generator object across a day, so any extra draw would
desync the end-of-day draws and break ``test_replay_matches_run_daily``.
No ``random`` module, no real clocks — determinism is per (seed, day).

Persistence
-----------
Every mutation goes through the §15 store seam subset (injected): life arcs
via ``upsert_life_arc`` / ``get_life_arc`` / ``list_life_arcs`` /
``update_life_arc_status``, agendas via ``save_agenda`` / ``load_agenda`` /
``update_agenda_item_status`` / ``list_agenda_items``. The store is the source
of truth: after a restart, ``list_life_arcs`` + ``load_agenda`` reproduce the
same persistent state, so the life trajectory is continuous, not 30
independent calendars.

Time semantics
--------------
``t_h`` is absolute hours since simulation start; ``day = int(t_h // 24)``;
local hour = ``t_h % 24``. Agenda items live inside the awake window
08:00-23:00 (``AWAKE_START_H``..``AWAKE_END_H``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np

from engine.rng import stream_rng

from harness.domain import (
    AgendaItem,
    CurrentActivity,
    DailyAgenda,
    Interest,
    LifeArc,
    PersonaProfile,
)

#: Reserved RNG stream key for the LIFE stream (plan §15: 4 = LIFE, 5 = PERSONA).
LIFE_STREAM = 4

#: Day length in hours and the awake window agenda items must live inside.
DAY_HOURS = 24.0
AWAKE_START_H = 8.0
AWAKE_END_H = 23.0

#: Probability that an active arc's next_intention lands on a given day's agenda.
_ARC_ITEM_PROB = 0.8
#: Daily probability that an active arc is abandoned (modest deviation).
_ABANDON_PROB = 0.005
#: Daily progress gain of an active arc: ``0.01 + rng.random() * 0.04``.
_PROGRESS_MIN = 0.01
_PROGRESS_SPAN = 0.04

#: Arc-name templates; one draw per arc ({interest} is substituted).
_ARC_NAME_TEMPLATES = (
    "learning {interest}",
    "mastering {interest}",
    "a {interest} project",
    "weekly {interest} practice",
    "deep dive into {interest}",
)

#: Next-intention pool; one draw per arc (generic enough for any interest).
_NEXT_INTENTIONS = (
    "practice the fundamentals",
    "try a new variation",
    "review recent progress",
    "plan the next steps",
    "experiment with a fresh idea",
    "finish the current piece",
    "prepare the materials",
    "reflect on how it is going",
)

#: Standalone interest-activity templates; one draw per interest item.
_INTEREST_ACTIVITIES = (
    "read about {interest}",
    "practice {interest}",
    "watch a video on {interest}",
    "try a small {interest} exercise",
    "plan a {interest} session",
)


class LifeStore(Protocol):
    """Store seam subset used by life.py (orchestrator-frozen shapes, §15).

    Implemented by A2's ``SQLiteStore``; the A4 module tests use a
    seam-faithful fake because ``wip/vslice-a2`` had not landed when A4 ran.
    """

    def upsert_life_arc(self, arc: LifeArc) -> None: ...
    def get_life_arc(self, arc_id: str) -> LifeArc | None: ...
    def list_life_arcs(self, status: str | None = None) -> list[LifeArc]: ...
    def update_life_arc_status(self, arc_id: str, status: str) -> None: ...
    def save_agenda(self, day: int, agenda: DailyAgenda) -> None: ...
    def load_agenda(self, day: int) -> DailyAgenda | None: ...
    def update_agenda_item_status(self, item_id: str, status: str) -> None: ...
    def list_agenda_items(
        self, day: int | None = None, status: str | None = None
    ) -> list[AgendaItem]: ...


@dataclass(frozen=True)
class LifeStepResult:
    """Outcome of one day's life step (§15 life seam).

    ``updated_arcs``: arcs after progress/status updates (persisted).
    ``agenda``: the day's agenda with item statuses updated (persisted).
    ``current_activity``: the day's MAIN activity — the highest-salience
    completed item, or the highest-salience item when nothing completed;
    ``None`` for an empty agenda. (The seam signature carries no ``t_h``, so
    the day-level interpretation is used; per-t_h views can be re-derived from
    the returned agenda.)
    """

    updated_arcs: list[LifeArc]
    agenda: DailyAgenda
    current_activity: CurrentActivity | None


def init_life(
    seed: int, persona: PersonaProfile, store: LifeStore, start_day: int = 1
) -> list[LifeArc]:
    """Seed 2-4 active life arcs from the persona's interests and persist them.

    Deterministic per ``seed``: all draws come from ``stream_rng(seed, 4)``
    (the init sub-stream of the reserved LIFE stream — never ``day_rng``).
    Arc interests are drawn without replacement, weighted by salience, so every
    arc is tied to a real persona interest. The first arc starts nearer
    completion (progress 0.70-0.85) so the complete transition is reachable
    within a month; the others start at 0.00-0.35. Each arc is persisted via
    ``store.upsert_life_arc`` before being returned (creation order:
    arc_1, arc_2, ...).
    """
    rng = stream_rng(seed, LIFE_STREAM)
    interests = persona.interests
    if not interests:
        return []

    n_arcs = int(rng.integers(2, 5))  # 2..4
    n_arcs = min(n_arcs, len(interests))
    weights = [max(float(i.salience), 1e-3) for i in interests]
    total = sum(weights)
    chosen = rng.choice(
        len(interests), size=n_arcs, replace=False, p=[w / total for w in weights]
    )

    arcs: list[LifeArc] = []
    for i, idx in enumerate(chosen, start=1):
        interest = interests[int(idx)]
        name = _ARC_NAME_TEMPLATES[int(rng.integers(len(_ARC_NAME_TEMPLATES)))].format(
            interest=interest.name
        )
        started_day = start_day + int(rng.integers(0, 8))
        if i == 1:
            progress = 0.70 + float(rng.random()) * 0.15  # oldest arc: may complete
        else:
            progress = float(rng.random()) * 0.35
        arc = LifeArc(
            id=f"arc_{i}",
            name=name,
            interest=interest.name,
            started_day=started_day,
            progress=round(progress, 3),
            status="active",
            next_intention=_NEXT_INTENTIONS[int(rng.integers(len(_NEXT_INTENTIONS)))],
        )
        store.upsert_life_arc(arc)
        arcs.append(arc)
    return arcs


def generate_agenda(
    day: int,
    persona: PersonaProfile,
    arcs: list[LifeArc],
    store: LifeStore,
    rng: np.random.Generator,
) -> DailyAgenda:
    """Build and persist the day's agenda, predominantly from persona sources.

    Sources, in order of drawing:
    * routines — each routine whose cadence draw succeeds (prob = cadence);
    * arcs — each ACTIVE arc's ``next_intention`` with probability 0.8;
    * interests — exactly 2 standalone interest items drawn weighted by
      salience (guarantees a non-empty, interest-grounded agenda every day).

    Items live inside the awake window (t_h = day*24 + local hour, 08:00-23:00),
    are sorted by start time, carry status "planned", and are persisted via
    ``store.save_agenda``. All draws come from the passed ``rng`` — callers
    must hand in ``stream_rng(seed, LIFE_STREAM, day)`` (never ``day_rng``).
    """
    items: list[AgendaItem] = []
    day_start = day * DAY_HOURS

    for routine in persona.routines:
        if rng.random() >= routine.cadence:
            continue
        start = day_start + round(routine.start_frac * DAY_HOURS, 2)
        end = min(start + routine.duration_h, day_start + AWAKE_END_H)
        if end <= start:  # routine scheduled past the awake window: skip
            continue
        items.append(
            AgendaItem(
                id=f"ag_{day}_r_{len(items):02d}",
                start_t_h=start,
                end_t_h=round(end, 2),
                activity=routine.name,
                source_type="routine",
                source_id=routine.name,
                salience=routine.salience,
                status="planned",
            )
        )

    for arc in arcs:
        if arc.status != "active" or rng.random() >= _ARC_ITEM_PROB:
            continue
        start = day_start + float(rng.integers(9, 21))  # 09:00..20:00 local
        end = min(start + 1.0 + float(rng.random()) * 1.5, day_start + AWAKE_END_H)
        interest = _interest_by_name(persona, arc.interest)
        items.append(
            AgendaItem(
                id=f"ag_{day}_a_{arc.id}",
                start_t_h=start,
                end_t_h=round(end, 2),
                activity=arc.next_intention,
                source_type="arc",
                source_id=arc.id,
                salience=round(interest.salience if interest else 0.5, 3),
                status="planned",
            )
        )

    pool = [i for i in persona.interests if i.salience > 0] or list(persona.interests)
    if not pool:  # interest-less persona: agenda from routines/arcs only
        items.sort(key=lambda it: it.start_t_h)
        agenda = DailyAgenda(day=day, items=tuple(items))
        store.save_agenda(day, agenda)
        return agenda
    n_interest = min(2, len(pool))
    weights = [max(float(i.salience), 1e-3) for i in pool]
    total = sum(weights)
    picks = rng.choice(
        len(pool), size=n_interest, replace=False, p=[w / total for w in weights]
    )
    for idx in picks:
        interest = pool[int(idx)]
        start = day_start + float(rng.integers(8, 21))  # 08:00..20:00 local
        end = min(start + 0.5 + float(rng.random()) * 1.5, day_start + AWAKE_END_H)
        activity = _INTEREST_ACTIVITIES[
            int(rng.integers(len(_INTEREST_ACTIVITIES)))
        ].format(interest=interest.name)
        items.append(
            AgendaItem(
                id=f"ag_{day}_i_{interest.name}",
                start_t_h=start,
                end_t_h=round(end, 2),
                activity=activity,
                source_type="interest",
                source_id=interest.name,
                salience=interest.salience,
                status="planned",
            )
        )

    items.sort(key=lambda it: it.start_t_h)
    agenda = DailyAgenda(day=day, items=tuple(items))
    store.save_agenda(day, agenda)
    return agenda


def step_life(
    day: int,
    persona: PersonaProfile,
    arcs: list[LifeArc],
    agenda: DailyAgenda,
    store: LifeStore,
    rng: np.random.Generator,
) -> LifeStepResult:
    """Advance one day: arc progress/status and item statuses, persist, report.

    * Arc progress: each ACTIVE arc gains ``0.01 + rng.random() * 0.04``;
      an arc reaching 1.0 completes; a 2% daily chance abandons it. Updated
      arcs are persisted via ``store.upsert_life_arc``.
    * Item statuses: planned items deviate modestly — ~80% completed, ~10%
      skipped, ~10% shifted — persisted via
      ``store.update_agenda_item_status``.
    * ``current_activity``: the day's MAIN activity — the highest-salience
      completed item (fallback: highest-salience item overall); ``None`` for an
      empty agenda. (The seam signature carries no ``t_h``; the day-level
      interpretation is documented on ``LifeStepResult``.)

    Draw order is fixed (arcs in given order, then items in agenda order), so
    the outcome is deterministic per (seed, day) when ``rng`` is
    ``stream_rng(seed, LIFE_STREAM, day)``.
    """
    updated_arcs: list[LifeArc] = []
    for arc in arcs:
        new_arc = arc
        if arc.status == "active":
            progress = min(1.0, arc.progress + _PROGRESS_MIN + float(rng.random()) * _PROGRESS_SPAN)
            status = "completed" if progress >= 1.0 else arc.status
            if status == "active" and rng.random() < _ABANDON_PROB:
                status = "abandoned"
            new_arc = replace(arc, progress=round(progress, 3), status=status)
            store.upsert_life_arc(new_arc)
        updated_arcs.append(new_arc)

    updated_items: list[AgendaItem] = []
    for item in agenda.items:
        if item.status != "planned":
            updated_items.append(item)
            continue
        draw = float(rng.random())
        if draw < 0.80:
            status = "completed"
        elif draw < 0.90:
            status = "skipped"
        else:
            status = "shifted"
        new_item = replace(item, status=status)
        store.update_agenda_item_status(new_item.id, status)
        updated_items.append(new_item)

    updated_agenda = DailyAgenda(day=agenda.day, items=tuple(updated_items))

    completed = [it for it in updated_items if it.status == "completed"]
    candidates = completed or updated_items
    current: CurrentActivity | None = None
    if candidates:
        main = max(candidates, key=lambda it: (it.salience, it.start_t_h))
        current = CurrentActivity(t_h=main.start_t_h, item=main, description=main.activity)

    return LifeStepResult(
        updated_arcs=updated_arcs, agenda=updated_agenda, current_activity=current
    )


def _interest_by_name(persona: PersonaProfile, name: str) -> Interest | None:
    """Look up a persona interest by name (used to derive arc-item salience)."""
    for interest in persona.interests:
        if interest.name == name:
            return interest
    return None
