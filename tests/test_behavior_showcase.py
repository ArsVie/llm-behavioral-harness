"""Integración: emulación reproducible de treinta días."""

from __future__ import annotations

import json

from experiments.behavior_showcase import build_emulation, write_showcase


def test_build_emulation_is_reproducible_and_keeps_all_days() -> None:
    first = build_emulation(days=30, seed=3001)
    second = build_emulation(days=30, seed=3001)

    assert first == second
    assert len(first) == 30
    assert [day.day for day in first] == list(range(30))
    assert len({round(day.energy_afternoon, 3) for day in first}) > 3
    assert len({round(day.behavior.warmth, 3) for day in first}) > 3


def test_write_showcase_creates_graph_trace_and_examples(tmp_path) -> None:
    outputs = write_showcase(tmp_path, days=30, seed=3001)

    assert outputs.graph.name == "30-day-behavior.png"
    assert outputs.graph.stat().st_size > 10_000
    assert outputs.trace.name == "behavior-trace.json"
    assert outputs.examples.name == "examples.md"

    trace = json.loads(outputs.trace.read_text(encoding="utf-8"))
    examples = outputs.examples.read_text(encoding="utf-8")
    assert len(trace) == 30
    assert {"day", "mood", "phase", "energy", "warmth", "initiative"} <= trace[0].keys()
    assert "30-day behavioral emulation" in examples
    assert "Keep care intact" in examples

