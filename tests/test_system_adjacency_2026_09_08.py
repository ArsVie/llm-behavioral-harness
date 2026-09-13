"""Owner law (2026-09-13): the context stream is APPEND-ONLY.

Nothing the model already saw is ever rewritten, merged, folded, removed or
repositioned: system blocks append as their own messages
(``assembler.append_system``), and the state card is a stream row appended
only when its bytes change (:meth:`Session._ensure_state_card`) — never a
tail block. Replayed decisions keep their real ``assistant tool_calls ->
role="tool"`` exchange. The only forks of context that are NOT appended, for
system machinery, are the judge call and the day-planner call.
"""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import append_system
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.session import Session
from harness.tools import DecisionConfig
from tests.helpers.store import make_store


# the helper

def test_a_system_block_appends_as_its_own_message():
    msgs = [{"role": "user", "content": "hi"}]
    msgs = append_system(msgs, "card")
    msgs = append_system(msgs, "popup")
    assert [m["role"] for m in msgs] == ["user", "system", "system"]
    assert [m["content"] for m in msgs[1:]] == ["card", "popup"]


def test_appending_never_disturbs_earlier_messages():
    original = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey"}]
    out = append_system(list(original), "card")
    assert out[:2] == original


def test_empty_blocks_are_dropped_not_appended():
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


def test_a_mainline_turn_appends_only_stream_rows(tmp_path):
    """Every system message a call carries is a stored row (or an event)."""
    client = FakeClient(responses=[
        {"content": "main reply",
         "tool_calls": [{"id": "c1", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": \"yes\", \"reason\": \"in the mood\"}"}]},
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        stored = {m["content"] for m in store.messages_for_day(0)
                  if m["role"] == "system"}
        for call in client.calls:
            for m in call["messages"]:
                if m.get("role") != "system":
                    continue
                content = m.get("content") or ""
                assert content in stored or "Event:" in content, (
                    "a synthetic system tail rode the request: "
                    f"{content[:80]!r}"
                )
    finally:
        store.close()


def test_an_aux_popup_call_carries_the_steer_last(tmp_path):
    """The pop-up is the last thing the model sees; the card is a stream row
    and is never re-appended behind the steer."""
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
        stored = {m["content"] for m in store.messages_for_day(0)
                  if m["role"] == "system"}
        for call in popup_calls:
            roles = [m.get("role") for m in call["messages"]]
            assert roles[-1] == "system"
            assert "Event:" in call["messages"][-1]["content"]
            # No block may follow the steer, and every other system message
            # must be the stored stream.
            for m in call["messages"][:-1]:
                if m.get("role") == "system":
                    assert m.get("content") in stored
    finally:
        store.close()


# the day block: written once, never re-rendered

def test_the_day_block_is_written_once_and_never_rerendered(tmp_path):
    """The day block is rendered ONCE and persisted; every later turn replays the
    stored bytes, and nothing re-renders an emitted block."""
    client = FakeClient(responses=["r1", "r2", "r3"])
    store, session = _session(tmp_path, client, t_h=9.5)
    try:
        session.on_message("one")
        session.on_message("two")
        session.on_message("three")

        rows = [m for m in store.messages_for_day(0) if m["role"] == "system"]
        assert rows, "the day block was never emitted"
        stored = rows[0]["content"]
        assert sum(1 for r in rows if r["content"] == stored) == 1, (
            "the day block was emitted more than once"
        )

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
    """The decide round extends the shared stream with its steer; the
    generation follows the same stream one step further — the round's decision
    pair rides it as rows. No card tail, ever."""
    client = FakeClient(responses=[
        # The decide round answers the pop-up...
        {"content": "", "tool_calls": [{"id": "c4", "name": "tool_decide_event",
         "arguments_json": "{\"initiate\": \"yes\", \"reason\": \"in the mood\"}"}]},
        # ...then the generation speaks.
        "main reply",
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        # The decide round and then the generation: nothing else.
        assert len(client.calls) == 2, "decide round + generation, nothing else"
        first, second = client.calls[0], client.calls[1]
        # One stable prefix, byte for byte, across the two lanes.
        assert first["system"] == second["system"]
        a, b = first["messages"], second["messages"]
        # The steer is transient and rides last on the round only.
        assert a[-1]["role"] == "system" and "Event:" in a[-1]["content"]
        # The generation's request extends the round's stream byte for byte.
        head = len(a) - 1
        assert b[:head] == a[:head]
        # The round's decision pair follows as native rows, adjacent.
        roles = [m.get("role") for m in b[head:]]
        pair_at = roles.index("assistant") if "assistant" in roles else -1
        assert pair_at >= 0, f"no decision pair after the prefix: {roles}"
        assert roles[pair_at + 1] == "tool"
    finally:
        store.close()
