"""La revisión visual usa ensemble y contrasta fase con energía."""

from __future__ import annotations

import json

from experiments.behavior_showcase import write_showcase


def test_showcase_includes_ensemble_phase_evidence_and_controlled_examples(tmp_path) -> None:
    outputs = write_showcase(
        tmp_path,
        days=30,
        seed=None,
        ensemble_seeds=range(30),
    )

    assert outputs.phase_graph.name == "phase-semantics.png"
    assert outputs.phase_graph.stat().st_size > 10_000
    assert outputs.summary.name == "phase-summary.json"

    summary = json.loads(outputs.summary.read_text(encoding="utf-8"))
    menstrual = summary["phases"]["menstrual"]
    ovulatory = summary["phases"]["ovulatory"]
    assert menstrual["mood_mean"] < ovulatory["mood_mean"]
    assert menstrual["mood_sd"] > ovulatory["mood_sd"]
    assert menstrual["energy_mean"] < ovulatory["energy_mean"]
    assert menstrual["energy_range"] > ovulatory["energy_range"]

    examples = outputs.examples.read_text(encoding="utf-8")
    assert "Same mood, different energy" in examples
    assert "Expected versus sampled mood" in examples

