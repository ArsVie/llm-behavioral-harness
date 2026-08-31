"""A2 — G0 negotiation verdict/request schema tests (harness/tools.py).

Covers the availability-negotiation contract deltas (docs/
availability-negotiation-contract.md): the SERVER-filled ``defer_turns``
mapping from reason phrases (DEFER_N_PATTERNS, clamped, fallback), the
inform-phase mention verdict ``{message: str}`` (native + textual +
legacy-shape normalization), the backward compatibility of legacy decide
verdicts and pop-ups without the new input keys, and the runner plumbing
(phase-aware schema, defer_turns on the recorded decision verdict).

Legacy verdicts and pop-ups must parse/render EXACTLY as before — the
probe/decision tests depend on that; this file only ADDS coverage.
"""

import json

import pytest

from harness.negotiation_contract import (
    DEFAULT_DEFER_TURNS,
    DEFER_N_MAX,
    DEFER_N_MIN,
    DEFER_TURNS_KEY,
)
from harness.store import SQLiteStore
from harness.tools import (
    Capabilities,
    DecisionRequeue,
    DecisionRunner,
    RawReply,
    TOOL_SCHEMAS,
    TOOL_SCHEMAS_INFORM,
    fill_defer_turns,
    map_defer_turns,
    parse_native_reply,
    parse_textual_reply,
    parse_verdict,
    render_popup,
)

# helpers


def _store(tmp_path):
    return SQLiteStore(tmp_path / "decisions.db", audit_mode=True)


def _native_call(name, args: dict):
    return RawReply(
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]
    )


def _call_from(responses):
    queue = list(responses)
    calls = []

    def _call(request):
        calls.append(request)
        return queue.pop(0)

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


EVENT_INPUTS = {
    "event_id": "evt-1",
    "event_label": "gym",
    "state_label": "start",
    "time": "19.0",
}

# defer N mapping (server-filled from the reason text)


def test_map_defer_explicit_number():
    assert map_defer_turns("let me finish 3 more turns") == 3
    assert map_defer_turns("stay for 1 more message") == 1
    assert map_defer_turns("just 2 more replies and I am there") == 2


def test_map_defer_explicit_clamped():
    assert map_defer_turns("hold on, 10 more turns") == DEFER_N_MAX
    assert map_defer_turns("0 more turns") == DEFER_N_MIN


def test_map_defer_just_a_sec():
    for phrase in ("just a sec", "just a second", "just a moment",
                   "just a minute", "wait, just a sec"):
        assert map_defer_turns(phrase) == 1, phrase


def test_map_defer_bit_longer():
    for phrase in ("a bit longer", "stay a bit longer",
                   "just a bit longer, please"):
        assert map_defer_turns(phrase) == DEFAULT_DEFER_TURNS, phrase


def test_map_defer_few_more():
    for phrase in ("a few more", "a few more messages", "just a few more"):
        assert map_defer_turns(phrase) == 3, phrase


def test_map_defer_fallback():
    assert map_defer_turns("I want to stay with you") == DEFAULT_DEFER_TURNS
    assert map_defer_turns("") == DEFAULT_DEFER_TURNS
    assert map_defer_turns("fake: s10") == DEFAULT_DEFER_TURNS


def test_map_defer_tuple_order_first_match_wins():
    # Precedence follows DEFER_N_PATTERNS row order (same as runtime re-arm).
    assert map_defer_turns("just a sec, I need 3 more turns") == 1


def test_fill_defer_turns_only_for_defer_action():
    assert fill_defer_turns({"initiate": True, "reason": "go now",
                             "action": "follow"}) == {
        "initiate": True, "reason": "go now", "action": "follow",
    }
    assert fill_defer_turns({"initiate": False, "reason": "skip",
                             "action": "abandon"}) == {
        "initiate": False, "reason": "skip", "action": "abandon",
    }
    assert fill_defer_turns({"initiate": True, "reason": "start"}) == {
        "initiate": True, "reason": "start",
    }


def test_fill_defer_turns_adds_server_n():
    verdict = fill_defer_turns({"initiate": False, "reason": "just a sec",
                                "action": "defer"})
    assert verdict[DEFER_TURNS_KEY] == 1


