"""Proactive-as-decision primitives (WS2): the ``proactive_intent`` steer kind,
the ``tool_decide_proactive`` schema and runner path, and the
``render_popup`` branch.

Uses the real SQLiteStore (tmp_path); the model callable and the capabilities
object are fakes.
"""

import json

import numpy as np
import pytest

from harness.steering import (
    KIND_DAY_ROLLOVER,
    KIND_EVENT_POPUP,
    KIND_PROACTIVE,
    KIND_SCHEDULE_FIRE,
    KIND_USER_MESSAGE,
    render_steer_block,
    wrap_steer_marker,
)
from harness.store import SQLiteStore
from harness.tools import (
    Capabilities,
    DecisionRequeue,
    DecisionRunner,
    EVENT_DECISION_PARSE_FAILED,
    EVENT_DECISION_REPLAYED,
    RawReply,
    parse_native_reply,
    parse_textual_reply,
    render_popup,
)
from tests.helpers.tools import call_from, decision_store, native_call

# helpers


PROACTIVE_INPUTS = {
    "intent_id": "pi-gym-check",
    "hook": "she mentioned trying the new gym",
    "reason": "reach_out_after_gym_talk",
    "source_type": "conversation",
    "source_id": "msg-42",
    "valid_until": "19.75",
    "time": "19.25",
    "silence_h": 4.0,
    "latest_user_message": "gym was packed today",
}
MINIMAL_INPUTS = {
    "hook": "h",
    "reason": "r",
    "source_type": "s",
    "source_id": "i",
    "time": "19.25",
}
LATEST_INPUTS = {
    **PROACTIVE_INPUTS,
    "latest_user_message": "gym was packed today",
}


# -- steer kind registration + priority ------------------------------------- #


def test_proactive_priority_between_event_popup_and_schedule_fire():
    from harness.steering import KIND_PRIORITY

    assert KIND_PRIORITY[KIND_USER_MESSAGE] < KIND_PRIORITY[KIND_EVENT_POPUP]
    assert KIND_PRIORITY[KIND_EVENT_POPUP] < KIND_PRIORITY[KIND_PROACTIVE]
    assert KIND_PRIORITY[KIND_PROACTIVE] < KIND_PRIORITY[KIND_SCHEDULE_FIRE]
    assert KIND_PRIORITY[KIND_SCHEDULE_FIRE] < KIND_PRIORITY[KIND_DAY_ROLLOVER]


# -- steer rendering (payload keys hook/reason/source_type/source_id/valid_until) -- #


def test_render_proactive_never_raises_on_minimal_payload():
    block = render_steer_block(
        {"kind": KIND_PROACTIVE, "payload": {}, "t_h": 5.0}
    )
    assert block
    # str()-fallbacks keep non-string payload values render-safe.
    block2 = render_steer_block(
        {"kind": KIND_PROACTIVE, "payload": {"hook": 42}, "t_h": 5.0}
    )
    assert "Proactive: 42" in block2
    # wrap_steer_marker round-trips the rendered block.
    wrapped = wrap_steer_marker(block)
    assert block in wrapped


# -- schema + verdict parsing ------------------------------------------------- #


def test_parse_native_proactive_verdict():
    verdict = parse_native_reply(
        "tool_decide_proactive",
        [{
            "function": {
                "name": "tool_decide_proactive",
                "arguments": '{"initiate": true, "reason": "she sounds excited"}',
            }
        }],
    )
    assert verdict == {"initiate": True, "reason": "she sounds excited"}


def test_parse_textual_proactive_shorthand():
    verdict = parse_textual_reply(
        "tool_decide_proactive", 'tool_decide_proactive: {no, "too quiet"}'
    )
    assert verdict["initiate"] is False
    assert verdict["reason"] == "too quiet"


def test_parse_proactive_conservative_defaults():
    # Missing decide flag -> conservative decline, never a silent fire.
    verdict = parse_native_reply(
        "tool_decide_proactive",
        [{"function": {"name": "tool_decide_proactive",
                       "arguments": '{"reason": "only a reason"}'}}],
    )
    assert verdict == {"initiate": False, "reason": "only a reason"}


# -- render_popup branch ------------------------------------------------------ #


def test_render_popup_proactive_matches_context():
    popup = render_popup("tool_decide_proactive", PROACTIVE_INPUTS)
    for value in ("she mentioned trying the new gym",
                  "reach_out_after_gym_talk", "conversation", "msg-42",
                  "gym was packed today"):
        assert value in popup
    # Virtual hours reach the model as wall-clock times, never raw decimals.
    assert "19:15" in popup and "19:45" in popup
    assert "19.25" not in popup and "19.75" not in popup
    assert "Latest user message" in popup and "User silence" in popup


