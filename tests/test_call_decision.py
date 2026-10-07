"""Voice call on the session: the pick-up decision and the call turns.

The pick-up rides the same decision layer as the proactive decision (an
``incoming_call`` steer, ``tool_decide_call``). A decline is a missed call and
the turn still writes her text; an answer writes the call-start row and sends
nothing. Call turns are ordinary main-stream turns: his words are user rows,
her stored reply is exactly what he heard, and every request extends the one
before it, through the call and after it.
"""

from __future__ import annotations

import itertools
import json

import pytest

from harness.assembler import stamped_stream
from harness.client import FakeClient, StreamCancel
from harness.clock import VirtualClock
from harness.prompts import CALL_END_OPENER, CALL_START_OPENER, VOICE_CALL_RULES
from harness.session import CALL_ACTIVE_KEY, CALL_LAST_END_KEY
from harness.steering import KIND_INCOMING_CALL
from harness.tools import TOOL_PAYLOAD, parse_textual_reply
from tests.helpers.store import make_session, make_store
from tests.test_cache_prefix_gate import _agenda_item, _durable, _persona

SEED = 4242


def _verdicts(answer: bool) -> str:
    return (
        f'tool_decide_call: {{"answer": {"true" if answer else "false"}, '
        '"reason": "my call"}\n'
        'tool_decide_event: {"initiate": "yes", "reason": "heading over"}\n'
        'tool_decide_reply: {"reply": true, "reason": "sure"}\n'
        'tool_decide_proactive: {"initiate": true, "reason": "checking in"}'
    )


class CallAwareClient(FakeClient):
    """Answers the pick-up pop-up with ``call_line`` and everything else from
    the queue (the textual parser reads the FIRST verdict marker, so one
    shared response cannot carry a call verdict ahead of an event one)."""

    def __init__(self, responses, call_line):
        super().__init__(responses=responses)
        self.call_line = call_line

    def chat_with_meta(self, messages, **kwargs):
        last = (messages[-1].get("content") or "") if messages else ""
        if "Incoming call" in last:
            self.responses.appendleft(self.call_line)
        return super().chat_with_meta(messages, **kwargs)


@pytest.fixture()
def decision_env(monkeypatch):
    monkeypatch.setenv("HARNESS_DECISIONS", "1")
    monkeypatch.setenv("HARNESS_DECISION_SOURCE", "model")
    monkeypatch.setenv("HARNESS_TOOL_MODE", "textual")
    monkeypatch.delenv("HARNESS_CALL_THINKING", raising=False)


def _setup(tmp_path, responses, name="call.db"):
    store = make_store(tmp_path, name)
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=responses)
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    return store, session, client


def _rows(store):
    return store.recent_messages(limit=500)


# -- registration ------------------------------------------------------------ #


def test_textual_call_verdict_parses():
    verdict = parse_textual_reply(
        "tool_decide_call",
        'tool_decide_call: {"answer": true, "reason": "bored anyway"}',
    )
    assert verdict["answer"] is True


# -- the pick-up decision ---------------------------------------------------- #


def test_answer_writes_the_start_row_and_sends_nothing(tmp_path, decision_env):
    store, session, client = _setup(
        tmp_path, [f"reply {i}.\n" + _verdicts(True) for i in range(10)])
    try:
        session.on_message("hey")
        before = len(_rows(store))
        result = session.incoming_call("abc123")
        assert result.call_answered is True
        assert result.reply == "" and not result.bubbles
        assert session.call_active()["id"] == "abc123"
        new = _rows(store)[before:]
        # Only the call-start row (and possibly a card update): no text of hers.
        assert [m["role"] for m in new if m["role"] != "system"] == []
        start = [m for m in new if m["content"].startswith(CALL_START_OPENER)]
        assert len(start) == 1 and VOICE_CALL_RULES in start[0]["content"]
        decisions = [d for d in store.decisions_for_day(0)
                     if d["popup_kind"] == "tool_decide_call"]
        assert len(decisions) == 1
        assert json.loads(store.get_kv(CALL_ACTIVE_KEY))["id"] == "abc123"
    finally:
        store.close()


def test_decline_is_a_missed_call_that_still_writes_her_text(tmp_path, decision_env):
    store, session, client = _setup(
        tmp_path, [f"reply {i}. busy rn\n" + _verdicts(False) for i in range(10)])
    try:
        session.on_message("hey")
        result = session.incoming_call("abc123")
        assert result.call_answered is False
        assert result.reply.startswith("reply")          # markup stripped
        assert "tool_decide" not in result.reply
        assert session.call_active() is None
        last = _rows(store)[-1]
        assert last["role"] == "assistant" and last["content"] == result.reply
        assert last["proactive"] == 1
        # The generation read the missed-call note.
        sent = client.calls[-1]["messages"]
        assert any("let his call ring out" in (m.get("content") or "")
                   for m in sent if m["role"] == "system")
        assert not any(m["content"].startswith(CALL_START_OPENER) for m in _rows(store))
    finally:
        store.close()


