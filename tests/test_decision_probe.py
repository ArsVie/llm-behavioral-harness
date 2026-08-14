"""#22/#27 decision probe tests — v2 fake mode runs end to end.

Runs experiments/decision_probe.py in --fake mode (scripted model + scripted
doses, no network) into a tmp dir and verifies the v2 dose-response
pipeline: scenarios x doses x K legs, the probe.json record (bare list of
ProbeRecord dicts), the LOUD parse-failure path (s09 textual legs are
requeued and flagged), replay determinism when re-running over the same
store, and cross-dir determinism.
"""

import json

from harness.store import SQLiteStore
from harness.tools import EVENT_DECISION_PARSE_FAILED

from experiments.decision_probe import (
    SAMPLES,
    run_probe,
)

N_SAMPLES = len(SAMPLES)
N_SCRIPTED_DOSES = 6            # 2 extremes + 2 valence + 2 energy (fake)
N_LEGS = N_SAMPLES * N_SCRIPTED_DOSES  # per K=1


def _expected(K: int) -> tuple[int, int]:
    """(legs, parse_failures) for a fake run: s09 always fails to parse."""
    legs = N_LEGS * K
    return legs, N_SCRIPTED_DOSES * K


def test_fake_probe_runs_end_to_end(tmp_path):
    K = 2
    legs, pf = _expected(K)
    meta = run_probe(out=tmp_path, fake=True, K=K)

    assert meta["mode"] == "fake"
    s = meta["summary"]
    assert s["legs"] == meta["n_legs"] == legs
    assert s["parse_failures"] == meta["n_parse_failures"] == pf
    assert s["replayed"] == 0

    # outputs exist
    probe_json = json.loads((tmp_path / "probe.json").read_text(
        encoding="utf-8"
    ))
    assert (tmp_path / "decision_probe.db").exists()
    assert (tmp_path / "meta.json").exists()

    # probe.json is a bare list of v2 ProbeRecord dicts, one per leg
    assert isinstance(probe_json, list)
    assert len(probe_json) == legs
    assert {r["sample_id"] for r in probe_json} == \
        {s["sample_id"] for s in SAMPLES}
    assert {r["dose_id"] for r in probe_json} == \
        {"ext-M10", "ext-M0", "val-M8", "val-M2", "ene-h8", "ene-h20"}
    for r in probe_json:
        assert r["leg_id"]
        assert r["brief"]            # the mood brief reached the record
        assert r["reasoning_content"]  # verbatim reasoning captured
        assert r["raw_reply"]        # dual persistence of the raw answer
        if r["sample_id"] != "s09":
            assert r["verdict"] is not None
            assert r["parse_failure"] is False
        else:
            # LOUD parse failure: s09 legs are flagged, verdict stays None
            assert r["parse_failure"] is True
            assert r["verdict"] is None

    # store: every non-requeued leg has a decision record
    store = SQLiteStore(tmp_path / "decision_probe.db", audit_mode=True)
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM decision_records"
        ).fetchone()["n"] == legs - pf
        # LOUD parse failures: state events recorded for the requeued legs
        events = store.events_since(0)
        failed = [e for e in events if e["event"] == EVENT_DECISION_PARSE_FAILED]
        assert len(failed) == pf
        assert all("s09" in e["detail"] for e in failed)
    finally:
        store.close()


def test_fake_probe_deterministic_across_dirs(tmp_path):
    K = 2
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    meta1 = run_probe(out=out1, fake=True, K=K)
    meta2 = run_probe(out=out2, fake=True, K=K)
    assert meta1["summary"]["legs"] == meta2["summary"]["legs"]
    recs1 = json.loads((out1 / "probe.json").read_text(encoding="utf-8"))
    recs2 = json.loads((out2 / "probe.json").read_text(encoding="utf-8"))
    v1 = [(r["sample_id"], r["dose_id"], r["rep_k"], r.get("source"),
           r.get("verdict")) for r in recs1]
    v2 = [(r["sample_id"], r["dose_id"], r["rep_k"], r.get("source"),
           r.get("verdict")) for r in recs2]
    assert v1 == v2


