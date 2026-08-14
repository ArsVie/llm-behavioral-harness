"""#22 decision probe tests — fake mode runs end to end.

Runs experiments/decision_probe.py in --fake mode (scripted model, no
network) into a tmp dir and verifies the full pipeline: ~105 evaluations
(15 samples x 3 states x 2 transports + 15 server draws), the OKF report
with the per-evaluation table and verbatim answers, the probe.json record,
the LOUD parse-failure path (s09 textual legs are requeued and recorded as
state events), and replay determinism when re-running over the same store.
"""

import json

from harness.store import SQLiteStore
from harness.tools import EVENT_DECISION_PARSE_FAILED

from experiments.decision_probe import (
    SAMPLES,
    run_probe,
)

N_SAMPLES = len(SAMPLES)
N_MODEL_CALLS = N_SAMPLES * 3 * 2      # states x transports
N_DRAWS = N_SAMPLES
N_TOTAL = N_MODEL_CALLS + N_DRAWS
# s09 textual legs (3 states) are scripted to be unparseable -> requeued
N_REQUEUED = 3
N_RECORDS = N_MODEL_CALLS - N_REQUEUED + N_DRAWS


def test_fake_probe_runs_end_to_end(tmp_path):
    meta = run_probe(out=tmp_path, fake=True)

    assert meta["mode"] == "fake"
    assert meta["n_samples"] == N_SAMPLES == 15
    s = meta["summary"]
    assert s["evaluations"] == N_TOTAL == 105  # ~100 calls per the #22 spec
    assert s["parse_failures"] == N_REQUEUED == 3

    # outputs exist
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    probe_json = json.loads((tmp_path / "probe.json").read_text(
        encoding="utf-8"
    ))
    assert (tmp_path / "decision_probe.db").exists()

    # OKF frontmatter: type + seeds, starting at byte 0
    assert report.startswith("---\ntype: decision-probe-report")
    assert "seeds: [20260814]" in report
    # per-evaluation table with the required columns
    for col in ("sample", "transport", "reasoning", "verdict", "reason",
                "parse failure"):
        assert f"| {col}" in report
    # plain-language verbatim listing quotes the exact model answers
    assert "## Verbatim answers (plain-language listing)" in report
    assert "fake: s01" in report
    assert "tool_decide_reply:" in report

    # probe.json carries every evaluation with the raw reply
    evals = probe_json["evaluations"]
    assert len(evals) == N_TOTAL
    assert {r["sample_id"] for r in evals} == {s["sample_id"] for s in SAMPLES}
    by_kind = {s["sample_id"]: s["kind"] for s in SAMPLES}
    for r in evals:
        assert r["popup_kind"] == by_kind[r["sample_id"]]
        if r["transport"] in ("native", "textual"):
            assert r["raw_reply"]  # dual persistence of the raw answer
    # requeued legs are flagged loudly
    requeued = [r for r in evals if r.get("requeued")]
    assert len(requeued) == N_REQUEUED
    assert {r["sample_id"] for r in requeued} == {"s09"}
    assert all(r["parse_failure"] for r in requeued)
    # server draws never call the model and carry the canned reason
    draws = [r for r in evals if r["transport"] == "server_draw"]
    assert len(draws) == N_DRAWS
    assert all(r["source"] == "server_draw" for r in draws)
    assert all("server draw" in r["reason"] for r in draws)

    # store: every non-requeued evaluation has a decision record
    store = SQLiteStore(tmp_path / "decision_probe.db", audit_mode=True)
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM decision_records"
        ).fetchone()["n"] == N_RECORDS
        # LOUD parse failures: state events recorded for the requeued legs
        events = store.events_since(0)
        failed = [e for e in events if e["event"] == EVENT_DECISION_PARSE_FAILED]
        assert len(failed) == N_REQUEUED
        assert all("s09" in e["detail"] for e in failed)
        # budget untouched: no forced replies in the probe (budget off)
        assert not any(e["event"] == "budget_exhausted_forced_reply"
                       for e in events)
    finally:
        store.close()


def test_fake_probe_deterministic_across_dirs(tmp_path):
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    meta1 = run_probe(out=out1, fake=True)
    meta2 = run_probe(out=out2, fake=True)
    assert meta1["summary"]["replied_or_initiated"] == \
        meta2["summary"]["replied_or_initiated"]
    evals1 = json.loads((out1 / "probe.json").read_text(
        encoding="utf-8"))["evaluations"]
    evals2 = json.loads((out2 / "probe.json").read_text(
        encoding="utf-8"))["evaluations"]
    v1 = [(r["sample_id"], r["state"], r["transport"], r.get("reply"))
          for r in evals1]
    v2 = [(r["sample_id"], r["state"], r["transport"], r.get("reply"))
          for r in evals2]
    assert v1 == v2


def test_fake_probe_rerun_same_dir_replays(tmp_path):
    """Re-running over the same store replays recorded verdicts: the model
    is never consulted again and no new decision records are written."""
    run_probe(out=tmp_path, fake=True)
    before = SQLiteStore(tmp_path / "decision_probe.db", audit_mode=True)
    try:
        n_before = before.conn.execute(
            "SELECT COUNT(*) AS n FROM decision_records"
        ).fetchone()["n"]
    finally:
        before.close()

    meta2 = run_probe(out=tmp_path, fake=True)
    s2 = meta2["summary"]
    assert s2["evaluations"] == N_TOTAL  # replay still yields rows
    # the requeued legs never recorded a verdict, so they fail loudly again
    assert s2["parse_failures"] == N_REQUEUED

    store = SQLiteStore(tmp_path / "decision_probe.db", audit_mode=True)
    try:
        n_after = store.conn.execute(
            "SELECT COUNT(*) AS n FROM decision_records"
        ).fetchone()["n"]
        # no new records: every recorded decision was replayed, not re-rolled
        assert n_after == n_before == N_RECORDS
        replay_events = [e for e in store.events_since(0)
                         if e["event"] == "decision_replayed"]
        assert len(replay_events) == N_RECORDS
    finally:
        store.close()


def test_fake_probe_samples_subset(tmp_path):
    meta = run_probe(out=tmp_path, fake=True, limit_samples=3)
    assert meta["n_samples"] == 3
    assert meta["summary"]["evaluations"] == 3 * 6 + 3
