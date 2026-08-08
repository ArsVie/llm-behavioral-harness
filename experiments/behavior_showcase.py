"""Emulación ensemble de treinta días y consecuencias conductuales por fase."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine import circadian
from engine.types import DayRecord, MoodVariant, TimingParams
from harness.behavior import BehaviorDirective, derive_behavior
from sim.run_daily import run


PHASE_ORDER = [
    "menstrual",
    "follicular",
    "ovulatory",
    "luteal_early",
    "luteal_late",
]


@dataclass(frozen=True)
class EmulatedDay:
    day: int
    mood: int
    phase: str
    energy_morning: float
    energy_afternoon: float
    energy_evening: float
    behavior: BehaviorDirective


@dataclass(frozen=True)
class ShowcaseOutputs:
    graph: Path
    trace: Path
    examples: Path
    phase_graph: Path
    summary: Path


@dataclass
class EnsembleAnalysis:
    seeds: list[int]
    typical_seed: int
    mood_q10: np.ndarray
    mood_q50: np.ndarray
    mood_q90: np.ndarray
    expected_q50: np.ndarray
    phase_moods: dict[str, list[int]]
    phase_gains: dict[str, list[float]]
    phase_stats: dict[str, dict[str, float]]


def build_emulation(days: int = 30, seed: int = 3001) -> list[EmulatedDay]:
    """Compone motor, energía y actuadores para una conversación vespertina."""
    result = run(days=days, seed=seed, variant=MoodVariant.DECOUPLED_OFFSETS)
    timing = TimingParams()
    emulation: list[EmulatedDay] = []
    for index, record in enumerate(result.records):
        previous = result.records[index - 1] if index else None
        directive = derive_behavior(
            record,
            timing,
            hour=20.0,
            mood_scale=result.params.N,
            previous=previous,
        )
        emulation.append(
            EmulatedDay(
                day=record.t,
                mood=record.M,
                phase=record.phase_label,
                energy_morning=circadian.energy(9.0, record.phase_label, timing),
                energy_afternoon=circadian.energy(14.0, record.phase_label, timing),
                energy_evening=circadian.energy(20.0, record.phase_label, timing),
                behavior=directive,
            )
        )
    return emulation


def _analyze_ensemble(days: int, seeds: Iterable[int]) -> EnsembleAnalysis:
    seed_list = list(seeds)
    if not seed_list:
        raise ValueError("ensemble_seeds no puede estar vacío")

    results = [run(days, seed, MoodVariant.DECOUPLED_OFFSETS) for seed in seed_list]
    moods = np.stack([result.M for result in results])
    expected = np.stack([result.params.N * result.p for result in results])

    means = np.mean(moods, axis=1)
    low_days = np.sum(moods < 5, axis=1)
    median_curve = np.median(moods, axis=0)
    curve_rmse = np.sqrt(np.mean((moods - median_curve) ** 2, axis=1))
    features = np.column_stack((means, low_days, curve_rmse))
    target = np.median(features, axis=0)
    scale = np.std(features, axis=0)
    scale[scale == 0.0] = 1.0
    distance = np.sum(((features - target) / scale) ** 2, axis=1)
    saturated_days = np.sum((moods == 0) | (moods == 10), axis=1)
    distance += 0.5 * saturated_days
    typical_index = int(np.argmin(distance))

    phase_moods: dict[str, list[int]] = defaultdict(list)
    phase_gains: dict[str, list[float]] = defaultdict(list)
    for result in results:
        for record in result.records:
            phase_moods[record.phase_label].append(record.M)
            phase_gains[record.phase_label].append(record.g)

    timing = TimingParams()
    hours = np.linspace(0.0, 24.0, 97, endpoint=False)
    phase_stats: dict[str, dict[str, float]] = {}
    for phase in PHASE_ORDER:
        mood_values = np.asarray(phase_moods[phase], dtype=float)
        energy_values = np.asarray([circadian.energy(h, phase, timing) for h in hours])
        phase_stats[phase] = {
            "mood_mean": float(np.mean(mood_values)),
            "mood_sd": float(np.std(mood_values)),
            "mood_below_five_rate": float(np.mean(mood_values < 5.0)),
            "energy_mean": float(np.mean(energy_values)),
            "energy_range": float(np.ptp(energy_values)),
            "reactivity_mean": float(np.mean(phase_gains[phase])),
        }

    return EnsembleAnalysis(
        seeds=seed_list,
        typical_seed=seed_list[typical_index],
        mood_q10=np.quantile(moods, 0.10, axis=0),
        mood_q50=np.quantile(moods, 0.50, axis=0),
        mood_q90=np.quantile(moods, 0.90, axis=0),
        expected_q50=np.quantile(expected, 0.50, axis=0),
        phase_moods=dict(phase_moods),
        phase_gains=dict(phase_gains),
        phase_stats=phase_stats,
    )


def _phase_spans(axis: plt.Axes, emulation: list[EmulatedDay]) -> None:
    colors = {
        "menstrual": "C3",
        "follicular": "C2",
        "ovulatory": "C1",
        "luteal_early": "C4",
        "luteal_late": "C5",
    }
    start = 0
    for index in range(1, len(emulation) + 1):
        if index == len(emulation) or emulation[index].phase != emulation[start].phase:
            axis.axvspan(start - 0.5, index - 0.5, color=colors[emulation[start].phase], alpha=0.055)
            start = index


def _write_graph(
    emulation: list[EmulatedDay],
    representative: list[DayRecord],
    analysis: EnsembleAnalysis,
    path: Path,
) -> None:
    days = np.arange(len(emulation))
    mood = np.asarray([day.mood for day in emulation])
    expected = np.asarray([10.0 * record.p for record in representative])
    fig, axes = plt.subplots(4, 1, figsize=(11, 12), sharex=True, dpi=125)

    axes[0].fill_between(days, analysis.mood_q10, analysis.mood_q90, color="C0", alpha=0.17, label="ensemble p10–p90")
    axes[0].plot(days, analysis.mood_q50, "--", color="C0", label="ensemble median")
    axes[0].plot(days, expected, color="C2", linewidth=1.8, label="expected N·p")
    axes[0].plot(days, mood, "o-", color="C1", markersize=3.5, label="sampled M")
    axes[0].axhline(5.0, color="gray", linewidth=0.8)
    axes[0].set_ylabel("mood 0–10")
    axes[0].set_ylim(-0.5, 10.5)
    axes[0].legend(loc="upper right", ncol=4, fontsize=8)

    axes[1].plot(days, [day.energy_morning for day in emulation], label="09:00", color="C2")
    axes[1].plot(days, [day.energy_afternoon for day in emulation], label="14:00", color="C3")
    axes[1].plot(days, [day.energy_evening for day in emulation], label="20:00", color="C4")
    axes[1].set_ylabel("energy")
    axes[1].set_ylim(0.0, 1.0)
    axes[1].legend(loc="upper right", ncol=3, fontsize=8)

    axes[2].plot(days, [day.behavior.warmth for day in emulation], label="warmth", color="C5")
    axes[2].plot(days, [day.behavior.playfulness for day in emulation], label="playfulness", color="C6")
    axes[2].plot(days, [day.behavior.reflectiveness for day in emulation], label="reflectiveness", color="C7")
    axes[2].set_ylabel("tone controls")
    axes[2].set_ylim(0.0, 1.0)
    axes[2].legend(loc="upper right", ncol=3, fontsize=8)

    axes[3].plot(days, [day.behavior.reactivity for day in emulation], label="reactivity", color="C3")
    axes[3].plot(days, [day.behavior.initiative for day in emulation], label="initiative", color="C8")
    axes[3].plot(days, [day.behavior.closing_tendency for day in emulation], label="closing", color="C9")
    axes[3].set_ylabel("behavior controls")
    axes[3].set_xlabel("day")
    axes[3].set_ylim(0.0, 1.0)
    axes[3].legend(loc="upper right", ncol=3, fontsize=8)

    for axis in axes:
        _phase_spans(axis, emulation)
        axis.grid(True, alpha=0.22)
    fig.suptitle(
        f"Behavioral harness · 30 days · representative seed {representative[0].seed} · ensemble n={len(analysis.seeds)}"
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.97))
    fig.savefig(path)
    plt.close(fig)


def _write_phase_graph(analysis: EnsembleAnalysis, path: Path) -> None:
    timing = TimingParams()
    hours = np.linspace(0.0, 24.0, 97, endpoint=False)
    labels = [phase.replace("_", " ") for phase in PHASE_ORDER]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), dpi=125)

    axes[0].boxplot(
        [analysis.phase_moods[phase] for phase in PHASE_ORDER],
        tick_labels=labels,
        showfliers=False,
        showmeans=True,
    )
    axes[0].axhline(5.0, color="gray", linewidth=0.8)
    axes[0].set_ylabel("sampled mood")
    axes[0].set_title("Mood distribution")
    axes[0].tick_params(axis="x", rotation=24)

    for phase, color in [("menstrual", "C3"), ("ovulatory", "C1")]:
        values = [circadian.energy(hour, phase, timing) for hour in hours]
        axes[1].plot(hours, values, color=color, linewidth=2.2, label=phase)
    axes[1].set_xlabel("local hour")
    axes[1].set_ylabel("energy")
    axes[1].set_ylim(0.0, 1.0)
    axes[1].set_title("Energy is a separate channel")
    axes[1].legend()

    axes[2].boxplot(
        [analysis.phase_gains[phase] for phase in PHASE_ORDER],
        tick_labels=labels,
        showfliers=False,
        showmeans=True,
    )
    axes[2].axhline(1.0, color="gray", linewidth=0.8)
    axes[2].set_ylabel("reactivity gain g")
    axes[2].set_title("Menstrual reactive · ovulatory steady")
    axes[2].tick_params(axis="x", rotation=24)

    for axis in axes:
        axis.grid(True, alpha=0.22)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _trace_row(day: EmulatedDay) -> dict[str, object]:
    return {
        "day": day.day,
        "mood": day.mood,
        "phase": day.phase,
        "energy": day.behavior.energy,
        "warmth": day.behavior.warmth,
        "playfulness": day.behavior.playfulness,
        "reflectiveness": day.behavior.reflectiveness,
        "initiative": day.behavior.initiative,
        "response_length_scale": day.behavior.response_length_scale,
        "response_delay_s": day.behavior.response_delay_s,
        "trace": asdict(day.behavior.trace),
        "prompt_brief": day.behavior.prompt_brief,
    }


def _example_days(emulation: list[EmulatedDay]) -> list[EmulatedDay]:
    candidates = [
        min(emulation, key=lambda day: day.mood),
        max(emulation, key=lambda day: day.mood),
        max(emulation, key=lambda day: day.behavior.momentum),
        min(emulation, key=lambda day: day.behavior.momentum),
    ]
    return sorted({day.day: day for day in candidates}.values(), key=lambda day: day.day)


def _write_examples(
    emulation: list[EmulatedDay],
    representative: list[DayRecord],
    analysis: EnsembleAnalysis,
    path: Path,
) -> None:
    stats = analysis.phase_stats
    residual_index = int(np.argmax(np.abs(np.asarray([r.M - 10.0 * r.p for r in representative]))))
    residual = representative[residual_index]
    ovulatory = next(record for record in representative if record.phase_label == "ovulatory")
    timing = TimingParams()
    overnight = derive_behavior(ovulatory, timing, hour=2.0)
    daytime = derive_behavior(ovulatory, timing, hour=14.0)

    lines = [
        "# 30-day behavioral emulation",
        "",
        f"Representative seed: `{representative[0].seed}` selected from ensemble n={len(analysis.seeds)}.",
        "",
        "## Phase implications",
        "",
        f"- Menstrual: mood mean `{stats['menstrual']['mood_mean']:.2f}`, sd `{stats['menstrual']['mood_sd']:.2f}`, energy mean `{stats['menstrual']['energy_mean']:.2f}`, energy range `{stats['menstrual']['energy_range']:.2f}`.",
        f"- Ovulatory: mood mean `{stats['ovulatory']['mood_mean']:.2f}`, sd `{stats['ovulatory']['mood_sd']:.2f}`, energy mean `{stats['ovulatory']['energy_mean']:.2f}`, energy range `{stats['ovulatory']['energy_range']:.2f}`.",
        "",
        "## Expected versus sampled mood",
        "",
        f"Day {residual.t + 1}: expected `{10.0 * residual.p:.2f}`, sampled `{residual.M}`. The sampled score is an observation, not the whole emotional state.",
        "",
        "## Same mood, different energy",
        "",
        f"The same ovulatory mood `{ovulatory.M}/10` has energy `{overnight.energy:.2f}` overnight and `{daytime.energy:.2f}` in the afternoon; valence remains `{daytime.valence:.2f}` in both cases.",
        "",
        f"**Overnight:** {overnight.prompt_brief}",
        "",
        f"**Afternoon:** {daytime.prompt_brief}",
        "",
        "## Representative days",
        "",
    ]
    for day in _example_days(emulation):
        lines.extend(
            (
                f"### Day {day.day + 1}",
                "",
                f"Mood `{day.mood}/10`; phase `{day.phase}`; evening energy `{day.behavior.energy:.2f}`.",
                "",
                day.behavior.prompt_brief,
                "",
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_showcase(
    out_dir: Path,
    days: int = 30,
    seed: int | None = None,
    ensemble_seeds: Iterable[int] = range(30),
) -> ShowcaseOutputs:
    """Escribe evidencia ensemble, fase, traza y ejemplos reproducibles."""
    out_dir.mkdir(parents=True, exist_ok=True)
    analysis = _analyze_ensemble(days, ensemble_seeds)
    representative_seed = analysis.typical_seed if seed is None else seed
    representative_result = run(days, representative_seed, MoodVariant.DECOUPLED_OFFSETS)
    emulation = build_emulation(days=days, seed=representative_seed)

    graph = out_dir / "30-day-behavior.png"
    phase_graph = out_dir / "phase-semantics.png"
    trace = out_dir / "behavior-trace.json"
    summary = out_dir / "phase-summary.json"
    examples = out_dir / "examples.md"

    _write_graph(emulation, representative_result.records, analysis, graph)
    _write_phase_graph(analysis, phase_graph)
    trace.write_text(
        json.dumps([_trace_row(day) for day in emulation], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    summary.write_text(
        json.dumps(
            {
                "ensemble_seeds": analysis.seeds,
                "representative_seed": representative_seed,
                "phases": analysis.phase_stats,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_examples(emulation, representative_result.records, analysis, examples)
    return ShowcaseOutputs(
        graph=graph,
        trace=trace,
        examples=examples,
        phase_graph=phase_graph,
        summary=summary,
    )


def main() -> int:
    out_dir = Path(__file__).resolve().parent.parent / "results" / "behavior-showcase"
    outputs = write_showcase(out_dir)
    for output in (outputs.graph, outputs.phase_graph, outputs.trace, outputs.summary, outputs.examples):
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

