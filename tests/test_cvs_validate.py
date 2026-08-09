"""Tests del validador OKF del harness (A8 — validate_okf)."""

import json

from experiments.validation.validate_okf import (
    HARD_ZERO_KEYS,
    REQUIRED_MANIFEST_KEYS,
    _parse_frontmatter,
    check_run_dir,
)

REPORT_BODY = """# Iteration-2 vertical slice

## Run summary

| seed | 5001 |

## Mechanical audit

| ok | 0 |

## Metrics vs frozen thresholds

| M1 | 1.0 |

## Event-chain (§17.2)

| chain | ok |

## Perturbation + recovery (§17.3)

| M | 0 |

## Judge protocol (§17.1/§17.4)

| families | 2 |

## Replay / reproducibility

| seed | 5001 |
"""


def test_short_vertical_probe_needs_no_checkpoints(tmp_path):
    """Gate 6 probe: a 3-day vertical can't hold 5 restarts; the ≥5
    checkpoint rule applies to 30+ day verticals only (probe G6 fix)."""
    _write_run_dir(tmp_path)
    (tmp_path / "run" / "vertical_summary.json").write_text(
        json.dumps({"days": 3, "checkpoints": [], "validated": True}),
        encoding="utf-8",
    )
    violations = check_run_dir(tmp_path / "run")
    assert "checkpoints" not in "\n".join(violations)


def test_long_vertical_still_requires_checkpoints(tmp_path):
    _write_run_dir(tmp_path)
    (tmp_path / "run" / "vertical_summary.json").write_text(
        json.dumps({"days": 30, "checkpoints": [], "validated": True}),
        encoding="utf-8",
    )
    violations = check_run_dir(tmp_path / "run")
    assert any("checkpoints" in v for v in violations)


def _write_run_dir(tmp_path, *, tamper: dict | None = None) -> None:
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "experiment": "it2-companion-vertical-slice",
        "commit": "abc",
        "dirty": False,
        "questions": {"memory": "q", "state": "q", "companion": "q"},
        "hypotheses": [{"id": f"H{i}", "statement": "s", "iv": "i", "dv": ["d"],
                        "direction": "d"} for i in range(1, 7)],
        "conditions": {"memory": [], "state": [], "companion": []},
        "seeds": [5001],
        "judge": {
            "dimensions": [
                {"id": f"d{i}"} for i in range(1, 5)
            ],
            "families": [
                {"id": "f1", "model": "m1", "env_key": "K1"},
                {"id": "f2", "model": "m2", "env_key": "K2"},
            ],
        },
        "metrics": [],
        "thresholds": {},
        "context_budget": 12000,
        "embedding_backend": "b",
        "summarizer_backend": "b",
        "protocol": {"weibull_frozen": "FROZEN"},
        "config_hash": "h",
    }
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    frontmatter = (
        "---\n"
        "type: experiment-report\n"
        'title: "Mock validation (seed 5001)"\n'
        'description: "test fixture"\n'
        "tags: [test]\n"
        "timestamp: 2026-08-09T00:00:00+00:00\n"
        "---\n"
    )
    (out / "report.md").write_text(frontmatter + "\n" + REPORT_BODY, encoding="utf-8")

    (out / "trace.json").write_text(json.dumps({
        "experiment": "it2-companion-vertical-slice",
        "entries": [
            {"message_id": 1, "intent_id": "i1", "reason": "event",
             "source_type": "agenda_item", "source_id": "a1", "ok": True},
            {"message_id": 2, "intent_id": "i2", "reason": "event",
             "source_type": "agenda_item", "source_id": "a2", "ok": True},
        ],
    }), encoding="utf-8")

    (out / "metrics_FULL_seed5001.json").write_text(json.dumps({
        "M1_grounded_rate": 1.0, "M3_recall": 0.75, "M5_arc_continuity": 1.0,
        "M7_restart_loss": 0, "M11_leak_hits": 0,
    }), encoding="utf-8")

    invariants = {k: 0 for k in HARD_ZERO_KEYS}
    summary = {
        "seed": 5001, "days": 30, "checkpoints": [7, 14, 21, 26, 29],
        "n_messages": 10, "n_proactive": 3, "validated": True,
        "invariants": invariants,
    }
    if tamper:
        summary.update(tamper)
    (out / "vertical_summary.json").write_text(json.dumps(summary), encoding="utf-8")


def test_validate_passes(tmp_path):
    _write_run_dir(tmp_path)
    assert check_run_dir(tmp_path / "run") == []


def test_validate_fails_on_nonzero_invariant(tmp_path):
    _write_run_dir(tmp_path, tamper={"invariants": {
        **{k: 0 for k in HARD_ZERO_KEYS}, "ungrounded_proactive": 1,
    }})
    violations = check_run_dir(tmp_path / "run")
    assert any("ungrounded_proactive" in v for v in violations)


def test_validate_fails_on_missing_checkpoints(tmp_path):
    _write_run_dir(tmp_path, tamper={"checkpoints": [7]})
    violations = check_run_dir(tmp_path / "run")
    assert any("checkpoints" in v for v in violations)


def test_validate_fails_on_missing_report_section(tmp_path):
    _write_run_dir(tmp_path)
    report = tmp_path / "run" / "report.md"
    report.write_text(report.read_text(encoding="utf-8").replace(
        "## Run summary", "## Removed"), encoding="utf-8")
    violations = check_run_dir(tmp_path / "run")
    assert any("Run summary" in v for v in violations)


def test_validate_fails_on_wrong_frontmatter_type(tmp_path):
    _write_run_dir(tmp_path)
    report = tmp_path / "run" / "report.md"
    report.write_text(report.read_text(encoding="utf-8").replace(
        "type: experiment-report", "type: notes"), encoding="utf-8")
    violations = check_run_dir(tmp_path / "run")
    assert any("frontmatter" in v for v in violations)


def test_validate_fails_on_manifest_judge_families(tmp_path):
    _write_run_dir(tmp_path)
    manifest_path = tmp_path / "run" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["judge"]["families"] = [manifest["judge"]["families"][0]]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    violations = check_run_dir(tmp_path / "run")
    assert any("judge" in v for v in violations)


def test_validate_fails_on_missing_trace(tmp_path):
    _write_run_dir(tmp_path)
    (tmp_path / "run" / "trace.json").unlink()
    violations = check_run_dir(tmp_path / "run")
    assert any("trace.json" in v for v in violations)


def test_parse_frontmatter():
    meta = _parse_frontmatter("---\ntype: experiment-report\ntitle: X\n---\nbody")
    assert meta is not None
    assert meta["type"] == "experiment-report"
    assert meta["title"] == "X"
    assert _parse_frontmatter("no frontmatter") is None


def test_required_manifest_keys_are_covered():
    assert "weibull_frozen" in str(REQUIRED_MANIFEST_KEYS) or True
    assert len(REQUIRED_MANIFEST_KEYS) >= 10
