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


#: Closing guidance is DISABLED (2026-09-07, user directive).
#:
#: The band prose told the model how to end every reply, on every turn, from
#: a per-turn draw. That is the kind of instruction that flattens a reply
#: into an assistant's sign-off, and the closing behaviour it actuates is
#: itself unspecified (see the "Closing behavior" backlog entry). The
#: channel stays wired end to end -- ``GenerationControls.closing_guidance``
#: still exists, the assembler still renders a non-empty value -- so
#: re-enabling is a one-line change once the behaviour is specified.
CLOSING_GUIDANCE_ENABLED = False

#: The band prose, retained verbatim for the day the channel is re-enabled.
_CLOSING_BANDS: tuple[tuple[float, str], ...] = (
    (0.20, "The companion may naturally invite continuation; leaving the door open is fine."),
    (0.40, "The companion is still open; a natural follow-up is welcome if the moment calls for it."),
    (0.60, "End the reply naturally, without forcing either a question or a closing."),
    (0.80, "A settled ending is welcome; do not manufacture extra turns."),
    (float("inf"), "Do not force a follow-up question; a settled ending is welcome."),
)


def _closing_guidance(closing_tendency: float) -> str:
    """Continuation policy for the prompt -- currently the empty string.

    With ``CLOSING_GUIDANCE_ENABLED`` false this returns "" for every
    tendency, so the assembler's ``if controls.closing_guidance`` guard drops
    the CLOSING section from the state card entirely. ``closing_tendency``
    itself is untouched: it still drives the conversation-close draw, it just
    no longer speaks to the model.
    """
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