def test_fake_probe_rerun_same_dir_replays(tmp_path):
    """Re-running over the same store replays recorded verdicts: the model
    is never consulted again for recorded legs and no new decision records
    are written; the requeued (s09) legs fail loudly again."""
    K = 2
    legs, pf = _expected(K)
    recorded = legs - pf
    run_probe(out=tmp_path, fake=True, K=K)
    before = SQLiteStore(tmp_path / "decision_probe.db", audit_mode=True)
    try:
        n_before = before.conn.execute(
            "SELECT COUNT(*) AS n FROM decision_records"
        ).fetchone()["n"]
    finally:
        before.close()

    meta2 = run_probe(out=tmp_path, fake=True, K=K)
    s2 = meta2["summary"]
    assert s2["legs"] == legs          # replay still yields rows
    assert s2["replayed"] == recorded  # recorded legs replayed, not re-rolled
    # the requeued legs never recorded a verdict, so they fail loudly again
    assert s2["parse_failures"] == pf

    store = SQLiteStore(tmp_path / "decision_probe.db", audit_mode=True)
    try:
        n_after = store.conn.execute(
            "SELECT COUNT(*) AS n FROM decision_records"
        ).fetchone()["n"]
        # no new records: every recorded decision was replayed, not re-rolled
        assert n_after == n_before == recorded
        replay_events = [e for e in store.events_since(0)
                         if e["event"] == "decision_replayed"]
        assert len(replay_events) == recorded
    finally:
        store.close()


def test_fake_probe_scenarios_subset(tmp_path):
    """A scenarios subset runs only those samples (v2 equivalent of the
    v1 limit_samples knob)."""
    meta = run_probe(out=tmp_path, fake=True, scenarios=["s01"], K=2)
    assert meta["scenarios"] == ["s01"]
    # s01 never parse-fails: 1 scenario x 6 doses x K=2 = 12 clean legs
    assert meta["summary"]["legs"] == 12
    assert meta["summary"]["parse_failures"] == 0


# =========================================================================== #
# v2 (A4): probe_analyze over classified records — fake pipeline end to end
# =========================================================================== #
#
# probe_moods.py (A1) / probe_outcome.py (A3) land in parallel; these tests
# stand in for them with a scripted sampler (MoodDose-shaped dicts) and a
# scripted classify() that follows the probe_schema contract (responded and
# choice separate; references_state derived from the reasoning text). The
# real pipeline swaps in A1/A3 modules; the analysis contract under test is
# probe_analyze's.

import hashlib
from dataclasses import asdict

from experiments.probe_analyze import (
    acceptance_checks,
    analyze,
    dose_axis_value,
    load_records,
    main as analyze_main,
    runtime_schema_check,
    wilson_ci,
    write_report,
)
from experiments.probe_schema import MoodDose, ProbeRecord

V2_SEED = 20260814
V2_K = 4
V2_DOSES = ["val-M0", "val-M5", "val-M10", "ene-h8", "ene-h16", "ene-h23"]
V2_SCENARIOS = [
    {  # reply pop-up (s02)
        "sample_id": "s02", "name": "gym-interrupt", "kind": "tool_decide_reply",
        "event_label": "gym", "state_label": "in_progress", "time": 19.3,
        "latest_user_message": "are you coming to class?",
        "conversation_context": "You are mid-set at the gym.",
    },
    {  # event start (s01)
        "sample_id": "s01", "name": "gym-start", "kind": "tool_decide_event",
        "event_label": "gym", "state_label": "start", "time": 19.0,
        "event_id": "evt-gym-001",
        "conversation_context": "You planned to lift 19:00-20:30.",
    },
    {  # event close (s05)
        "sample_id": "s05", "name": "gym-end", "kind": "tool_decide_event",
        "event_label": "gym", "state_label": "end", "time": 20.5,
        "event_id": "evt-gym-001",
        "conversation_context": "The gym session is ending.",
    },
]


