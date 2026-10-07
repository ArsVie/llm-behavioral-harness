"""A heads-up written while nobody is talking still reaches him, and her
history holds it as her own message (live 2026-09-28: the life loop ran the
heads-up at 13:30 and the runtime threw the text away)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.runtime import AsyncRuntime
from harness.tools import DecisionConfig
from tests.helpers.store import booted, make_session, make_store


def _gym(start: float = 19.0, end: float = 21.0) -> AgendaItem:
    return AgendaItem("ag1", start, end, "gym", "arc", "arc1", 0.8, "planned")


def _settle_heads_up(tmp_path):
    store = booted(make_store(tmp_path))
    store.save_agenda(0, DailyAgenda(0, (_gym(),)))
    client = FakeClient(responses=[
        "hi you",
        'tool_decide_event: {"message": "gym in a bit"}',
    ])
    clock = VirtualClock(t_h=18.5)
    session = make_session(store, client=client, clock=clock,
                           decision_config=DecisionConfig())
    session.on_message("hey")           # the conversation is open
    clock.advance_hours(18.9 - clock.now_h())   # the heads-up instant
    return store, session, session.settle_pending("event-instant")


def test_a_settled_heads_up_comes_back_for_the_runtime_to_send(tmp_path):
    store, _session, drain = _settle_heads_up(tmp_path)
    assert list(drain.proactive_out) == [("event_popup", "gym in a bit")]
    store.close()


def test_the_heads_up_is_her_message_in_the_stream_and_said_once(tmp_path):
    store, session, _drain = _settle_heads_up(tmp_path)
    rows = [m for m in store.messages_since(0) if m["role"] == "assistant"]
    assert [(m["content"], m["proactive"]) for m in rows][-1] == ("gym in a bit", 1)
    rows = rows[-1:]
    assert json.loads(rows[0]["meta"])["decision_id"] == "neg-ag1-inform"
    # The model's context carries it as her message, not again as a tool pair.
    stream = session._context_turns()
    said = [m for m in stream if m.get("role") == "assistant"
            and m.get("content") == "gym in a bit"]
    assert len(said) == 1
    assert not any("gym in a bit" in json.dumps(m.get("tool_calls") or [])
                   for m in stream)
    store.close()


def test_a_replayed_decision_is_not_recorded_or_sent_twice(tmp_path):
    store, session, _drain = _settle_heads_up(tmp_path)
    t_h = session.clock.now_h()
    assert session._record_channel_text("gym in a bit", t_h,
                                        decision_id="neg-ag1-inform") is False
    rows = [m for m in store.messages_since(0)
            if m["role"] == "assistant" and m["content"] == "gym in a bit"]
    assert len(rows) == 1
    store.close()


def test_the_runtime_sends_what_a_settle_wrote():
    sent = []

    class _Channel:
        async def send(self, message):
            sent.append((message.text, message.proactive))

    fake = SimpleNamespace(channel=_Channel())
    fake._send_neg_outs = lambda outs: AsyncRuntime._send_neg_outs(fake, outs)
    drain = SimpleNamespace(proactive_out=[("event_popup", "gym in a bit")],
                            notices=[""])
    asyncio.run(AsyncRuntime._send_settled(fake, drain))
    assert sent == [("gym in a bit", True)]
