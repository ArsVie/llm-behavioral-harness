"""Persistent life simulation — life arcs, daily agenda, current activity.

Public entry points: ``init_life``, ``generate_agenda``, ``step_life`` (injected
store + seeded rng). Draws come from ``stream_rng(seed, LIFE_STREAM)`` for init
and ``stream_rng(seed, LIFE_STREAM, day)`` per day — never ``day_rng``.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np

from engine.rng import stream_rng

from harness.domain import (
    AgendaItem,
    CurrentActivity,
    DailyAgenda,
    LifeArc,
    PersonaProfile,
)

#: Reserved RNG stream key for the LIFE stream.
LIFE_STREAM = 4

#: Day length in hours and the awake window bounds for agenda items.
DAY_HOURS = 24.0
AWAKE_START_H = 8.0
AWAKE_END_H = 23.0

#: Daily probability that an active arc is abandoned.
_ABANDON_PROB = 0.005
#: Progress one completed session gives its arc: ``0.03 + rng.random() * 0.04``.
_PROGRESS_MIN = 0.03
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

#: A taste is enjoyed, never practised: its own fallback templates.
_TASTE_ACTIVITIES = (
    "some {interest}",
    "catch up on {interest}",
    "unwind with {interest}",
)

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
    def save_agenda(self, day: int, agenda: DailyAgenda) -> None: ...
    def load_agenda(self, day: int) -> DailyAgenda | None: ...
    def update_agenda_item_status(self, item_id: str, status: str) -> None: ...
    def list_agenda_items(
        self, day: int | None = None, status: str | None = None
    ) -> list[AgendaItem]: ...


@dataclass(frozen=True)
class LifeStepResult:
    """Outcome of one day's life step: the persisted post-step state."""

    updated_arcs: list[LifeArc]
    agenda: DailyAgenda


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


#: Hobby sessions on a day with her commitment, and on a free day.
HOBBIES_ON_COMMITMENT_DAY = 2
HOBBIES_ON_FREE_DAY = 3
#: An interest with an active project comes up once more a week, and at
#: least this often: a project is what she is working on.
ARC_EXTRA_PER_WEEK = 1
ARC_MIN_PER_WEEK = 3

#: Fixed commitments skip rarely, and the day records why.
COMMITMENT_SKIP_PROB = 0.03
COMMITMENT_SKIP_CAUSE = {"job": "called in sick", "school": "class cancelled",
                         "home": "took the day off"}

#: At most one errand a day, afternoon to evening.
ERRAND_PROB = 0.4
ERRANDS = ("groceries", "laundry", "a trip to the pharmacy",
           "tidying the flat", "the post office")
ERRAND_WINDOW = (15.0, 20.0)
ERRAND_MIN = (30, 60)
ERRAND_SALIENCE = 0.25
COMMITMENT_SALIENCE = 0.6

#: Starts sit on a 15-minute grid.
GRID_H = 0.25

#: Source types that are background: no pop-up, no decision.
BACKGROUND_SOURCES = frozenset({"errand"})


def _on_grid(t: float) -> float:
    return round(t / GRID_H) * GRID_H


def _fits(taken: list[tuple[float, float]], start: float, end: float) -> bool:
    return all(end <= a or start >= b for a, b in taken)


def _place(rng, taken: list[tuple[float, float]], window: tuple[float, float],
           length: float) -> float | None:
    """A free start on the grid with the whole session inside ``window``
    (local hours) and waking hours, or None. One draw picks among the fits."""
    low = max(window[0], AWAKE_START_H)
    high = min(window[1], AWAKE_END_H) - length
    fits = []
    t = _on_grid(low)
    while t <= high + 1e-9:
        if t >= low - 1e-9 and _fits(taken, t, t + length):
            fits.append(t)
        t += GRID_H
    if not fits:
        return None
    return fits[int(rng.integers(len(fits)))]


def _minutes(rng, span: tuple[int, int]) -> float:
    low, high = span
    return round((low + float(rng.random()) * (high - low)) / 5.0) * 5.0 / 60.0