def test_fill_defer_turns_overrides_model_emitted_n():
    # Server mapping overrides any model-emitted N.
    verdict = fill_defer_turns({"initiate": False,
                                "reason": "a bit longer",
                                "action": "defer", "defer_turns": 99})
    assert verdict[DEFER_TURNS_KEY] == DEFAULT_DEFER_TURNS
    assert verdict["reason"] == "a bit longer"


# backward compatibility: legacy verdicts parse exactly as before


def test_legacy_native_verdict_parses_exactly():
    verdict = parse_native_reply(
        "tool_decide_event",
        [{"function": {"name": "tool_decide_event",
                       "arguments": '{"initiate": true, "reason": "energized", '
                                    '"action": "follow"}'}}],
    )
    assert verdict == {"initiate": True, "reason": "energized",
                       "action": "follow"}


def test_legacy_native_verdict_with_explicit_decide_phase():
    verdict = parse_native_reply(
        "tool_decide_event",
        [{"function": {"name": "tool_decide_event",
                       "arguments": '{"initiate": false, "reason": "tired"}'}}],
        phase="decide",
    )
    assert verdict == {"initiate": False, "reason": "tired", "action": None}


def test_legacy_textual_verdict_parses_exactly():
    verdict = parse_textual_reply(
        "tool_decide_event",
        'tool_decide_event: {"initiate": false, "reason": "mid-set"}',
    )
    assert verdict["initiate"] is False
    assert verdict["reason"] == "mid-set"
    assert verdict["action"] is None


def test_legacy_shorthand_parses_exactly():
    verdict = parse_textual_reply(
        "tool_decide_event", 'tool_decide_event: {yes, "too tired"}'
    )
    assert verdict == {"initiate": True, "reason": "too tired", "action": None}


def test_message_only_payload_has_no_inform_semantics_in_decide_phase():
    # A {message: ...} payload is not a decide verdict; decide parsing drops it.
    verdict = parse_textual_reply(
        "tool_decide_event", 'tool_decide_event: {"message": "gym soon"}'
    )
    assert verdict == {"initiate": False, "reason": "", "action": None}
    assert "message" not in verdict


# inform-phase verdict: {message: str}, no go/skip/delay action


def test_inform_native_verdict():
    verdict = parse_native_reply(
        "tool_decide_event",
        [{"function": {"name": "tool_decide_event",
                       "arguments": '{"message": "I have gym soon"}'}}],
        phase="inform",
    )
    assert verdict == {"message": "I have gym soon"}


def test_inform_textual_verdict():
    verdict = parse_textual_reply(
        "tool_decide_event",
        'Lily: *glances at the clock* tool_decide_event: '
        '{"message": "gym at seven, heads up"}',
        phase="inform",
    )
    assert verdict == {"message": "gym at seven, heads up"}


def test_inform_legacy_shape_normalized_to_message():
    # Native models see the pinned decide schema on inform legs too; the mention lives in reason.
    verdict = parse_native_reply(
        "tool_decide_event",
        [{"function": {"name": "tool_decide_event",
                       "arguments": '{"initiate": false, "reason": "gym soon"}'}}],
        phase="inform",
    )
    assert verdict == {"message": "gym soon", "reason": "gym soon"}


def test_inform_shorthand_normalized_to_message():
    verdict = parse_textual_reply(
        "tool_decide_event", 'tool_decide_event: {yes, "gym soon"}',
        phase="inform",
    )
    assert verdict["message"] == "gym soon"
    assert verdict["reason"] == "gym soon"


def test_inform_both_message_and_reason_preserved():
    verdict = parse_verdict(
        "tool_decide_event",
        '{"message": "gym at seven", "reason": "the gym at seven"}',
        phase="inform",
    )
    assert verdict == {"message": "gym at seven", "reason": "the gym at seven"}


def test_inform_action_is_dropped():
    verdict = parse_verdict(
        "tool_decide_event",
        '{"message": "gym soon", "action": "follow"}',
        phase="inform",
    )
    assert verdict == {"message": "gym soon"}