def test_render_popup_proactive_minimal_and_conditional_lines():
    # No latest message / no silence -> context lines are not drawn.
    popup = render_popup("tool_decide_proactive", MINIMAL_INPUTS)
    assert "Latest user message" not in popup
    assert "User silence" not in popup
    # Unknown kinds still raise.
    with pytest.raises(ValueError, match="unknown popup_kind"):
        render_popup("tool_decide_proactive_typo", MINIMAL_INPUTS)


def test_render_popup_proactive_never_raises_on_garbage_values():
    # Missing keys and None/odd values degrade, never raise.
    popup = render_popup("tool_decide_proactive", {"hook": None})
    assert popup
    popup2 = render_popup(
        "tool_decide_proactive",
        {"hook": "h", "silence_h": "4.0", "latest_user_message": ""},
    )
    # silence must be numeric to render; empty latest message is falsy.
    assert "User silence" not in popup2
    assert "Latest user message" not in popup2


# -- DecisionRunner execute + replay via FakeClient --------------------------- #


def _proactive_inputs_json(inputs):
    return json.dumps(inputs, ensure_ascii=False, sort_keys=True)


def test_execute_proactive_textual_via_fake_client(tmp_path):
    """A textual proactive verdict is parsed, recorded and replayable."""
    store = decision_store(tmp_path)
    runner = DecisionRunner(store)

    def _call(request):
        assert request.popup_kind == "tool_decide_proactive"
        assert "gym was packed today" in request.popup
        return RawReply(text='tool_decide_proactive: {yes, "sounds good"}')

    res = runner.execute(
        "pro-1", "tool_decide_proactive", LATEST_INPUTS,
        Capabilities(has_native_tools=False), _call,
        day=0, t_h=19.25, delivered_t_h=19.3,
    )
    assert res.verdict == {"initiate": True, "reason": "sounds good"}
    assert res.source == "model"
    assert res.transport == "textual"
    assert res.record_id is not None
    assert res.notice is None  # a proactive fire is not a user-visible notice
    row = store.decision_for_replay("pro-1")
    assert row["popup_kind"] == "tool_decide_proactive"
    assert json.loads(row["verdict_json"]) == res.verdict
    assert json.loads(row["inputs_json"]) == LATEST_INPUTS
    assert row["raw_reply"] == 'tool_decide_proactive: {yes, "sounds good"}'
    assert row["budget_consumed"] == 0  # proactive never consumes the budget
    store.close()


def test_execute_proactive_native_and_budget_untouched(tmp_path):
    store = decision_store(tmp_path)
    runner = DecisionRunner(store, budget=0)  # 0 = always-reply budget gate
    call = call_from([native_call(
        "tool_decide_proactive", {"initiate": False, "reason": "too quiet"}
    )])
    res = runner.execute(
        "pro-2", "tool_decide_proactive", MINIMAL_INPUTS,
        Capabilities(has_native_tools=True), call,
    )
    assert res.verdict == {"initiate": False, "reason": "too quiet"}
    assert res.transport == "native"
    assert res.forced is False  # budget exhaustion never fires proactively
    assert res.budget_consumed is False
    assert res.notice is None  # decline is quiet, not a Telegram notice
    row = store.decision_for_replay("pro-2")
    assert row["source"] == "model"
    assert row["transport"] == "native"
    assert row["budget_consumed"] == 0
    store.close()


def test_execute_proactive_replay_reads_recorded_verdict(tmp_path):
    store = decision_store(tmp_path)
    runner = DecisionRunner(store)
    first = runner.execute(
        "pro-3", "tool_decide_proactive", LATEST_INPUTS,
        Capabilities(False),
        call_from([RawReply(
            text='tool_decide_proactive: {yes, "she asked first"}'
        )]),
        day=0, t_h=19.25,
    )
    assert first.verdict["initiate"] is True

    def _exploding_call(_request):
        raise AssertionError("replay must never call the model again")

    replay = runner.execute(
        "pro-3", "tool_decide_proactive", LATEST_INPUTS,
        Capabilities(False), _exploding_call, day=0, t_h=19.25,
    )
    assert replay.from_replay is True
    assert replay.source == "replay"
    assert replay.transport == "replay"
    assert replay.verdict == first.verdict
    assert replay.record_id == first.record_id
    assert len(store.decisions_for_day(0)) == 1
    events = store.events_since(0)
    assert any(e["event"] == EVENT_DECISION_REPLAYED for e in events)
    store.close()


