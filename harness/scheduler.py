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

`ProactiveSchedule` tracks which planned events have fired; the CLI fires due
events by advancing the virtual clock to each event's hour.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import sim.run_events as run_events
from engine.types import PersonaParams, TimingParams

#: Reason taxonomy (slice scope: schedule | callback per research/06 §12b).
REASON_SCHEDULE = "schedule"
REASON_CALLBACK = "callback"
VALID_REASONS = (REASON_SCHEDULE, REASON_CALLBACK)


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

    def next_pending(self, t_h: float) -> float | None:
        """First planned hour > t_h, or None."""
        for h in self.event_hours:
            if h > t_h and h not in self._fired:
                return float(h)
        return None
