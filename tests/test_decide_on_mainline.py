"""One steer, one round: every decide steer is its own model call.

Pins the flush shape ``state card + {steer} -> model -> decision ->
{steer}``. The pop-up block rides the decision request; each recorded decide
pair extends the context the later rounds read; every call carries the same
constant tools menu (tools are part of the context — never toggled). A
generation without prose is re-asked once and dropped if still empty.
"""

from __future__ import annotations

import json

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda, ProactiveIntent
from harness.judge import ScriptedJudge
from harness.session import REPLY_NUDGE, Session
from harness.steering import STEER_MARKER_OPEN
from harness.tools import DecisionConfig, TOOL_PAYLOAD
from tests.helpers import make_store

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 4242


def _item(start: float, end: float, activity: str = "pottery",
          item_id: str = "ag1", salience: float = 0.8) -> AgendaItem:
    return AgendaItem(item_id, start, end, activity, "arc", "arc1",
                      salience, "planned")


def _session(store, *, client, clock=None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client,
        clock=clock if clock is not None else VirtualClock(t_h=10.0),
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )


def _anchor(store) -> None:
    """An earlier stream row, like a real run always has.

    A decision stamped before the stream's head still replays (clamped);
    this row just matches the natural order of a real run.
    """
    store.add_message("assistant", "morning", 8.0, 0)


def _decide_event(call_id: str, initiate: bool = True,
                  reason: str = "ready to go") -> dict:
    return {
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "name": "tool_decide_event",
            "arguments_json": json.dumps(
                {"initiate": initiate, "reason": reason}
            ),
        }],
    }


def _has_pair(messages: dict) -> bool:
    return any(row.get("role") == "tool" for row in messages["messages"])


def test_a_steer_is_decided_in_its_own_round_before_the_generation(tmp_path):
    store = make_store(tmp_path)
    _anchor(store)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    client = FakeClient(responses=[
        _decide_event("c1"),
        {"content": "main reply"},
    ])
    session = _session(store, client=client)
    assert session.steering_enabled()

    result = session.on_message("hello")

    assert result.reply == "main reply"
    assert len(client.calls) == 2
    round_call, generation = client.calls
    # The round carries the question; the generation carries no steered block.
    # Both ride the same constant tools menu (never toggled).
    assert STEER_MARKER_OPEN in round_call["messages"][-1]["content"]
    assert round_call["tools"] == TOOL_PAYLOAD
    assert generation["tools"] == round_call["tools"], \
        "one constant menu rides every call — never toggled"
    assert STEER_MARKER_OPEN not in generation["messages"][-1]["content"]
    records = store.decisions_for_day(0)
    assert len(records) == 1
    assert records[0]["popup_kind"] == "tool_decide_event"
    assert records[0]["verdict"]["initiate"] is True
    assert store.pending_steers() == []
    store.close()


def test_two_steers_take_two_rounds_and_pairs_extend_the_next(tmp_path):
    store = make_store(tmp_path)
    _anchor(store)
    store.save_agenda(0, DailyAgenda(0, (
        _item(9.0, 11.0, "pottery", "ag1"),
        _item(9.5, 11.5, "run", "ag2"),
    )))
    client = FakeClient(responses=[
        _decide_event("c1"),
        _decide_event("c2", initiate=False, reason="not today"),
        {"content": "main reply"},
    ])
    session = _session(store, client=client)

    result = session.on_message("hello")

    assert result.reply == "main reply"
    assert len(client.calls) == 3
    first, second, generation = client.calls
    assert not _has_pair(first), "the first round has no earlier pair"
    assert _has_pair(second), "the next round reads the earlier decision"
    assert STEER_MARKER_OPEN in second["messages"][-1]["content"]
    assert generation["tools"] == TOOL_PAYLOAD
    # The generation reads both decisions this turn made, as rows and with
    # their tool results — the same session and context decided them.
    tool_rows = [r for r in generation["messages"] if r.get("role") == "tool"]
    assert len(tool_rows) == 2, "both pairs ride the generation"
    assert all((r.get("content") or "") for r in tool_rows), "results, not void"
    records = store.decisions_for_day(0)
    assert [r["verdict"]["initiate"] for r in records] == [True, False]
    store.close()


