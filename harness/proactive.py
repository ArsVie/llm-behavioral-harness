"""Grounded proactive intent resolution (A7; it2 A3).

Separates the CONTACT OPPORTUNITY (when the Weibull process says "she feels
like contacting around now") from the CONTACT REASON (why). At OPPORTUNITY
time the runtime asks :class:`IntentResolver` for a grounded
:class:`ProactiveIntent` — ``resolve(opportunity)`` resolves against the
opportunity's desired time, links the intent back via ``opportunity_id``,
and bounds the intent's validity by the opportunity's own window; no
grounded candidate ⇒ ``None`` ⇒ the runtime SUPPRESSES the event
(``no_grounded_reason`` is a legitimate outcome, never an error).

Candidates are STORE-BACKED ONLY — no imports from life.py/memory.py: the
store seam provides every source (agenda items, completed agenda items as
companion life events, CALLBACK memories, shared-interest memories tagged
with persona interest names, and legitimate check-in context). The hook is
COMPOSED DETERMINISTICALLY from source fields (:func:`compose_hook`) —
never invented free text — so the content gate can re-derive it and verify
the intent is attached to a real source.

Ranking: salience × recency × validity, with seeded tie-breaks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

import engine.rng as rng_mod
from harness.domain import (
    AgendaItem,
    ContactOpportunity,
    EpisodicMemory,
    LifeArc,
    MemoryKind,
    ProactiveIntent,
)
from harness.scheduler import (
    REASON_CALLBACK,
    REASON_CHECK_IN,
    REASON_EVENT,
    REASON_SCHEDULE,
    REASON_SHARED_INTEREST,
    REASON_VALIDITY_H,
)

#: source_type values stored on ProactiveIntent (A2 resolve_intent_source keys).
SOURCE_AGENDA = "agenda_item"
SOURCE_LIFE_EVENT = "life_event"
SOURCE_CALLBACK = "callback"
SOURCE_SHARED_INTEREST = "shared_interest"
SOURCE_CHECK_IN = "check_in"

#: "current/recent" margin around an agenda slot (hours either side).
AGENDA_MARGIN_H = 2.0
#: how far back a completed agenda item counts as a life event (hours).
LIFE_EVENT_RECENCY_H = 48.0
#: how far back CALLBACK / shared-interest episodes count (hours).
EPISODE_RECENCY_H = 72.0
#: recency decay time constants (hours): exp(-age/tau).
AGENDA_RECENCY_TAU_H = 6.0
LIFE_EVENT_RECENCY_TAU_H = 24.0
EPISODE_RECENCY_TAU_H = 48.0
#: check-in context: local-hour windows, minimum silence since last contact,
#: and the recency normalization for the gap factor.
CHECK_IN_WINDOWS: tuple[tuple[float, float], ...] = ((8.0, 11.0), (19.0, 22.0))
CHECK_IN_MIN_GAP_H = 12.0
CHECK_IN_RECENCY_NORM_H = 48.0
CHECK_IN_SALIENCE = 0.5
#: validity factor normalization: clip(validity_h[reason] / VALIDITY_WEIGHT_H, 0.25, 2.0)
VALIDITY_WEIGHT_H = 6.0


def compose_hook(source, reason: str) -> str:
    """Deterministic hook composed from source fields (rule 13: never
    invented free text). The content gate re-derives it from the resolved
    source and rejects intents whose hook does not match."""
    if isinstance(source, AgendaItem):
        if reason == REASON_EVENT:
            return f"Finished: {source.activity}"
        return f"Agenda: {source.activity} ({source.start_t_h:.1f}-{source.end_t_h:.1f}h)"
    if isinstance(source, LifeArc):
        return f"Arc: {source.name} — {source.next_intention}"
    if isinstance(source, EpisodicMemory):
        if reason == REASON_CHECK_IN:
            return f"Check in — last shared moment: {source.summary}"
        if reason == REASON_CALLBACK:
            return f"Callback: {source.summary}"
        if reason == REASON_SHARED_INTEREST:
            return f"Shared: {source.summary}"
        return f"Memory: {source.summary}"
    raise TypeError(f"cannot compose a hook for {type(source).__name__}")


def _validity_factor(reason: str) -> float:
    return float(np.clip(REASON_VALIDITY_H[reason] / VALIDITY_WEIGHT_H, 0.25, 2.0))


def _evidence(source, now_h: float, *, extra: str = "") -> str:
    """Provenance chain string: the exact store facts the intent is built on."""
    if isinstance(source, AgendaItem):
        base = (
            f"{SOURCE_AGENDA}:{source.id} activity={source.activity!r} "
            f"status={source.status} source={source.source_type}:{source.source_id} "
            f"window={source.start_t_h:.2f}..{source.end_t_h:.2f} salience={source.salience:.3f}"
        )
    elif isinstance(source, LifeArc):
        base = (
            f"life_arc:{source.id} name={source.name!r} interest={source.interest} "
            f"progress={source.progress:.3f} status={source.status} "
            f"intention={source.next_intention!r}"
        )
    elif isinstance(source, EpisodicMemory):
        base = (
            f"episode:{source.id} category={source.category.value} "
            f"summary={source.summary!r} occurred={source.occurred_at_t_h:.2f} "
            f"importance={source.importance:.3f} tags={source.tags}"
        )
    else:
        raise TypeError(f"cannot build evidence for {type(source).__name__}")
    return base if not extra else f"{base} {extra}"


@dataclass(frozen=True)
class _Candidate:
    """One grounded candidate: the source plus its ranking components."""

    reason: str
    source: AgendaItem | LifeArc | EpisodicMemory
    salience: float
    recency: float
    validity: float

    def score(self) -> float:
        return self.salience * self.recency * self.validity


class IntentResolver:
    """Store-backed resolver of grounded proactive intents.

    ``store`` implements the A2 store seam (agenda items, life arcs,
    episodes, interests, latest interaction). ``rng`` seeds tie-breaks; the
    default is a deterministic engine.rng stream.
    """

    def __init__(self, store, *, rng=None):
        self.store = store
        self._rng = rng if rng is not None else rng_mod.stream_rng(0)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def resolve(
        self, opportunity: ContactOpportunity | float
    ) -> ProactiveIntent | None:
        """Best grounded intent AT the opportunity's time, or None (SUPPRESS:
        no_grounded_reason — a legitimate outcome, never an error).

        ``opportunity`` is the scheduler's :class:`ContactOpportunity` (the
        intent links back via ``opportunity_id`` and its validity is bounded
        by the opportunity's own window); a bare float is accepted for
        legacy callers / store-injected rows without an opportunity.
        Read-only: the runtime persists the intent.
        """
        if isinstance(opportunity, ContactOpportunity):
            t_h = opportunity.desired_t_h
            opp = opportunity
        else:
            t_h = float(opportunity)
            opp = None
        candidates = self._candidates(t_h)
        if not candidates:
            return None
        best = self._rank(candidates)
        return self._build_intent(best, t_h, opportunity=opp)

    # ------------------------------------------------------------------ #
    # candidate collection (store-backed only)
    # ------------------------------------------------------------------ #

    def _candidates(self, now_h: float) -> list[_Candidate]:
        out: list[_Candidate] = []
        out.extend(self._agenda_candidates(now_h))
        out.extend(self._life_event_candidates(now_h))
        out.extend(self._callback_candidates(now_h))
        out.extend(self._shared_interest_candidates(now_h))
        check_in = self._check_in_candidate(now_h)
        if check_in is not None:
            out.append(check_in)
        return out

    def _agenda_candidates(self, now_h: float) -> list[_Candidate]:
        out = []
        for item in self.store.list_agenda_items():
            if item.status not in ("planned", "shifted"):
                continue
            if now_h < item.start_t_h:
                dist = item.start_t_h - now_h
            elif now_h > item.end_t_h:
                dist = now_h - item.end_t_h
            else:
                dist = 0.0
            if dist > AGENDA_MARGIN_H:
                continue
            recency = math.exp(-dist / AGENDA_RECENCY_TAU_H)
            out.append(
                _Candidate(REASON_SCHEDULE, item, item.salience, recency,
                           _validity_factor(REASON_SCHEDULE))
            )
        return out

    def _life_event_candidates(self, now_h: float) -> list[_Candidate]:
        out = []
        for item in self.store.list_agenda_items(status="completed"):
            age = now_h - item.end_t_h
            if age < 0.0 or age > LIFE_EVENT_RECENCY_H:
                continue
            recency = math.exp(-age / LIFE_EVENT_RECENCY_TAU_H)
            out.append(
                _Candidate(REASON_EVENT, item, item.salience, recency,
                           _validity_factor(REASON_EVENT))
            )
        return out

    def _callback_candidates(self, now_h: float) -> list[_Candidate]:
        out = []
        for ep in self.store.list_episodes(category=MemoryKind.CALLBACK):
            age = now_h - ep.occurred_at_t_h
            if age < 0.0 or age > EPISODE_RECENCY_H:
                continue
            recency = math.exp(-age / EPISODE_RECENCY_TAU_H)
            out.append(
                _Candidate(REASON_CALLBACK, ep, ep.importance, recency,
                           _validity_factor(REASON_CALLBACK))
            )
        return out

    def _shared_interest_candidates(self, now_h: float) -> list[_Candidate]:
        interests = {i.name: i.salience for i in self.store.list_interests()}
        if not interests:
            return []
        out = []
        for ep in self.store.list_episodes():
            matched = [t for t in ep.tags if t in interests]
            if not matched:
                continue
            age = now_h - ep.occurred_at_t_h
            if age < 0.0 or age > EPISODE_RECENCY_H:
                continue
            salience = ep.importance * max(interests[t] for t in matched)
            recency = math.exp(-age / EPISODE_RECENCY_TAU_H)
            out.append(
                _Candidate(REASON_SHARED_INTEREST, ep, salience, recency,
                           _validity_factor(REASON_SHARED_INTEREST))
            )
        return out

    def _check_in_candidate(self, now_h: float) -> _Candidate | None:
        local = now_h % 24.0
        if not any(a <= local < b for a, b in CHECK_IN_WINDOWS):
            return None
        last = self.store.latest_interaction_t_h()
        gap = math.inf if last is None else now_h - last
        if gap < CHECK_IN_MIN_GAP_H:
            return None
        episodes = self.store.list_episodes()
        if not episodes:
            return None  # blank slate: no shared history to ground a check-in
        anchor = max(episodes, key=lambda e: (e.occurred_at_t_h, e.created_at_t_h))
        recency = float(np.clip(gap / CHECK_IN_RECENCY_NORM_H, 0.2, 1.0))
        return _Candidate(REASON_CHECK_IN, anchor, CHECK_IN_SALIENCE, recency,
                          _validity_factor(REASON_CHECK_IN))

    # ------------------------------------------------------------------ #
    # ranking + intent construction
    # ------------------------------------------------------------------ #

    def _rank(self, candidates: list[_Candidate]) -> _Candidate:
        ordered = sorted(candidates, key=lambda c: -c.score())
        best_score = ordered[0].score()
        tied = [c for c in ordered if abs(c.score() - best_score) < 1e-12]
        if len(tied) == 1:
            return tied[0]
        return tied[int(self._rng.integers(0, len(tied)))]

    @staticmethod
    def _source_kind(reason: str) -> str:
        return {
            REASON_SCHEDULE: SOURCE_AGENDA,
            REASON_EVENT: SOURCE_LIFE_EVENT,
            REASON_CALLBACK: SOURCE_CALLBACK,
            REASON_SHARED_INTEREST: SOURCE_SHARED_INTEREST,
            REASON_CHECK_IN: SOURCE_CHECK_IN,
        }[reason]

    def _build_intent(
        self,
        candidate: _Candidate,
        now_h: float,
        *,
        opportunity: ContactOpportunity | None = None,
    ) -> ProactiveIntent:
        source = candidate.source
        reason = candidate.reason
        source_type = self._source_kind(reason)
        source_id = source.id
        if reason == REASON_CHECK_IN:
            last = self.store.latest_interaction_t_h()
            gap = "inf" if last is None else f"{now_h - last:.2f}"
            extra = f"local={now_h % 24.0:.2f}h last_interaction={last} gap_h={gap}"
        else:
            extra = ""
        # The intent is valid while BOTH the reason is fresh AND the
        # opportunity that made this a plausible moment is still plausible
        # (the opportunity's window is the scheduler's claim; the reason's
        # window is the source's claim — the intent expires at the earlier).
        valid_until = now_h + REASON_VALIDITY_H[reason]
        if opportunity is not None:
            valid_until = min(valid_until, opportunity.valid_until_t_h)
        return ProactiveIntent(
            id=f"pi_{source_type}_{source_id}_{now_h:.3f}",
            reason=reason,
            source_type=source_type,
            source_id=source_id,
            hook=compose_hook(source, reason),
            created_t_h=now_h,
            valid_until_t_h=valid_until,
            salience=float(candidate.score()),
            evidence=_evidence(source, now_h, extra=extra),
            opportunity_id=opportunity.id if opportunity is not None else None,
        )
