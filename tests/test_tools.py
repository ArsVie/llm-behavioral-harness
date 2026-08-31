"""Decision-layer tests (WS2): verdict parsing (native + textual), dual
persistence, loud parse failures, per-day budget with forced-reply
exhaustion, server_draw determinism, replay-reads-verdict, notice builder,
transport selection and the env config loader.

Uses the real SQLiteStore (tmp_path) so the runner's persistence surface is
exercised end to end; only the model callable and the capabilities object
are fakes.
"""

import json

import numpy as np
import pytest

from harness.store import SQLiteStore
from harness.tools import (
    Capabilities,
    DecisionConfig,
    DecisionParseError,
    DecisionRequeue,
    DecisionRunner,
    EVENT_BUDGET_FORCED_REPLY,
    EVENT_DECISION_PARSE_FAILED,
    EVENT_DECISION_REPLAYED,
    RawReply,
    TOOL_SCHEMAS,
    build_notice,
    load_decision_config,
    parse_native_reply,
    parse_textual_reply,
    render_popup,
)

# helpers


def _store(tmp_path):
    return SQLiteStore(tmp_path / "decisions.db", audit_mode=True)


def _call_from(responses):
    """Injected callable factory: returns queued RawReply objects."""
    queue = list(responses)
    calls = []

    def _call(request):
        calls.append(request)
        return queue.pop(0)

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


def _native_call(name, args: dict):
    return RawReply(
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]
    )


REPLY_INPUTS = {
    "event_label": "gym",
    "state_label": "in_progress",
    "time": "19.5",
    "latest_user_message": "are you coming to class?",
    "conversation_context": "she said she would go to the gym at 19:00.",
}

EVENT_INPUTS = {
    "event_id": "evt-1",
    "event_label": "gym",
    "state_label": "start",
    "time": "19.0",
}

# parsing


def test_parse_native_reply_event():
    verdict = parse_native_reply(
        "tool_decide_event",
        [{
            "function": {
                "name": "tool_decide_event",
                "arguments": '{"initiate": true, "reason": "energized", '
                             '"action": "follow"}',
            }
        }],
    )
    assert verdict == {"initiate": True, "reason": "energized", "action": "follow"}


def test_parse_native_reply_reply():
    verdict = parse_native_reply(
        "tool_decide_reply",
        [{
            "function": {
                "name": "tool_decide_reply",
                "arguments": '{"reply": false, "reason": "in class", '
                             '"terminate_event": true}',
            }
        }],
    )
    assert verdict == {
        "reply": False, "reason": "in class", "terminate_event": True,
    }


def test_parse_native_reply_missing_optional_fields_default():
    verdict = parse_native_reply(
        "tool_decide_event",
        [{"function": {"name": "tool_decide_event",
                       "arguments": '{"initiate": false}'}}],
    )
    assert verdict == {"initiate": False, "reason": "", "action": None}


def test_parse_native_reply_wrong_tool_name_raises():
    with pytest.raises(ValueError):
        parse_native_reply(
            "tool_decide_event",
            [{"function": {"name": "tool_decide_reply",
                           "arguments": "{}"}}],
        )


def test_parse_textual_json_with_prose_around():
    text = (
        "She's mid-set, honestly. tool_decide_event: "
        '{"initiate": false, "reason": "mid-set, spotter needed"} '
        "so I'll wait for the break."
    )
    verdict = parse_textual_reply("tool_decide_event", text)
    assert verdict["initiate"] is False
    assert verdict["reason"] == "mid-set, spotter needed"


def test_parse_textual_shorthand_sketch():
# Form: {name}: {thinking} tool_decide_event: {yes, "too tired"}
    verdict = parse_textual_reply(
        "tool_decide_event", 'Lily: *sighs* tool_decide_event: {yes, "too tired"}'
    )
    assert verdict["initiate"] is True
    assert verdict["reason"] == "too tired"


def test_parse_textual_reply_no_raises():
    verdict = parse_textual_reply(
        "tool_decide_reply", 'tool_decide_reply: {no, "in class"}'
    )
    assert verdict["reply"] is False
    assert verdict["reason"] == "in class"
    assert verdict["terminate_event"] is False