def test_a_plain_turn_still_makes_exactly_one_call(tmp_path):
    store = make_store(tmp_path)
    client = FakeClient(responses=[{"content": "main reply"}])
    session = _session(store, client=client)

    result = session.on_message("hello")

    assert result.reply == "main reply"
    assert len(client.calls) == 1
    assert client.calls[0]["tools"] == TOOL_PAYLOAD
    store.close()


def test_a_prose_less_generation_is_reasked_once(tmp_path):
    store = make_store(tmp_path)
    client = FakeClient(responses=[
        _decide_event("stray"),
        {"content": "the actual reply"},
    ])
    session = _session(store, client=client)

    result = session.on_message("hello")

    assert result.reply == "the actual reply"
    assert len(client.calls) == 2
    assert REPLY_NUDGE in client.calls[1]["messages"][-1]["content"]
    store.close()


def test_a_still_empty_reply_is_dropped_not_persisted(tmp_path):
    store = make_store(tmp_path)
    client = FakeClient(responses=[
        _decide_event("stray1"),
        _decide_event("stray2"),
    ])
    session = _session(store, client=client)

    result = session.on_message("hello")

    assert result.reply == ""
    assert len(client.calls) == 2
    persisted = store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'assistant'"
    ).fetchone()[0]
    assert persisted == 0
    store.close()


def test_a_decision_stamped_before_the_stream_head_still_replays(tmp_path):
    """The live defect: a boundary stamped before the stream's first row (boot
    after the boundary) must still ride the context — clamped, never dropped."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    client = FakeClient(responses=[
        _decide_event("c1"),
        {"content": "first"},
        {"content": "second"},
    ])
    session = _session(store, client=client)

    session.on_message("hello")
    records = store.decisions_for_day(0)
    assert len(records) == 1
    # premise: the boundary (9.0) precedes the stream head (the turn at 10.0)
    assert float(records[0]["t_h"]) == 9.0

    session.on_message("again")
    later = client.calls[-1]
    roles = [row.get("role") for row in later["messages"]]
    assert "tool" in roles, "the earlier decision rides the next turn's context"
    assert roles.index("tool") >= 2, "integrated into the stream, not prepended"
    assert roles[roles.index("tool") - 1] == "assistant"
    store.close()


def test_a_proactive_intent_is_decided_then_the_reply_is_the_message(tmp_path):
    store = make_store(tmp_path)
    store.save_proactive_intent(ProactiveIntent(
        "pi1", "schedule", "agenda_item", "ag1",
        "Agenda: pottery (9.0-11.0h)", 8.0, 14.0, 0.6, "agenda_item:ag1",
    ))
    client = FakeClient(responses=[
        {
            "content": "",
            "tool_calls": [{
                "id": "p1",
                "name": "tool_decide_proactive",
                "arguments_json": '{"initiate": true, "reason": "it fits"}',
            }],
        },
        {"content": "hey, how did the pottery go?"},
    ])
    session = _session(store, client=client)

    result = session.fire_proactive(intent_id="pi1")

    assert result.reply == "hey, how did the pottery go?"
    assert len(client.calls) == 2
    round_call, generation = client.calls
    assert STEER_MARKER_OPEN in round_call["messages"][-1]["content"]
    assert generation["tools"] == TOOL_PAYLOAD
    records = store.decisions_for_day(0)
    assert len(records) == 1
    assert records[0]["popup_kind"] == "tool_decide_proactive"
    assert records[0]["verdict"]["initiate"] is True
    delivered = store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'assistant'"
    ).fetchone()[0]
    assert delivered == 1
    store.close()