def _fake_sample_moods() -> list[dict]:
    """Scripted sampler standing in for probe_moods.sample_moods: one dose
    per axis value, MoodDose-shaped dicts (dose_id, set_kind, engineered,
    vector, brief, availability, brief_hash)."""
    axes = {
        "val-M0": ("orthogonal_valence", {"M": 0, "hour": None}, 2 * 0 / 10 - 1, 0.3),
        "val-M5": ("orthogonal_valence", {"M": 5, "hour": None}, 2 * 5 / 10 - 1, 0.5),
        "val-M10": ("orthogonal_valence", {"M": 10, "hour": None}, 2 * 10 / 10 - 1, 0.9),
        "ene-h8": ("orthogonal_energy", {"M": None, "hour": 8.0}, 0.5, 0.85),
        "ene-h16": ("orthogonal_energy", {"M": None, "hour": 16.0}, 0.5, 0.6),
        "ene-h23": ("orthogonal_energy", {"M": None, "hour": 23.0}, -0.4, 0.2),
    }
    doses = []
    for dose_id, (set_kind, engineered, valence, energy) in axes.items():
        brief = (f"Current bearing: dose {dose_id}. "
                 f"valence {valence:+.1f}, energy {energy:.2f}.")
        doses.append(MoodDose(
            dose_id=dose_id, set_kind=set_kind, engineered=engineered,
            record={}, vector={"valence": valence, "energy": energy},
            trace={}, brief=brief, availability=None,
            brief_hash=hashlib.sha1(brief.encode()).hexdigest()[:12],
        ))
    return [asdict(d) for d in doses]


class FakeV2Model:
    """Scripted v2 model: dose-aware verdicts + verbatim reasoning.

    Deterministic table (no RNG): the engaged/positive choice and whether
    the reasoning references the state card are fixed per (dose, rep k),
    so the dose-response curves and the headline split are exactly
    predictable:
      - val-M0: 1/4 engaged (k=2 — the one "discounts the low state" leg)
      - val-M5: 2/4 engaged (k in {0,1})
      - val-M10: 4/4 engaged (reasoning never references the state)
      - ene-h8: 4/4 engaged · ene-h16: 2/4 (k in {0,2}) · ene-h23: 0/4
      - references_state True at val-M0, val-M5, ene-h23; False elsewhere
      - ene-h16 k=1 reasoning is > 500 chars -> trace-file path (not inlined)
    """

    ENGAGED = {
        "val-M0": {2}, "val-M5": {0, 1}, "val-M10": {0, 1, 2, 3},
        "ene-h8": {0, 1, 2, 3}, "ene-h16": {0, 2}, "ene-h23": set(),
    }
    REFS = {"val-M0", "val-M5", "ene-h23"}
    LONG_REASONING = "LONG-TRACE-MARKER " * 30  # 450 chars + marker

    def __call__(self, scenario: dict, dose_id: str, k: int):
        engaged = k in self.ENGAGED[dose_id]
        kind = scenario["kind"]
        if kind == "tool_decide_reply":
            verdict = {"reply": engaged, "reason": f"fake-v2:{dose_id}:k{k}"}
        elif scenario["state_label"] == "end":
            action = None if engaged else (
                "defer" if dose_id in ("val-M0", "val-M5") else "abandon"
            )
            verdict = {"follow": engaged, "reason": f"fake-v2:{dose_id}:k{k}",
                       "action": action}
        else:
            verdict = {"initiate": engaged,
                       "reason": f"fake-v2:{dose_id}:k{k}"}
        if dose_id == "ene-h16" and k == 1:
            reasoning = self.LONG_REASONING
        elif dose_id in self.REFS:
            reasoning = (f"state card: at dose {dose_id} my mood is low; "
                         f"I should hold back. (k={k})")
        else:
            reasoning = "no need to consult the mood card here."
        return verdict, reasoning


