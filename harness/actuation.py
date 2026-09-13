"""Mechanical actuation: BehaviorDirective -> observable generation controls.

Pure and deterministic: no I/O, never blocks. Latency is data, not behavior.
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


#: When False, ``_closing_guidance`` returns "" for every tendency.
CLOSING_GUIDANCE_ENABLED = False

#: Closing bands, keyed by the closing-tendency threshold.
_CLOSING_BANDS: tuple[tuple[float, str], ...] = (
    (0.20, "The companion may naturally invite continuation; leaving the door open is fine."),
    (0.40, "The companion is still open; a natural follow-up is welcome if the moment calls for it."),
    (0.60, "End the reply naturally, without forcing either a question or a closing."),
    (0.80, "A settled ending is welcome; do not manufacture extra turns."),
    (float("inf"), "Do not force a follow-up question; a settled ending is welcome."),
)


def _closing_guidance(closing_tendency: float) -> str:
    """Continuation policy for the prompt; "" while the channel is disabled."""
    if not CLOSING_GUIDANCE_ENABLED:
        return ""
    for threshold, text in _CLOSING_BANDS:
        if closing_tendency < threshold:
            return text
    return _CLOSING_BANDS[-1][1]


def controls_from_directive(
    directive: BehaviorDirective,
    *,
    base_max_tokens: int = 600,
    min_tokens: int = 96,
    max_tokens: int = 1500,
    beta: float = 2.0,
) -> domain.GenerationControls:
    """Derive mechanical generation controls from a behavioral directive.

    ``max_tokens`` = clamp(round(base_max_tokens * response_length_scale),
    min_tokens, max_tokens). ``response_delay_s`` clamps to [0, 60] and
    ``initiative_factor`` = exp(beta * (initiative - 0.5)) clamps to [0.2, 5.0].
    ``closing_tendency`` passes through unchanged.
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
