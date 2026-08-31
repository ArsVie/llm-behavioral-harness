"""WS-D (reduced, 2026-08-19) — structural prompt-cache order tests.

The DeepSeek-harness alpha read found zero ``cache_control`` in the reference
harness: caching is 100% STRUCTURAL — request N+1 is a byte-identical
extension of request N, with the stable prefix (system + tools + history)
never rewritten and the volatile runtime context appended as the LAST user
message. The reduced WS-D scope implements ONLY the structural reorder.

  PRE (one system message + transcript)        POST (two-part request)
  ------------------------------------         ----------------------
  system = core | persona | agenda |           system = core | persona
           state-card sections                           (STABLE, byte-identical
  messages = [history..., user request]                     every turn / conv)
                                               messages = [history...,
                                                           user request,
                                                           STATE CARD]  <tail
                                               state card = agenda | temporal
                                                           | ... | popup
                                                           (VOLATILE tail)

Byte-identity contract kept: ``assemble_snapshot`` (the legacy/aux full
3-tier string) outputs the SAME bytes as pre-WS-D — the agenda block merely
relocated from the day-start block into the state card (probe-verified
sha256 on this fixture before/after the change; hashes pinned below). The
stable prefix of the new layout is a byte-identical DECOMPOSITION of the
legacy prompt::

    assemble_snapshot(...) == stable_system + "\\n\\n" + state_card_tail

What changed (labeled): (1) ``render_day_block`` is now the PERSONA ONLY —
the day-plan agenda moved to the state card; (2) the state card gained a
pinned AGENDA section rendered from the identical plan lines; (3) the new
``build_context_messages`` seam returns the stable system + a message list
whose LAST element is the volatile state card (the session mainline wires
this seam; ``assemble_snapshot`` stays byte-identical for aux/experiment
callers).
"""

import dataclasses
import hashlib
from datetime import datetime, timezone

from harness.anchor import RealTimeAnchor
from harness.assembler import (
    AFFECTIVE_HEADER,
    AGENDA_HEADER,
    BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER,
    MEMORIES_HEADER,
    SYSTEM_CORE_WITH_TOOLS,
    TEMPORAL_HEADER,
    assemble_snapshot,
    build_context_messages,
    build_messages,
    render_day_block,
    render_state_card,
)
from harness.behavior import _render_brief
from harness.domain import (
    AgendaItem,
    BehaviorBrief,
    CompanionSnapshot,
    CurrentActivity,
    EpisodicMemory,
    GenerationControls,
    LifeArc,
    MemoryContext,
    MemoryKind,
    PersonaProfile,
    ProactiveIntent,
    Turn,
)
from harness.prompts import render_popup_block

#: Anchor fixture: 2026-08-15T13:30:00Z at t_h 7.5 in America/Chihuahua
#: (UTC-6 in August) → t_h 27.0 is 03:00 local, Sunday, virtual day 1.
G3_EPOCH0_S = datetime(2026, 8, 15, 13, 30, 0, tzinfo=timezone.utc).timestamp()

#: Pinned sha256 of the pre-reorder assembled prompts on this fixture,
#: reproduced byte-for-byte by the post-reorder run.
PINNED_PRE_ANCHORED_FULL = "5cfa689a6e93185ece52c433a924bedb3c5644b644e05855f3d404fd6de1d87c"
PINNED_PRE_UNANCHORED_FULL = "c5cf7cb0edb0f1822389d7ff60b41578d1022f5446bd946ad0ef282aabfd4e3e"
PINNED_PRE_BARE_FULL = "7f6411963780cfaf849a85081105aea2ec6c5a72db6ddc2a5751081f19dca5a2"


def _anchor() -> RealTimeAnchor:
    return RealTimeAnchor(epoch0_s=G3_EPOCH0_S, t_h0=7.5, tz="America/Chihuahua")


def _brief(**overrides) -> BehaviorBrief:
    base = BehaviorBrief(
        valence=0.5, energy=0.7, reactivity=0.5, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=0.3,
    )
    return dataclasses.replace(base, **overrides)


