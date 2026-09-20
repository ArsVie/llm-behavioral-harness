"""The state card as full cards plus section-level updates.

A mid-day change appends an UPDATE row against the last FULL card — only the
sections that changed, with ``(cleared)`` for sections that went away — and
a day's first card is always the full one. The stream stays append-only.
"""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import (
    AFFECTIVE_HEADER,
    STATE_UPDATE_LEAD,
    apply_state_update,
    card_blocks,
    render_state_card,
    render_state_update,
)
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import (
    BehaviorBrief,
    CompanionSnapshot,
    MemoryContext,
    PersonaProfile,
)
from harness.session import Session
from harness.tools import DecisionConfig
from tests.helpers.store import make_store

CARD = (
    "AFFECTIVE BEARING:\n"
    "Current bearing: bright, calm.\n"
    "\n"
    "Availability: calmly present and available.\n"
    "\n"
    "BEHAVIORAL BEARING:\n"
    "You follow the lead on topics; you keep the thread.\n"
    "\n"
    "CURRENT INTENT:\n"
    "No active intent."
)


def test_only_changed_sections_are_sent():
    changed = CARD.replace("bright, calm", "bright, subdued")
    update = render_state_update(CARD, changed)
    assert update.startswith(STATE_UPDATE_LEAD)
    assert "bright, subdued" in update
    assert "BEHAVIORAL BEARING" not in update
    assert "CURRENT INTENT" not in update
    assert "Availability:" not in update  # unchanged — stays out of the row


def test_a_vanished_section_is_marked_cleared():
    without = CARD.replace("\n\nAvailability: calmly present and available.", "")
    update = render_state_update(CARD, without)
    assert "Availability: (cleared)" in update
    assert "BEHAVIORAL BEARING" not in update


def test_an_update_never_outgrows_the_card():
    other = (
        "AFFECTIVE BEARING:\nCurrent bearing: quiet.\n\n"
        "Availability: low on energy.\n\n"
        "BEHAVIORAL BEARING:\nYou pause before answering.\n\n"
        "CURRENT INTENT:\nWaiting."
    )
    assert render_state_update(CARD, other) == other


def test_an_unchanged_card_returns_itself():
    assert render_state_update(CARD, CARD) == CARD


def test_availability_is_its_own_section():
    card = render_state_card(_snapshot(), prompt_brief="Current bearing: calm.")
    blocks = dict(card_blocks(card))
    assert "Availability" in blocks
    assert "Availability" not in blocks["AFFECTIVE BEARING"]
    assert blocks["AFFECTIVE BEARING"] == AFFECTIVE_HEADER + "\nCurrent bearing: calm."


def _snapshot() -> CompanionSnapshot:
    return CompanionSnapshot(
        persona=PersonaProfile(name="Nova", core="CORE TEXT.", interests=(), routines=()),
        current_behavior=BehaviorBrief(
            valence=0.5, energy=0.7, reactivity=0.5, warmth=0.8,
            expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
            initiative=0.6, response_length_scale=1.0, response_delay_s=3.0,
            closing_tendency=0.3,
        ),
        current_activity=None,
        agenda=(),
        life_arcs=(),
        memory_context=MemoryContext(
            recent_turns=(), session_context=(), episodes=(),
            user_model=None, evidence_anchors=(),
        ),
        recent_conversation=(),
        proactive_intent=None,
    )


def _session(tmp_path):
    store = make_store(tmp_path)
    session = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=FakeClient(responses=[]), clock=VirtualClock(t_h=10.0),
        decision_config=DecisionConfig(),
    )
    return store, session


def _rows(store, day: int) -> list[str]:
    return [m["content"] for m in store.messages_for_day(day)
            if m["role"] == "system"]


