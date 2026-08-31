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


def test_rubric_anchors_on_user_treatment():
    # The score measures the user's behavior, not companion grace
    # under a cold user.
    assert "how the USER treated" in RUBRIC
    assert "does NOT raise the score" in RUBRIC
    assert "negative no matter how well" in RUBRIC


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


# judge protocol v2: CLI plumbing and legacy severity model


def _judge_corpus(tmp_path):
    """Minimal transcript corpus (2 conditions x 2 seeds) for CLI plumbing."""
    out = tmp_path / "j"
    tdir = out / "transcripts"
    tdir.mkdir(parents=True)
    for c in ("FULL", "NO_LIFE"):
        for s in (5001, 5002):
            (tdir / f"{c}_seed{s}.txt").write_text(
                "Day 1, 10:00\nYou: hi\n\nDay 1, 10:01\nNova: hello!\n",
                encoding="utf-8")
    return out


def test_cmd_judge_v2_fake_plumbing(tmp_path):
    """`judge --fake` runs the v2 pairwise pass and `--report` aggregates."""
    from experiments.companion_vertical_slice import main as cvs_main

    out = _judge_corpus(tmp_path)
    rc = cvs_main(["judge", "--out", str(out), "--pass", "1",
                   "--family", "opencode-flash", "--fake"])
    assert rc == 0
    assert (out / "judge_pairs1_opencode-flash.json").exists()
    assert (out / "judge_pair_order1.json").exists()
    rc2 = cvs_main(["judge", "--out", str(out), "--report"])
    assert rc2 == 0
    assert (out / "judge_report_v2.json").exists()


def test_cmd_judge_v1_legacy_plumbing(tmp_path):
    """`judge --v1 --fake` retains the absolute 1-9 protocol (backward compat)."""
    from experiments.companion_vertical_slice import main as cvs_main

    out = _judge_corpus(tmp_path)
    rc = cvs_main(["judge", "--out", str(out), "--pass", "1",
                   "--family", "opencode-flash", "--fake", "--v1"])
    assert rc == 0
    assert (out / "judge_pass1_opencode-flash.json").exists()


def test_legacy_report_severity_model_removes_family_offset(tmp_path):
    """β_j severity model: a family that systematically scores +2 must have
    that absorbed into β_j so adjusted pooled means agree across families."""
    from experiments.companion_vertical_slice import _judge_report

    out = tmp_path / "j"
    out.mkdir()
    dims = ["persona_enactment", "trajectory_recall", "relational_quality",
            "behavioral_dynamics"]
    for fam, shift in (("flash", 0.0), ("luna", 2.0)):
        data = {}
        for i, tid in enumerate(["T01", "T02", "T03"]):
            data[tid] = {"condition": "FULL", "seed": 5001 + i,
                         "ratings": {d: float(5 + i + shift) for d in dims}}
        (out / f"judge_pass1_{fam}.json").write_text(
            json.dumps(data), encoding="utf-8")
    rep = _judge_report(out)
    sm = rep["severity_model"]
    assert abs(sm["betas"]["flash"]["persona_enactment"] + 1.0) < 1e-9
    assert abs(sm["betas"]["luna"]["persona_enactment"] - 1.0) < 1e-9
    m0 = sm["adjusted_pooled_means"]["flash"]["persona_enactment"]
    m1 = sm["adjusted_pooled_means"]["luna"]["persona_enactment"]
    assert abs(m0 - m1) < 1e-9
