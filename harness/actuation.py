"""Mechanical actuation: BehaviorDirective -> observable generation controls.

This module is the A3 seam: it converts a behavioral directive into the
mechanical parameters the rest of the harness can execute — token budget,
delivery latency, closing policy and the initiative multiplier. It is pure
and deterministic: it contains no I/O and never blocks. Latency is data, not
behavior; nothing here waits.
"""

from __future__ import annotations

import math

from harness import domain
from harness.behavior import BehaviorDirective


def to_brief(directive: BehaviorDirective) -> domain.BehaviorBrief:
    """Project a directive onto the conversation-safe brief (all channels)."""

    return domain.BehaviorBrief(
        valence=directive.valence,
        energy=directive.energy,
        reactivity=directive.reactivity,
        warmth=directive.warmth,
        expressiveness=directive.expressiveness,
        playfulness=directive.playfulness,
        reflectiveness=directive.reflectiveness,
        initiative=directive.initiative,
        response_length_scale=directive.response_length_scale,
        response_delay_s=directive.response_delay_s,
        closing_tendency=directive.closing_tendency,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _closing_guidance(closing_tendency: float) -> str:
    """Continuation policy that makes closing tendency observable.

    A high closing tendency means the companion is settled and winding down:
    the reply should end cleanly without manufacturing a question. A low
    closing tendency means the companion is still open: leaving the door open
    is natural. The middle band leaves the choice to the moment.
    """

    if closing_tendency < 0.35:
        return "The companion may naturally invite continuation; leaving the door open is fine."
    if closing_tendency > 0.60:
        return "Do not force a follow-up question; a settled ending is welcome."
    return "End the reply naturally, without forcing either a question or a closing."


def controls_from_directive(
    directive: BehaviorDirective,
    *,
    base_max_tokens: int = 600,
    min_tokens: int = 96,
    max_tokens: int = 1500,
    beta: float = 2.0,
) -> domain.GenerationControls:
    """Derive mechanical generation controls from a behavioral directive.

    Mapping (deterministic, documented):
    * ``max_tokens`` = clamp(round(base_max_tokens * response_length_scale),
      min_tokens, max_tokens) — expressiveness and reflectiveness scale the
      reply budget; ``closing_tendency`` is not part of the budget so that
      closing stays a policy, not a length artifact.
    * ``response_delay_s`` = directive.response_delay_s clamped to [0, 60].
    * ``closing_tendency`` passes through unchanged; ``closing_guidance`` is
      the prompt-level continuation policy derived from it.
    * ``initiative_factor`` = exp(beta * (initiative - 0.5)) clamped to
      [0.2, 5.0] — the mechanical multiplier that enters scheduling.
    """

    budget = _clamp(round(base_max_tokens * directive.response_length_scale), min_tokens, max_tokens)
    delay = _clamp(directive.response_delay_s, 0.0, 60.0)
    initiative_factor = _clamp(
        math.exp(beta * (directive.initiative - 0.5)),
        0.2,
        5.0,
    )
    return domain.GenerationControls(
        max_tokens=int(budget),
        response_delay_s=delay,
        closing_tendency=directive.closing_tendency,
        initiative_factor=initiative_factor,
        closing_guidance=_closing_guidance(directive.closing_tendency),
    )