def test_parse_textual_tolerant_of_linebreaks_and_quotes():
    text = (
        "tool_decide_reply:\n"
        '  {"reply": true,\n'
        '   "reason": "she needs me",\n'
        '   "terminate_event": false}\n'
        "then I answer."
    )
    verdict = parse_textual_reply("tool_decide_reply", text)
    assert verdict["reply"] is True
    assert verdict["reason"] == "she needs me"


def test_parse_textual_missing_marker_raises():
    with pytest.raises(ValueError):
        parse_textual_reply("tool_decide_reply", "I will just reply to her.")


def test_parse_textual_garbage_payload_raises():
    with pytest.raises(ValueError):
        parse_textual_reply("tool_decide_reply", "tool_decide_reply: {maybe}")


# popup rendering


def test_render_popup_event_matches_sketch():
    assert render_popup("tool_decide_event", EVENT_INPUTS) == (
        "{Event: gym, State: start, Time: 19.0}\n"
        '{Initiate:{yes,no}, Reason: ""}'
    )


def test_render_popup_reply_matches_sketch():
    assert render_popup("tool_decide_reply", REPLY_INPUTS) == (
        "{Event: gym, State: in_progress, Time: 19.5}\n"
        '{Reply:{yes,no}, Reason: "", Terminate_event:{yes,no}}\n'
        'Latest user message: "are you coming to class?"'
    )


def test_tool_schemas_shape():
# The tool is the answer form: required params are the verdict fields, not the pop-up inputs.
    assert [t["name"] for t in TOOL_SCHEMAS] == [
        "tool_decide_event", "tool_decide_reply",
    ]
    for t in TOOL_SCHEMAS:
        assert t["description"]
        assert t["parameters"]["type"] == "object"
        assert t["parameters"]["required"]
    event = TOOL_SCHEMAS[0]
    assert set(event["parameters"]["required"]) == {"initiate", "reason"}
    assert set(event["parameters"]["properties"]) == {
        "initiate", "reason", "action",
    }
    reply = TOOL_SCHEMAS[1]
    assert set(reply["parameters"]["required"]) == {"reply", "reason"}
    assert set(reply["parameters"]["properties"]) == {
        "reply", "reason", "terminate_event",
    }


# runner: transport selection + execution


def test_transport_selection_auto_by_capability(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([RawReply(text='tool_decide_reply: {yes, "ok"}')])
    runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False), call
    )
    assert call.calls[0].native is False

    call2 = _call_from([_native_call("tool_decide_reply",
                                     {"reply": True, "reason": "ok"})])
    runner.execute(
        "d2", "tool_decide_reply", REPLY_INPUTS, Capabilities(True), call2
    )
    assert call2.calls[0].native is True
    assert call2.calls[0].tools == TOOL_SCHEMAS
    store.close()


def test_transport_forced_modes(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, tool_mode="textual")
    call = _call_from([RawReply(text='tool_decide_reply: {yes, "ok"}')])
    res = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(True), call
    )
    assert res.transport == "textual"

    runner2 = DecisionRunner(store, tool_mode="native")
    call2 = _call_from([_native_call("tool_decide_reply",
                                     {"reply": True, "reason": "ok"})])
    res2 = runner2.execute(
        "d2", "tool_decide_reply", REPLY_INPUTS, Capabilities(False), call2
    )
    assert res2.transport == "native"
    store.close()


def test_execute_native_dual_persistence(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    tool_calls = [{
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "tool_decide_reply",
            "arguments": json.dumps(
                {"reply": False, "reason": "in class",
                 "terminate_event": True}
            ),
        },
    }]
    call = _call_from([RawReply(tool_calls=tool_calls)])
    res = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(True), call
    )
    assert res.source == "model"
    assert res.transport == "native"
    assert res.verdict == {
        "reply": False, "reason": "in class", "terminate_event": True,
    }
    assert res.record_id is not None
    row = store.decision_for_replay("d1")
    # dual persistence: raw reply AND parsed verdict both on the record
    assert row["raw_reply"] == json.dumps(tool_calls)
    assert json.loads(row["verdict_json"]) == res.verdict
    assert json.loads(row["inputs_json"]) == REPLY_INPUTS
    assert row["popup_kind"] == "tool_decide_reply"
    assert row["source"] == "model"
    assert row["transport"] == "native"
    assert row["day"] == 0
    assert row["budget_consumed"] == 1  # accepted no-reply
    store.close()


