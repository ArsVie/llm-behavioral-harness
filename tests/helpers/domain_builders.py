"""Domain-object builders shared across assembler/prompt tests.

Consolidated from byte-identical copies in test_assembler, test_prompts and
test_prompt_cache_order. Nothing changes semantics: these are exactly the
builder bodies the files already ran (``sample_brief`` takes the
``**overrides`` test_prompt_cache_order's copy accepted; with none it is the
plain copy).
"""

from __future__ import annotations

import dataclasses

from harness.behavior import _render_brief
from harness.domain import BehaviorBrief, Interest, PersonaProfile, Routine


def nova_persona() -> PersonaProfile:
    """Nova with one interest and one routine."""
    return PersonaProfile(
        name="Nova",
        core="CORE TEXT.",
        interests=(Interest("pottery", "exact", 0.9),),
        routines=(Routine("morning walk", 0.38, 0.5, 0.8, 0.3),),
    )


def sample_brief(**overrides) -> BehaviorBrief:
    """A mid-range BehaviorBrief; keyword overrides replace single fields."""
    base = BehaviorBrief(
        valence=0.5, energy=0.7, reactivity=0.5, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=0.3,
    )
    return dataclasses.replace(base, **overrides)


def sample_prompt_brief() -> str:
    """The rendered prose brief for the same mid-range state."""
    return _render_brief(valence=0.5, energy=0.7, momentum=0.1,
                         warmth=0.8, playfulness=0.5, reflectiveness=0.4)