def test_execute_proactive_decline_never_builds_notice(tmp_path):
    """decline is the everyday no-go verdict: consumed quietly, unlike a
    decide_reply no-reply which surfaces a user-visible notice."""
    store = decision_store(tmp_path)
    runner = DecisionRunner(store, verbose=True)
    res = runner.execute(
        "pro-4", "tool_decide_proactive", LATEST_INPUTS,
        Capabilities(False),
        call_from([RawReply(
            text='tool_decide_proactive: {no, "mid-task"}'
        )]),
        day=0, t_h=19.25,
    )
    assert res.verdict["initiate"] is False
    assert res.notice is None
    store.close()


def test_execute_proactive_parse_failure_loud_and_requeue(tmp_path):
    store = decision_store(tmp_path)
    runner = DecisionRunner(store, parse_failure_mode="requeue")
    call = call_from([RawReply(text="hmm, maybe I should message her")])
    with pytest.raises(DecisionRequeue):
        runner.execute(
            "pro-5", "tool_decide_proactive", MINIMAL_INPUTS,
            Capabilities(False), call,
        )
    events = store.events_since(0)
    failed = [e for e in events if e["event"] == EVENT_DECISION_PARSE_FAILED]
    assert len(failed) == 1
    detail = json.loads(failed[0]["detail"])
    assert detail["decision_id"] == "pro-5"
    assert detail["transport"] == "textual"
    assert store.decision_for_replay("pro-5") is None
    store.close()


# -- server_draw branch -------------------------------------------------------- #


def test_server_draw_proactive_verdict_and_no_model_call(tmp_path):
    store = decision_store(tmp_path)
    runner = DecisionRunner(
        store, decision_source="server_draw",
        rng=np.random.default_rng(99), draw_p=0.6,
    )

    def _exploding_call(_request):
        raise AssertionError("server_draw must never call the model")

    res = runner.execute(
        "pro-draw-1", "tool_decide_proactive", MINIMAL_INPUTS,
        Capabilities(True), _exploding_call, day=0, t_h=19.25,
    )
    assert res.source == "server_draw"
    assert res.transport == "server_draw"
    assert res.verdict["initiate"] in (True, False)
    assert "server draw" in res.verdict["reason"]
    assert set(res.verdict) == {"initiate", "reason"}
    row = store.decision_for_replay("pro-draw-1")
    assert row["source"] == "server_draw"
    assert row["raw_reply"] is None
    store.close()


def test_server_draw_proactive_deterministic_per_seed(tmp_path):
    store1 = decision_store(tmp_path)
    store2 = SQLiteStore(tmp_path / "decisions2.db", audit_mode=True)
    runner1 = DecisionRunner(
        store1, decision_source="server_draw",
        rng=np.random.default_rng(4242), draw_p=0.6,
    )
    runner2 = DecisionRunner(
        store2, decision_source="server_draw",
        rng=np.random.default_rng(4242), draw_p=0.6,
    )

    def _exploding_call(_request):
        raise AssertionError("server_draw must never call the model")

    v1, v2 = [], []
    for i in range(10):
        r1 = runner1.execute(
            f"pro-draw-{i}", "tool_decide_proactive", MINIMAL_INPUTS,
            Capabilities(True), _exploding_call, day=0, t_h=19.25,
        )
        r2 = runner2.execute(
            f"pro-draw-{i}", "tool_decide_proactive", MINIMAL_INPUTS,
            Capabilities(True), _exploding_call, day=0, t_h=19.25,
        )
        assert set(r1.verdict) == {"initiate", "reason"}
        v1.append(r1.verdict["initiate"])
        v2.append(r2.verdict["initiate"])
    assert v1 == v2  # same seed -> same draw sequence
    assert any(v1) and not all(v1)  # and it actually varied (p=0.6)
    store1.close()
    store2.close()


def test_execute_rejects_unknown_popup_kind(tmp_path):
    store = decision_store(tmp_path)
    runner = DecisionRunner(store)
    with pytest.raises(ValueError, match="unknown popup_kind"):
        runner.execute(
            "bad", "tool_decide_unknown", MINIMAL_INPUTS,
            Capabilities(False), call_from([RawReply(text="x")]),
        )
    store.close()
