"""Judge tests (W-E1): parsing, clipping, scripted judge."""

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


def test_judge_day_calls_client_json_mode():
    client = FakeClient(responses=['{"score": 0.4, "justification": "decent"}'])
    result = judge_day("user: hi\nassistant: hello!", client)
    assert result.score == 0.4
    call = client.calls[0]
    assert call["temperature"] == 0.0
    assert call["json_mode"] is True
    assert RUBRIC in call["messages"][0]["content"]


def test_scripted_judge():
    judge = ScriptedJudge(score=0.9, justification="scripted")
    result = judge.judge_day("anything")
    assert result.score == 0.9
    clipped = ScriptedJudge(score=5.0)
    assert clipped.score == 1.0
