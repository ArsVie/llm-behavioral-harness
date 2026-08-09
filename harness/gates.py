"""Content + context gates for proactive contact (wave 2, seam A-4; A7).

Pure functions: no I/O except reads through the injected store. Since A7 the
content gate is REAL: instead of checking ``reason in VALID_REASONS`` it
verifies, against the store, that the intent's source still exists, is not
deleted/superseded, the intent is still timely, and the supplied hook is
actually attached to that source (deterministic re-derivation). The context
gate re-checks at FIRE time that the moment is still good (quiet hours,
cooldown, daily cap), because user activity, restarts, and clock pacing can
change state since planning.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.circadian import envelope
from engine.types import TimingParams
from harness.domain import AgendaItem, EpisodicMemory, LifeArc
from harness.proactive import compose_hook
from harness.scheduler import REASON_EVENT, REASON_VALIDITY_H, VALID_REASONS


@dataclass(frozen=True)
class GateDecision:
    """One gate's verdict: pass/fail plus the failing code (or 'ok')."""

    allowed: bool
    # 'ok'|'no_valid_reason'|'expired'|'cooldown'|'quiet_hours'|'daily_cap'
    # |'no_source'|'source_superseded'|'hook_mismatch'
    code: str


def content_gate(intent, store, *, now_h: float | None = None) -> GateDecision:
    """PASS iff the intent is fully grounded at gate time:

      - intent is not None                                   -> 'no_valid_reason'
      - now_h <= intent.valid_until_t_h (when now_h given)   -> 'expired'
      - store.resolve_intent_source(intent) is not None      -> 'no_source'
      - source not deleted/superseded (status checks:
        AgendaItem 'skipped', LifeArc 'abandoned')           -> 'source_superseded'
      - a life_event claim ("Finished: X") requires the agenda
        item to be persisted 'completed' (A9 G-5)            -> 'source_superseded'
      - an episode linked to a SUPERSEDED L4 assertion
        carries stale truth (A9 G-4)                         -> 'source_superseded'
      - an episode whose source session no longer exists has
        broken provenance (A9 G-8b)                          -> 'no_source'
      - compose_hook(source, intent.reason) == intent.hook
        (the hook is actually attached to that source)       -> 'hook_mismatch'

    New checks run only against seams the store exposes (duck-typed):
    seam-faithful fakes without ``list_assertions`` / ``session_exists`` are
    skipped, matching the codebase's optional-seam convention.
    """
    if intent is None:
        return GateDecision(allowed=False, code="no_valid_reason")
    if now_h is not None and intent.valid_until_t_h < now_h:
        return GateDecision(allowed=False, code="expired")
    source = store.resolve_intent_source(intent)
    if source is None:
        return GateDecision(allowed=False, code="no_source")
    if isinstance(source, AgendaItem):
        if source.status == "skipped":
            return GateDecision(allowed=False, code="source_superseded")
        # G-5: a life_event claim is only grounded when the store records the
        # item as completed — the completion write is the source of the claim.
        if intent.reason == REASON_EVENT and source.status != "completed":
            return GateDecision(allowed=False, code="source_superseded")
    if isinstance(source, LifeArc) and source.status == "abandoned":
        return GateDecision(allowed=False, code="source_superseded")
    if isinstance(source, EpisodicMemory):
        # G-4: the episode embodies a fact whose L4 assertion was superseded
        # (stale truth must not reach a proactive message).
        list_assertions = getattr(store, "list_assertions", None)
        if list_assertions is not None and any(
            source.id in a.source_memory_ids
            for a in list_assertions(status="superseded")
        ):
            return GateDecision(allowed=False, code="source_superseded")
        # G-8b: broken provenance — the source session that witnessed the
        # memory no longer exists; no record of the promise, no claim.
        session_exists = getattr(store, "session_exists", None)
        if (
            session_exists is not None
            and source.source_session_id
            and not session_exists(source.source_session_id)
        ):
            return GateDecision(allowed=False, code="no_source")
    if compose_hook(source, intent.reason) != intent.hook:
        return GateDecision(allowed=False, code="hook_mismatch")
    return GateDecision(allowed=True, code="ok")


def context_gate(
    now_h: float,
    day: int,
    *,
    store,
    timing: TimingParams,
    last_fired_t_h: float | None,
) -> GateDecision:
    """PASS iff ALL hold, else the first failing code:
      quiet_hours : engine.circadian.envelope(now_h % 24, timing) >= 1e-9
      cooldown    : last_fired_t_h is None OR
                    (now_h - last_fired_t_h) >= timing.min_gap_min/60
      daily_cap   : store.proactive_count(day) < timing.daily_cap
    (envelope==0 already encodes 'active window'/quiet hours by construction,
    matching run_events guards; the gate re-checks at FIRE time because
    user activity, restarts, and clock pacing can change state since planning.)
    """
    if envelope(now_h % 24.0, timing) < 1e-9:
        return GateDecision(allowed=False, code="quiet_hours")
    if last_fired_t_h is not None and (
        now_h - last_fired_t_h
    ) < timing.min_gap_min / 60.0:
        return GateDecision(allowed=False, code="cooldown")
    if store.proactive_count(day) >= timing.daily_cap:
        return GateDecision(allowed=False, code="daily_cap")
    return GateDecision(allowed=True, code="ok")