def test_inform_empty_message_is_invalid():
    with pytest.raises(ValueError):
        parse_verdict(
            "tool_decide_event", '{"message": ""}', phase="inform"
        )
    with pytest.raises(ValueError):
        parse_verdict(
            "tool_decide_event", '{"initiate": true}', phase="inform"
        )


def test_inform_verdict_not_parsed_without_phase():
    # Without phase, decide parsing drops the message key.
    verdict = parse_textual_reply(
        "tool_decide_event", 'tool_decide_event: {"message": "gym soon"}'
    )
    assert "message" not in verdict
    assert verdict["initiate"] is False


def test_inform_plain_prose_is_the_mention():
    """G2 finding: a real model answering the inform popup in plain prose
    (no tool call, no textual marker) IS the natural mention — the inform
    is a mention, not a verdict. The runner's _parse_raw must accept it."""
    from harness.tools import RawReply, DecisionRunner
    from harness.store import SQLiteStore

    store = SQLiteStore(":memory:", audit_mode=True)
    runner = DecisionRunner(store)
    verdict = runner._parse_raw(
        "tool_decide_event",
        RawReply(
            text=(
                "haha wait, that's the whole thing? you can't just leave "
                "me hanging — also heads up, it's gym o'clock for me in a "
                "bit, so if I go quiet you'll know where I ran off to"
            ),
            tool_calls=None,
        ),
        "native",
        phase="inform",
    )
    assert verdict["message"].startswith("haha wait")
    # Decide phase is strict: prose without a marker fails.
    from harness.tools import DecisionParseError

    with pytest.raises(DecisionParseError):
        runner._parse_raw(
            "tool_decide_event",
            RawReply(text="just some prose", tool_calls=None),
            "native",
            phase="decide",
        )
    store.close()


# popup rendering: negotiation context lines, legacy byte-identical


def test_render_popup_legacy_inputs_byte_identical():
    assert render_popup("tool_decide_event", EVENT_INPUTS) == (
        "{Event: gym, State: start, Time: 19.0}\n"
        '{Initiate:{yes,no}, Reason: ""}'
    )


def test_render_popup_decide_phase_context():
    popup = render_popup("tool_decide_event", {
        **EVENT_INPUTS,
        "phase": "decide",
        "skippable": False,
        "delay_count": 2,
        "window_ending": True,
    })
    assert popup == (
        "{Event: gym, State: start, Time: 19.0}\n"
        '{Initiate:{yes,no}, Reason: ""}\n'
        "Phase: decide\n"
        "Skippable: no\n"
        "Delays so far: 2\n"
        "Window ending: yes"
    )


def test_render_popup_inform_phase_context():
    popup = render_popup("tool_decide_event", {
        **EVENT_INPUTS,
        "phase": "inform",
        "skippable": True,
    })
    assert popup == (
        "{Event: gym, State: start, Time: 19.0}\n"
        '{Message: ""}\n'
        "Phase: inform\n"
        "Skippable: yes"
    )


def test_inform_schema_is_mention_only():
    assert [t["name"] for t in TOOL_SCHEMAS_INFORM] == ["tool_decide_event"]
    params = TOOL_SCHEMAS_INFORM[0]["parameters"]
    assert set(params["properties"]) == {"message"}
    assert params["required"] == ["message"]


def test_decide_schema_stays_pinned():
    # The runtime verdict schema is unchanged; only the description text gained phase guidance.
    event = TOOL_SCHEMAS[0]
    assert set(event["parameters"]["required"]) == {"initiate", "reason"}
    assert set(event["parameters"]["properties"]) == {
        "initiate", "reason", "action",
    }


# runner plumbing: phase-aware request, defer_turns on the recorded verdict


