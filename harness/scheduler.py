"""Proactive scheduler — plan + fire spontaneous messages (W-E2).

Reuses the PROVEN composition from sim/run_events (envelope × phase × adj,
Weibull hazard + thinning, queue guards) instead of reimplementing the
process. The slice's simplification: the schedule is planned up front for the
run horizon with `scores=None` (adj ≡ 1); the live judge-score feedback on
frequency is a documented follow-up (the adj mechanics themselves are already
validated by experiment w34).

Guards inherited from run_events.run:
  - zero events in quiet hours (envelope = 0 by construction);
  - min gap between accepted events (15 min default);
  - daily cap (3 default);
  - max-gap forcing (48 h) — if the hazard would let silence exceed it, a
    contact is forced at the first awake instant.

`ProactiveSchedule` tracks which planned events have fired; the async
runtime (harness/runtime.py) fires due events by pacing the virtual clock
to each event's hour (sim/run_async.py is the entrypoint).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import sim.run_events as run_events
from engine.types import PersonaParams, TimingParams

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

    Deterministic given (seed, persona, timing). `scores` optional per-day
    array feeding the adj term; None ⇒ adj ≡ 1 (slice default).
    """
    return run_events.run(days, seed, persona, timing, scores=scores)


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
        """First planned hour > t_h, or None."""
        for h in self.event_hours:
            if h > t_h and h not in self._fired:
                return float(h)
        return None
