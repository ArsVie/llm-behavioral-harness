"""Persistent life simulation — life arcs, daily agenda, current activity.

Public entry points: ``init_life``, ``generate_agenda``, ``step_life`` (injected
store + seeded rng). Draws come from ``stream_rng(seed, LIFE_STREAM)`` for init
and ``stream_rng(seed, LIFE_STREAM, day)`` per day — never ``day_rng``.
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

#: Per-bucket multiplier on an interest's salience when drawing the day's
#: standalone interest items; the independent slice is nudged up to be visible.
BUCKET_WEIGHT: dict[str, float] = {
    "exact": 1.0,
    "adjacent": 1.0,
    "independent": 1.5,
}

#: Standalone interest-activity templates; one draw per interest item.
#: FALLBACK ONLY: ``harness.day_planner`` supplies the text when available.
_INTEREST_ACTIVITIES = (
    "read about {interest}",
    "practice {interest}",
    "watch a video on {interest}",
    "try a small {interest} exercise",
    "plan a {interest} session",
)


class LifeStore(Protocol):
    """Store seam subset used by life.py.

    Implemented by the SQLite store; tests use a seam-faithful fake.
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
    """Outcome of one day's life step.

    ``updated_arcs`` and ``agenda`` are the persisted post-step state. With
    ``t_h`` (NOW semantics), ``current_activity`` is the item active at
    ``t_h``, ``None`` when nothing is active; without it, the day-level MAIN
    activity — the highest-salience completed item, falling back to the
    highest-salience item when nothing completed; ``None`` for an empty agenda.
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

    Deterministic per ``seed`` (draws from ``stream_rng(seed, LIFE_STREAM)``,
    never ``day_rng``). Arc interests are drawn without replacement, weighted
    by salience, so every arc is tied to a real persona interest; the first arc
    starts nearer completion (progress 0.70-0.85), the others at 0.00-0.35.
    Each arc is persisted via ``store.upsert_life_arc`` before being returned.

    ``epoch`` (default 0) prefixes arc ids (``arc_<epoch>_<i>``) so a later
    seeding never reuses an id from a wiped generation; callers derive it from
    the store's persisted state.
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
    *,
    planner_client=None,
    fork=None,
    weekday: str = "today",
    logger=None,
) -> DailyAgenda:
    """Build and persist the day's agenda, predominantly from persona sources.

    Sources in draw order: each routine whose cadence draw succeeds; each
    ACTIVE arc that has already STARTED (``started_day <= day``), with
    probability 0.8 — a future ``started_day`` must NOT generate activities
    before its start; then exactly 2 standalone interest items, weighted by
    ``salience * BUCKET_WEIGHT[bucket]``.

    Items live inside the awake window, are sorted by start time, carry status
    "planned" and are persisted via ``store.save_agenda``. All draws come from
    the passed ``rng`` — callers must hand in ``stream_rng(seed, LIFE_STREAM,
    day)``, never ``day_rng``. ``planner_client`` (optional) hands the arc and
    interest slots to ``harness.day_planner`` for concrete activity text; the
    engine keeps selection, windows, salience and ids either way, any planner
    failure keeps the template text, and the RNG draw order is identical with
    and without a planner.
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
    weights = [
        max(float(i.salience), 1e-3) * BUCKET_WEIGHT.get(i.bucket, 1.0)
        for i in pool
    ]
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

    items = _apply_plan(
        day, persona, arcs, items, store,
        planner_client=planner_client, fork=fork, weekday=weekday,
        logger=logger,
    )
    items.sort(key=lambda it: it.start_t_h)
    agenda = DailyAgenda(day=day, items=tuple(items))
    store.save_agenda(day, agenda)
    return agenda