def test_unreadable_verdict_is_a_missed_call(tmp_path, decision_env, monkeypatch):
    monkeypatch.setenv("HARNESS_DECISION_PARSE_FAILURE", "requeue")
    store, session, client = _setup(
        tmp_path, ["no verdict here", "oh, missed you."] + ["later."] * 5)
    try:
        session.on_message("hey")
        result = session.incoming_call("abc123")
        assert result.call_answered is False
        assert session.call_active() is None
        # The steer is gone for good: a later turn does not pick it up.
        pending = [r for r in store.pending_steers() if r["kind"] == KIND_INCOMING_CALL]
        assert pending == []
    finally:
        store.close()


def test_a_ring_he_hung_up_on_never_opens(tmp_path, decision_env):
    import threading

    store, session, client = _setup(
        tmp_path, [f"reply {i}.\n" + _verdicts(True) for i in range(10)])
    try:
        session.on_message("hey")
        hung_up = threading.Event()
        hung_up.set()
        result = session.incoming_call("abc", cancelled=hung_up)
        assert result.call_answered is False and result.reply == ""
        assert session.call_active() is None
        last = _rows(store)[-1]
        assert last["role"] == "system" and last["content"].startswith("He hung up at")
        assert not any(m["content"].startswith(CALL_START_OPENER) for m in _rows(store))
        # The next turn still extends the stream the ring left.
        session.clock.advance_hours(0.05)
        session.on_message("sorry, wrong button")
        tail = session._context_turns()[-4:]
        assert [m["role"] for m in tail] == ["tool", "system", "user", "assistant"]
    finally:
        store.close()


def test_a_declined_ring_he_hung_up_on_still_texts(tmp_path, decision_env):
    import threading

    store, session, client = _setup(
        tmp_path, [f"reply {i}. busy\n" + _verdicts(False) for i in range(10)])
    try:
        session.on_message("hey")
        hung_up = threading.Event()
        hung_up.set()
        result = session.incoming_call("abc", cancelled=hung_up)
        assert result.call_answered is False and result.reply.startswith("reply")
    finally:
        store.close()


def test_a_stale_ring_is_consumed_without_a_decision(tmp_path, decision_env):
    store, session, client = _setup(
        tmp_path, [f"reply {i}.\n" + _verdicts(True) for i in range(10)])
    try:
        session.on_message("hey")
        session._steering.enqueue(KIND_INCOMING_CALL, {"call_id": "old"}, 0,
                                  session.clock.now_h())
        session.clock.advance_hours(0.01)
        session.on_message("again")
        assert session.call_active() is None
        assert not [d for d in store.decisions_for_day(0)
                    if d["popup_kind"] == "tool_decide_call"]
        events = [e["event"] for e in store.events_since(0)]
        assert "call_stale" in events
    finally:
        store.close()


