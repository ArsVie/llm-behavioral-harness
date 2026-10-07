"""Judge tests (W-E1): parsing, clipping, scripted judge."""

import json

from harness.client import FakeClient
from harness.judge import RUBRIC, ScriptedJudge, _parse_score, judge_day


def test_parse_clean_json():
    result = _parse_score('{"score": 0.7, "justification": "warm day"}')
    assert result.score == 0.7
    assert result.justification == "warm day"


def test_parse_clips_out_of_range():
    assert _parse_score('{"score": 3.5}').score == 1.0
    assert _parse_score('{"score": -4.0}').score == -1.0


def test_parse_fallback_number():
    result = _parse_score("score: -0.25")
    assert result.score == -0.25


def test_parse_garbage_returns_zero():
    result = _parse_score("no numbers here at all")
    assert result.score == 0.0
    assert "unparseable" in result.justification


def test_parse_valid_json_non_object_shapes():
    # json.loads succeeds but the payload is not a dict.
    # The first number is extracted and clipped.
    assert _parse_score('"0.5"').score == 0.5
    assert _parse_score("[1, 2]").score == 1.0  # first number, clipped
    assert _parse_score("42").score == 1.0  # bare number, clipped to range
    assert _parse_score('"text"').score == 0.0  # no number at all


def test_parse_null_and_bad_score_fields():
    assert _parse_score('{"score": null}').score == 0.0
    assert _parse_score('{"score": "high"}').score == 0.0
    assert "unparseable" in _parse_score('{"score": "high"}').justification


def test_judge_day_calls_client_json_mode():
    client = FakeClient(responses=['{"score": 0.4, "justification": "decent"}'])
    result = judge_day("user: hi\nassistant: hello!", client)
    assert result.score == 0.4
    call = client.calls[0]
    assert call["temperature"] == 0.0
    assert call["json_mode"] is True
    assert RUBRIC in call["messages"][0]["content"]


def test_a_forked_judge_is_told_where_today_starts():
    """The fork reads a week of stream; the day's plan header bounds today,
    and the last such row wins once a weekday repeats."""
    seen = []
    client = FakeClient(responses=[json.dumps({"score": 0.1, "justification": "x"})] * 2)

    def fork(rubric):
        seen.append(rubric)
        return "SYS", [{"role": "system", "content": "task"}], None

    judge_day("", client, fork=fork, day_marker="Friday's plan:")
    judge_day("", client, fork=fork)
    assert "Friday's plan:" in seen[0]
    assert "Friday's plan:" not in seen[1]


def test_judge_day_honors_client_capability():
    # A client without json_mode support does not receive the flag.
    client = FakeClient(responses=['{"score": -0.2, "justification": "meh"}'])
    client.supports_json = False
    result = judge_day("user: hi", client)
    assert result.score == -0.2
    assert client.calls[0]["json_mode"] is False


def test_scripted_judge():
    judge = ScriptedJudge(score=0.9, justification="scripted")
    result = judge.judge_day("anything")
    assert result.score == 0.9
    clipped = ScriptedJudge(score=5.0)
    assert clipped.score == 1.0


def test_day_facts_count_his_messages_and_her_waits():
    """The live Thursday of week 1: she wrote at 10:15, he answered once at
    23:23; the judge read "he reached out twice" off the bare clock stamps."""
    from harness.judge import day_facts

    clock = lambda t: f"{int(t):02d}:{round((t % 1) * 60):02d}"  # noqa: E731
    thursday = [(10.25, "assistant", True), (23.0, "user", False),
                (23.1, "assistant", False)]
    assert day_facts(thursday, clock, 24.0) == (
        "He wrote you 1 message today, the first at 23:00 and the last at 23:00. "
        "You wrote to him first at 10:15 and he answered at 23:00, 12 h 45 min later.")
    silent = [(21.0, "assistant", True)]
    assert day_facts(silent, clock, 24.0) == (
        "He didn't write to you at all today. You wrote to him first at 21:00 "
        "and he never answered it today (3 h 00 min left).")


def test_the_facts_ride_the_forked_rubric():
    seen = []
    client = FakeClient(responses=[json.dumps({"score": -0.4, "justification": "x"})])
    judge_day("", client, fork=lambda rubric: seen.append(rubric) or None,
              facts="He didn't write to you at all today.")
    assert "He didn't write to you at all today." in seen[0]


def test_an_empty_reply_is_asked_once_more():
    """One of eight forked calls came back empty in the 2026-09-26 replay; an
    empty reply used to land a neutral 0.0 straight into her mood."""
    client = FakeClient(responses=["", json.dumps({"score": -0.5, "justification": "gone all day"})])
    fork = lambda rubric: ("SYS", [{"role": "system", "content": rubric}], None)  # noqa: E731
    result = judge_day("", client, fork=fork)
    assert (result.score, result.justification) == (-0.5, "gone all day")
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1], (
        "the retry is the identical request"
    )


def test_a_second_failure_keeps_the_neutral_fallback():
    client = FakeClient(responses=["", "no json here"])
    result = judge_day("user: hi", client)
    assert result.score == 0.0 and len(client.calls) == 2
