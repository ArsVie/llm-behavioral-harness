"""Availability-event negotiation — phase machine (A1, G0 contract).

The G0 contract (docs/availability-negotiation-contract.md + the frozen
``harness/negotiation_contract.py``) defines one negotiation per AgendaItem
that hits its start boundary while a conversation is open:

    INFORM (once, idempotent) -> DECIDE (recurring, go/skip/delay(N))
        -> BACKSTOP (now >= end_t_h forces a skip; defer never re-arms
           past the window)

This module owns the MECHANICS only — the pure, deterministic phase machine:

* :class:`NegotiationState` — one item's negotiation, with the responded-bool
  idempotency marker (``informed`` checked as VALUE True, never key
  presence — commit 3005b9e discipline);
* trigger arithmetic — the companion-turn counter and the AFK bomb
  (``last_user_turn + SHORT_AFK_H``), both re-armed on every delay;
* the window-close backstop — a decide instant at/after ``end_t_h`` is a
  forced skip (no model call), and a delay whose re-arm would land at/after
  ``end_t_h`` resolves immediately instead of re-arming;
* the converging pull-to-go — ``delay_count * PULL_PER_DELAY`` presented as
  rising pressure in the decide request (the MODEL still chooses);
* the deterministic defer(N) mapping (``DEFER_N_PATTERNS``, clamped) and
  deterministic decision ids (``neg-<item_id>-inform`` /
  ``neg-<item_id>-decide-<delay_index>``).

The harness session (harness/session.py) drives the machine at turn
boundaries and runtime wakes; the runtime (harness/runtime.py) parks the
rollover at the AFK-bomb / backstop instants via
``Session.next_negotiation_trigger_t_h``. Nothing here touches a store, a
clock or a client — everything is passed in, so the machine is deterministic
and unit-testable with a fake DecisionRunner.

Replay contract: the state is persisted as a ``negotiation_state`` state
event (full snapshot per mutation) and rebuilt on session init, so a
restart resumes the negotiation mid-loop; each decide leg's decision id is
derived from the item id and the CURRENT delay index, so
``DecisionRunner``'s replay-by-decision_id returns the recorded verdict
instead of re-rolling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from harness.negotiation_contract import (
    DEFER_N_MAX,
    DEFER_N_MIN,
    DEFER_N_PATTERNS,
    DEFAULT_DEFER_TURNS,
    PULL_PER_DELAY,
    SHORT_AFK_H,
    NegotiationPhase,
)

#: Decision-id prefixes (deterministic replay: literal item id + delay index).
INFORM_DECISION_PREFIX = "neg-{item_id}-inform"
DECIDE_DECISION_PREFIX = "neg-{item_id}-decide-{index}"


@dataclass
class NegotiationState:
    """One AgendaItem's availability negotiation (mutable; persisted as a
    JSON snapshot per mutation).

    Idempotency marker: ``informed`` is a RESPONDED-BOOL — the session
    checks ``state.informed is True`` (value), never key presence
    (``"informed" in ...``), per the G0 floor (commit 3005b9e discipline).
    """

    item_id: str
    activity: str
    source_type: str          # "arc" | "interest" | "routine"
    start_t_h: float
    end_t_h: float
    salience: float
    phase: str = NegotiationPhase.INFORM.value
    #: Responded-bool Inform marker: True once the model produced the
    #: natural mention. Checked as a VALUE, never key presence.
    informed: bool = False
    #: Companion turns that must still pass before the decide fires.
    #: 0 means "the next companion turn decides". Re-armed to N-1 on a
    #: delay(N) (the decide leg that produced the delay is turn 0), to 0
    #: right after Inform (the next turn decides).
    turns_to_decide: int = 0
    #: AFK bomb: last user turn + SHORT_AFK_H (None = not yet armed).
    afk_deadline_t_h: float | None = None
    #: Virtual instant of the last executed decide leg (or the inform
    #: turn). Guards the decide to fire at most once per virtual instant
    #: per item, so a runtime poll loop can never hammer the model.
    last_decide_at_t_h: float | None = None
    delay_count: int = 0
    resolved_action: str | None = None   # "follow" | "abandon" | "forced"
    resolved_t_h: float | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_t_h is not None

    @property
    def decide_index(self) -> int:
        """Deterministic decide-leg index: the number of delays already
        taken. The first decide leg is index 0, the next after one delay is
        index 1, ... -> decision id ``neg-<item>-decide-<index>``."""
        return self.delay_count


# --------------------------------------------------------------------------- #
# decide trigger / backstop
# --------------------------------------------------------------------------- #


def decide_status_at(
    state: NegotiationState, *, now: float, companion_turn: bool
) -> str:
    """The decide status of ``state`` at a virtual instant.

    Returns one of:

    * ``"inactive"`` — not in the DECIDE phase or already resolved;
    * ``"forced"``   — BACKSTOP: ``now >= end_t_h``; the decide is a forced
      skip with NO model call;
    * ``"due"``      — the decide leg fires now: the AFK bomb has fired
      (``now >= afk_deadline_t_h``) or this is a companion turn with the
      turn counter at 0 — and the leg was not already executed at this
      exact instant (at-most-once per instant);
    * ``"waiting"``  — nothing due; on a companion turn the turn counter is
      decremented (the turn passes without a decide).
    """
    if state.phase != NegotiationPhase.DECIDE.value or state.resolved:
        return "inactive"
    if now >= state.end_t_h - 1e-12:
        return "forced"
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
    """Next strictly-future runtime wake instant for this negotiation: the
    earlier of the AFK-bomb deadline and the window-close backstop instant.
    None when nothing is pending in the future. The rollover parks here
    exactly like ``next_conversation_close_t_h`` parks at conversation
    closes (the park is always a strictly-future instant; a past deadline
    fires at the next wake of any kind)."""
    if state.phase != NegotiationPhase.DECIDE.value or state.resolved:
        return None
    candidates: list[float] = []
    if (
        state.afk_deadline_t_h is not None
        and state.afk_deadline_t_h > now + 1e-12
    ):
        candidates.append(state.afk_deadline_t_h)
    if state.end_t_h > now + 1e-12:
        candidates.append(state.end_t_h)
    return min(candidates) if candidates else None


# --------------------------------------------------------------------------- #
# defer(N): deterministic server-side mapping + re-arm arithmetic
# --------------------------------------------------------------------------- #


def map_defer_n(reason: str) -> int:
    """Map the model's natural delay reason to a concrete N (the server
    owns the mechanics; the model never emits arithmetic). Deterministic:
    the FIRST matching ``DEFER_N_PATTERNS`` row wins; the explicit
    "N more turns/messages" pattern is clamped to [DEFER_N_MIN,
    DEFER_N_MAX]; no match falls back to ``DEFAULT_DEFER_TURNS``."""
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
    """Re-arm both triggers after a delay(N) verdict.

    The turn counter re-arms to ``n - 1`` (the decide leg that produced the
    delay is turn 0; the next decide fires on the n-th companion turn
    after it). The AFK bomb re-arms off the LAST user turn
    (``last_user_turn_t_h + SHORT_AFK_H``; falls back to ``now`` when no
    user turn exists yet) — active talk keeps pushing it out, silence lets
    it fire.

    Returns True when the re-arm is REFUSED: the AFK bomb would land at or
    after ``end_t_h`` — the backstop clamp, the delay resolves immediately
    (forced skip) instead of re-arming. (The turn counter cannot be
    predicted ahead of time, so the AFK deadline is the deterministic
    clamp; a turn-triggered decide that would land past the window is
    caught by the ``now >= end_t_h`` backstop at its own instant.)
    """
    anchor = last_user_turn_t_h if last_user_turn_t_h is not None else now
    afk_new = anchor + SHORT_AFK_H
    if afk_new >= state.end_t_h - 1e-12:
        return False
    state.turns_to_decide = max(0, int(n) - 1)
    state.afk_deadline_t_h = afk_new
    state.delay_count += 1
    return True


# --------------------------------------------------------------------------- #
# converging pull-to-go (presented to the model as context, never a verdict)
# --------------------------------------------------------------------------- #


def pull_toward_go(state: NegotiationState) -> float:
    """Rising pressure toward go: ``delay_count * PULL_PER_DELAY``, capped
    at 1.0. The session includes this (plus the remaining window) in the
    decide request as context the MODEL sees — the server never overrides
    the verdict."""
    return min(1.0, state.delay_count * PULL_PER_DELAY)


def window_ending_at(state: NegotiationState, now: float) -> bool:
    """True when the remaining window is at most one AFK-bomb period
    (``SHORT_AFK_H``) — the next AFK-bomb decide could land at/after the
    window close, so the request flags the ending window."""
    return (state.end_t_h - now) <= SHORT_AFK_H + 1e-12


# --------------------------------------------------------------------------- #
# persistence (full-snapshot JSON state events, rebuilt on session init)
# --------------------------------------------------------------------------- #

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
    """Rebuild a NegotiationState from a persisted snapshot; None when the
    snapshot is unusable (foreign/corrupt rows are skipped, never fatal)."""
    try:
        return NegotiationState(
            item_id=str(data["item_id"]),
            activity=str(data.get("activity", "")),
            source_type=str(data.get("source_type", "arc")),
            start_t_h=float(data["start_t_h"]),
            end_t_h=float(data["end_t_h"]),
            salience=float(data.get("salience", 0.0)),
            phase=str(data.get("phase", NegotiationPhase.INFORM.value)),
            # Responded-bool restore: ``informed`` is a real bool VALUE
            # (never key presence) both in memory and on disk — a snapshot
            # without the key restores False, never True.
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
