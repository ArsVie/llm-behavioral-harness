"""prompts module tests (WS1): the stable system core contains NO state, the
typed audit headers match the L393 sketch, and the pinned decision/steering
payload sections survive the assembler's budget trim.
"""

import re

from engine.types import PHASE_FRACTIONS
from harness.assembler import MAX_PROMPT_CHARS, assemble_snapshot
from harness.behavior import _render_brief
from harness.domain import (
    BehaviorBrief,
    CompanionSnapshot,
    CurrentActivity,
    EpisodicMemory,
    MemoryContext,
    MemoryKind,
    PersonaProfile,
    Turn,
)
from harness.prompts import (
    HEADER_CONVERSATION,
    HEADER_REPLY,
    HEADER_SYSTEM,
    HEADER_THINKING,
    HEADER_TOOL,
    HEADER_TOOL_CALL,
    HEADER_USER,
    POPUP_MARKER_CLOSE,
    POPUP_MARKER_OPEN,
    SYSTEM_CORE,
    SYSTEM_CORE_WITH_TOOLS,
    TOOL_PROTOCOL,
    render_popup_block,
)

PHASE_LABELS = {p[0] for p in PHASE_FRACTIONS}


def test_system_core_contains_no_state():
    """The stable core is constant: no digits, no phase labels, no engine
    internals, no dynamic wording — it can never leak snapshot state."""
    low = SYSTEM_CORE_WITH_TOOLS.lower()
    assert not re.search(r"\d", SYSTEM_CORE_WITH_TOOLS), "core must not contain digits"
    assert not re.search(r"\b\d+\.\d+\b", SYSTEM_CORE_WITH_TOOLS), (
        "core must not contain raw numbers"
    )
    for token in (
        "cycle_day", "phase_label", "menstrual", "follicular", "ovulatory",
        "luteal", "hormon", "mu", "eta", "phase:",
    ):
        assert token not in low, f"core leaked engine internals: {token}"
    assert not re.search(r"\bg\b", low), "standalone 'g' leaked"
    # no state-shaped placeholders either
    assert "{hook}" not in SYSTEM_CORE_WITH_TOOLS


def test_system_core_names_the_state_card_and_rules():
    """The core is ABOUT how to handle the {state} card (user L393): reading,
    compliance, show-don't-announce, never-name-the-state, tool protocol."""
    core = SYSTEM_CORE
    assert "state card" in core.lower()
    assert "Show, do not announce" in core
    assert "Never name the internal state" in core
    assert "Read the card" in core
    assert "Comply with the personality" in core
    # the tool protocol is a separate paragraph, attached to the core
    assert "decision tool" in TOOL_PROTOCOL
    assert SYSTEM_CORE_WITH_TOOLS == SYSTEM_CORE + "\n\n" + TOOL_PROTOCOL


def test_typed_headers_match_l393_sketch():
    assert HEADER_SYSTEM == "#System prompt"
    assert HEADER_USER == "#User"
    assert HEADER_TOOL == "#Tool"
    assert HEADER_TOOL_CALL == "##{tool}"
    assert HEADER_THINKING == "#Thinking"
    assert HEADER_REPLY == "#Reply"
    assert HEADER_CONVERSATION == "#Conversation"


def test_render_popup_block_markers_and_trust_rule():
    content = "EVENT: the walk ended"
    block = render_popup_block(content)
    assert block.startswith("An event is arriving right now.")
    assert "real arriving event" in block
    assert POPUP_MARKER_OPEN in block
    assert POPUP_MARKER_CLOSE in block
    assert content in block
    # the marker wraps the content; the trust rule precedes it
    assert block.index(POPUP_MARKER_OPEN) < block.index(content)
    assert block.index(content) < block.index(POPUP_MARKER_CLOSE)


def _persona() -> PersonaProfile:
    return PersonaProfile(name="Nova", core="CORE TEXT.", interests=(), routines=())


def _brief() -> BehaviorBrief:
    return BehaviorBrief(
        valence=0.5, energy=0.7, reactivity=0.5, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=0.3,
    )


def _snapshot(memory: MemoryContext | None = None) -> CompanionSnapshot:
    return CompanionSnapshot(
        persona=_persona(),
        current_behavior=_brief(),
        current_activity=CurrentActivity(t_h=10.5, item=None,
                                         description="practice pottery"),
        agenda=(),
        life_arcs=(),
        memory_context=memory if memory is not None else MemoryContext(
            recent_turns=(), session_context=(), episodes=(), user_model=None,
            evidence_anchors=(),
        ),
        recent_conversation=(),
        proactive_intent=None,
    )


def test_pinned_sections_protected_under_budget_drop():
    """Pinned-section protection: with the memory section oversized past the
    cap, the whole memory section is evicted but the pinned decision/steering
    payload (current activity + event/pop-up block) is NEVER dropped."""
    huge = tuple(
        EpisodicMemory(
            id=f"ep_{i}",
            summary=f"very long episode {i} " + ("details " * 1200),
            category=MemoryKind.USER_FACT,
            occurred_at_t_h=10.0,
            created_at_t_h=12.0,
            importance=0.6,
            access_count=0,
            last_accessed_t_h=None,
            affect=None,
            source_session_id="day-0",
            source_turn_ids=(i,),
            verbatim_anchors=(),
            tags=(),
        )
        for i in range(6)
    )
    memory = MemoryContext(
        recent_turns=(), session_context=(), episodes=huge, user_model=None,
        evidence_anchors=(),
    )
    popup = render_popup_block("EVENT: pottery class starts in 10 minutes")
    prose = _render_brief(valence=0.5, energy=0.7, momentum=0.1, warmth=0.8,
                          playfulness=0.5, reflectiveness=0.4)
    prompt = assemble_snapshot(_snapshot(memory), prompt_brief=prose, popup=popup)
    assert len(prompt) <= MAX_PROMPT_CHARS
    # pinned sections survive
    assert "Current activity: practice pottery" in prompt
    assert popup in prompt
    # the oversized non-pinned section was dropped whole (never mangled)
    assert "Relevant memories:" not in prompt
    assert "very long episode" not in prompt
    # the dropped section's text never appears in fragments
    assert "details " not in prompt