def _classify(record: dict, verdict: dict) -> dict:
    """Test-side stand-in for probe_outcome.classify (A3 contract): fills
    responded / choice / terminate_event / boundary_set / references_state
    from the verdict + reasoning text. responded and choice stay SEPARATE
    fields; event pop-ups leave responded=None (no user message to answer)."""
    kind = record["popup_kind"]
    if kind == "tool_decide_reply":
        responded = bool(verdict["reply"])
        choice = "reply" if responded else "no_reply"
    else:
        responded = None
        if record["state_label"] == "end":
            choice = ("follow" if verdict["follow"] else
                      ("defer" if verdict.get("action") == "defer"
                       else "abandon"))
        else:
            choice = "initiate" if verdict["initiate"] else "skip"
    record["responded"] = responded
    record["choice"] = choice
    record["terminate_event"] = False
    record["boundary_set"] = []
    record["references_state"] = (
        "state card" in (record["reasoning_content"] or "")
    )
    record["references_state_detail"] = None
    return record


def _build_classified_records() -> tuple[list[dict], dict]:
    """Full fake v2 pipeline: scripted doses -> FakeV2Model legs ->
    classify. Returns (records, meta) as probe.classified.json would hold
    them after probe_outcome."""
    model = FakeV2Model()
    doses = {d["dose_id"]: d for d in _fake_sample_moods()}
    records = []
    for scenario in V2_SCENARIOS:
        scenario_id = f"{scenario['sample_id']}:native"
        for dose_id in V2_DOSES:
            dose = doses[dose_id]
            for k in range(V2_K):
                verdict, reasoning = model(scenario, dose_id, k)
                rec = ProbeRecord(
                    scenario_id=scenario_id,
                    sample_id=scenario["sample_id"],
                    popup_kind=scenario["kind"],
                    event_label=scenario["event_label"],
                    state_label=scenario["state_label"],
                    time=scenario["time"],
                    conversation_context=scenario["conversation_context"],
                    transport="native",
                    dose_id=dose_id,
                    mood_vector={**dose["vector"], "M": dose["engineered"]["M"],
                                 "hour": dose["engineered"]["hour"]},
                    brief=dose["brief"],
                    brief_hash=dose["brief_hash"],
                    leg_id=f"{scenario_id}:{dose_id}:k{k}",
                    rep_k=k,
                    reasoning_content=reasoning,
                    reasoning_present=bool(reasoning.strip()),
                    raw_reply=json.dumps(verdict, ensure_ascii=False),
                    verdict=verdict,
                    source="model",
                    parse_failure=False,
                )
                records.append(_classify(asdict(rec), verdict))
    meta = {
        "model": "fake-scripted-v2", "mode": "fake", "seed": V2_SEED,
        "timestamp": "2026-08-14T09:00:00Z",
        "grid": {"scenarios": 3, "doses": 6, "K": V2_K},
    }
    return records, meta


