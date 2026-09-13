"""Integration wiring tests: steering + decision layer in Session._chat."""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.steering import (
    KIND_EVENT_POPUP,
    KIND_USER_MESSAGE,
    STEER_MARKER_OPEN,
)
from harness.store import SQLiteStore
from tests.helpers import make_store
from harness.tools import DecisionConfig

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 4242


def _item(start: float, end: float, activity: str = "pottery",
          item_id: str = "ag1", salience: float = 0.8) -> AgendaItem:
    return AgendaItem(item_id, start, end, activity, "arc", "arc1",
                      salience, "planned")


def _session(store, *, client, clock, decision=None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=decision,
    )




# event pop-ups (decide_event)


def test_event_popup_initiate_fires_proactive_out(tmp_path):
    """An agenda item started before the turn: the idle boundary enqueues an
    event pop-up; the decision and its delivery are persisted."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        {"content": "",
         "tool_calls": [{"id": "c1", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": true, \"reason\": \"ready to go\"}"}]},
        {"content": "main reply"},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    assert session.steering_enabled()

    result = session.on_message("hello")

    assert result.reply == "main reply"
    # the verdict's `reason` stays in decision_records, out of the conversation
    assert result.proactive_out == ()
    assert result.notices == ()
    # one steer, one round: the decide call carries the question, the
    # generation speaks and carries no steer block
    assert len(client.calls) == 2
    assert STEER_MARKER_OPEN in client.calls[0]["messages"][-1]["content"]
    assert STEER_MARKER_OPEN not in client.calls[1]["messages"][-1]["content"]
    # dual persistence: the decision record + the delivered steer
    records = store.decisions_for_day(0)
    assert len(records) == 1
    assert records[0]["popup_kind"] == "tool_decide_event"
    assert records[0]["verdict"]["initiate"] is True
    assert records[0]["transport"] == "native"  # capabilities said native
    assert store.pending_steers() == []
    delivered = store.conn.execute(
        "SELECT delivered_t_h, boundary FROM steering_queue"
    ).fetchall()
    assert len(delivered) == 1 and delivered[0]["boundary"] == "idle"
    assert delivered[0]["delivered_t_h"] == 10.0
    store.close()


def test_event_popup_no_initiate_no_channel_output(tmp_path):
    """An initiate=no verdict (with a reason) records the decision and
    produces no channel output at all."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        {"content": "",
         "tool_calls": [{"id": "c2", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"too tired\"}"}]},
        {"content": "main reply"},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    result = session.on_message("hello")

    assert result.reply == "main reply"
    assert result.proactive_out == () and result.notices == ()
    records = store.decisions_for_day(0)
    assert len(records) == 1
    assert records[0]["verdict"]["initiate"] is False
    assert records[0]["verdict"]["reason"] == "too tired"
    store.close()


def test_backlog_initiate_omits_channel_send(tmp_path):
    """A steer enqueued before the conversation opened is decided and
    persisted, but its reason never reaches the channel."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    store.enqueue_steer(0, 8.0, KIND_EVENT_POPUP, {
        "event_id": "ag1", "event": "pottery", "state": "start",
        "time": 9.0, "item_id": "ag1",
    })
    clock = VirtualClock(t_h=8.5)
    client = FakeClient(responses=[
        {"content": "",
         "tool_calls": [{"id": "c3", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": true, \"reason\": \"ready to go\"}"}]},
        {"content": "main reply"},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    result = session.on_message("hello")

    assert result.reply == "main reply"
    assert result.proactive_out == () and result.notices == ()
    records = store.decisions_for_day(0)
    assert len(records) == 1
    assert records[0]["verdict"]["initiate"] is True
    omits = store.conn.execute(
        "SELECT detail FROM state_events WHERE event='decision_catchup_omit'"
    ).fetchall()
    assert len(omits) == 1 and "pottery" in omits[0][0]
    store.close()


def test_start_no_marks_item_skipped_and_end_asks_nothing(tmp_path):
    """The decision is offered once, at the start; the END boundary makes
    no model call."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        {"content": "morning reply",
         "tool_calls": [{"id": "c4", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": \"no\", \"reason\": \"later\"}"}]},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # the ONE decision, at the start
    assert store.pending_steers() == []
    items = store.list_agenda_items(day=0)
    assert [it.status for it in items] == ["skipped"]

    clock.advance_hours(2.5)  # 12:00 — the window ended
    client.responses.append("main reply")
    result = session.on_message("hello")

    assert result.reply == "main reply"   # no end-boundary pop-up ate it
    assert not client.responses   # nothing left unconsumed
    items = store.list_agenda_items(day=0)
    assert [it.status for it in items] == ["skipped"]
    store.close()


