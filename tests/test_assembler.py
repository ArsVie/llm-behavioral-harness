"""Assembler tests (W-E1): system prompt shape + leakage invariants.
Wave 2: CompanionSnapshot assembly — bounded sections, proactive hook
verbatim, closing guidance, no reason labels."""

import dataclasses
import re

from engine.types import PHASE_FRACTIONS
from harness.assembler import (
    AGENDA_ITEMS_MAX,
    DEFAULT_PROACTIVE_HOOK,
    LIFE_ARCS_MAX,
    MAX_PROMPT_CHARS,
    MEMORY_EPISODES_MAX,
    assemble_snapshot,
    build_messages,
    build_system_prompt,
    proactive_block,
)
from harness.behavior import BehaviorDirective, BehaviorTrace
from harness.behavior import _render_brief
from harness.domain import (
    AgendaItem,
    BehaviorBrief,
    CompanionSnapshot,
    CurrentActivity,
    EpisodicMemory,
    GenerationControls,
    Interest,
    LifeArc,
    MemoryContext,
    MemoryKind,
    PersonaProfile,
    ProactiveIntent,
    Routine,
    Turn,
)

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


# --------------------------------------------------------------------------- #
# Wave 2: CompanionSnapshot assembly
# --------------------------------------------------------------------------- #


def _persona() -> PersonaProfile:
    return PersonaProfile(
        name="Nova",
        core="CORE TEXT.",
        interests=(Interest("pottery", "exact", 0.9),),
        routines=(Routine("morning walk", 0.38, 0.5, 0.8, 0.3),),
    )


def _brief() -> BehaviorBrief:
    return BehaviorBrief(
        valence=0.5, energy=0.7, reactivity=0.5, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=0.3,
    )


def _activity() -> CurrentActivity:
    return CurrentActivity(t_h=10.5, item=None, description="practice pottery")


def _arcs(n: int = 3) -> tuple[LifeArc, ...]:
    names = ["pottery", "photography", "chess", "running", "drawing"]
    return tuple(
        LifeArc(
            id=f"arc_{i}",
            name=f"learning {names[i]}",
            interest=names[i],
            started_day=1,
            progress=0.4,
            status="active",
            next_intention="practice the fundamentals",
        )
        for i in range(min(n, len(names)))
    )


def _agenda_items(n: int = 2) -> tuple[AgendaItem, ...]:
    return tuple(
        AgendaItem(
            id=f"ag_{i}",
            start_t_h=25.5 + i,
            end_t_h=26.5 + i,
            activity=f"agenda item {i}",
            source_type="arc",
            source_id="arc_1",
            salience=0.8,
            status="planned",
        )
        for i in range(n)
    )


def _episodes(n: int = 3) -> tuple[EpisodicMemory, ...]:
    return tuple(
        EpisodicMemory(
            id=f"ep_{i}",
            summary=f"episode summary {i}",
            category=MemoryKind.USER_FACT,
            occurred_at_t_h=10.0,
            created_at_t_h=12.0,
            importance=0.6,
            access_count=0,
            last_accessed_t_h=None,
            affect=None,
            source_session_id="day-0",
            source_turn_ids=(i,),
            verbatim_anchors=(f"anchor {i}",),
            tags=("x",),
        )
        for i in range(1, n + 1)
    )


def _memory_context(n_episodes: int = 3) -> MemoryContext:
    return MemoryContext(
        recent_turns=(Turn("user", "hi", 9.0), Turn("assistant", "hello", 9.1)),
        session_context=(),
        episodes=_episodes(n_episodes),
        user_model=None,
        evidence_anchors=(),
    )


def _snapshot(
    *,
    intent: ProactiveIntent | None = None,
    arcs: tuple[LifeArc, ...] | None = None,
    agenda: tuple[AgendaItem, ...] | None = None,
    memory: MemoryContext | None = None,
    brief: BehaviorBrief | None = None,
    activity: CurrentActivity | None = None,
) -> CompanionSnapshot:
    return CompanionSnapshot(
        persona=_persona(),
        current_behavior=brief if brief is not None else _brief(),
        current_activity=activity if activity is not None else _activity(),
        agenda=agenda if agenda is not None else _agenda_items(),
        life_arcs=arcs if arcs is not None else _arcs(),
        memory_context=memory if memory is not None else _memory_context(),
        recent_conversation=(Turn("user", "hi", 9.0), Turn("assistant", "hello", 9.1)),
        proactive_intent=intent,
    )


