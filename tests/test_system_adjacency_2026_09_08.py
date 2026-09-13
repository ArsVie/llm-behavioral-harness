"""The wire never carries two system messages in a row.

The 2026-09-08 leak: a conversational turn came back as DeepSeek DSML
tool-call markup and was persisted as her message and sent to the channel.

The cause was adjacency. A turn's tail could stack up to FOUR ``role="system"``
messages — state card, injections, decided-notes, and on an aux call the
pop-up — with the last assistant turn far behind them. A run of system blocks
reads as one undifferentiated instruction block, and the model answers
whichever question it latches onto: a pop-up as prose (the decision-lane parse
failures), or a conversational turn as a tool call (the leak).

So blocks FOLD into one trailing system message. Exactly one place in the
message list is addressing the model, immediately before it answers.

Two things deliberately NOT done, pinned here so they are not "fixed" later:

* No synthetic assistant turn is interleaved between system blocks. An
  assistant message the model never produced is a lie in its own history.
* Replayed DECISIONS keep their real ``assistant tool_calls -> role="tool"``
  exchange. Those turns actually happened; this is only about the live tail.
"""

from __future__ import annotations

import itertools

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import SYSTEM_BLOCK_SEPARATOR, append_system
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.session import Session
from harness.tools import DecisionConfig
from tests.helpers.store import make_store


def _no_adjacent_system(messages) -> bool:
    roles = [m.get("role") for m in messages]
    return not any(a == "system" and b == "system"
                   for a, b in itertools.pairwise(roles))


# the helper


def test_folds_into_a_trailing_system_message():
    msgs = [{"role": "user", "content": "hi"}]
    msgs = append_system(msgs, "card")
    msgs = append_system(msgs, "popup")
    assert [m["role"] for m in msgs] == ["user", "system"]
    assert msgs[-1]["content"] == "card" + SYSTEM_BLOCK_SEPARATOR + "popup"


def test_folding_never_disturbs_earlier_messages():
    original = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey"}]
    out = append_system(list(original), "card")
    assert out[:2] == original


def test_empty_blocks_are_dropped_not_folded():
    msgs = [{"role": "assistant", "content": "hey"}]
    assert append_system(list(msgs), "") == msgs
    assert append_system(list(msgs), None) == msgs
    assert append_system(list(msgs), "   \n ") == msgs


def test_a_system_block_after_an_assistant_turn_starts_a_new_message():
    msgs = [{"role": "system", "content": "old"},
            {"role": "assistant", "content": "hey"}]
    out = append_system(msgs, "card")
    assert [m["role"] for m in out] == ["system", "assistant", "system"]
    assert out[-1]["content"] == "card"


def test_no_synthetic_assistant_turn_is_invented():
    msgs = append_system(append_system([], "a"), "b")
    assert all(m["role"] != "assistant" for m in msgs)


# the assembled turn


def _session(tmp_path, client, t_h=10.0):
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (
        AgendaItem("ag1", 9.0, 11.0, "pottery", "arc", "arc1", 0.8, "planned"),
    )))
    session = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=client, clock=VirtualClock(t_h=t_h),
        decision_config=DecisionConfig(),
    )
    return store, session


