"""WS-D (reduced) -- structural prompt-cache order tests.

Caching here is structural: request N+1 is a byte-identical extension of
request N, with the stable prefix (system + tools + history) never rewritten
and the volatile runtime context appended as the LAST message.

    system   = core | persona                    (STABLE prefix)
    messages = [history..., user request, STATE CARD]   <- volatile tail

``assemble_snapshot`` keeps the legacy full 3-tier string for aux/experiment
callers, as a byte-identical decomposition of the two parts.
"""

import dataclasses
from datetime import datetime, timezone

from harness.anchor import RealTimeAnchor
from harness.prompts import MEMORIES_HEADER
from harness.assembler import (
    AFFECTIVE_HEADER,
    AGENDA_HEADER,
    BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER,
    TEMPORAL_HEADER,
    assemble_snapshot,
    build_context_messages,
    build_messages,
    render_day_block,
    render_day_start_block,
    render_state_card,
    stable_system,
)
from harness.assembler import prefix_break, wire_pair
from harness.domain import (
    AgendaItem,
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
from tests.helpers.domain_builders import sample_brief, sample_prompt_brief

#: Anchor fixture: 2026-08-15T13:30:00Z at t_h 7.5 in America/Chihuahua
#: (UTC-6 in August) → t_h 27.0 is 03:00 local, Sunday, virtual day 1.
G3_EPOCH0_S = datetime(2026, 8, 15, 13, 30, 0, tzinfo=timezone.utc).timestamp()


def test_prefix_break_is_none_when_the_request_only_appends():
    previous = [{"role": "system", "content": "core"},
                {"role": "user", "content": "hi"}]
    assert prefix_break(previous, previous + [{"role": "assistant", "content": "yo"}]) is None


def test_prefix_break_reports_the_first_rewritten_message():
    previous = [{"role": "system", "content": "core"},
                {"role": "user", "content": "hi"}]
    edited = [{"role": "system", "content": "core EDITED"},
              {"role": "user", "content": "hi"}]
    assert prefix_break(previous, edited) == 0
    # A request that SHRANK stops extending at the index it no longer covers.
    assert prefix_break(previous, previous[:1]) == 1


def test_wire_pair_compares_text_not_object_identity():
    # A rebuilt message carrying identical text is not a change: the provider
    # sees bytes, not Python object identity.
    assert prefix_break([{"role": "user", "content": "hi"}],
                        [{"role": "user", "content": "hi"}]) is None
    assert wire_pair({"role": "user", "content": "hi", "extra": 1}) == ("user", "hi")


def _anchor() -> RealTimeAnchor:
    return RealTimeAnchor(epoch0_s=G3_EPOCH0_S, t_h0=7.5, tz="America/Chihuahua")


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
        current_behavior=sample_brief(),
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


def _recent_turns(n: int = 4) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(n)
    ]


# --- (a) stable-prefix byte identity across turns and across conversations ---


def test_stable_system_byte_identical_across_turns():
    """Two consecutive turns share a byte-identical stable prefix, and request
    N+1 is request N plus the persisted (reply, next request) rows."""
    snap = _snapshot(rich=True)
    recent = _recent_turns(4)
    system1, messages1 = build_context_messages(
        snapshot=snap, recent_turns=recent, user_request="hi",
        t_h=27.0, anchor=_anchor(),
    )
    system2, messages2 = build_context_messages(
        snapshot=snap, user_request="hi again",
        recent_turns=recent
        # The persisted row carries its own t_h, which makes the stamp
        # reproducible across turns.
        + [{"role": "user", "content": "hi", "t_h": 27.0},
           {"role": "assistant", "content": "hello"}],
        t_h=28.0, anchor=_anchor(),
    )
    assert system1 == system2
    # Strict extension: request N+1 = request N + the persisted pair + the
    # new request. The state card never rides the request.
    assert messages2[:len(messages1)] == messages1
    assert [m["role"] for m in messages2[len(messages1):]] == ["assistant", "user"]


def test_stable_system_byte_identical_across_conversations():
    """"Across conversations" claim: a different transcript (new conversation)
    must not change the stable system bytes for the same profile/config."""
    snap = _snapshot(rich=True)
    system_a, messages_a = build_context_messages(
        snapshot=snap, recent_turns=[], user_request="hello",
        t_h=27.0, anchor=_anchor(),
    )
    system_b, messages_b = build_context_messages(
        snapshot=snap, recent_turns=_recent_turns(8), user_request="hi",
        t_h=28.0, anchor=_anchor(),
    )
    # The claim: a different transcript must not change the stable bytes.
    assert system_a == system_b
    # And the bytes are exactly the canonical stable assembly for this day
    # block (source-derived, so a new stable block never edits this test).
    assert system_a == stable_system(render_day_block(snap))
    # The requests carry true transcript roles only: no card block anywhere.
    assert all(m["role"] != "system" for m in messages_a + messages_b)
    # The card render is a pure function of the snapshot: same state, same
    # bytes — that is what the stream-row change gate compares.
    card = render_state_card(snap, controls=_controls(), prompt_brief=sample_prompt_brief())
    assert card == render_state_card(
        snap, controls=_controls(), prompt_brief=sample_prompt_brief()
    )