def test_calls_need_the_decision_layer(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_DECISIONS", raising=False)
    store, session, client = _setup(tmp_path, ["hi."])
    try:
        with pytest.raises(RuntimeError):
            session.incoming_call("abc")
    finally:
        store.close()


# -- call turns ---------------------------------------------------------------- #


def _answered(tmp_path, call_replies, name="turns.db"):
    responses = ["reply 0.\n" + _verdicts(True), _verdicts(True)] + list(call_replies)
    store, session, client = _setup(tmp_path, responses, name)
    session.on_message("hey")
    assert session.incoming_call("cid").call_answered
    return store, session, client


def test_call_turn_streams_sentences_and_stores_what_was_heard(tmp_path, decision_env):
    store, session, client = _answered(tmp_path, [
        "Hey you. What's up?",
        "Long story. So basically I was at the store and then everything happened.",
        "Hm.",
        "Okay okay.",
    ])
    try:
        # Her opening line (no text from him).
        sentences = list(session.call_turn(None))
        assert sentences == ["Hey you.", "What's up?"]
        session.call_heard("Hey you. What's up?", False)
        assert _rows(store)[-1]["role"] == "assistant"
        assert _rows(store)[-1]["content"] == "Hey you. What's up?"

        # He speaks; she is cut: only the heard prefix is stored.
        list(session.call_turn("not much, you?"))
        user = [m for m in _rows(store) if m["role"] == "user"][-1]
        assert user["content"] == "not much, you?"
        session.call_heard("Long story.", True)
        assert _rows(store)[-1]["content"] == "Long story."

        # Nothing heard: no assistant row. A continuation is a second user row.
        list(session.call_turn("wait"))
        count = len(_rows(store))
        session.call_heard("", True)
        assert len(_rows(store)) == count
        list(session.call_turn("also this"))
        tail = [m for m in _rows(store) if m["role"] in ("user", "assistant")][-2:]
        assert [(m["role"], m["content"]) for m in tail] == [
            ("user", "wait"), ("user", "also this")]
        session.call_heard("Okay okay.", False)

        # Each call request: ordinary chat, thinking off, the constant menu.
        voice = client.calls[-1]
        assert voice["extra"] == {"thinking": {"type": "disabled"}}
        assert voice["reasoning_effort"] is None
        assert voice["tools"] == TOOL_PAYLOAD
        rows = store.conn.execute(
            "SELECT role, meta FROM llm_calls ORDER BY id").fetchall()
        voice_rows = [r for r in rows if r["meta"] and json.loads(r["meta"]).get("voice")]
        assert len(voice_rows) == 4
        assert {r["role"] for r in voice_rows} == {"chat"}
        # Call rows are strictly increasing in time (the merge keeps their order).
        times = [m["t_h"] for m in _rows(store)]
        assert times == sorted(times)
    finally:
        store.close()


def test_call_turn_cancel_stops_the_reply(tmp_path, decision_env):
    store, session, client = _answered(tmp_path, ["One. Two. Three. Four."])
    try:
        cancel = StreamCancel()
        got = []
        for sentence in session.call_turn(None, cancel=cancel):
            got.append(sentence)
            cancel.set()
        assert len(got) <= 1
        row = store.conn.execute(
            "SELECT meta FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone()
        assert json.loads(row["meta"])["cancelled"] is True
    finally:
        store.close()


def test_tool_markup_is_never_spoken(tmp_path, decision_env):
    store, session, client = _answered(tmp_path, [
        'Sure thing. <｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="tool_decide_reply">'
    ])
    try:
        sentences = list(session.call_turn(None))
        assert sentences == ["Sure thing."]
        assert "reply_tool_markup" in [e["event"] for e in store.events_since(0)]
    finally:
        store.close()


def test_no_close_and_no_settle_while_the_call_is_on(tmp_path, decision_env):
    store, session, client = _answered(tmp_path, [])
    try:
        now = session.clock.now_h()
        assert session.check_conversation_lifecycle(now + 30.0) is None
        assert session.open_conversation_id() is not None
        drain = session.settle_pending("event-instant")
        assert not drain.notices and not drain.proactive_out
    finally:
        store.close()


def test_end_row_and_restart_recovery(tmp_path, decision_env):
    store, session, client = _answered(tmp_path, ["Hi."], name="rec.db")
    try:
        list(session.call_turn(None))
        session.clock.advance_hours(0.5)
        session.call_heard("Hi.", False)
        last_t = _rows(store)[-1]["t_h"]
        path = store.path
    finally:
        store.close()

    # A new process on the same store finds the call open and closes it,
    # showing the last call row's time as the end.
    from harness.store import SQLiteStore

    store2 = SQLiteStore(path)
    profile = _persona()
    session2 = make_session(store2, clock=VirtualClock(t_h=last_t + 2.0),
                            client=FakeClient(responses=["x"]), seed=SEED,
                            persona_profile=profile)
    try:
        assert session2.call_active()["id"] == "cid"
        assert session2.recover_open_call() is True
        end = _rows(store2)[-1]
        assert end["role"] == "system" and end["content"].startswith(CALL_END_OPENER)
        assert "(30 min)" in end["content"]
        assert session2.call_active() is None
        assert store2.get_kv(CALL_ACTIVE_KEY) == ""
        assert float(store2.get_kv(CALL_LAST_END_KEY)) >= last_t
        assert session2.recover_open_call() is False
    finally:
        store2.close()


# -- cache: every request extends the previous one, through the call ---------- #


def _gate_client():
    return CallAwareClient(
        [f"Reply {i}. More {i}.\n"
         'tool_decide_event: {"message": "pottery soon", "initiate": "yes", '
         '"action": "follow", "reason": "heading over"}\n'
         'tool_decide_reply: {"reply": true, "reason": "sure"}'
         for i in range(60)],
        'tool_decide_call: {"answer": true, "reason": "free"}',
    )


def _gate_session(tmp_path, name):
    store = make_store(tmp_path, name)
    profile = _persona()
    store.save_persona(profile)
    client = _gate_client()
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    return store, session, client


def test_call_requests_extend_each_other_and_the_next_text_turn(tmp_path, decision_env):
    """The gate across a call: pick-up decision, call turns (with an agenda
    boundary crossed mid-call and a card update), the end row, then text
    turns that take up the crossed boundary."""
    store, session, client = _gate_session(tmp_path, "gate.db")
    try:
        for i in range(2):
            session.clock.advance_hours(0.05)
            session.on_message(f"text {i}")
        session.clock.advance_hours(0.05)
        assert session.incoming_call("gatecall").call_answered
        now = session.clock.now_h()
        # A window that opens while they talk.
        _agenda_item(store, start=now + 0.05, end=now + 2.0)
        for i in range(4):
            session.clock.advance_hours(0.03)
            list(session.call_turn(None if i == 0 else f"said {i}"))
            session.call_heard(f"Reply heard {i}.", i == 2)
        session.clock.advance_hours(0.03)
        session.end_call("hangup")
        for i in range(3):
            session.clock.advance_hours(0.05)
            session.on_message(f"after call {i}")
        kinds = [d["popup_kind"] for d in store.decisions_for_day(0)]
    finally:
        store.close()

    assert "tool_decide_call" in kinds
    assert "tool_decide_event" in kinds, kinds          # the crossed boundary
    voice = [c for c in client.calls if c.get("extra")]
    assert len(voice) == 4
    assert len({c["system"] for c in client.calls}) == 1
    durables = [_durable(c) for c in client.calls]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), f"call {index + 1} does not extend call {index}"


def test_a_boundary_inside_the_call_is_stamped_after_it(tmp_path, decision_env):
    """A start pop-up decided the plain way after the call, for a boundary
    that fell inside it, is stamped at the call's end: stamped at the
    boundary it would sort between call rows already sent. The pop-up still
    names the real boundary time."""
    store, session, client = _gate_session(tmp_path, "clamp.db")
    try:
        session.on_message("hey")
        assert session.incoming_call("c").call_answered
        now = session.clock.now_h()
        _agenda_item(store, start=now + 0.05, end=now + 9.0)
        session.clock.advance_hours(0.1)
        list(session.call_turn("hi"))
        session.call_heard("Hi.", False)
        session.end_call("hangup")
        end_t = float(store.get_kv(CALL_LAST_END_KEY))
        # He goes quiet; the next conversation opens after the window started.
        session.clock.advance_hours(6.1)
        session.check_conversation_lifecycle(session.clock.now_h())
        session.on_message("much later")
        event = [d for d in store.decisions_for_day(0)
                 if d["popup_kind"] == "tool_decide_event"]
    finally:
        store.close()
    assert event
    assert float(event[0]["t_h"]) >= end_t
    assert float(event[0]["inputs"]["time"]) < end_t


# -- held messages keep their arrival time -------------------------------------- #


def test_a_held_message_is_stamped_with_its_arrival_time(tmp_path, decision_env):
    from harness.anchor import RealTimeAnchor

    store, session, client = _setup(tmp_path, ["ok.", "ok."])
    try:
        session.clock.advance_hours(1.0)
        session.on_message("sent during the call", arrived_t_h=9.75)
        row = [m for m in _rows(store) if m["role"] == "user"][-1]
        assert json.loads(row["meta"]) == {"stamp_t_h": 9.75}
        anchor = RealTimeAnchor(epoch0_s=0.0, t_h0=0.0, tz="UTC")
        wire = stamped_stream([row], anchor)
        assert "| Time: 09:45" in wire[0]["content"]
    finally:
        store.close()


def test_a_call_over_midnight_keeps_the_stream_append_only(tmp_path, decision_env):
    """The rollover waits for the call; after the end row the day that ended
    during the call settles at its last instant. Its rows (a card, the
    decisions for boundaries crossed during the call) must sort after the
    call rows already sent, not at 23:59."""
    store = make_store(tmp_path, "midnight.db")
    profile = _persona()
    store.save_persona(profile)
    client = _gate_client()
    session = make_session(store, clock=VirtualClock(t_h=23.5), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        session.on_message("still up?")
        assert session.incoming_call("late").call_answered
        now = session.clock.now_h()
        _agenda_item(store, start=now + 0.1, end=23.95)
        for i in range(3):
            session.clock.advance_hours(0.2)          # past midnight
            list(session.call_turn(None if i == 0 else f"said {i}"))
            session.call_heard(f"Heard {i}.", False)
        assert session.clock.day() == 1 and session.current_day == 0
        session.end_call("hangup")
        end_t = max(m["t_h"] for m in _rows(store))
        session.settle_pending("call-end")
        session.clock.advance_hours(0.05)
        session.on_message("night")
        event = [d for d in store.decisions_for_day(0)
                 if d["popup_kind"] == "tool_decide_event"]
    finally:
        store.close()
    assert event and all(float(d["t_h"]) >= end_t for d in event)
    durables = [_durable(c) for c in client.calls]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), f"call {index + 1} does not extend call {index}"