def weekday_index_of(name: str, day: int) -> int:
    """0 = Monday; an unknown name counts days from a Monday."""
    days = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")
    name = (name or "").lower()
    return days.index(name) if name in days else day % 7


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
    plan=None,
    seed: int = 0,
) -> DailyAgenda:
    """Build and persist the day's agenda from her saved habits.

    In placement order: her commitment (job, school or housework, on its
    days, rarely skipped with the cause recorded), her routines, at most one
    errand, then one to three hobby sessions in the gaps. A hobby comes up
    by its weekly frequency; an interest with an active project comes up as
    that project. Each session lasts what its kind and stage say, starts on
    a 15-minute grid inside its part of the day, and nothing overlaps.

    ``plan`` is the persona's :class:`harness.habits.LifePlan`; without one
    it is derived from ``seed``. All draws come from ``rng`` — callers hand
    in ``stream_rng(seed, LIFE_STREAM, day)``, never ``day_rng``. The planner
    (optional) names the hobby sessions after every draw, so the seeded
    schedule is identical with and without it.
    """
    from harness import habits as hb

    if plan is None:
        plan = hb.derive_plan(seed, persona)
    day_start = day * DAY_HOURS
    items: list[AgendaItem] = []
    taken: list[tuple[float, float]] = []

    def add(item_id, start_local, length, activity, source_type, source_id,
            salience, status="planned", outcome=None):
        start = day_start + start_local
        end = min(start + length, day_start + AWAKE_END_H)
        items.append(AgendaItem(
            id=item_id, start_t_h=round(start, 4), end_t_h=round(end, 4),
            activity=activity, source_type=source_type, source_id=source_id,
            salience=round(salience, 3), status=status, outcome=outcome,
        ))
        if status == "planned":
            taken.append((start_local, start_local + length))

    weekday_index = weekday_index_of(weekday, day)
    commitment = plan.commitment
    if commitment.on(weekday_index):
        skipped = float(rng.random()) < COMMITMENT_SKIP_PROB
        add(f"ag_{day}_c", commitment.start_h, commitment.hours,
            commitment.text, "commitment", commitment.kind,
            COMMITMENT_SALIENCE,
            status="skipped" if skipped else "planned",
            outcome=COMMITMENT_SKIP_CAUSE[commitment.kind] if skipped else None)
    busy_day = any(it.source_type == "commitment" and it.status == "planned"
                   for it in items)

    routined: set[str] = set()
    for routine in persona.routines:
        if rng.random() >= routine.cadence:
            continue
        start = max(_on_grid(routine.start_frac * DAY_HOURS), AWAKE_START_H)
        length = min(routine.duration_h, AWAKE_END_H - start)
        if length <= 0 or not _fits(taken, start, start + length):
            continue
        add(f"ag_{day}_r_{len(items):02d}", start, length, routine.name,
            "routine", routine.name, routine.salience)
        routined.add(routine.name)

    if float(rng.random()) < ERRAND_PROB:
        errand = ERRANDS[int(rng.integers(len(ERRANDS)))]
        length = _minutes(rng, ERRAND_MIN)
        start = _place(rng, taken, ERRAND_WINDOW, length)
        if start is not None:
            add(f"ag_{day}_e", start, length, errand, "errand", errand,
                ERRAND_SALIENCE)

    arc_for = {}
    for arc in started_arcs(arcs, day):
        arc_for.setdefault(arc.interest, arc)
    # An interest her routine already covers today (math practice ->
    # mathematics) gets no second session; its project still can.
    pool = [i for i in persona.interests
            if i.salience > 0 and i.name in plan.habits
            and (i.name in arc_for
                 or not any(routine_covers(r, i.name) for r in routined))]
    def weight(interest):
        return max(float(interest.salience), 1e-3) * BUCKET_WEIGHT.get(interest.bucket, 1.0)

    picked = []
    for interest in sorted(pool, key=lambda i: i.name):
        habit = plan.habits[interest.name]
        per_week = habit.per_week
        if interest.name in arc_for:
            per_week = max(per_week + ARC_EXTRA_PER_WEEK, ARC_MIN_PER_WEEK)
        chance = per_week / 7.0 * BUCKET_WEIGHT.get(interest.bucket, 1.0)
        if float(rng.random()) < min(1.0, chance):
            picked.append(interest)
    cap = HOBBIES_ON_COMMITMENT_DAY if busy_day else HOBBIES_ON_FREE_DAY
    # None came up: one still happens. More than the day holds: a weighted
    # draw without replacement keeps a low-weight interest in the running.
    keep = min(len(picked), cap)
    if not picked:
        picked, keep = list(pool), min(1, len(pool))
    if picked:
        weights = [weight(i) for i in picked]
        total = sum(weights)
        order = rng.choice(len(picked), size=keep, replace=False,
                           p=[w / total for w in weights])
        picked = [picked[int(i)] for i in order]
    for interest in picked:
        habit = plan.habits[interest.name]
        length = hb.duration_h(habit, rng)
        low, high = habit.window
        start = _place(rng, taken, (low, high), length)
        if start is None:  # a long session or a full window: widen once
            start = _place(rng, taken, (low - 2.0, high + 2.0), length)
        if start is None:
            continue
        arc = arc_for.get(interest.name)
        if arc is not None:
            add(f"ag_{day}_a_{arc.id}", start, length, arc.next_intention,
                "arc", arc.id, interest.salience)
        else:
            templates = (_TASTE_ACTIVITIES if habit.kind == hb.TASTE
                         else _INTEREST_ACTIVITIES)
            add(f"ag_{day}_i_{interest.name}", start, length,
                templates[int(rng.integers(len(templates)))]
                .format(interest=interest.name),
                "interest", interest.name, interest.salience)

    items.sort(key=lambda it: it.start_t_h)
    items = _apply_plan(
        day, persona, arcs, items, store,
        planner_client=planner_client, fork=fork, weekday=weekday,
        logger=logger, plan=plan,
    )
    agenda = DailyAgenda(day=day, items=tuple(items))
    store.save_agenda(day, agenda)
    return agenda


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 4]