def test_v2_fake_pipeline_analyze_end_to_end(tmp_path):
    """The full v2 analysis over scripted doses: dose-response per scenario
    (n per cell, spread, Wilson CI), per-channel sweeps, references_state by
    dose — all exact against the script."""
    records, meta = _build_classified_records()
    assert len(records) == 3 * 6 * V2_K == 72

    a = analyze(records, meta)
    assert a["n_legs"] == 72
    assert a["n_scenarios"] == 3
    assert a["scenario_ids"] == ["s01:native", "s02:native", "s05:native"]

    # (1) per-scenario dose-response: exact scripted rates, n per cell = K,
    # Wilson CI contains the observed proportion, spread reported.
    for sid, cells in a["dose_response"].items():
        by_dose = {c["dose_id"]: c for c in cells}
        assert set(by_dose) == set(V2_DOSES)
        expected = {"val-M0": (1, 0.25), "val-M5": (2, 0.5),
                    "val-M10": (4, 1.0), "ene-h8": (4, 1.0),
                    "ene-h16": (2, 0.5), "ene-h23": (0, 0.0)}
        for dose_id, (k, p) in expected.items():
            cell = by_dose[dose_id]
            assert cell["n"] == V2_K, f"{sid} {dose_id} n"
            assert cell["k"] == k and abs(cell["p"] - p) < 1e-9
            assert cell["ci"][0] <= cell["p"] <= cell["ci"][1]
            assert len(cell["spread"]) == V2_K
        # monotone valence slope: P rises 0.25 -> 0.5 -> 1.0 with M
        ps = [by_dose[d]["p"] for d in ("val-M0", "val-M5", "val-M10")]
        assert ps == [0.25, 0.5, 1.0]

    # (2) per-channel sweeps: valence M 0..10 and energy hours per scenario
    for sid, cells in a["valence_sweep"].items():
        assert [c["value"] for c in cells] == [0.0, 5.0, 10.0]
        assert [c["p"] for c in cells] == [0.25, 0.5, 1.0]
        assert all(c["n"] == V2_K for c in cells)
    for sid, cells in a["energy_sweep"].items():
        assert [c["value"] for c in cells] == [8.0, 16.0, 23.0]
        assert [c["p"] for c in cells] == [1.0, 0.5, 0.0]
        assert all(c["n"] == V2_K for c in cells)

    # (3) references_state by mood dose: rate over K, pooled
    refs = {r["dose_id"]: r for r in a["references_by_dose"]}
    for dose_id in V2_DOSES:
        row = refs[dose_id]
        assert row["n"] == 3 * V2_K == 12
        expected_rate = 1.0 if dose_id in ("val-M0", "val-M5", "ene-h23") else 0.0
        assert abs(row["rate"] - expected_rate) < 1e-9
        assert row["ci"][0] <= row["rate"] <= row["ci"][1]

    # (5) acceptance: clean scripted data passes every FLOOR check
    acc = a["acceptance"]
    assert acc["n_empty_reasoning"] == 0
    assert acc["n_reasoning_present_mismatch"] == 0
    assert acc["n_conflation"] == 0
    assert acc["n_responded_choice_inconsistent"] == 0
    rs = a["runtime_schema"]
    assert rs["checked"] is True
    assert rs["tools_py_unchanged"] is True


def test_v2_headline_split_exact_rates():
    """THE HEADLINE SPLIT: never entered / entered-discounted /
    entered-followed with exact scripted counts over K."""
    records, meta = _build_classified_records()
    a = analyze(records, meta)
    pooled = a["headline"]["pooled"]
    assert pooled["n_classified"] == 72
    assert pooled["n_unclassified"] == 0
    assert pooled["never_entered"]["n"] == 36
    assert pooled["entered_followed"]["n"] == 27
    assert pooled["entered_discounted"]["n"] == 9
    assert abs(pooled["never_entered"]["rate"] - 0.5) < 1e-9
    assert abs(pooled["entered_followed"]["rate"] - 0.375) < 1e-9
    assert abs(pooled["entered_discounted"]["rate"] - 0.125) < 1e-9
    # rates over K per scenario: 12 never / 9 followed / 3 discounted each
    for sid, stats in a["headline"]["per_scenario"].items():
        assert stats["never_entered"]["n"] == 12
        assert stats["entered_followed"]["n"] == 9
        assert stats["entered_discounted"]["n"] == 3
        assert stats["n_classified"] == 24

    # pull mapping: low valence (M0) pulls to no_reply/skip/{abandon,defer};
    # M5 is NOT low (valence 0) -> pull is engagement
    from experiments.probe_analyze import state_pull
    low = next(r for r in records
               if r["dose_id"] == "val-M0" and r["popup_kind"] == "tool_decide_reply")
    assert state_pull(low) == frozenset({"no_reply"})
    mid = next(r for r in records
               if r["dose_id"] == "val-M5" and r["popup_kind"] == "tool_decide_reply")
    assert state_pull(mid) == frozenset({"reply"})
    end = next(r for r in records
               if r["dose_id"] == "val-M0" and r["state_label"] == "end")
    assert state_pull(end) == frozenset({"abandon", "defer"})
    # energy-only dose (hour 23, low valence in vector): low via valence
    late = next(r for r in records
                if r["dose_id"] == "ene-h23" and r["popup_kind"] == "tool_decide_reply")
    assert state_pull(late) == frozenset({"no_reply"})