def _agenda_items() -> tuple:
    return (
        AgendaItem("ag_0", 25.5, 26.5, "agenda item 0", "arc", "arc_1", 0.8, "planned"),
        AgendaItem("ag_1", 26.5, 27.5, "agenda item 1", "arc", "arc_1", 0.8, "planned"),
    )


def _arcs() -> tuple:
    return tuple(
        LifeArc(
            id=f"arc_{i}", name=f"learning {n}", interest=n,
            started_day=1, progress=0.4, status="active",
            next_intention="practice the fundamentals",
        )
        for i, n in enumerate(["pottery", "photography", "chess"])
    )


def _episodes() -> tuple:
    return tuple(
        EpisodicMemory(
            id=f"ep_{i}", summary=f"episode summary {i}",
            category=MemoryKind.USER_FACT, occurred_at_t_h=10.0,
            created_at_t_h=12.0, importance=0.6, access_count=0,
            last_accessed_t_h=None, affect=None, source_session_id="day-0",
            source_turn_ids=(i,), verbatim_anchors=(f"anchor {i}",), tags=("x",),
        )
        for i in range(1, 4)
    )


def _snapshot(rich: bool = True) -> CompanionSnapshot:
    return CompanionSnapshot(
        persona=PersonaProfile(name="Nova", core="CORE TEXT.", interests=(), routines=()),
        current_behavior=_brief(),
        current_activity=CurrentActivity(t_h=10.5, item=None, description="practice pottery"),
        agenda=_agenda_items() if rich else (),
        life_arcs=_arcs() if rich else (),
        memory_context=MemoryContext(
            recent_turns=(Turn("user", "hi", 9.0), Turn("assistant", "hello", 9.1)),
            session_context=(),
            episodes=_episodes() if rich else (),
            user_model=None,
            evidence_anchors=(),
        ),
        recent_conversation=(Turn("user", "hi", 9.0), Turn("assistant", "hello", 9.1)),
        proactive_intent=ProactiveIntent(
            id="pi_1", reason="schedule", source_type="agenda_item",
            source_id="ag_1", hook="Agenda: pottery class (14.0-15.5h)",
            created_t_h=10.0, valid_until_t_h=13.0, salience=0.5,
            evidence="agenda_item:ag_1",
        ) if rich else None,
    )


def _controls() -> GenerationControls:
    return GenerationControls(
        response_delay_s=3.0, closing_tendency=0.3, initiative_factor=0.6,
        closing_guidance="End the conversation warmly when it winds down.",
        max_tokens=128,
    )


def _prompt_brief() -> str:
    return _render_brief(valence=0.5, energy=0.7, momentum=0.1,
                         warmth=0.8, playfulness=0.5, reflectiveness=0.4)


def _recent_turns(n: int = 4) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(n)
    ]


# --- (a) stable-prefix byte identity across turns and across conversations ---


def test_stable_system_byte_identical_across_turns():
    """Two consecutive turns with the SAME stable config share a byte-identical
    stable prefix: the whole system string must not differ, and the message
    list up to the volatile tail must be an identical prefix (only the last
    message — the state card — changes)."""
    snap = _snapshot(rich=True)
    recent = _recent_turns(4)
    controls = _controls()
    brief = _prompt_brief()
    system1, messages1 = build_context_messages(
        snapshot=snap, recent_turns=recent, user_request="hi",
        controls=controls, prompt_brief=brief, t_h=27.0, anchor=_anchor(),
    )
# Turn 2: same config, later time; the transcript gained turn 1's persisted (request, reply) pair.
    system2, messages2 = build_context_messages(
        snapshot=snap, user_request="hi again",
        recent_turns=recent
        + [{"role": "user", "content": "hi"},
           {"role": "assistant", "content": "hello"}],
        controls=controls, prompt_brief=brief, t_h=28.0, anchor=_anchor(),
    )
# The STABLE system is byte-identical across turns.
    assert system1 == system2
# The prefix up to the volatile tail is byte-identical; turn 2's history
# is turn 1's history plus the persisted (request, reply) pair.
    assert messages1[:-1] == messages2[:-3]