def _intent(hook: str = "Agenda: pottery class (14.0-15.5h)") -> ProactiveIntent:
    return ProactiveIntent(
        id="pi_1",
        reason="schedule",
        source_type="agenda_item",
        source_id="ag_1",
        hook=hook,
        created_t_h=10.0,
        valid_until_t_h=13.0,
        salience=0.5,
        evidence="agenda_item:ag_1",
    )


def test_snapshot_assembly_has_all_sections():
    prompt = assemble_snapshot(_snapshot())
    assert prompt.startswith("CORE TEXT.")
    assert "Current behavioral guidance:" in prompt
    assert "Current activity: practice pottery" in prompt
    assert "Active life arcs:" in prompt
    assert "Relevant memories:" in prompt
    assert "Recent conversation:" in prompt
    assert "Today's agenda:" in prompt


def test_snapshot_assembly_is_bounded():
    # Oversized memory context + long conversation: still under the budget.
    memory = dataclasses.replace(
        _memory_context(n_episodes=12),
        episodes=_episodes(12),
    )
    conversation = tuple(
        Turn("user", f"long turn number {i} " + "words " * 40, 9.0 + i)
        for i in range(20)
    )
    snapshot = dataclasses.replace(
        _snapshot(memory=memory), recent_conversation=conversation
    )
    prompt = assemble_snapshot(snapshot)
    assert len(prompt) <= MAX_PROMPT_CHARS


def test_proactive_section_only_with_intent():
    prompt = assemble_snapshot(_snapshot())
    assert "reaching out first" not in prompt
    prompt2 = assemble_snapshot(_snapshot(intent=_intent()))
    assert "reaching out first" in prompt2


def test_proactive_hook_verbatim_from_source():
    hook = (
        "You just finished the pottery class scheduled this afternoon. "
        "You had been nervous about glazing the bowl."
    )
    prompt = assemble_snapshot(_snapshot(intent=_intent(hook=hook)))
    assert hook in prompt
    # The hook, not a reason label — never "Contact reason: schedule".
    assert "Contact reason" not in prompt


def test_proactive_block_fallback_claims_no_source():
    block = proactive_block()
    assert "reaching out first" in block
    assert DEFAULT_PROACTIVE_HOOK in block
    assert "Agenda:" not in block and "Finished:" not in block


def test_closing_guidance_appears_when_set():
    controls = GenerationControls(
        max_tokens=600, response_delay_s=3.0, closing_tendency=0.8,
        initiative_factor=1.0,
        closing_guidance="Do not force a follow-up question; a settled ending is welcome.",
    )
    prompt = assemble_snapshot(_snapshot(), controls=controls)
    assert "Closing guidance:" in prompt
    assert controls.closing_guidance in prompt
    empty = dataclasses.replace(controls, closing_guidance="")
    prompt2 = assemble_snapshot(_snapshot(), controls=empty)
    assert "Closing guidance:" not in prompt2


def test_life_arcs_bounded_to_three():
    prompt = assemble_snapshot(_snapshot(arcs=_arcs(5)))
    assert prompt.count("Active life arcs:") == 1
    for name in ("learning pottery", "learning photography", "learning chess"):
        assert name in prompt
    assert "learning running" not in prompt
    assert "learning drawing" not in prompt
    assert len(_arcs(5)) > LIFE_ARCS_MAX  # the fixture really is oversized


def test_no_arcs_no_arc_section():
    prompt = assemble_snapshot(_snapshot(arcs=()))
    assert "Active life arcs:" not in prompt


def test_memory_episodes_bounded_to_budget():
    prompt = assemble_snapshot(_snapshot(memory=_memory_context(n_episodes=10)))
    assert "episode summary 1" in prompt
    assert "episode summary 9" not in prompt
    assert len(_episodes(10)) > MEMORY_EPISODES_MAX


def test_agenda_bounded():
    prompt = assemble_snapshot(_snapshot(agenda=_agenda_items(8)))
    assert "agenda item 0" in prompt
    assert "agenda item 7" not in prompt
    assert len(_agenda_items(8)) > AGENDA_ITEMS_MAX