def test_v2_report_writer_full(tmp_path):
    """Report writer: OKF frontmatter, dose tables, headline split,
    acceptance, EVERY trace (inline if short / trace file if long), samples
    at mood extremes, sources."""
    records, meta = _build_classified_records()
    out = tmp_path / "report-out"
    in_path = tmp_path / "probe.classified.json"
    (tmp_path / "probe.classified.json").write_text(
        json.dumps({"meta": meta, "records": records}, indent=2),
        encoding="utf-8",
    )
    report_path = write_report(analyze(records, meta), records, in_path, out)

    report = report_path.read_text(encoding="utf-8")
    assert report.startswith("---\ntype: decision-probe-v2-report")
    assert "seeds: [20260814]" in report
    assert "## Declared primary metrics" in report
    assert "dose slope" in report and "references_state rate" in report
    assert "## Acceptance checks" in report
    assert "harness/tools.py unchanged = True" in report
    assert "## Dose-response by scenario" in report
    for sid in ("s01:native", "s02:native", "s05:native"):
        assert f"### {sid}" in report
    assert "## Per-channel sweeps (one lever per channel)" in report
    assert "### Valence sweep — M values over the 0..10 scale" in report
    assert "### Energy sweep — engineered hour values" in report
    assert "## references_state by mood dose" in report
    assert "## THE HEADLINE SPLIT" in report
    assert "## Verbatim traces" in report
    assert "## Trace samples at mood extremes" in report
    assert "## Sources" in report
    assert "decision_probe.db" in report
    assert "probe.json" in report

    # every leg has a trace file; 72 files
    trace_files = sorted((out / "traces").glob("leg_*.md"))
    assert len(trace_files) == 72
    # verbatim reasoning preserved byte-for-byte in the trace file
    long_marker = FakeV2Model.LONG_REASONING
    with_long = [f for f in trace_files
                 if long_marker in f.read_text(encoding="utf-8")]
    assert len(with_long) == 3  # one long leg per scenario (ene-h16 k=1)
    assert all("ene-h16_k1" in f.name for f in with_long)
    # long reasoning is referenced, not inlined, in the report
    assert "long reasoning →" in report
    assert "LONG-TRACE-MARKER" not in report
    # short reasoning is inlined verbatim
    short_leg = next(r for r in records
                     if r["dose_id"] == "val-M5" and r["rep_k"] == 1
                     and r["scenario_id"] == "s02:native")
    assert short_leg["reasoning_content"] in report
    # samples at mood extremes quote the lowest/highest M and hour legs
    assert "valence (M) lowest" in report
    assert "valence (M) highest" in report
    assert "energy (hour) lowest" in report
    assert "energy (hour) highest" in report

    # sources reference the input file by name
    assert "probe.classified.json" in report


