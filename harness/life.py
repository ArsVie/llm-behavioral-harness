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
08:00-23:00 (``AWAKE_START_H``..``AWAKE_END_H``). Arcs only act from their
``started_day`` onward (no activities and no progress before the start), and
``CurrentActivity`` means active NOW — ``current_activity_now`` resolves the
item in progress at a given ``t_h`` and returns ``None`` when nothing is
active; future plans stay in the DailyAgenda.

Replenishment (Iteration 2, plan §5-A2 T3)
------------------------------------------
Active life must never permanently die: while ``N_active < N_MIN_ACTIVE``
``step_life`` rolls a replacement-arc spawn with probability > 0 (certain
when nothing is active). Spawn candidates originate from prior completed
arcs (descendants), the persona's adjacent interests, the persona's own
interests, and meaningful recent companion events (audit-log day scores);
not every completed arc creates another. See ``_maybe_spawn_arc``.
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

#: Reserved RNG stream key for the LIFE stream.
LIFE_STREAM = 4

#: Day length in hours and the awake window bounds for agenda items.
DAY_HOURS = 24.0
AWAKE_START_H = 8.0
AWAKE_END_H = 23.0

#: Probability that an active arc's next_intention lands on a given day's agenda.
_ARC_ITEM_PROB = 0.8
#: Daily probability that an active arc is abandoned.
_ABANDON_PROB = 0.005
#: Daily progress gain of an active arc: ``0.01 + rng.random() * 0.04``.
_PROGRESS_MIN = 0.01
_PROGRESS_SPAN = 0.04

#: Spawn policy while ``N_active < N_MIN_ACTIVE``: probability ``_SPAWN_PROB``,
#: certain (``_SPAWN_PROB_EMPTY``) when no arc is active.
_N_MIN_ACTIVE = 2
_SPAWN_PROB = 0.5
_SPAWN_PROB_EMPTY = 1.0
#: Spawn-probability boost when ``_recent_good_days`` reports recent good days.
_SPAWN_EVENT_BOOST = 0.25
#: Score threshold for a ``day_finalized`` audit event to count as meaningful.
_GOOD_DAY_SCORE = 0.7
#: Look-back window (days) for meaningful recent companion events.
_EVENT_WINDOW_DAYS = 7
#: Replacement arcs start with progress already underway; descendants inherit momentum.
_DESCENDANT_PROGRESS_MIN = 0.45
_DESCENDANT_PROGRESS_SPAN = 0.20
_FRESH_PROGRESS_MIN = 0.40
_FRESH_PROGRESS_SPAN = 0.20

#: Name templates for replacement arcs; one draw per spawn.
_SPAWN_DESCENDANT_TEMPLATES = (
    "practicing {interest}",
    "leveling up {interest}",
    "a {interest} follow-up",
    "deepening {interest}",
    "the next {interest} step",
)
_SPAWN_FRESH_TEMPLATES = (
    "learning {interest}",
    "exploring {interest}",
    "a {interest} project",
    "weekly {interest} practice",
    "getting into {interest}",
)

#: Arc-name templates; one draw per arc ({interest} is substituted).
_ARC_NAME_TEMPLATES = (
    "learning {interest}",
    "mastering {interest}",
    "a {interest} project",
    "weekly {interest} practice",
    "deep dive into {interest}",
)

