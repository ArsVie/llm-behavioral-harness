"""Ablation experiment tests (W-E3): fake-mode plumbing + invariants."""

import json

from experiments.e2e_ablation import (
    MONTHS,
    _cell_stats,
    _fake_client,
    _leakage_hits,
    _month_judge,
    _month_user,
    _tone_metrics,
    run_cell,
)


def test_leakage_hits():
    assert _leakage_hits("I feel great today") == []
    assert "phase_label" in _leakage_hits("I'm in my follicular phase")
    assert "internal_tokens" in _leakage_hits("my mu is high")
    assert "self_report" in _leakage_hits("today my mood is low")


def test_tone_metrics():
    m = _tone_metrics("I really love this! Yes!")
    assert m["words"] == 5
    assert m["exclamations"] == 2
    assert m["first_person"] >= 1


def test_month_user_scripts_differ():
    bad = _month_user("horrible", 3)
    good = _month_user("perfect", 3)
    assert bad.message_for(0) != good.message_for(0)


def test_month_judge_scores():
    assert _month_judge("horrible")("", None).score == -0.7
    assert _month_judge("perfect")("", None).score == 0.7
    assert _month_judge("flat")("", None).score == 0.0


def test_fake_client_scripted():
    client = _fake_client("horrible", 4)
    assert client.chat([{"role": "user", "content": "x"}]) == "Fine."


def test_run_cell_structure(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    client = _fake_client("horrible", 3)
    records = run_cell(
        "horrible", True, 3, 1001, out, client, "Persona.", "fake",
        judge=_month_judge("horrible"),
    )
    assert len(records) == 3
    assert all(r["reply"] for r in records)
    assert all(r["leaks"] == [] for r in records)
    assert all(r["tone"]["words"] > 0 for r in records)
    assert all(r["M"] is not None for r in records)
    # persisted store exists
    assert (out / "cell_horrible_on.db").exists()


def test_run_cell_off_has_no_state(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    client = _fake_client("perfect", 2)
    records = run_cell(
        "perfect", False, 2, 1001, out, client, "Persona.", "fake",
        judge=_month_judge("perfect"),
    )
    assert all(r["M"] is None for r in records)
    assert all(r["directive"] is None for r in records)


def test_cell_stats_shape():
    records = [
        {"tone": {"words": 4, "exclamations": 1, "first_person": 2}, "leaks": [],
         "M": 7, "directive": {"valence": 0.5, "playfulness": 0.4,
                                "reflectiveness": 0.3, "warmth": 0.8, "energy": 0.6}},
    ]
    stats = _cell_stats(records)
    assert stats["mean_words"] == 4.0
    assert stats["mean_M"] == 7.0
    assert stats["leak_hits"] == {"phase_label": 0, "internal_tokens": 0, "self_report": 0}


def test_transcripts_json_written_by_main(tmp_path, capsys):
    from experiments.e2e_ablation import main

    rc = main(["--fake", "--days", "2", "--out", str(tmp_path / "ablation")])
    assert rc == 0
    data = json.loads((tmp_path / "ablation" / "transcripts.json").read_text())
    assert len(data) == 12  # 3 months x 2 harness x 2 days
    assert {r["cell"] for r in data} == {
        f"{m}_{h}" for m in MONTHS for h in ("on", "off")
    }
    report = (tmp_path / "ablation" / "report.md").read_text()
    assert "Manipulation checks" in report