# decide_reply and the single reply path


def test_decide_reply_no_reply_suppresses_ordinary_reply(tmp_path):
    """A user message during an event runs decide_reply; a no-reply verdict
    suppresses the ordinary reply and the notice rides out through TurnResult."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        {"content": "morning reply",
         "tool_calls": [{"id": "c5", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"later\"}"}]},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # consumes the START pop-up
    assert store.pending_steers() == []

    client.responses.append({
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "name": "tool_decide_reply",
            "arguments_json": '{"reply": false, "reason": "in class"}',
        }],
    })
    session.enqueue_user_message_steer("are you coming?", 10.0)
    clock.advance_hours(0.5)  # 10:00 — inside the item's window

    result = session.on_message("are you coming?")

    assert result.reply == ""
    assert result.notices == ("Lily saw your message but chose not to reply yet",)
    assert result.proactive_out == ()
    assert len(client.calls) == 3  # T1 round + T1 generation + T2 decide round (the suppressed turn never generates)
    msgs = store.messages_for_day(0)
    # The leading system row is the day-start block (plan + arcs), emitted
    # once at the day's first turn into the stream.
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    records = store.decisions_for_day(0)
    assert len(records) == 2
    assert records[-1]["popup_kind"] == "tool_decide_reply"
    assert records[-1]["verdict"]["reply"] is False
    store.close()


def test_decide_reply_yes_proceeds_with_ordinary_reply(tmp_path):
    """A reply=yes verdict proceeds with the ordinary reply; terminate_event
    closes the event server-side (item -> skipped)."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        {"content": "morning reply",
         "tool_calls": [{"id": "c6", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"later\"}"}]},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # consumes the START pop-up

    client.responses.extend([
        {"content": "",
         "tool_calls": [{"id": "c7", "name": "tool_decide_reply",
                         "arguments_json": "{\"reply\": true, \"reason\": \"one sec\", \"terminate_event\": true}"}]},
        {"content": "ok here I am"},
    ])
    session.enqueue_user_message_steer("are you coming?", 10.0)
    clock.advance_hours(0.5)

    result = session.on_message("are you coming?")

    assert result.reply == "ok here I am"
    assert result.notices == ()
    assert [it.status for it in store.list_agenda_items(day=0)] == ["skipped"]
    store.close()


def test_decide_reply_verbose_notice_carries_reason(tmp_path):
    """HARNESS_VERBOSE=1: the no-reply notice carries the model's reason."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        {"content": "morning reply",
         "tool_calls": [{"id": "c8", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"later\"}"}]},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig(verbose=True))
    session.on_message("morning")

    client.responses.append(
        'tool_decide_reply: {"reply": false, "reason": "in class"}'
    )
    session.enqueue_user_message_steer("are you coming?", 10.0)
    clock.advance_hours(0.5)
    result = session.on_message("are you coming?")
    assert result.reply == ""
    assert result.notices == ("Lily is not replying, reason: in class",)
    store.close()


# re-queue semantics


def test_parse_failure_requeues_steer_for_next_boundary(tmp_path):
    """A textual reply without a parseable marker requeues the steer; the
    next turn drains it again with a parseable reply."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        "I guess I should? maybe?",                    # popup 1: unparseable
        "first reply",                                 # main call 1
        {"content": "second reply",
         "tool_calls": [{"id": "c9", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"no\"}"}]},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("hello")
    assert store.pending_steers(), "failed steer must be pending again"
    assert any(
        e["event"] == "decision_parse_failed" for e in store.events_since(0)
    )

    session.on_message("hello again")
    assert store.pending_steers() == []
    records = store.decisions_for_day(0)
    assert len(records) == 1 and records[0]["verdict"]["initiate"] is False
    assert client.calls[0]["messages"][-1]["content"]  # popup payload present
    store.close()