def test_a_mainline_turn_sends_no_adjacent_system_messages(tmp_path):
    """The tail carries a state card AND a decided-note; they must fold."""
    client = FakeClient(responses=[
        {"content": "main reply",
         "tool_calls": [{"id": "c1", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": \"yes\", \"reason\": \"in the mood\"}"}]},
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        for call in client.calls:
            assert _no_adjacent_system(call["messages"]), [
                m.get("role") for m in call["messages"]
            ]
    finally:
        store.close()


def test_an_aux_popup_call_sends_no_adjacent_system_messages(tmp_path):
    """The aux call appends the pop-up behind the state card — the exact
    pair that produced the live leak."""
    client = FakeClient(responses=[
        {"content": "main reply",
         "tool_calls": [{"id": "c2", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": \"yes\", \"reason\": \"in the mood\"}"}]},
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        popup_calls = [c for c in client.calls
                       if any("Event:" in (m.get("content") or "")
                              for m in c["messages"])]
        assert popup_calls, "no pop-up call was made — the lane is not covered"
        for call in popup_calls:
            roles = [m.get("role") for m in call["messages"]]
            assert _no_adjacent_system(call["messages"]), roles
            # The pop-up is still the LAST thing the model sees.
            assert roles[-1] == "system"
            assert "Event:" in call["messages"][-1]["content"]
    finally:
        store.close()


def test_the_state_card_and_popup_arrive_in_one_block(tmp_path):
    """Folded, not dropped: both blocks still reach the model."""
    client = FakeClient(responses=[
        {"content": "main reply",
         "tool_calls": [{"id": "c3", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": \"no\", \"reason\": \"not now\"}"}]},
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        popup = next(c for c in client.calls
                     if any("Event:" in (m.get("content") or "")
                            for m in c["messages"]))
        tail = popup["messages"][-1]["content"]
        assert "Event:" in tail, "the pop-up was lost in the fold"
        assert SYSTEM_BLOCK_SEPARATOR in tail, (
            "nothing was folded — the state card is missing from the aux call"
        )
    finally:
        store.close()


# the day block: written once, never re-rendered


def test_the_day_block_is_written_once_and_never_rerendered(tmp_path):
    """The whole cache argument in one test.

    The day block is rendered ONCE and persisted; every later turn replays
    the stored bytes. So its wording can change freely — a dated header, a
    new section — without touching a single already-cached prefix, because
    nothing re-renders an emitted block.

    What would break the cache is re-emitting or re-rendering it per turn,
    which is precisely what the per-turn state card was doing with it.
    """
    client = FakeClient(responses=["r1", "r2", "r3"])
    store, session = _session(tmp_path, client, t_h=9.5)
    try:
        session.on_message("one")
        session.on_message("two")
        session.on_message("three")

        rows = [m for m in store.messages_for_day(0) if m["role"] == "system"]
        assert len(rows) == 1, "the day block was emitted more than once"
        stored = rows[0]["content"]

        # Every call carries the SAME bytes for it — replayed, not rebuilt.
        seen = [
            m["content"]
            for call in client.calls
            for m in call["messages"]
            if m.get("content") == stored
        ]
        assert len(seen) >= 2, "later turns did not replay the stored block"

        # And it appears before the first user turn in every call, so the
        # stream stays a strict extension turn over turn.
        for call in client.calls:
            contents = [m.get("content") or "" for m in call["messages"]]
            if stored in contents:
                first_user = next(
                    (i for i, m in enumerate(call["messages"])
                     if m.get("role") == "user"), len(contents)
                )
                assert contents.index(stored) < first_user
    finally:
        store.close()


# the cache property

def test_a_popup_call_extends_the_mainline_request(tmp_path):
    """A pop-up call re-sends the mainline request; only its tail grows.

    This is the provider-cache story in one assertion. Measured on the live
    run (14 calls): every consecutive pair — chat->chat and chat->decide —
    was byte-identical up to the trailing state card, so the whole prefix is
    servable and only the tail changes. The tail is free to change: a
    re-sent message costs the cache, an EXTENDED last message does not.

    A pop-up is therefore not a request of its own. It is the mainline array
    with the pop-up folded into the trailing system block. Anything that
    rebuilds its own prefix — a sliding window, a re-rendered transcript, a
    card in the middle — breaks the property, and this test is what breaks
    loudly when that happens.
    """
    client = FakeClient(responses=[
        {"content": "main reply",
         "tool_calls": [{"id": "c4", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": \"yes\", \"reason\": \"in the mood\"}"}]},
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        # ONE generation: the pop-up is answered by the turn's own tool call,
        # which is the whole point of the ruling (owner, 2026-09-12).
        assert len(client.calls) == 1, "the pop-up must not cost a second call"
        first, second = client.calls[0], client.calls[1]
        # One stable prefix, byte for byte, across the two lanes.
        assert first["system"] == second["system"]
        a, b = first["messages"], second["messages"]
        assert [m.get("role") for m in a] == [m.get("role") for m in b]
        # Every message but the last is re-sent unchanged...
        assert a[:-1] == b[:-1]
        # ...and the last is the state card, with the pop-up appended to it,
        # still the last thing the model sees and still a single message.
        assert a[-1]["role"] == b[-1]["role"] == "system"
        shorter, longer = ((a, b) if len(a[-1]["content"]) <= len(b[-1]["content"])
                          else (b, a))
        assert shorter[-1]["content"] in longer[-1]["content"]
    finally:
        store.close()
