"""WS4 integration wiring tests: steering + decision layer in Session._chat.

Exercises the wiring the integration agent added on top of the three WS
streams (design plans/harness-runtime-design-2026-08-14.md §2):

- idle-boundary drain (event pop-ups, mid-turn user messages) and the
  decide_event / decide_reply pop-ups through the real DecisionRunner;
- single reply-path invariant: a no-reply verdict suppresses the ordinary
  reply and the notice rides out through TurnResult;
- re-queue on parse failure (requeue policy) and on interrupted turns;
- reasoning effort passthrough + reasoning persistence in llm_calls meta;
- the cached day-start block (tier 2) and default inertness (no env vars =>
  the harness behaves exactly as before the redesign).

All tests use the real SQLiteStore (v5 seams) and the real DecisionRunner;
the model is a scripted FakeClient.
"""

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


def _store(tmp_path, name: str = "w.db") -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


# --------------------------------------------------------------------------- #
# event pop-ups (decide_event)
# --------------------------------------------------------------------------- #


def test_event_popup_initiate_fires_proactive_out(tmp_path):
    """An agenda item started before the turn: the idle boundary enqueues an
    event pop-up, the DecisionRunner executes decide_event (textual reply on
    a native-capable client exercises the text fallback), an initiate
    verdict produces a proactive_out message, and the decision + delivery
    are both persisted (delivered_t_h recorded)."""
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": true, "reason": "ready to go"}',
        "main reply",
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    assert session.steering_enabled()

    result = session.on_message("hello")

    assert result.reply == "main reply"
    assert result.proactive_out == (("event_popup", "ready to go"),)
    assert result.notices == ()
    # the pop-up was a real second model call whose message payload carried
    # the steer-marker-wrapped pop-up block
    assert len(client.calls) == 2
    assert STEER_MARKER_OPEN in client.calls[0]["messages"][-1]["content"]
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
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": false, "reason": "too tired"}',
        "main reply",
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


def test_event_popup_end_abandon_marks_item_skipped(tmp_path):
    """An END pop-up with action=abandon closes the event server-side: the
    agenda item is marked skipped (the NOW-semantics state card stops
    showing it). The start pop-up is consumed by an earlier turn so the end
    pop-up is the only one in play at 12:00."""
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": false, "reason": "later"}',
        "morning reply",
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # consumes the START pop-up
    assert store.pending_steers() == []

    clock.advance_hours(2.5)  # 12:00 — the item ended
    client.responses.extend([
        'tool_decide_event: {"initiate": false, "reason": "done", '
        '"action": "abandon"}',
        "main reply",
    ])
    result = session.on_message("hello")

    assert result.reply == "main reply"
    items = store.list_agenda_items(day=0)
    assert [it.status for it in items] == ["skipped"]
    store.close()


# --------------------------------------------------------------------------- #
# decide_reply + the single reply-path invariant
# --------------------------------------------------------------------------- #


def test_decide_reply_no_reply_suppresses_ordinary_reply(tmp_path):
    """A user message while an event is in progress runs decide_reply; a
    no-reply verdict suppresses the ordinary reply entirely (single
    reply-path invariant) and the notice rides out through TurnResult. The
    user message IS persisted; no assistant row is created; the main LLM
    call never happens (the pop-up call is the only call)."""
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": false, "reason": "later"}',
        "morning reply",
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
    # 2 calls in the morning turn (start pop-up + main reply) + 1 pop-up call
    # for the decide_reply turn — the main call never happens for this turn
    assert len(client.calls) == 3  # morning pop-up + morning main + reply pop-up
    msgs = store.messages_for_day(0)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    records = store.decisions_for_day(0)
    assert len(records) == 2
    assert records[-1]["popup_kind"] == "tool_decide_reply"
    assert records[-1]["verdict"]["reply"] is False
    store.close()


