"""Proactive scheduler — plan + fire spontaneous messages (W-E2, A7).

Reuses the PROVEN composition from sim/run_events (envelope × phase × adj,
Weibull hazard + thinning, queue guards) instead of reimplementing the
process. Since A7 the timing feedback is LIVE: the runtime plans only the
CURRENT day with a per-day effective-scores array encoding the previous
day's real judge score and the day's behavioral initiative:

    h(tau,t) = h0(tau) * C(t) * P(t) * A(score_{d-1}) * I(t)

    A(s)   = adj_from_score(s) = clip(1 + ADJ_SLOPE·s, *adj_bounds)
             (the engine's monotone bounded previous-day adjustment)
    I(i)   = initiative_factor(i) = clip(exp(beta·(i-0.5)), *bounds)
             (mechanical multiplier from BehaviorDirective.initiative)
    scores[d-1] = (A(score_{d-1}) * I(d) - 1) / ADJ_SLOPE

so that the engine's own adj_from_score(scores[d-1]) reproduces the product
A(score_{d-1})·I(d) (clipped at adj_bounds). `scores=None` ⇒ adj ≡ 1 is kept
ONLY for tests/legacy callers — live scheduling (runtime._replan) always
passes a concrete array (never None).

Guards inherited from run_events.run:
  - zero events in quiet hours (envelope = 0 by construction);
  - min gap between accepted events (15 min default);
  - daily cap (3 default);
  - max-gap forcing (48 h) — if the hazard would let silence exceed it, a
    contact is forced at the first awake instant.

`ProactiveSchedule` tracks which planned events have fired; the async
runtime (harness/runtime.py) fires due events by pacing the virtual clock
to each event's hour (sim/run_async.py is the entrypoint).

Restart recovery (A7): `next_pending(t_h)` surfaces PENDING events with
event_time <= t_h (overdue-visible: at now == event_time the event MUST be
visible, and overdue rows are never stranded). The runtime then evaluates
each overdue event — still valid ⇒ fire, past its validity window ⇒ expire.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import sim.run_events as run_events
from engine.types import ADJ_SLOPE, DayRecord, PersonaParams, TimingParams
from harness.behavior import derive_behavior

#: A(s) — the previous-day score adjustment (monotone, bounded; engine-validated).
adj_from_score = run_events.adj_from_score

#: I(i) — initiative factor parameters: r_I = clip(exp(beta·(i-0.5)), *bounds).
INITIATIVE_BETA = 1.2
INITIATIVE_BOUNDS = (0.7, 1.3)

#: Representative hour used to derive the day's initiative from its
#: BehaviorDirective (initiative is hourly via circadian energy; the
#: scheduler is per-day, so the directive is sampled at the diurnal peak).
INITIATIVE_SAMPLE_HOUR = 14.0

#: Reason taxonomy (full DESIGN taxonomy; schedule | callback are the slice's
#: original members, the rest are added for gates + runtime).
REASON_SCHEDULE = "schedule"
REASON_CALLBACK = "callback"
REASON_EVENT = "event"
REASON_SHARED_INTEREST = "shared_interest"
REASON_CHECK_IN = "check_in"
VALID_REASONS = (REASON_SCHEDULE, REASON_CALLBACK, REASON_EVENT,
                 REASON_SHARED_INTEREST, REASON_CHECK_IN)
#: default validity window (hours) after the planned t_h, per reason
REASON_VALIDITY_H = {
    REASON_SCHEDULE: 3.0, REASON_CALLBACK: 6.0, REASON_EVENT: 4.0,
    REASON_SHARED_INTEREST: 12.0, REASON_CHECK_IN: 12.0,
}


def plan_proactive_events(
    days: int,
    seed: int,
    persona: PersonaParams,
    timing: TimingParams,
    scores: np.ndarray | None = None,
) -> np.ndarray:
    """Absolute hours (in [0, days*24)) of accepted proactive events.

    Deterministic given (seed, persona, timing, scores). `scores` optional
    per-day array feeding the adj term; None ⇒ adj ≡ 1 (tests/legacy only —
    live scheduling always passes `day_scores` output).
    """
    return run_events.run(days, seed, persona, timing, scores=scores)


def initiative_factor(
    initiative: float,
    *,
    beta: float = INITIATIVE_BETA,
    bounds: tuple[float, float] = INITIATIVE_BOUNDS,
) -> float:
    """I(i) — mechanical initiative multiplier: clip(exp(beta·(i-0.5)), *bounds).

    initiative=0.5 ⇒ 1.0 (neutral); higher initiative ⇒ factor > 1 (more
    frequent contact), lower ⇒ factor < 1. Monotone and bounded.
    """
    return float(np.clip(np.exp(beta * (initiative - 0.5)), *bounds))


def _record_from_row(row: dict) -> DayRecord:
    """Rebuild a DayRecord from a store daily_state row (same mapping as
    session._record_from_row; duplicated here to avoid an import cycle —
    session imports scheduler)."""
    return DayRecord(
        t=int(row["day"]),
        m=float(row["m"]),
        g=float(row["g"]),
        arg=float(row["arg"]),
        p=float(row["p"]),
        M=int(row["M"]),
        score=float(row["score"] or 0.0),
        mu=float(row["mu"]),
        eta=float(row["eta"]),
        cycle_day=float(row["cycle_day"]),
        phase_label=row["phase_label"],
        seed=int(row["seed"]),
    )


def day_initiative(store, day: int, timing: TimingParams, *, hour: float = INITIATIVE_SAMPLE_HOUR) -> float:
    """The day's initiative (0..1) from its stored BehaviorDirective.

    Mechanical path: load the day's daily_state (today's DayRecord exists —
    the runtime plans only the current day), derive the deterministic
    BehaviorDirective, return directive.initiative. Missing state (should not
    happen for the current day) degrades to the neutral 0.5.
    """
    row = store.load_daily_state(day)
    if row is None:
        return 0.5
    prev_row = store.load_daily_state(day - 1)
    directive = derive_behavior(
        _record_from_row(row),
        timing,
        hour=hour,
        previous=_record_from_row(prev_row) if prev_row is not None else None,
    )
    return float(directive.initiative)


def day_scores(store, current_day: int, timing: TimingParams) -> np.ndarray:
    """Effective per-day scores array for a plan covering days 0..current_day.

    scores[i] = (A(score_i) · I(i+1) − 1) / ADJ_SLOPE for i < current_day,
    where score_i is the REAL judge score of day i (store.load_judgement;
    missing ⇒ A=1.0 neutral) and I(i+1) is day i+1's initiative factor.
    scores[current_day] is an unused placeholder (the engine reads
    scores[day-1], and day 0's adj is 1 by construction). The engine's
    adj_from_score(scores[d-1]) then equals clip(A(score_{d-1})·I(d), bounds)
    — the A·I term of the A7 hazard modulator. Deterministic, and stable
    across replans: entry i is fixed once day i is judged (score_i) and day
    i+1's state exists (initiative_i+1), both true the first time the plan
    covers day i+1, so re-planning never drifts already-persisted rows.
    """
    n = current_day + 1
    scores = np.zeros(n, dtype=float)
    for i in range(current_day):
        judgement = store.load_judgement(i)
        a = adj_from_score(float(judgement["score"]) if judgement else None, timing)
        init = day_initiative(store, i + 1, timing)
        scores[i] = (a * initiative_factor(init) - 1.0) / ADJ_SLOPE
    return scores


@dataclass
class ProactiveSchedule:
    """Planned event times + fire bookkeeping."""

    event_hours: np.ndarray
    _fired: set[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._fired is None:
            self._fired = set()

    @classmethod
    def plan(
        cls,
        days: int,
        seed: int,
        persona: PersonaParams,
        timing: TimingParams,
        scores: np.ndarray | None = None,
    ) -> "ProactiveSchedule":
        return cls(event_hours=plan_proactive_events(days, seed, persona, timing, scores))

    def due_at(self, t_h: float) -> list[float]:
        """Planned event hours <= t_h that have not fired yet, ascending."""
        due = [
            float(h) for h in self.event_hours if h <= t_h and h not in self._fired
        ]
        return sorted(due)

    def mark_fired(self, t_h: float) -> None:
        self._fired.add(float(t_h))

    @classmethod
    def plan_and_persist(cls, days, seed, persona, timing, store, *,
                         reason: str = REASON_SCHEDULE,
                         scores=None) -> "ProactiveSchedule":
        """plan() then store.save_schedule_events(seed, [{t_h, day, reason} ...]).
        Idempotent (INSERT OR IGNORE). Returns a schedule whose _fired set is
        pre-seeded from the store: any planned hour whose row is no longer
        'pending' (i.e. already fired/expired) is treated as fired."""
        schedule = cls.plan(days, seed, persona, timing, scores=scores)
        events = [
            {"t_h": float(h), "day": int(h // 24.0), "reason": reason}
            for h in schedule.event_hours
        ]
        store.save_schedule_events(seed, events)
        pending = {float(r["t_h"]) for r in store.pending_schedule_events(seed)}
        schedule._fired = {
            float(h) for h in schedule.event_hours if float(h) not in pending
        }
        return schedule

    @classmethod
    def restore(cls, seed, store) -> "ProactiveSchedule":
        """Rebuild from store: event_hours = all rows' t_h for seed; _fired =
        every row whose status != 'pending'. For restart-resume without re-planning."""
        rows = store.schedule_events_for_seed(seed)
        event_hours = np.asarray([float(r["t_h"]) for r in rows])
        fired = {float(r["t_h"]) for r in rows if r["status"] != "pending"}
        return cls(event_hours=event_hours, _fired=fired)

    def mark_fired_persisted(self, t_h: float, fired_t_h: float, seed: int,
                             store) -> None:
        """self.mark_fired(t_h) + store.mark_schedule_fired(seed, t_h, fired_t_h)."""
        self.mark_fired(t_h)
        store.mark_schedule_fired(seed, t_h, fired_t_h)

    def next_pending(self, t_h: float) -> float | None:
        """Earliest pending event hour due at `t_h`, else the earliest
        pending future hour; None when nothing is pending.

        A7 restart fix: pending events with event_time <= t_h are VISIBLE
        (at now == event_time the event must be found, and overdue rows are
        never stranded). Overdue events are returned first — the runtime
        evaluates each (still valid ⇒ fire, past validity ⇒ expire).
        """
        pending = [float(h) for h in self.event_hours if h not in self._fired]
        overdue = [h for h in pending if h <= t_h]
        if overdue:
            return min(overdue)
        return min(pending) if pending else None