def test_execute_textual_persists_raw_text(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([RawReply(text='tool_decide_event: {yes, "lets go"}')])
    res = runner.execute(
        "d1", "tool_decide_event", EVENT_INPUTS, Capabilities(False), call
    )
    assert res.transport == "textual"
    row = store.decision_for_replay("d1")
    assert row["raw_reply"] == 'tool_decide_event: {yes, "lets go"}'
    assert json.loads(row["verdict_json"])["initiate"] is True
    assert row["budget_consumed"] == 0  # event decisions never consume budget
    store.close()


# loud parse failures


def test_parse_failure_loud_and_requeue(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, parse_failure_mode="requeue")
    call = _call_from([RawReply(text="I think I should reply to her.")])
    with pytest.raises(DecisionRequeue):
        runner.execute(
            "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False), call
        )
    events = store.events_since(0)
    failed = [e for e in events if e["event"] == EVENT_DECISION_PARSE_FAILED]
    assert len(failed) == 1
    detail = json.loads(failed[0]["detail"])
    assert detail["decision_id"] == "d1"
    assert detail["transport"] == "textual"
    assert "I think I should reply" in detail["raw_excerpt"]
    # no verdict record for the failed decision (nothing to replay)
    assert store.decision_for_replay("d1") is None
    store.close()


def test_parse_failure_abort(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, parse_failure_mode="abort")
    call = _call_from([RawReply(text="no marker here")])
    with pytest.raises(DecisionParseError):
        runner.execute(
            "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False), call
        )
    events = store.events_since(0)
    assert any(e["event"] == EVENT_DECISION_PARSE_FAILED for e in events)
    store.close()


def test_parse_failure_server_draw_fallback(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(
        store,
        parse_failure_mode="server_draw",
        rng=np.random.default_rng(7),
    )
    call = _call_from([RawReply(text="no marker here")])
    res = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False), call
    )
    assert res.source == "server_draw"
    assert res.transport == "server_draw_fallback"
    assert res.parse_failed is True
    row = store.decision_for_replay("d1")
    assert row["source"] == "server_draw"
    assert row["transport"] == "server_draw_fallback"
    assert row["raw_reply"] == "no marker here"  # raw still persisted
    events = store.events_since(0)
    assert any(e["event"] == EVENT_DECISION_PARSE_FAILED for e in events)
    store.close()


# budget (per-day window; 0 = always reply; unset = off)


def _no_reply_call(text='tool_decide_reply: {no, "busy"}'):
    return _call_from([RawReply(text=text)])


def test_budget_off_unlimited(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, budget=None)
    for i in range(3):
        res = runner.execute(
            f"d{i}", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
            _no_reply_call(),
        )
        assert res.verdict["reply"] is False
        assert res.budget_consumed is True
        assert res.forced is False
    assert len(store.decisions_for_day(0)) == 3
    store.close()


def test_budget_zero_always_reply(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, budget=0)
    res = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _no_reply_call(),
    )
    assert res.verdict["reply"] is True
    assert res.verdict["forced"] is True
    assert "budget exhausted" in res.verdict["reason"]
    assert res.budget_consumed is False
    row = store.decision_for_replay("d1")
    assert row["budget_consumed"] == 0
    events = store.events_since(0)
    assert any(e["event"] == EVENT_BUDGET_FORCED_REPLY for e in events)
    store.close()