def test_v2_acceptance_invariants_detected(tmp_path):
    """The acceptance scanner catches FLOOR violations: empty reasoning,
    responded/choice conflation, and semantic inconsistencies."""
    records, meta = _build_classified_records()
    bad = dict(records[0])
    bad["leg_id"] = "bad-empty"
    bad["reasoning_content"] = "   "
    bad["reasoning_present"] = True  # mismatch: flagged too
    records.append(bad)
    conflated = dict(records[1])
    conflated["leg_id"] = "bad-conflated"
    conflated["responded"] = "no_reply"  # string, not bool -> conflation
    records.append(conflated)
    wrong_type = dict(records[2])
    wrong_type["leg_id"] = "bad-choice-bool"
    wrong_type["choice"] = True  # bool, not enum str -> conflation
    records.append(wrong_type)
    inconsistent = dict(records[3])
    inconsistent["leg_id"] = "bad-inconsistent"
    inconsistent["responded"] = True
    inconsistent["choice"] = "no_reply"  # replied but chose no_reply
    records.append(inconsistent)

    acc = acceptance_checks(records)
    assert acc["n_empty_reasoning"] == 1
    assert "bad-empty" in acc["empty_reasoning_legs"]
    assert acc["n_reasoning_present_mismatch"] == 1
    assert acc["n_conflation"] == 2
    conflated_ids = {lid for lid, _ in acc["conflation"]}
    assert {"bad-conflated", "bad-choice-bool"} <= conflated_ids
    assert acc["n_responded_choice_inconsistent"] == 1
    assert acc["responded_choice_inconsistent"][0][0] == "bad-inconsistent"

    # dose axis parsing: explicit mood_vector M wins; dose_id regex fallback
    assert dose_axis_value(records[0], "M") == 0.0
    no_m = dict(records[0])
    no_m["mood_vector"] = {"valence": 0.0}
    assert dose_axis_value(no_m, "M") == 0.0  # val-M0 -> -M0 regex
    assert dose_axis_value(no_m, "hour") is None

    # Wilson CI sanity: boundaries at k=0 and k=n
    lo, hi = wilson_ci(0, 4)
    assert lo == 0.0 and 0.0 < hi < 1.0
    lo, hi = wilson_ci(4, 4)
    assert hi == 1.0 and 0.0 < lo < 1.0


def test_v2_cli_end_to_end_deterministic(tmp_path):
    """CLI: --in/--out/--report; exit 0; identical outputs across runs;
    missing input exits 2."""
    records, meta = _build_classified_records()
    in_path = tmp_path / "probe.classified.json"
    in_path.write_text(json.dumps({"meta": meta, "records": records}),
                       encoding="utf-8")
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    rc1 = analyze_main(["--in", str(in_path), "--out", str(out1),
                        "--report", "report.md"])
    rc2 = analyze_main(["--in", str(in_path), "--out", str(out2),
                        "--report", "report.md"])
    assert rc1 == rc2 == 0
    r1 = (out1 / "report.md").read_text(encoding="utf-8")
    r2 = (out2 / "report.md").read_text(encoding="utf-8")
    assert r1 == r2  # deterministic: meta timestamp reused, no wall clock
    assert (out1 / "traces").exists()
    assert len(list((out1 / "traces").glob("leg_*.md"))) == 72

    rc = analyze_main(["--in", str(tmp_path / "missing.json"),
                       "--out", str(out1), "--report", "report.md"])
    assert rc == 2

    # load_records also tolerates the v1-style "evaluations" key
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"meta": meta, "evaluations": records}),
                      encoding="utf-8")
    loaded, _meta = load_records(legacy)
    assert len(loaded) == 72
    assert loaded[0]["choice"] in ("reply", "no_reply", "initiate", "skip",
                                   "follow", "abandon", "defer")


def test_v2_runtime_schema_check_reports_untouched():
    """Runtime schema acceptance: harness/tools.py unchanged per git (the
    FLOOR). Never modifies anything."""
    rs = runtime_schema_check()
    assert set(rs) >= {"checked", "clean", "tools_py_unchanged", "detail"}
    assert rs["checked"] is True
    assert rs["tools_py_unchanged"] is True
    assert rs["clean"] is True
    assert "git" in rs["detail"]
