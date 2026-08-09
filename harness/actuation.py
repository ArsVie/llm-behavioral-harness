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

    Five bands across the widened [0.04, 0.85] range (B4): a very low
    closing tendency invites continuation, a very high one asks for a
    settled ending, and the middle bands leave the choice to the moment.
    The 0.40–0.60 band is the flat-controls string, so NO_ACTUATORS pins a
    real band of the mapping instead of a value no directive can produce.
    """

    if closing_tendency < 0.20:
        return "The companion may naturally invite continuation; leaving the door open is fine."
    if closing_tendency < 0.40:
        return "The companion is still open; a natural follow-up is welcome if the moment calls for it."
    if closing_tendency < 0.60:
        return "End the reply naturally, without forcing either a question or a closing."
    if closing_tendency < 0.80:
        return "A settled ending is welcome; do not manufacture extra turns."
    return "Do not force a follow-up question; a settled ending is welcome."


def controls_from_directive(
    directive: BehaviorDirective,
    *,
    base_max_tokens: int = 600,
    min_tokens: int = 96,
    max_tokens: int = 1500,
    beta: float = 2.0,
) -> domain.GenerationControls:
    """Derive mechanical generation controls from a behavioral directive.

    Mapping (deterministic, documented — B4 widened ranges):
    * ``max_tokens`` = clamp(round(base_max_tokens * response_length_scale),
      min_tokens, max_tokens). ``response_length_scale`` now spans
      [0.22, 1.30] — coupled to energy and expressiveness, so a terse
      low-energy day realizes roughly 130–350 tokens and an expansive
      high-energy day 600–780 (F4: ±6% around 551). ``closing_tendency`` is
      not part of the budget so that closing stays a policy, not a length
      artifact.
    * ``response_delay_s`` = directive.response_delay_s clamped to [0, 60];
      the directive channel now spans ~0.8 s (high energy) to ~44 s
      (low energy, recent dip) — real inter-turn latency inside a
      conversation.
    * ``closing_tendency`` passes through unchanged; it now spans
      [0.04, 0.85]. ``closing_guidance`` is the prompt-level continuation
      policy derived from it, with five distinct bands.
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