def test_budget_exhaustion_forces_reply_then_day_rollover_resets(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, budget=1)
    # first no-reply: accepted, consumes the single budget slot
    r1 = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _no_reply_call(),
    )
    assert r1.verdict["reply"] is False and r1.budget_consumed is True
    # second no-reply the same day: rejected, reply forced
    r2 = runner.execute(
        "d2", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _no_reply_call(),
    )
    assert r2.verdict["reply"] is True and r2.forced is True
    assert r2.budget_consumed is False
    # third decision on the NEXT day: the window reset, no-reply accepted
    inputs_day1 = dict(REPLY_INPUTS, time="25.0")
    r3 = runner.execute(
        "d3", "tool_decide_reply", inputs_day1, Capabilities(False),
        _no_reply_call(),
    )
    assert r3.verdict["reply"] is False and r3.budget_consumed is True
    assert store.decisions_for_day(1)  # recorded under day 1
    store.close()


def test_budget_counts_only_accepted_no_replies(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, budget=1)
    # a reply=True verdict does not consume budget
    runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _call_from([RawReply(text='tool_decide_reply: {yes, "sure"}')]),
    )
    # so a no-reply afterwards is still accepted
    r2 = runner.execute(
        "d2", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _no_reply_call(),
    )
    assert r2.verdict["reply"] is False and r2.budget_consumed is True
    store.close()


# server_draw (decision_source=server_draw)


def test_server_draw_deterministic_per_seed(tmp_path):
    store1 = _store(tmp_path)
    store2 = SQLiteStore(tmp_path / "decisions2.db", audit_mode=True)
    rng1 = np.random.default_rng(4242)
    rng2 = np.random.default_rng(4242)
    runner1 = DecisionRunner(
        store1, decision_source="server_draw", rng=rng1, draw_p=0.6
    )
    runner2 = DecisionRunner(
        store2, decision_source="server_draw", rng=rng2, draw_p=0.6
    )

    def _exploding_call(_request):
        raise AssertionError("server_draw must never call the model")

    v1, v2 = [], []
    for i in range(10):
        r1 = runner1.execute(
            f"d{i}", "tool_decide_reply", REPLY_INPUTS, Capabilities(True),
            _exploding_call,
        )
        r2 = runner2.execute(
            f"d{i}", "tool_decide_reply", REPLY_INPUTS, Capabilities(True),
            _exploding_call,
        )
        assert r1.transport == "server_draw" and r2.transport == "server_draw"
        v1.append(r1.verdict["reply"])
        v2.append(r2.verdict["reply"])
    assert v1 == v2  # same seed -> same draw sequence
    assert any(v1) and not all(v1)  # and it actually varied (p=0.6)
    store1.close()
    store2.close()


def test_server_draw_requires_rng(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store, decision_source="server_draw", rng=None)
    with pytest.raises(RuntimeError):
        runner.execute(
            "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
            _call_from([RawReply(text="unused")]),
        )
    store.close()


def test_server_draw_event_verdict(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(
        store, decision_source="server_draw", rng=np.random.default_rng(1)
    )
    res = runner.execute(
        "d1", "tool_decide_event", EVENT_INPUTS, Capabilities(False),
        _call_from([RawReply(text="unused")]),
    )
    assert res.verdict["initiate"] in (True, False)
    assert res.verdict["reason"]  # canned server-draw reason
    row = store.decision_for_replay("d1")
    assert row["source"] == "server_draw"
    assert row["raw_reply"] is None
    store.close()


# replay reads the recorded verdict, without re-rolling


def test_replay_reads_recorded_verdict(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([RawReply(text='tool_decide_reply: {no, "in class"}')])
    first = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False), call
    )
    assert first.verdict["reply"] is False

    def _exploding_call(_request):
        raise AssertionError("replay must never call the model again")

    replay = runner.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _exploding_call,
    )
    assert replay.from_replay is True
    assert replay.source == "replay"
    assert replay.transport == "replay"
    assert replay.verdict == first.verdict
    assert replay.record_id == first.record_id
    # exactly one decision record; the replay did not duplicate it
    assert len(store.decisions_for_day(0)) == 1
    events = store.events_since(0)
    assert any(e["event"] == EVENT_DECISION_REPLAYED for e in events)
    store.close()


