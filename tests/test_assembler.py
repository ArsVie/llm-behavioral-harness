"""Assembler tests (W-E1): system prompt shape + leakage invariants."""

import dataclasses
import re

from engine.types import PHASE_FRACTIONS
from harness.assembler import build_messages, build_system_prompt
from harness.behavior import BehaviorDirective, BehaviorTrace
from harness.behavior import _render_brief

PHASE_LABELS = {p[0] for p in PHASE_FRACTIONS}


def _directive(**overrides) -> BehaviorDirective:
    base = BehaviorDirective(
        valence=0.5, energy=0.7, momentum=0.1, reactivity=0.5, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=0.3,
        prompt_brief=_render_brief(valence=0.5, energy=0.7, momentum=0.1,
                                   warmth=0.8, playfulness=0.5, reflectiveness=0.4),
        trace=BehaviorTrace(phase_label="menstrual", hormonal_gain=1.2,
                            event_memory=0.3, endogenous_tone=-0.2, mood_delta=0.1),
    )
    return dataclasses.replace(base, **overrides)


def test_system_prompt_contains_core_and_brief():
    directive = _directive()
    prompt = build_system_prompt("CORE TEXT.", directive)
    assert prompt.startswith("CORE TEXT.")
    assert "Current behavioral guidance:" in prompt
    assert directive.prompt_brief in prompt


def test_system_prompt_without_directive_is_persona_only():
    prompt = build_system_prompt("CORE TEXT.")
    assert prompt == "CORE TEXT."
    assert "behavioral guidance" not in prompt


def test_default_persona_fallback():
    prompt = build_system_prompt(None, None)
    assert "Nova" in prompt


def test_brief_leaks_no_numbers_or_phase_labels():
    directive = _directive()
    brief = directive.prompt_brief
    assert not re.search(r"\d", brief), "brief must not contain digits"
    assert not any(label in brief.lower() for label in PHASE_LABELS), (
        "brief must not name a hormonal phase"
    )
    for banned in (r"\bmu\b", r"\beta\b", "hormon", "cycle", "score", "M ="):
        assert not re.search(banned, brief, re.IGNORECASE), f"brief leaked: {banned}"


def test_messages_shape():
    recent = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    messages = build_messages(recent, "c")
    assert messages == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


def test_messages_tail_limited():
    recent = [{"role": "user", "content": str(i)} for i in range(20)]
    messages = build_messages(recent, "last")
    assert len(messages) == 13  # 12 tail turns + request


def test_messages_ignore_meta_fields():
    recent = [{"role": "user", "content": "x", "id": 1, "t_h": 0.0}]
    messages = build_messages(recent, "y")
    assert set(messages[0]) == {"role", "content"}