def test_interrupted_turn_requeues_delivered_steers(tmp_path):
    """If the pop-up call raises (abandoned turn), the steers delivered to
    that turn are re-queued and delivered again at the next boundary."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        {"content": "morning reply",
         "tool_calls": [{"id": "c10", "name": "tool_decide_event",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"later\"}"}]},
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # consumes the START pop-up
    assert store.pending_steers() == []

    class BoomClient(FakeClient):
        def chat_with_meta(self, messages, **kwargs):
            raise RuntimeError("boom")

    session.client = BoomClient()  # the pop-up call raises
    session.enqueue_user_message_steer("are you coming?", 10.0)
    clock.advance_hours(0.5)
    try:
        session.on_message("are you coming?")
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected the interrupted turn to raise")
    pending = store.pending_steers()
    assert len(pending) == 1 and pending[0]["kind"] == KIND_USER_MESSAGE
    assert pending[0]["delivered_t_h"] is None  # cleared by requeue
    store.close()


# thinking passthrough + persistence


def test_thinking_effort_passthrough_and_reasoning_persistence(
    tmp_path, monkeypatch,
):
    """HARNESS_THINKING_EFFORT=low reaches the client as reasoning_effort;
    the reasoning is persisted in the llm_call meta."""
    monkeypatch.setenv("HARNESS_THINKING_EFFORT", "low")
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[{
        "content": "hello back",
        "reasoning": "she seems fine, keep it light",
    }])
    session = _session(store, client=client, clock=clock)
    session.on_message("hello")

    call = client.calls[-1]
    assert call["reasoning_effort"] == "low"
    assert call["max_tokens"] is None  # cap dropped for reasoning models
    row = store.conn.execute(
        "SELECT id FROM llm_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    persisted = store.get_llm_call(int(row["id"]))
    assert persisted is not None and persisted["meta"] == {
        "reasoning": "she seems fine, keep it light"
    }
    store.close()


def test_defaults_inert_no_thinking_no_steering(tmp_path):
    """With no HARNESS_* env vars the harness is inert: one model call per
    turn, no reasoning_effort, no steering activity, no meta."""
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=["plain reply"])
    session = _session(store, client=client, clock=clock)
    assert not session.steering_enabled()
    result = session.on_message("hello")
    assert result.reply == "plain reply"
    assert result.notices == () and result.proactive_out == ()
    assert len(client.calls) == 1
    assert client.calls[0]["reasoning_effort"] is None
    assert client.calls[0]["max_tokens"] is not None
    assert store.pending_steers() == []
    assert store.decisions_for_day(0) == []
    store.close()


# three-tier context: cached day-start block


def test_day_start_block_stable_within_day_changes_across_days(tmp_path):
    """Tier 2 (day-start block) is cached: identical within a day, refreshed
    at rollover."""
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0, activity="pottery"),)))
    store.save_agenda(1, DailyAgenda(1, (_item(33.0, 35.0, activity="chess",
                                                item_id="ag2"),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=["r1", "r2", "r3"])
    session = _session(store, client=client, clock=clock)

    def _agenda_segment(call) -> str:
        # The day plan is emitted once, at the day's first turn; read the
        # LAST one — on day 1 the stream still carries day 0's block as history.
        found = ""
        for m in call["messages"]:
            _, sep, rest = (m.get("content") or "").partition("Today's agenda:")
            if sep:
                found = rest.split("\n\n", 1)[0]
        return found

    session.on_message("morning")
    session.on_message("still here")
    block0a = _agenda_segment(client.calls[0])
    block0b = _agenda_segment(client.calls[1])
    assert block0a == block0b, "day-start block must be stable within the day"
    assert "pottery" in block0a

    # Emitted ONCE: the second turn re-reads the same stream message rather
    # than appending a fresh copy.
    def _plan_messages(call) -> int:
        return sum(1 for m in call["messages"]
                   if "Today's agenda:" in (m.get("content") or ""))

    assert _plan_messages(client.calls[1]) == 1

    clock.advance_to_day(1)
    clock.advance_hours(10.0)
    session.on_message("next day")
    block1 = _agenda_segment(client.calls[2])
    assert block1 != block0a, "day-start block must refresh at rollover"
    assert "chess" in block1
    store.close()