def test_a_changed_card_appends_a_section_update(tmp_path):
    store, session = _session(tmp_path)
    snapshot = _snapshot()
    session._ensure_state_card(snapshot, "Current bearing: calm.", 0, 10.0)
    rows = _rows(store, 0)
    assert len(rows) == 1
    assert rows[0].startswith(AFFECTIVE_HEADER)

    session._ensure_state_card(snapshot, "Current bearing: bright.", 0, 11.0)
    rows = _rows(store, 0)
    assert len(rows) == 2
    assert rows[1].startswith(STATE_UPDATE_LEAD)
    assert "Current bearing: bright." in rows[1]
    assert "CURRENT INTENT" not in rows[1]  # unchanged — stays out

    # The state stands: no further row.
    session._ensure_state_card(snapshot, "Current bearing: bright.", 0, 12.0)
    assert len(_rows(store, 0)) == 2

    # A new day opens with the full card again.
    session._ensure_state_card(snapshot, "Current bearing: bright.", 1, 25.0)
    assert len(_rows(store, 0)) == 2
    day_one = _rows(store, 1)
    assert len(day_one) == 1
    assert day_one[0].startswith(AFFECTIVE_HEADER)
    assert not day_one[0].startswith(STATE_UPDATE_LEAD)


def test_a_restart_diffs_against_the_full_memo(tmp_path):
    store, session = _session(tmp_path)
    snapshot = _snapshot()
    session._ensure_state_card(snapshot, "Current bearing: calm.", 0, 10.0)
    session._ensure_state_card(snapshot, "Current bearing: bright.", 0, 11.0)

    fresh = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=FakeClient(responses=[]), clock=VirtualClock(t_h=12.0),
        decision_config=DecisionConfig(),
    )
    # Resumed from the persisted FULL memo: unchanged state, nothing appended.
    fresh._ensure_state_card(snapshot, "Current bearing: bright.", 0, 12.0)
    assert len(_rows(store, 0)) == 2

    # A change diffs against the FULL card — not against the update row.
    fresh._ensure_state_card(snapshot, "Current bearing: stormy.", 0, 13.0)
    rows = _rows(store, 0)
    assert len(rows) == 3
    assert rows[2].startswith(STATE_UPDATE_LEAD)
    assert "Current bearing: stormy." in rows[2]
    assert "CURRENT INTENT" not in rows[2]


def test_a_restart_without_the_memo_diffs_instead_of_resending(tmp_path):
    """The recovered baseline carries its DAY: a missing kv memo must not
    turn an already-carded day into "the day's first card" and re-send the
    full card mid-day (live run: the 02:09 restart re-sent day 1's card)."""
    store, session = _session(tmp_path)
    snapshot = _snapshot()
    session._ensure_state_card(snapshot, "Current bearing: calm.", 1, 25.0)
    assert len(_rows(store, 1)) == 1

    # The memo is gone (a store written before the memo landed, or a wiped
    # kv); the card row in the stream stays.
    store.conn.execute("DELETE FROM kv_store WHERE key LIKE 'state_card.%'")
    store.conn.commit()

    fresh = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=FakeClient(responses=[]), clock=VirtualClock(t_h=26.0),
        decision_config=DecisionConfig(),
    )
    # Same state, same day: nothing appended — the day came off the row.
    fresh._ensure_state_card(snapshot, "Current bearing: calm.", 1, 26.0)
    assert len(_rows(store, 1)) == 1

    # A change diffs against the recovered full card, not a re-sent card.
    fresh._ensure_state_card(snapshot, "Current bearing: stormy.", 1, 27.0)
    rows = _rows(store, 1)
    assert len(rows) == 2
    assert rows[1].startswith(STATE_UPDATE_LEAD)
    assert "Current bearing: stormy." in rows[1]
    assert "CURRENT INTENT" not in rows[1]


def test_a_memo_without_its_day_reads_the_stream(tmp_path):
    """A missing ``state_card.day`` — a key a deploy just introduced, a
    partial kv — used to read as "the day's first card" and re-send the full
    card mid-day (the live 02:09 restart's duplicate). The stream names the
    day even when the cache cannot."""
    store, session = _session(tmp_path)
    snapshot = _snapshot()
    session._ensure_state_card(snapshot, "Current bearing: calm.", 1, 25.0)
    assert len(_rows(store, 1)) == 1

    store.conn.execute("DELETE FROM kv_store WHERE key = 'state_card.day'")
    store.conn.commit()

    fresh = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=FakeClient(responses=[]), clock=VirtualClock(t_h=26.0),
        decision_config=DecisionConfig(),
    )
    fresh._ensure_state_card(snapshot, "Current bearing: calm.", 1, 26.0)
    assert len(_rows(store, 1)) == 1, "the day already had its card"

    fresh._ensure_state_card(snapshot, "Current bearing: stormy.", 1, 27.0)
    rows = _rows(store, 1)
    assert len(rows) == 2
    assert rows[1].startswith(STATE_UPDATE_LEAD)