def _apply_plan(
    day: int,
    persona: PersonaProfile,
    arcs: list[LifeArc],
    items: list[AgendaItem],
    store: LifeStore,
    *,
    planner_client,
    fork=None,
    weekday: str,
    logger,
) -> list[AgendaItem]:
    """Replace template activity text with planned text, where available.

    Only arc and interest items are planned; a routine is the same thing every
    day by definition. Returns the items unchanged whenever planning does not
    happen. Runs AFTER every RNG draw, so a planner never perturbs the seeded
    schedule.
    """
    if planner_client is None:
        return items
    from harness import day_planner as planner

    slots: list[planner.PlanSlot] = []
    indices: list[int] = []
    by_name = {i.name: i for i in persona.interests}
    arc_by_id = {a.id: a for a in arcs}
    for index, item in enumerate(items):
        if item.source_type == "arc":
            arc = arc_by_id.get(item.source_id)
            label = (
                f"her project '{arc.name}' (next: {arc.next_intention})"
                if arc is not None else f"her project {item.source_id}"
            )
            slots.append(planner.PlanSlot(
                source_type="arc", source_id=item.source_id,
                fallback=item.activity, label=label,
            ))
            indices.append(index)
        elif item.source_type == "interest":
            interest = by_name.get(item.source_id)
            bucket = interest.bucket if interest is not None else None
            tag = "HERS" if bucket == "independent" else "SHARED"
            slots.append(planner.PlanSlot(
                source_type="interest", source_id=item.source_id,
                fallback=item.activity,
                label=f"her interest in {item.source_id} ({tag})",
                bucket=bucket,
            ))
            indices.append(index)
    if not slots:
        return items

    outcomes = []
    getter = getattr(store, "recent_outcomes", None)
    if getter is not None:
        try:
            outcomes = getter(before_day=day, limit=planner.OUTCOME_CONTEXT)
        except Exception:  # never block on continuity
            outcomes = []

    planned = planner.plan_day(
        name=persona.name, weekday=weekday, arcs=arcs, slots=slots,
        outcomes=outcomes, client=planner_client, fork=fork, logger=logger,
    )
    if planned is None:
        return items
    out = list(items)
    filled = 0
    for index, text in zip(indices, planned):
        if text:
            out[index] = replace(out[index], activity=text)
            filled += 1
    if logger is not None:
        logger(f"day planner: {filled}/{len(slots)} activities planned")
    return out


def _step_arc(arc: LifeArc, day: int, store: LifeStore,
              rng: np.random.Generator) -> LifeArc:
    """One day of progress for one arc.

    An arc that is not active or has not started yet is returned untouched and
    consumes NO draws. Reaching 1.0 completes the arc; an active arc is then
    abandoned with ``_ABANDON_PROB``.
    """
    if arc.status != "active" or arc.started_day > day:
        return arc
    progress = min(
        1.0, arc.progress + _PROGRESS_MIN + float(rng.random()) * _PROGRESS_SPAN
    )
    status = "completed" if progress >= 1.0 else arc.status
    if status == "active" and rng.random() < _ABANDON_PROB:
        status = "abandoned"
    new_arc = replace(arc, progress=round(progress, 3), status=status)
    store.upsert_life_arc(new_arc)
    return new_arc


def _step_item(item: AgendaItem, store: LifeStore,
               rng: np.random.Generator) -> AgendaItem:
    """Resolve one planned agenda item: ~80% completed, ~10% skipped,
    ~10% shifted. An item that is no longer ``planned`` is already resolved
    and consumes no draw."""
    if item.status != "planned":
        return item
    draw = float(rng.random())
    if draw < 0.80:
        status = "completed"
    elif draw < 0.90:
        status = "skipped"
    else:
        status = "shifted"
    new_item = replace(item, status=status)
    store.update_agenda_item_status(new_item.id, status)
    return new_item