def test_decide_reply_yes_proceeds_with_ordinary_reply(tmp_path):
    """A reply=yes verdict proceeds with the ordinary reply; terminate_event
    closes the event server-side (item -> skipped)."""
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": false, "reason": "later"}',
        "morning reply",
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # consumes the START pop-up

    client.responses.extend([
        'tool_decide_reply: {"reply": true, "reason": "one sec", '
        '"terminate_event": true}',
        "ok here I am",
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
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": false, "reason": "later"}',
        "morning reply",
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


# --------------------------------------------------------------------------- #
# re-queue semantics
# --------------------------------------------------------------------------- #


def test_parse_failure_requeues_steer_for_next_boundary(tmp_path):
    """A textual reply without a parseable marker raises DecisionRequeue
    inside the runner; the steer returns to pending (the parse failure is a
    LOUD recorded event) and the next turn drains it again — this time with
    a parseable reply. The raw gibberish reply stays persisted."""
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        "I guess I should? maybe?",                    # popup 1: unparseable
        "first reply",                                 # main call 1
        'tool_decide_event: {"initiate": false, "reason": "no"}',
        "second reply",
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
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=9.5)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": false, "reason": "later"}',
        "morning reply",
    ])
    session = _session(store, client=client, clock=clock,
                       decision=DecisionConfig())
    session.on_message("morning")  # consumes the START pop-up
    assert store.pending_steers() == []

    class BoomClient(FakeClient):
        def chat_with_meta(self, messages, **kwargs):
            raise RuntimeError("boom")

    session.client = BoomClient()  # the pop-up call now explodes
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


# --------------------------------------------------------------------------- #
# thinking passthrough + persistence
# --------------------------------------------------------------------------- #


def test_thinking_effort_passthrough_and_reasoning_persistence(
    tmp_path, monkeypatch,
):
    """HARNESS_THINKING_EFFORT=low reaches the client as reasoning_effort,
    the max_tokens cap is dropped (repo pitfall 3af0a5a: never cap a
    reasoning model), and the model's reasoning is persisted in the llm_call
    meta (audit renders it under #Thinking)."""
    monkeypatch.setenv("HARNESS_THINKING_EFFORT", "low")
    store = _store(tmp_path)
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
    """With no HARNESS_* env vars and no injected config the harness is
    exactly as before: one model call per turn, no reasoning_effort, no
    steering activity, no meta."""
    store = _store(tmp_path)
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


# --------------------------------------------------------------------------- #
# three-tier context: cached day-start block
# --------------------------------------------------------------------------- #


def test_day_start_block_stable_within_day_changes_across_days(tmp_path):
    """Tier 2 (day-start block) is rendered once per day and cached: two
    turns of the same day share the identical block; the next day's block
    differs (new agenda)."""
    store = _store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0, activity="pottery"),)))
    store.save_agenda(1, DailyAgenda(1, (_item(33.0, 35.0, activity="chess",
                                                item_id="ag2"),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=["r1", "r2", "r3"])
    session = _session(store, client=client, clock=clock)

    def _agenda_segment(system: str) -> str:
        # Tier-2 day-start block ends with the agenda section ("Today's
        # agenda:"); pull just that segment so the cached-block comparison
        # is independent of section ordering inside the state card.
        _, sep, rest = system.partition("\n\nToday's agenda:")
        if not sep:
            return ""
        return rest.split("\n\n", 1)[0]

    session.on_message("morning")
    session.on_message("still here")
    block0a = _agenda_segment(client.calls[0]["system"])
    block0b = _agenda_segment(client.calls[1]["system"])
    assert block0a == block0b, "day-start block must be stable within the day"
    assert "pottery" in block0a

    clock.advance_to_day(1)
    clock.advance_hours(10.0)
    session.on_message("next day")
    block1 = _agenda_segment(client.calls[2]["system"])
    assert block1 != block0a, "day-start block must refresh at rollover"
    assert "chess" in block1
    store.close()