def test_replay_never_rerolls_event_decisions(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([RawReply(text='tool_decide_event: {yes, "go"}')])
    runner.execute(
        "d1", "tool_decide_event", EVENT_INPUTS, Capabilities(False), call
    )

    def _exploding_call(_request):
        raise AssertionError("replay must never call the model again")

    replay = runner.execute(
        "d1", "tool_decide_event", EVENT_INPUTS, Capabilities(False),
        _exploding_call,
    )
    assert replay.from_replay and replay.verdict["initiate"] is True
    store.close()


# notice builder (verbose flag)


def test_build_notice_verbose_off():
    assert build_notice(
        "Lily", {"reply": False, "reason": "in class"}, verbose=False
    ) == "Lily saw your message but chose not to reply yet"


def test_build_notice_verbose_on():
    assert build_notice(
        "Lily", {"reply": False, "reason": "in class"}, verbose=True
    ) == "Lily is not replying, reason: in class"


def test_build_notice_none_when_replying():
    assert build_notice("Lily", {"reply": True}, verbose=True) is None
    assert build_notice("Lily", {"reply": True}, verbose=False) is None


def test_runner_notice_flag(tmp_path):
    store = _store(tmp_path)
    quiet = DecisionRunner(store, verbose=False)
    res = quiet.execute(
        "d1", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _no_reply_call(),
    )
    assert res.notice == "Lily saw your message but chose not to reply yet"

    chatty = DecisionRunner(store, verbose=True)
    res2 = chatty.execute(
        "d2", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _call_from([RawReply(text='tool_decide_reply: {no, "in class"}')]),
    )
    assert res2.notice == "Lily is not replying, reason: in class"

    # replying -> no notice at all
    res3 = chatty.execute(
        "d3", "tool_decide_reply", REPLY_INPUTS, Capabilities(False),
        _call_from([RawReply(text='tool_decide_reply: {yes, "sure"}')]),
    )
    assert res3.notice is None
    store.close()


# env config loader


def test_load_decision_config_defaults(monkeypatch):
    for var in ("HARNESS_VERBOSE", "HARNESS_BUDGET", "HARNESS_DECISION_SOURCE",
                "HARNESS_DECISION_PARSE_FAILURE", "HARNESS_TOOL_MODE"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_decision_config()
    assert cfg.verbose is False
    assert cfg.budget is None          # unset = off
    assert cfg.decision_source == "model"
    assert cfg.parse_failure_mode == "requeue"
    assert cfg.tool_mode == "auto"


def test_load_decision_config_explicit(monkeypatch):
    monkeypatch.setenv("HARNESS_VERBOSE", "1")
    monkeypatch.setenv("HARNESS_BUDGET", "2")
    monkeypatch.setenv("HARNESS_DECISION_SOURCE", "server_draw")
    monkeypatch.setenv("HARNESS_DECISION_PARSE_FAILURE", "abort")
    monkeypatch.setenv("HARNESS_TOOL_MODE", "textual")
    cfg = load_decision_config()
    assert cfg.verbose is True
    assert cfg.budget == 2
    assert cfg.decision_source == "server_draw"
    assert cfg.parse_failure_mode == "abort"
    assert cfg.tool_mode == "textual"


def test_load_decision_config_budget_zero_and_empty(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGET", "0")
    assert load_decision_config().budget == 0  # always reply
    monkeypatch.setenv("HARNESS_BUDGET", "")
    assert load_decision_config().budget is None  # empty = off


def test_load_decision_config_rejects_bad_values(monkeypatch):
    monkeypatch.setenv("HARNESS_BUDGET", "abc")
    with pytest.raises(ValueError):
        load_decision_config()
    monkeypatch.setenv("HARNESS_BUDGET", "-1")
    with pytest.raises(ValueError):
        load_decision_config()
    monkeypatch.delenv("HARNESS_BUDGET")
    monkeypatch.setenv("HARNESS_DECISION_SOURCE", "dice")
    with pytest.raises(ValueError):
        load_decision_config()


def test_decision_config_validation():
    with pytest.raises(ValueError):
        DecisionConfig(tool_mode="magic")
    with pytest.raises(ValueError):
        DecisionConfig(parse_failure_mode="ignore")
    with pytest.raises(ValueError):
        DecisionConfig(decision_source="calculator")
