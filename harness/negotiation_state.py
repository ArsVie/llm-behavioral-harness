"""Availability-event negotiation — the pure phase machine (A1).

Deterministic mechanics only: inform -> decide -> backstop transitions, trigger
arithmetic, defer(N) mapping. No store, clock or client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harness.negotiation_contract import (
    DEFER_N_MAX,
    DEFER_N_MIN,
    DEFER_N_PATTERNS,
    DEFAULT_DEFER_TURNS,
    PULL_PER_DELAY,
    SHORT_AFK_H,
    NegotiationPhase,
)

# Decision-id prefixes are built at the call sites (item id + delay index).


@dataclass
class NegotiationState:
    """One AgendaItem's availability negotiation; JSON snapshot per mutation."""

    item_id: str
    activity: str
    source_type: str          # "arc" | "interest" | "routine"
    start_t_h: float
    end_t_h: float
    salience: float
    phase: str = NegotiationPhase.INFORM.value
    #: Responded-bool marker; checked as ``is True`` (value), never key presence.
    informed: bool = False
    #: Companion turns remaining before the decide fires (0 = next turn decides).
    turns_to_decide: int = 0
    #: AFK bomb: last user turn + SHORT_AFK_H (None = not yet armed).
    afk_deadline_t_h: float | None = None
    #: Virtual instant of the last decide leg; one decide per instant per item.
    last_decide_at_t_h: float | None = None
    delay_count: int = 0
    resolved_action: str | None = None   # "follow" | "abandon" | "forced"
    resolved_t_h: float | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_t_h is not None

    @property
    def decide_index(self) -> int:
        """Decide-leg index (deterministic): the number of delays taken."""
        return self.delay_count


# decide trigger / backstop


def decide_status_at(
    state: NegotiationState, *, now: float, companion_turn: bool
) -> str:
    """The decide status of ``state`` at a virtual instant.

    "inactive" — not deciding or resolved; "forced" — backstop, ``now >= end_t_h``
    (no model call); "due" — the AFK bomb fired or a companion turn with the
    counter at 0, at most once per instant; "waiting" — nothing due (a companion
    turn decrements the counter).
    """
    if state.resolved:
        return "inactive"
    if now < state.start_t_h - 1e-12:
        return "waiting"
    # Backstop first: a closed window forces a skip in any phase.
    if now >= state.end_t_h - 1e-12:
        return "forced"
    if state.phase != NegotiationPhase.DECIDE.value:
        return "inactive"
    afk_fired = (
        state.afk_deadline_t_h is not None
        and now >= state.afk_deadline_t_h - 1e-12
    )
    turn_fired = companion_turn and state.turns_to_decide <= 0
    fresh = (
        state.last_decide_at_t_h is None
        or now > state.last_decide_at_t_h + 1e-9
    )
    if (afk_fired or turn_fired) and fresh:
        return "due"
    if companion_turn and state.turns_to_decide > 0 and not afk_fired:
        state.turns_to_decide -= 1
    return "waiting"


def next_trigger_t_h(state: NegotiationState, now: float) -> float | None:
    """Next strictly-future wake instant (AFK-bomb deadline or window close);
    None when nothing is pending. A past deadline fires at the next wake.
    """
    if state.phase != NegotiationPhase.DECIDE.value or state.resolved:
        return None
    if now < state.start_t_h - 1e-12:
        # Informed but not open yet: the window opening is the next wake.
        return state.start_t_h
    candidates: list[float] = []
    if (
        state.afk_deadline_t_h is not None
        and state.afk_deadline_t_h > now + 1e-12
    ):
        candidates.append(state.afk_deadline_t_h)
    if state.end_t_h > now + 1e-12:
        candidates.append(state.end_t_h)
    return min(candidates) if candidates else None


# defer(N): deterministic server-side mapping + re-arm arithmetic


def map_defer_n(reason: str) -> int:
    """Map the model's delay reason to a concrete N (the server owns the
    arithmetic). First matching ``DEFER_N_PATTERNS`` row wins; explicit N is
    clamped to [DEFER_N_MIN, DEFER_N_MAX]; no match -> DEFAULT_DEFER_TURNS.
    """
    text = (reason or "").strip()
    for pattern, n in DEFER_N_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m is None:
            continue
        if n == 0:  # explicit "N more turns/messages"
            return min(max(int(m.group(1)), DEFER_N_MIN), DEFER_N_MAX)
        return n
    return DEFAULT_DEFER_TURNS


def rearm_after_delay(
    state: NegotiationState,
    *,
    now: float,
    last_user_turn_t_h: float | None,
    n: int,
) -> bool:
    """Re-arm both triggers after a delay(N): the turn counter to ``n - 1``, the
    AFK bomb to last user turn + ``SHORT_AFK_H``.

    Returns False when refused — the AFK bomb would land at/after ``end_t_h``;
    the caller resolves the delay as a forced skip.
    """
    anchor = last_user_turn_t_h if last_user_turn_t_h is not None else now
    afk_new = anchor + SHORT_AFK_H
    if afk_new >= state.end_t_h - 1e-12:
        return False
    state.turns_to_decide = max(0, int(n) - 1)
    state.afk_deadline_t_h = afk_new
    state.delay_count += 1
    return True


# converging pull-to-go (presented to the model as context, not a verdict)


def pull_toward_go(state: NegotiationState) -> float:
    """Rising pressure toward go: ``delay_count * PULL_PER_DELAY``, capped at 1.0.
    Request context for the model; the server never overrides the verdict.
    """
    return min(1.0, state.delay_count * PULL_PER_DELAY)


def window_ending_at(state: NegotiationState, now: float) -> bool:
    """True when the remaining window is at most ``SHORT_AFK_H`` (one AFK period)."""
    return (state.end_t_h - now) <= SHORT_AFK_H + 1e-12


# persistence (full-snapshot JSON state events, rebuilt on session init)

_STATE_KEYS = (
    "item_id", "activity", "source_type", "start_t_h", "end_t_h",
    "salience", "phase", "informed", "turns_to_decide",
    "afk_deadline_t_h", "last_decide_at_t_h", "delay_count",
    "resolved_action", "resolved_t_h",
)


def state_to_dict(state: NegotiationState) -> dict:
    """JSON-safe snapshot of one negotiation (the persisted form)."""
    return {k: getattr(state, k) for k in _STATE_KEYS}


def state_from_dict(data: dict) -> NegotiationState | None:
    """Rebuild a NegotiationState from a snapshot; None when unusable (foreign
    or corrupt rows are skipped, never fatal)."""
    try:
        return NegotiationState(
            item_id=str(data["item_id"]),
            activity=str(data.get("activity", "")),
            source_type=str(data.get("source_type", "arc")),
            start_t_h=float(data["start_t_h"]),
            end_t_h=float(data["end_t_h"]),
            salience=float(data.get("salience", 0.0)),
            phase=str(data.get("phase", NegotiationPhase.INFORM.value)),
            # Responded-bool restore: a snapshot without the key restores False.
            informed=bool(data.get("informed")),
            turns_to_decide=int(data.get("turns_to_decide", 0)),
            afk_deadline_t_h=(
                float(data["afk_deadline_t_h"])
                if data.get("afk_deadline_t_h") is not None else None
            ),
            last_decide_at_t_h=(
                float(data["last_decide_at_t_h"])
                if data.get("last_decide_at_t_h") is not None else None
            ),
            delay_count=int(data.get("delay_count", 0)),
            resolved_action=data.get("resolved_action"),
            resolved_t_h=(
                float(data["resolved_t_h"])
                if data.get("resolved_t_h") is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None