def test_a_wiped_memo_rebuilds_from_the_stream(tmp_path):
    """Both halves gone: the newest full card row plus the update rows after
    it rebuild the accumulated text, so a wiped cache costs no bytes at all
    (verified against the live run's own memo: 625 chars, byte-identical)."""
    store, session = _session(tmp_path)
    snapshot = _snapshot()
    session._ensure_state_card(snapshot, "Current bearing: calm.", 1, 25.0)
    session._ensure_state_card(snapshot, "Current bearing: stormy.", 1, 26.0)
    assert len(_rows(store, 1)) == 2

    store.conn.execute("DELETE FROM kv_store WHERE key LIKE 'state_card.%'")
    store.conn.commit()

    fresh = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=FakeClient(responses=[]), clock=VirtualClock(t_h=27.0),
        decision_config=DecisionConfig(),
    )
    fresh._ensure_state_card(snapshot, "Current bearing: stormy.", 1, 27.0)
    assert len(_rows(store, 1)) == 2, "the rebuilt memo matched the render"

    fresh._ensure_state_card(snapshot, "Current bearing: calm.", 1, 28.0)
    rows = _rows(store, 1)
    assert len(rows) == 3 and rows[2].startswith(STATE_UPDATE_LEAD)
    assert "Current bearing: calm." in rows[2]


def test_an_update_applies_back_onto_the_card():
    """``apply_state_update`` inverts ``render_state_update`` — that is what
    lets the loader rebuild the memo from the stream. Changed sections
    replace, cleared ones drop, a key the update introduces lands last."""
    changed = CARD.replace("bright, calm.", "bright, stormy.")
    assert apply_state_update(CARD, render_state_update(CARD, changed)) == changed

    without = "\n\n".join(
        b for b in CARD.split("\n\n") if not b.startswith("CURRENT INTENT")
    )
    assert apply_state_update(CARD, render_state_update(CARD, without)) == without

    added = CARD + "\n\nCurrent activity: read history"
    assert apply_state_update(CARD, render_state_update(CARD, added)) == added

    # A row that is not an update (a full card, a day-start block) is skipped.
    assert apply_state_update(CARD, added) == CARD
    assert apply_state_update(CARD, "Sunday's plan:\n- read history") == CARD


def test_a_cleared_block_without_a_header_still_drops():
    """A block whose first line carries no parseable header — the per-turn
    guidance a proactive turn writes into the card — is added by one update
    and cleared by the next. The clear marker is keyed by TEXT, not by the
    header grammar: parsing it back through `card_blocks` yields a different
    key than the one that was written (live: the proactive turn's row #39
    added the guidance, #41 cleared it)."""
    guidance = (
        "You are reaching out first. Agenda: rewrite the proof (12:00–13:04)\n"
        "State what you are reaching out about naturally in your FIRST sentence."
    )
    with_it = f"{CARD}\n\n{guidance}"
    added = render_state_update(CARD, with_it)
    assert apply_state_update(CARD, added) == with_it

    cleared = render_state_update(with_it, CARD)
    assert "(cleared)" in cleared
    assert apply_state_update(with_it, cleared) == CARD


def test_an_update_row_from_an_earlier_build_still_applies():
    """The lead lost its clause on 2026-09-20. A run that spans the rename
    carries rows in both shapes — the live stream does — and the memo loader
    walks exactly such a stream, so the reader accepts the pair."""
    changed = CARD.replace("bright, calm.", "bright, subdued.")
    rendered = render_state_update(CARD, changed)
    assert rendered.startswith(STATE_UPDATE_LEAD)
    legacy = "STATE UPDATE — only what changed:" + rendered[len(STATE_UPDATE_LEAD):]
    assert apply_state_update(CARD, legacy) == changed