def test_execute_inform_leg_records_message_and_uses_inform_schema(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([_native_call(
        "tool_decide_event", {"message": "gym at seven"}
    )])
    res = runner.execute(
        "neg-gym-inform", "tool_decide_event",
        {**EVENT_INPUTS, "phase": "inform", "skippable": True},
        Capabilities(has_native_tools=True),
        call,
    )
    assert res.verdict == {"message": "gym at seven"}
    assert call.calls[0].tools is TOOL_SCHEMAS_INFORM
    assert call.calls[0].inputs["phase"] == "inform"
    row = store.decision_for_replay("neg-gym-inform")
    assert json.loads(row["verdict_json"]) == {"message": "gym at seven"}
    store.close()


def test_execute_inform_leg_textual_mention(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([RawReply(
        text='tool_decide_event: {"message": "I have gym soon"}'
    )])
    res = runner.execute(
        "neg-gym-inform", "tool_decide_event",
        {**EVENT_INPUTS, "phase": "inform", "skippable": False},
        Capabilities(has_native_tools=False),
        call,
    )
    assert res.verdict == {"message": "I have gym soon"}
    store.close()


def test_execute_inform_silent_mention_requeues(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([_native_call("tool_decide_event", {"initiate": True})])
    with pytest.raises(DecisionRequeue):
        runner.execute(
            "neg-gym-inform", "tool_decide_event",
            {**EVENT_INPUTS, "phase": "inform"},
            Capabilities(has_native_tools=True),
            call,
        )
    store.close()


def test_execute_defer_records_server_filled_defer_turns(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([RawReply(text=(
        'tool_decide_event: {"initiate": false, "reason": "just a sec, '
        'stay with me", "action": "defer"}'
    ))])
    res = runner.execute(
        "neg-gym-decide-0", "tool_decide_event", EVENT_INPUTS,
        Capabilities(has_native_tools=False),
        call,
    )
    assert res.verdict["action"] == "defer"
    assert res.verdict[DEFER_TURNS_KEY] == 1
    row = store.decision_for_replay("neg-gym-decide-0")
    recorded = json.loads(row["verdict_json"])
    assert recorded["action"] == "defer"
    assert recorded[DEFER_TURNS_KEY] == 1
    store.close()


def test_execute_defer_model_emitted_n_is_overridden(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([_native_call("tool_decide_event", {
        "initiate": False, "reason": "a bit longer",
        "action": "defer", "defer_turns": 99,
    })])
    res = runner.execute(
        "neg-gym-decide-1", "tool_decide_event", EVENT_INPUTS,
        Capabilities(has_native_tools=True),
        call,
    )
    # Model-supplied N is ignored; the server mapping wins.
    assert res.verdict[DEFER_TURNS_KEY] == DEFAULT_DEFER_TURNS
    row = store.decision_for_replay("neg-gym-decide-1")
    recorded = json.loads(row["verdict_json"])
    assert recorded[DEFER_TURNS_KEY] == DEFAULT_DEFER_TURNS
    assert "defer_turns" in recorded and recorded["defer_turns"] != 99
    store.close()


def test_execute_follow_verdict_untouched(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([_native_call("tool_decide_event", {
        "initiate": True, "reason": "energized", "action": "follow",
    })])
    res = runner.execute(
        "neg-gym-decide-2", "tool_decide_event", EVENT_INPUTS,
        Capabilities(has_native_tools=True),
        call,
    )
    assert res.verdict == {"initiate": True, "reason": "energized",
                           "action": "follow"}
    assert DEFER_TURNS_KEY not in res.verdict
    store.close()


def test_execute_legacy_inputs_use_pinned_schema(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    call = _call_from([_native_call("tool_decide_event", {
        "initiate": True, "reason": "go",
    })])
    runner.execute(
        "neg-gym-decide-3", "tool_decide_event", EVENT_INPUTS,
        Capabilities(has_native_tools=True),
        call,
    )
    assert call.calls[0].tools is TOOL_SCHEMAS
    assert "defer_turns" not in call.calls[0].popup
    store.close()


def test_execute_rejects_bad_phase(tmp_path):
    store = _store(tmp_path)
    runner = DecisionRunner(store)
    with pytest.raises(ValueError, match="phase"):
        runner.execute(
            "neg-bad", "tool_decide_event",
            {**EVENT_INPUTS, "phase": "nonsense"},
            Capabilities(has_native_tools=False),
            _call_from([RawReply(text="unused")]),
        )
    store.close()