def routine_covers(routine_name: str, interest_name: str) -> bool:
    """Whether a routine is a session of an interest: a word of one opens a
    word of the other ("math practice" / "mathematics", "read history" /
    "history")."""
    return any(a.startswith(b) or b.startswith(a)
               for a in _words(routine_name) for b in _words(interest_name))


def started_arcs(arcs, day: int) -> list[LifeArc]:
    """The arcs she is working on by ``day``: active and already begun. An
    arc seeded to start later is invisible to her and the planner until then."""
    return [a for a in arcs if a.status == "active" and a.started_day <= day]


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
    plan=None,
) -> list[AgendaItem]:
    """Replace template activity text with planned text, where available.

    Only hobby sessions (arc and interest items) are planned; the commitment,
    routines and errands are what they are. The planner is told each
    session's interest, kind, stage and hours — never the bucket. Returns the
    items unchanged whenever planning does not happen. Runs AFTER every RNG
    draw, so a planner never perturbs the seeded schedule.
    """
    if planner_client is None:
        return items
    from harness import day_planner as planner
    from harness.clock import hhmm

    habits = plan.habits if plan is not None else {}
    arc_by_id = {a.id: a for a in arcs}
    slots: list[planner.PlanSlot] = []
    indices: list[int] = []
    fixed: list[str] = []
    for index, item in enumerate(items):
        hours = f"{hhmm(item.start_t_h)}–{hhmm(item.end_t_h)}"
        if item.source_type not in ("arc", "interest"):
            if item.status == "planned":
                fixed.append(f"{item.activity}, {hours}")
            continue
        arc = arc_by_id.get(item.source_id) if item.source_type == "arc" else None
        interest = arc.interest if arc is not None else item.source_id
        habit = habits.get(interest)
        level = f"{habit.stage} ({habit.kind})" if habit is not None else ""
        label = ", ".join(x for x in (interest, level, hours) if x)
        if arc is not None:
            label += f", her project '{arc.name}', next: {arc.next_intention}"
        slots.append(planner.PlanSlot(
            source_type=item.source_type, source_id=item.source_id,
            fallback=item.activity, label=label,
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
        name=persona.name, weekday=weekday, arcs=started_arcs(arcs, day),
        slots=slots,
        outcomes=outcomes, client=planner_client, fork=fork, logger=logger,
        fixed=fixed,
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


def _step_arc(arc: LifeArc, day: int, sessions: int, store: LifeStore,
              rng: np.random.Generator) -> LifeArc:
    """One day of an arc: each session she actually did moves it
    ``_PROGRESS_MIN + rng.random() * _PROGRESS_SPAN``; a day without one
    moves nothing. Reaching 1.0 completes the arc; an active arc is then
    abandoned with ``_ABANDON_PROB``. An arc that is not active or has not
    started yet is returned untouched and consumes NO draws.
    """
    if arc.status != "active" or arc.started_day > day:
        return arc
    progress = arc.progress
    for _ in range(sessions):
        progress += _PROGRESS_MIN + float(rng.random()) * _PROGRESS_SPAN
    progress = min(1.0, progress)
    status = "completed" if progress >= 1.0 else arc.status
    if status == "active" and rng.random() < _ABANDON_PROB:
        status = "abandoned"
    new_arc = replace(arc, progress=round(progress, 3), status=status)
    store.upsert_life_arc(new_arc)
    return new_arc


def _step_item(item: AgendaItem, store: LifeStore) -> AgendaItem:
    """Resolve an item still planned at the day's end: it happened. A skip
    only ever comes from a cause — her own decision, or the commitment's
    rare recorded one — never from a flat draw."""
    if item.status != "planned":
        return item
    new_item = replace(item, status="completed")
    store.update_agenda_item_status(new_item.id, "completed")
    return new_item


def step_life(
    day: int,
    persona: PersonaProfile,
    arcs: list[LifeArc],
    agenda: DailyAgenda,
    store: LifeStore,
    rng: np.random.Generator,
    *,
    plan=None,
) -> LifeStepResult:
    """Advance one day from what happened in it.

    * Items still planned at the day's end completed (``_step_item``).
    * Arcs move by the sessions she completed for them today
      (``_step_arc``); arcs with a future ``started_day`` are untouched.
    * Habits (with ``plan``): each completed hobby session counts toward its
      interest's next stage, saved with the plan.
    * Replenishment: see ``_maybe_spawn_arc``; its roll and draws happen
      AFTER all other draws of the day.

    Draw order is fixed (arcs in given order, then the optional
    replenishment roll), so the result is deterministic per (seed, day)
    when ``rng`` is ``stream_rng(seed, LIFE_STREAM, day)``.
    """
    updated_items = [_step_item(item, store) for item in agenda.items]
    done_for_arc: dict[str, int] = {}
    done_for_interest: dict[str, int] = {}
    arc_interest = {a.id: a.interest for a in arcs}
    for item in updated_items:
        if item.status != "completed":
            continue
        if item.source_type == "arc":
            done_for_arc[item.source_id] = done_for_arc.get(item.source_id, 0) + 1
            name = arc_interest.get(item.source_id)
        elif item.source_type == "interest":
            name = item.source_id
        else:
            continue
        if name:
            done_for_interest[name] = done_for_interest.get(name, 0) + 1
    updated_arcs = [
        _step_arc(arc, day, done_for_arc.get(arc.id, 0), store, rng)
        for arc in arcs
    ]
    if plan is not None and done_for_interest:
        from harness import habits as hb

        for name, count in done_for_interest.items():
            habit = plan.habits.get(name)
            if habit is not None:
                plan.habits[name] = hb.record_sessions(habit, count)
        hb.save_habits(store, plan.habits)
    updated_agenda = DailyAgenda(day=agenda.day, items=tuple(updated_items))

    spawned = _maybe_spawn_arc(day, persona, updated_arcs, store, rng)
    if spawned is not None:
        updated_arcs.append(spawned)

    return LifeStepResult(updated_arcs=updated_arcs, agenda=updated_agenda)


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
    events_since = getattr(store, "events_since", None)
    if events_since is None:
        return 0
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