# Request N+1 is an extension of request N up to the tail.
    tail1 = messages1[-1]["content"]
    tail2 = messages2[-1]["content"]
    assert tail1 != tail2  # volatile tail differs between turns
    assert messages1[-1]["role"] == "user" and messages2[-1]["role"] == "user"


def test_stable_system_byte_identical_across_conversations():
    """"Across conversations" claim: a different transcript (new conversation)
    must not change the stable system bytes for the same profile/config."""
    snap = _snapshot(rich=True)
    system_a, messages_a = build_context_messages(
        snapshot=snap, recent_turns=[], user_request="hello",
        controls=_controls(), prompt_brief=_prompt_brief(),
        t_h=27.0, anchor=_anchor(),
    )
    system_b, messages_b = build_context_messages(
        snapshot=snap, recent_turns=_recent_turns(8), user_request="hi",
        controls=_controls(), prompt_brief=_prompt_brief(),
        t_h=28.0, anchor=_anchor(),
    )
    assert system_a == system_b == "\n\n".join(
        [SYSTEM_CORE_WITH_TOOLS, render_day_block(snap)]
    )
# Different conversation + different turn → different volatile tail.
    assert messages_a[-1]["content"] != messages_b[-1]["content"]


def test_constant_state_yields_byte_identical_whole_request():
    """Strongest cache claim: same config AND same state → the entire
    assembled request (system + messages) is byte-identical."""
    snap = _snapshot(rich=True)
    kwargs = dict(
        snapshot=snap, recent_turns=_recent_turns(2), user_request="hi",
        controls=_controls(), prompt_brief=_prompt_brief(),
        t_h=27.0, anchor=_anchor(),
    )
    s1, m1 = build_context_messages(**kwargs)
    s2, m2 = build_context_messages(**kwargs)
    assert s1 == s2
    assert m1 == m2


# --- (b) volatile state differs between turns and appears at the TAIL ---


def test_volatile_state_is_the_last_user_message():
    """The state card (temporal/state-card content) is the LAST user message,
    never interleaved in the stable prefix."""
    snap = _snapshot(rich=True)
    system, messages = build_context_messages(
        snapshot=snap, recent_turns=_recent_turns(3), user_request="hi",
        controls=_controls(), prompt_brief=_prompt_brief(),
        t_h=27.0, anchor=_anchor(),
    )
    tail = messages[-1]
    assert tail["role"] == "user"
# The temporal/state-card content sits at the END of the wire layout.
    assert TEMPORAL_HEADER in tail["content"]
    assert AGENDA_HEADER in tail["content"]
    assert tail["content"].startswith(AGENDA_HEADER)  # agenda opens the card
# Volatile markers stay out of the stable prefix (system message).
    for volatile_marker in (
        TEMPORAL_HEADER, AFFECTIVE_HEADER, BEHAVIORAL_HEADER,
        CURRENT_INTENT_HEADER, AGENDA_HEADER, MEMORIES_HEADER,
    ):
        assert volatile_marker not in system, volatile_marker
# The time line and the last message differ between turns; the stable prefix does not.
    system2, messages2 = build_context_messages(
        snapshot=snap, recent_turns=_recent_turns(3), user_request="hi",
        controls=_controls(), prompt_brief=_prompt_brief(),
        t_h=28.0, anchor=_anchor(),
    )
    assert messages2[-1]["content"] != tail["content"]
    assert system == system2


def test_unanchored_replay_tail_still_carries_agenda():
    """Unanchored (replay/test) runs omit the temporal section entirely (G2)
    but the day-plan AGENDA section still rides in the tail — the pre-WS-D
    day-block agenda is not lost for unanchored runs."""
    system, messages = build_context_messages(
        snapshot=_snapshot(rich=True), recent_turns=[], user_request="hi",
        controls=_controls(), prompt_brief=_prompt_brief(),
    )
    tail = messages[-1]["content"]
    assert TEMPORAL_HEADER not in tail  # unanchored: not raw t_h
    assert AGENDA_HEADER in tail  # the moved day-plan view survives
    assert "agenda item 0" in tail
    assert TEMPORAL_HEADER not in system and AGENDA_HEADER not in system