#: Next-intention pool; one draw per arc.
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

    ``updated_arcs``: arcs after progress/status updates and any
    replenishment spawn (persisted).
    ``agenda``: the day's agenda with item statuses updated (persisted).
    ``current_activity``: with ``t_h`` (NOW semantics) the item active at
    ``t_h``, ``None`` when nothing is active; without ``t_h`` the day-level
    MAIN activity — the highest-salience completed item, or the
    highest-salience item when nothing completed; ``None`` for an empty
    agenda.
    """

    updated_arcs: list[LifeArc]
    agenda: DailyAgenda
    current_activity: CurrentActivity | None


def init_life(
    seed: int,
    persona: PersonaProfile,
    store: LifeStore,
    start_day: int = 1,
    epoch: int = 0,
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

    ``epoch`` (default 0) is the life-generation counter: the first seeding
    uses the legacy bare ids (``arc_1``, ``arc_2``, ...); every later seeding
    (a documented cold start after the store's life arcs were wiped) prefixes
    the epoch so ids are never reused from a previous generation
    (``arc_<epoch>_<i>``) — each epoch is a fresh id namespace. Callers
    derive the epoch from the store's persisted state (see Session).
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
            id=f"arc_{epoch}_{i}" if epoch else f"arc_{i}",
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
    * arcs — each ACTIVE arc that has already STARTED (``started_day <= day``)
      contributes its ``next_intention`` with probability 0.8 — an arc with a
      future ``started_day`` must NOT generate activities before its start;
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
        if arc.status != "active" or arc.started_day > day or rng.random() >= _ARC_ITEM_PROB:
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
    *,
    t_h: float | None = None,
) -> LifeStepResult:
    """Advance one day: arc progress/status, item statuses, replenishment.

    * Arc progress: each ACTIVE arc that has already STARTED (``started_day
      <= day``) gains ``0.01 + rng.random() * 0.04``; an arc reaching 1.0
      completes; a 2% daily chance abandons it. Arcs with a future
      ``started_day`` are untouched (no progress, no status change, no draws).
      Updated arcs are persisted via ``store.upsert_life_arc``.
    * Item statuses: planned items deviate modestly — ~80% completed, ~10%
      skipped, ~10% shifted — persisted via
      ``store.update_agenda_item_status``.
    * Replenishment (plan §5-A2 T3): when ``N_active < N_MIN_ACTIVE`` the
      policy may spawn a replacement arc — probability ``_SPAWN_PROB`` (with
      a boost from meaningful recent companion events), certain when nothing
      is active, so active life never permanently dies. Spawn candidates
      originate from prior completed arcs (descendants), the persona's
      adjacent interests, the persona's own interests, and meaningful recent
      companion events. The spawn roll and its draws happen AFTER all other
      draws of the day, so when ``N_active >= N_MIN_ACTIVE`` the day is
      byte-identical to the pre-replenishment behaviour.
    * ``current_activity``: when ``t_h`` is given (NOW semantics, plan
      §5-A2 T2, invariant 8) it is the item actually in progress at ``t_h``,
      or ``None`` when nothing is active — a future plan never becomes the
      current activity. Without ``t_h`` (legacy seam callers) it is the
      day-level MAIN activity: the highest-salience completed item, or the
      highest-salience item when nothing completed; ``None`` for an empty
      agenda.

    Draw order is fixed (started arcs in given order, then items in agenda
    order, then the optional replenishment roll), so the outcome is
    deterministic per (seed, day) when ``rng`` is
    ``stream_rng(seed, LIFE_STREAM, day)``.
    """
    updated_arcs: list[LifeArc] = []
    for arc in arcs:
        new_arc = arc
        if arc.status == "active" and arc.started_day <= day:
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

    if t_h is not None:
        # Only an item actually in progress at t_h is the current activity.
        current = current_activity_now(updated_agenda, t_h)
    else:
        completed = [it for it in updated_items if it.status == "completed"]
        candidates = completed or updated_items
        current = None
        if candidates:
            main = max(candidates, key=lambda it: (it.salience, it.start_t_h))
            current = CurrentActivity(t_h=main.start_t_h, item=main, description=main.activity)

    spawned = _maybe_spawn_arc(day, persona, updated_arcs, store, rng)
    if spawned is not None:
        updated_arcs.append(spawned)

    return LifeStepResult(
        updated_arcs=updated_arcs, agenda=updated_agenda, current_activity=current
    )


def current_activity_now(agenda: DailyAgenda, t_h: float) -> CurrentActivity | None:
    """NOW semantics (plan §5-A2 T2, orchestrator invariant 8).

    The item actually in progress at ``t_h`` (``start_t_h <= t_h < end_t_h``
    and not skipped/shifted — those are not happening at their planned slot),
    choosing the highest salience when several overlap; ``None`` when nothing
    is active. Future plans never become the current activity: a 7 PM plan
    is not what she is doing at 10 AM. Pure function: no rng, no persistence.
    """
    in_progress = [
        it
        for it in agenda.items
        if it.start_t_h <= t_h < it.end_t_h and it.status not in {"skipped", "shifted"}
    ]
    if not in_progress:
        return None
    main = max(in_progress, key=lambda it: (it.salience, it.start_t_h))
    return CurrentActivity(t_h=t_h, item=main, description=main.activity)


def transition_past_windows(
    agenda: DailyAgenda, t_h: float, day: int
) -> list[AgendaItem]:
    """Deterministic planned→completed transition as windows pass (S2/W2).

    Pure function of (item window, t_h, day) — no wall clock, no rng, no
    store. Every item of ``day``'s agenda whose window has FULLY passed
    (``end_t_h <= t_h``) while still ``planned`` becomes ``completed``: the
    slot came and went on the plan with no recorded deviation, so the day's
    plan is treated as fulfilled (``done``; ``skipped``/``shifted`` stay
    reserved for ``step_life``'s recorded deviations at rollover). Items
    still in their window or upcoming stay ``planned``; non-planned items
    are never touched — ``step_life``'s rollover draw still applies to
    whatever is left planned at day end.

    Returns ONLY the changed items; the caller persists each via
    ``store.update_agenda_item_status`` so the rendered state-card
    partition (which keys off the same window comparison) and the
    persisted status agree.
    """
    changed: list[AgendaItem] = []
    for item in agenda.items:
        if item.status != "planned":
            continue
        if int(item.start_t_h // 24) != day:
            continue
        if item.end_t_h <= t_h:
            changed.append(replace(item, status="completed"))
    return changed


def _recent_good_days(store: LifeStore, day: int) -> int:
    """Count meaningful recent companion events (plan §5-A2 T3 source 4).

    A ``day_finalized`` audit event inside the last ``_EVENT_WINDOW_DAYS``
    days with score >= ``_GOOD_DAY_SCORE`` counts as meaningful — a good
    recent day is an inspiration to start something new. Stores without the
    audit-log seam (``events_since``) contribute 0; the value is derived from
    persisted state only, so it is deterministic across restarts.
    """
    if not hasattr(store, "events_since"):
        return 0
    events_since = getattr(store, "events_since")
    good = 0
    for event in events_since(max(0, day - _EVENT_WINDOW_DAYS)):
        if event.get("event") != "day_finalized":
            continue
        score: float | None = None
        for part in str(event.get("detail") or "").split():
            if part.startswith("score="):
                try:
                    score = float(part.split("=", 1)[1])
                except ValueError:
                    score = None
        if score is not None and score >= _GOOD_DAY_SCORE:
            good += 1
    return good


def _maybe_spawn_arc(
    day: int,
    persona: PersonaProfile,
    arcs: list[LifeArc],
    store: LifeStore,
    rng: np.random.Generator,
) -> LifeArc | None:
    """Replenishment policy (plan §5-A2 T3, orchestrator invariant 9).

    ``N_active < N_MIN_ACTIVE`` -> P(spawn) > 0, evaluated on the POST-step
    state (the ``arcs`` argument is the day's updated list, so a day that
    completes its last arc spawns a replacement with certainty); with zero
    active arcs the spawn is certain, so active life never permanently dies.
    ``_SPAWN_PROB < 1`` keeps "not every completed arc creates another".
    Candidate interests, in pool order: descendants of prior COMPLETED arcs
    (the finished thread's interest, e.g. ``learn basic photography`` ->
    ``practice portrait photography``), the persona's ADJACENT interests,
    then the persona's own interests (any bucket); an arc whose interest
    already has an active arc is never duplicated. Meaningful recent
    companion events (``_recent_good_days``) raise the spawn probability.
    All draws come from the passed ``rng`` and happen after the day's other
    draws; ``None`` is returned (and NO draws are consumed) whenever the
    policy does not fire. The new arc is persisted before being returned.
    """
    active = [a for a in arcs if a.status == "active"]
    if len(active) >= _N_MIN_ACTIVE:
        return None

    prob = _SPAWN_PROB_EMPTY if not active else _SPAWN_PROB
    if _recent_good_days(store, day) > 0:
        prob = min(1.0, prob + _SPAWN_EVENT_BOOST)
    if float(rng.random()) >= prob:
        return None

    active_interests = {a.interest for a in arcs if a.status == "active"}
    seen: set[str] = set()
    pool: list[tuple[str, str]] = []  # (interest name, origin)

    for arc in store.list_life_arcs(status="completed"):  # descendants
        if arc.interest not in active_interests and arc.interest not in seen:
            pool.append((arc.interest, "descendant"))
            seen.add(arc.interest)
    for interest in persona.interests:  # adjacent interests
        if (
            interest.bucket == "adjacent"
            and interest.name not in active_interests
            and interest.name not in seen
        ):
            pool.append((interest.name, "adjacent"))
            seen.add(interest.name)
    for interest in persona.interests:  # companion interests (any bucket)
        if interest.name not in active_interests and interest.name not in seen:
            pool.append((interest.name, "companion"))
            seen.add(interest.name)
    if not pool:
        return None

    interest_name, origin = pool[int(rng.integers(len(pool)))]
    if origin == "descendant":
        templates = _SPAWN_DESCENDANT_TEMPLATES
        progress = _DESCENDANT_PROGRESS_MIN + float(rng.random()) * _DESCENDANT_PROGRESS_SPAN
    else:
        templates = _SPAWN_FRESH_TEMPLATES
        progress = _FRESH_PROGRESS_MIN + float(rng.random()) * _FRESH_PROGRESS_SPAN
    name = templates[int(rng.integers(len(templates)))].format(interest=interest_name)
    arc = LifeArc(
        id=f"arc_{day}_s0",
        name=name,
        interest=interest_name,
        started_day=day,
        progress=round(min(1.0, progress), 3),
        status="active",
        next_intention=_NEXT_INTENTIONS[int(rng.integers(len(_NEXT_INTENTIONS)))],
    )
    store.upsert_life_arc(arc)
    return arc


def _interest_by_name(persona: PersonaProfile, name: str) -> Interest | None:
    """Look up a persona interest by name (used to derive arc-item salience)."""
    for interest in persona.interests:
        if interest.name == name:
            return interest
    return None