# --- (b) volatile state differs between turns and appears at the TAIL ---


def test_the_role_scan_flags_harness_vocabulary_in_a_user_slot():
    """Pure scan behind the runtime sensor: harness vocabulary, not brackets."""
    from harness.assembler import harness_text_in_user_roles
    from harness.steering import wrap_steer_marker

    assert harness_text_in_user_roles([
        {"role": "user", "content": "what about [this] bracket, though?"},
        {"role": "system", "content": wrap_steer_marker("Event: he is back")},
    ]) == [], "the marker in a SYSTEM slot is the convention working"

    # Every family of harness-written text must be caught if it lands in a
    # slot the user owns.
    for leaked in (
        wrap_steer_marker("Event: he is back"),
        "TEMPORAL FRAME:\nIt is 13:34, Saturday afternoon \u2014 day 4.",
        "Happening now:\n- sand the last tight curve",
        "Relevant memories:\n- she likes villanelles",
    ):
        assert harness_text_in_user_roles(
            [{"role": "user", "content": "hey"}, {"role": "user", "content": leaked}]
        ) == [1], leaked[:26]

    # Defensive: junk entries and null content must not explode a live call.
    assert harness_text_in_user_roles([
        {"role": "user", "content": None}, "junk", {"role": "tool", "content": "x"},
    ]) == []


def test_the_role_scan_does_not_flag_a_real_built_request():
    """No false positives on the real shape -- a sensor that cries wolf is dead.

    The card (even with a pop-up rendered inside it) is a SYSTEM message and
    must scan clean, and so must an ordinary user turn.
    """
    from harness.assembler import harness_text_in_user_roles

    _, spoken = build_context_messages(
        _snapshot(), _recent_turns(3), "hey", t_h=27.0, anchor=_anchor(),
    )
    assert harness_text_in_user_roles(spoken) == []

    _, silent = build_context_messages(
        _snapshot(), _recent_turns(3), None, t_h=27.0, anchor=_anchor(),
    )
    assert harness_text_in_user_roles(silent) == []


def test_a_turn_the_user_did_not_speak_has_no_user_message_at_all():
    """The convention, pinned (CONVENTIONS:27, architecture-overview.md:33).

    Internal events are system-level context, never a user-role message.
    """
    _, silent = build_context_messages(
        _snapshot(), _recent_turns(3), None, t_h=27.0, anchor=_anchor(),
    )
    assert [m["role"] for m in silent] == ["user", "assistant", "user"]
    assert all(m["content"] for m in silent if m["role"] == "user"), (
        "no user message may carry empty content or harness text"
    )

    # The speaking turn appends one user-role message; nothing follows it.
    _, spoken = build_context_messages(
        _snapshot(), _recent_turns(3), "hey", t_h=27.0, anchor=_anchor(),
    )
    assert spoken[-1]["role"] == "user"
    assert spoken[-1]["content"].startswith("hey")
    assert " | Time: " in spoken[-1]["content"]


def test_the_card_never_rides_the_request():
    """The state card is a stream row (append-only, change-gated), never a
    request block: requests carry transcript roles only, and the card's
    volatile markers stay out of the stable prefix."""
    snap = _snapshot(rich=True)
    system, messages = build_context_messages(
        snapshot=snap, recent_turns=_recent_turns(3), user_request="hi",
        t_h=27.0, anchor=_anchor(),
    )
    assert all(m["role"] != "system" for m in messages), messages
    for volatile_marker in (
        TEMPORAL_HEADER, AFFECTIVE_HEADER, BEHAVIORAL_HEADER,
        CURRENT_INTENT_HEADER, AGENDA_HEADER, MEMORIES_HEADER,
    ):
        assert volatile_marker not in system, volatile_marker
    # The card render keeps the moving state and leaves the day plan out.
    card = render_state_card(snap, controls=_controls(), prompt_brief=sample_prompt_brief())
    assert card.startswith(AFFECTIVE_HEADER)
    assert AGENDA_HEADER not in card
    # Same state: same card bytes — the stream-row change gate holds.
    assert render_state_card(
        snap, controls=_controls(), prompt_brief=sample_prompt_brief()
    ) == card


def test_unanchored_replay_omits_temporal_and_the_agenda_moved_to_day_start():
    """Unanchored (replay/test) runs omit the temporal section entirely (G2).

    The day-plan AGENDA moved to the once-a-day stream block, so it is absent
    here and present there.
    """
    snap = _snapshot(rich=True)
    system, messages = build_context_messages(
        snapshot=snap, recent_turns=[], user_request="hi",
    )
    for m in messages:
        content = m.get("content") or ""
        assert TEMPORAL_HEADER not in content  # unanchored: not raw t_h
        assert AGENDA_HEADER not in content
    assert TEMPORAL_HEADER not in system and AGENDA_HEADER not in system

    day_start = render_day_start_block(snap)
    assert AGENDA_HEADER in day_start
    assert "agenda item 0" in day_start