# --- (c) replay parity: byte identity vs the pre-reorder layout ---


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_assemble_snapshot_bytes_unchanged_vs_pre_reorder():
    """The legacy/aux full 3-tier string is BYTE-IDENTICAL to the pre-reorder
    layout on this fixture (hashes pinned from the pre-change run at
    e2b830d). Labeled change: the agenda block relocated from the day-start
    block into the state card; the composed bytes did not change."""
    snap = _snapshot(rich=True)
    anchored = assemble_snapshot(
        snap, controls=_controls(), prompt_brief=_prompt_brief(),
        popup=render_popup_block("EVENT: pottery class starts in 10 minutes"),
        t_h=27.0, anchor=_anchor(),
    )
    unanchored = assemble_snapshot(
        snap, controls=_controls(), prompt_brief=_prompt_brief(),
    )
    bare = assemble_snapshot(_snapshot(rich=False))
    assert _sha256(anchored) == PINNED_PRE_ANCHORED_FULL
    assert _sha256(unanchored) == PINNED_PRE_UNANCHORED_FULL
    assert _sha256(bare) == PINNED_PRE_BARE_FULL


def test_new_layout_is_exact_decomposition_of_legacy_prompt():
    """The WS-D layout does not re-word or re-order content: the legacy full
    prompt decomposes EXACTLY into (stable system) + '\\n\\n' + (volatile
    tail). The stable system is a byte-identical PREFIX of the legacy prompt
    — nothing in the stable prefix changed."""
    snap = _snapshot(rich=True)
    controls = _controls()
    brief = _prompt_brief()
    popup = render_popup_block("EVENT: pottery class starts in 10 minutes")
    legacy = assemble_snapshot(
        snap, controls=controls, prompt_brief=brief, popup=popup,
        t_h=27.0, anchor=_anchor(),
    )
    system, messages = build_context_messages(
        snapshot=snap, recent_turns=[], user_request=None,
        controls=controls, prompt_brief=brief, popup=popup,
        t_h=27.0, anchor=_anchor(),
    )
    tail = messages[-1]["content"]
# Exact decomposition: stable prefix + one separator + volatile tail.
    assert legacy == system + "\n\n" + tail
# The stable system is the byte-identical prefix of the legacy prompt.
    assert legacy.startswith(system)
    assert legacy[: len(system)] == system
# The volatile tail is the byte-identical suffix (after the separator).
    assert legacy.endswith(tail)
    assert legacy[len(system) + 2:] == tail


def test_render_day_block_is_persona_only():
    """Labeled WS-D change: the day-start block is the STABLE persona only;
    the agenda plan lives in the volatile tail (AGENDA section)."""
    snap = _snapshot(rich=True)
    block = render_day_block(snap)
    assert block == "CORE TEXT."
    assert AGENDA_HEADER not in block  # agenda moved to the tail
    tail = render_state_card(snap)
    assert AGENDA_HEADER in tail
    assert "agenda item 0" in tail
# Skipped and past items are not planned (NOW semantics), as before.
    skipped = dataclasses.replace(
        _snapshot(rich=True),
        agenda=(
            AgendaItem("ag_s", 25.5, 26.5, "skipped thing", "routine", "r1", 0.9, "skipped"),
            AgendaItem("ag_p", 30.0, 31.0, "planned thing", "routine", "r2", 0.3, "planned"),
        ),
    )
    tail_skip = render_state_card(skipped)
    assert "skipped thing" not in tail_skip
    assert "planned thing" in tail_skip


def test_seam_deterministic_replay_parity():
    """Replay parity at the assembly level: the seam is a pure function of its
    inputs — same inputs (e.g. same-seed fake run) yield byte-identical
    (system, messages), so the reorder cannot change reproduction."""
    kwargs = dict(
        snapshot=_snapshot(rich=True),
        recent_turns=_recent_turns(4),
        user_request="hi",
        controls=_controls(), prompt_brief=_prompt_brief(),
        t_h=27.0, anchor=_anchor(),
    )
    s1, m1 = build_context_messages(**kwargs)
    s2, m2 = build_context_messages(**kwargs)
    assert s1 == s2
    assert m1 == m2
# Legacy helpers stay untouched, byte-compatible with older callers.
    assert build_messages(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], "c"
    ) == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]