def _current_activity_for(agenda: DailyAgenda, items: list[AgendaItem],
                          t_h: float | None) -> "CurrentActivity | None":
    """What she is doing, under whichever of the two semantics applies.

    With ``t_h`` (NOW semantics) only an item actually in progress counts — a
    plan for later today is not what she is doing now. Without it (legacy seam
    callers) the answer is the day's MAIN activity: the highest-salience
    completed item, falling back to the highest-salience item when nothing
    completed.
    """
    if t_h is not None:
        return current_activity_now(agenda, t_h)
    completed = [it for it in items if it.status == "completed"]
    candidates = completed or items
    if not candidates:
        return None
    main = max(candidates, key=lambda it: (it.salience, it.start_t_h))
    return CurrentActivity(
        t_h=main.start_t_h, item=main, description=main.activity
    )


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

    * Arcs: each ACTIVE, already-STARTED arc gains ``0.01 + rng.random() *
      0.04``, completing at 1.0; a small daily chance abandons it. Arcs with a
      future ``started_day`` are untouched (no progress, no status change, no
      draws). Persisted via ``store.upsert_life_arc``.
    * Items: planned items deviate modestly — ~80% completed, ~10% skipped,
      ~10% shifted — persisted via ``store.update_agenda_item_status``.
    * Replenishment: see ``_maybe_spawn_arc``; the spawn roll and its draws
      happen AFTER all other draws of the day, so a day with enough active
      arcs is byte-identical to the pre-replenishment behaviour.
    * ``current_activity`` follows the ``t_h`` / no-``t_h`` semantics of
      ``_current_activity_for``.

    Draw order is fixed (started arcs in given order, then items in agenda
    order, then the optional replenishment roll), so the outcome is
    deterministic per (seed, day) when ``rng`` is
    ``stream_rng(seed, LIFE_STREAM, day)``.
    """
    updated_arcs = [_step_arc(arc, day, store, rng) for arc in arcs]
    updated_items = [_step_item(item, store, rng) for item in agenda.items]
    updated_agenda = DailyAgenda(day=agenda.day, items=tuple(updated_items))
    current = _current_activity_for(updated_agenda, updated_items, t_h)

    spawned = _maybe_spawn_arc(day, persona, updated_arcs, store, rng)
    if spawned is not None:
        updated_arcs.append(spawned)

    return LifeStepResult(
        updated_arcs=updated_arcs, agenda=updated_agenda, current_activity=current
    )


def current_activity_now(agenda: DailyAgenda, t_h: float) -> CurrentActivity | None:
    """NOW semantics: the item actually in progress at ``t_h``.

    In progress means ``start_t_h <= t_h < end_t_h`` and not skipped/shifted;
    the highest salience wins when several overlap. ``None`` when nothing is
    active — a future plan never becomes the current activity. Pure function:
    no rng, no persistence.
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
    """Deterministic planned→completed transition as windows pass.

    Pure function of (item window, t_h, day) — no wall clock, no rng, no
    store. Every item of ``day``'s agenda whose window has FULLY passed
    (``end_t_h <= t_h``) while still ``planned`` becomes ``completed``;
    ``skipped``/``shifted`` stay reserved for ``step_life``'s recorded
    rollover deviations, and non-planned items are never touched.

    Returns ONLY the changed items; the caller persists each via
    ``store.update_agenda_item_status``.
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
    """Count ``day_finalized`` audit events in the last ``_EVENT_WINDOW_DAYS``
    days with score >= ``_GOOD_DAY_SCORE``. Stores without the audit-log seam
    (``events_since``) contribute 0; derived from persisted state only.
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
    """Replenishment policy: never let active life permanently die.

    While ``N_active < N_MIN_ACTIVE`` the policy spawns a replacement arc with
    probability ``_SPAWN_PROB`` (certain when nothing is active, higher after
    recent good days), evaluated on the POST-step ``arcs`` list. Candidate
    interests, in pool order: descendants of prior COMPLETED arcs, the
    persona's ADJACENT interests, then its remaining interests — one already
    carried by an active arc is never duplicated. All draws come from the
    passed ``rng`` and happen after the day's other draws; ``None`` is
    returned (and NO draws consumed) whenever the policy does not fire. The
    new arc is persisted before being returned.
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
    pool = _spawn_pool(persona, store, active_interests)
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


def _spawn_pool(persona: PersonaProfile, store: LifeStore,
                active_interests: set) -> list[tuple[str, str]]:
    """Interests a replacement arc could come from, as (name, origin).

    Fixed source order — descendants of completed arcs, then the persona's
    ADJACENT interests, then any remaining persona interest. Order matters
    twice over: it decides which origin an interest is tagged with when it
    appears in more than one source, and the pool is indexed by a keyed draw,
    so reordering it changes every future spawn. Interests already carried by
    an active arc are excluded.
    """
    seen: set[str] = set()
    pool: list[tuple[str, str]] = []
    for arc in store.list_life_arcs(status="completed"):
        if arc.interest not in active_interests and arc.interest not in seen:
            pool.append((arc.interest, "descendant"))
            seen.add(arc.interest)
    for interest in persona.interests:
        if (
            interest.bucket == "adjacent"
            and interest.name not in active_interests
            and interest.name not in seen
        ):
            pool.append((interest.name, "adjacent"))
            seen.add(interest.name)
    for interest in persona.interests:
        if interest.name not in active_interests and interest.name not in seen:
            pool.append((interest.name, "companion"))
            seen.add(interest.name)
    return pool


def _interest_by_name(persona: PersonaProfile, name: str) -> Interest | None:
    """Look up a persona interest by name (used to derive arc-item salience)."""
    for interest in persona.interests:
        if interest.name == name:
            return interest
    return None