#: Every section header the assembled request can carry.
_ALL_HEADERS = (
    AGENDA_HEADER, TEMPORAL_HEADER, AFFECTIVE_HEADER, BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER, MEMORIES_HEADER,
)


def test_the_layout_loses_no_content_and_duplicates_none():
    """Content preservation across the three scopes, with no overlap.

    Nothing was dropped and nothing is sent twice: every section lives in
    exactly one scope.
    """
    snap = _snapshot(rich=True)
    controls = _controls()
    brief = sample_prompt_brief()
    popup = render_popup_block("EVENT: pottery class starts in 10 minutes")
    legacy = assemble_snapshot(
        snap, controls=controls, prompt_brief=brief, popup=popup,
    )
    system, messages = build_context_messages(
        snapshot=snap, recent_turns=[], user_request=None,
        t_h=27.0, anchor=_anchor(),
    )
    scopes = {
        "system": system,
        "day_start": render_day_start_block(snap),
        "card": render_state_card(snap, controls=controls, prompt_brief=brief),
    }
    for header in _ALL_HEADERS:
        if header not in legacy:
            continue
        holders = [name for name, text in scopes.items() if header in text]
        assert holders, f"{header!r} was dropped entirely"
        assert len(holders) == 1, f"{header!r} is sent twice: {holders}"
# The stable system is still the byte-identical prefix of the legacy prompt:
# nothing in the persona/rules core moved.
    assert legacy.startswith(system)


def test_the_card_never_carries_a_temporal_frame():
    """No clock reading, no partition: the card is per-moment state only."""
    card = render_state_card(_snapshot(rich=True))
    assert TEMPORAL_HEADER not in card
    assert "It is " not in card
    assert BEHAVIORAL_HEADER in card, "every other section still rides"


def test_the_three_scopes_are_disjoint():
    """Each piece of state sits in exactly one place, by how often it changes.

    * STABLE prefix (``render_day_block``) -- the persona.
    * DAY stream block (``render_day_start_block``) -- plan, arcs, user model.
    * PER-TURN card (``render_state_card``) -- only what actually moves.
    """
    snap = _snapshot(rich=True)
    stable = render_day_block(snap)
    day_start = render_day_start_block(snap)
    card = render_state_card(snap)

    assert stable == "CORE TEXT."
    assert AGENDA_HEADER not in stable
    assert AGENDA_HEADER in day_start        # the plan is day-scoped
    assert AGENDA_HEADER not in card         # and no longer per-turn
    # The card keeps what genuinely moves.
    assert BEHAVIORAL_HEADER in card
    assert "agenda item 0" in day_start
# Skipped and past items are not planned (NOW semantics), as before — the
# filter moved with the section, so it is asserted where the section now is.
    skipped = dataclasses.replace(
        _snapshot(rich=True),
        agenda=(
            AgendaItem("ag_s", 25.5, 26.5, "skipped thing", "routine", "r1", 0.9, "skipped"),
            AgendaItem("ag_p", 30.0, 31.0, "planned thing", "routine", "r2", 0.3, "planned"),
        ),
    )
    day_start_skip = render_day_start_block(skipped)
    assert "skipped thing" not in day_start_skip
    assert "planned thing" in day_start_skip


def test_seam_transcript_matches_legacy_build_messages():
    """Task-11 switch gate: the seam carries the SAME transcript bytes as the
    legacy mainline path — only the state card moves (system → trailing system
    message). After the switch the model sees identical history bytes."""
    snap = _snapshot(rich=True)
    recent = _recent_turns(4)
    controls = _controls()
    brief = sample_prompt_brief()
    day_block = render_day_block(snap)  # the session-cached block, as wired
    legacy_system = assemble_snapshot(
        snap, controls=controls, prompt_brief=brief, day_block=day_block,
    )
    # Same anchor and turn time: identical bytes for identical inputs, and the
    # user-turn stamp is one of those inputs.
    legacy_messages = build_messages(recent, "hi", anchor=_anchor(), t_h=27.0)
    system, messages = build_context_messages(
        snapshot=snap, recent_turns=recent, user_request="hi",
        t_h=27.0, anchor=_anchor(), day_block=day_block,
    )
    # The seam IS the legacy transcript builder, byte for byte — no blocks.
    assert messages == legacy_messages
    # Content parity with the legacy request, across the three scopes. Byte
    # concatenation no longer holds: day-scoped state left the prompt.
    covered = system + "\n\n" + render_day_start_block(snap) \
        + "\n\n" + render_state_card(snap, controls=controls, prompt_brief=brief)
    for header in _ALL_HEADERS:
        if header in legacy_system:
            assert header in covered, f"{header!r} was dropped"
    # No-request variant: the transcript passes through untouched.
    system2, messages2 = build_context_messages(
        snapshot=snap, recent_turns=recent, user_request=None,
        t_h=27.0, anchor=_anchor(), day_block=day_block,
    )
    assert messages2 == [
        {"role": turn["role"], "content": turn["content"]}
        for turn in recent
    ]
    assert system2 == system
