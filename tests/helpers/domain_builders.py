"""Domain-object builders shared across assembler/prompt/snapshot tests.

Consolidated byte-identical builders from test_prompts, test_assembler,
test_prompt_cache_order and friends. Nothing changes semantics: these are
exactly the builder bodies the files already ran.
"""

from __future__ import annotations

from harness.behavior import _render_brief
from harness.domain import BehaviorBrief, Interest, PersonaProfile, Routine


def persona() -> PersonaProfile:
    return PersonaProfile(
        name="Nova",
        core="CORE TEXT.",
        interests=(Interest("pottery", "exact", 0.9),),
        routines=(Routine("morning walk", 0.38, 0.5, 0.8, 0.3),),
    )


def brief() -> BehaviorBrief:
    return BehaviorBrief(
        valence=0.5, energy=0.7, reactivity=0.5, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=0.3,
    )


def prompt_brief() -> str:
    return _render_brief(valence=0.5, energy=0.7, momentum=0.1,
                         warmth=0.8, playfulness=0.5, reflectiveness=0.4)
