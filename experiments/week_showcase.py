"""README showcase figure: one representative week of the full engine.

Composites, for a single seed (7 days):
  1. Mood M with expected value N*p + ensemble p10-p90 band
  2. Circadian energy at morning/afternoon/evening + phase band
  3. Behavior directives derived from state (warmth, expressiveness,
     initiative)
  4. Proactive contact events (raster) + hourly contact rate vs
     circadian envelope

Deterministic: fixed seed, uses sim.run_daily + sim.run_events +
harness.behavior.derive_behavior — the real engine code paths, no
re-implementation.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine import circadian
from engine.types import MoodVariant, TimingParams
from harness.behavior import derive_behavior
from sim import run_events
from sim.run_daily import run

DAYS = 7
SEED = 3001
ENSEMBLE_SEEDS = list(range(3001, 3031))

PHASE_COLORS = {
    "menstrual": "#f2c4cd",
    "follicular": "#cdebc9",
    "ovulatory": "#fce8b0",
    "luteal_early": "#c9d9f2",
    "luteal_late": "#d8cdef",
}


def main() -> None:
    timing = TimingParams()

    # --- daily engine: representative seed -------------------------------
    result = run(days=DAYS, seed=SEED, variant=MoodVariant.DECOUPLED_OFFSETS)

    # --- ensemble band ----------------------------------------------------
    ens = [
        run(days=DAYS, seed=s, variant=MoodVariant.DECOUPLED_OFFSETS)
        for s in ENSEMBLE_SEEDS
    ]
    moods = np.stack([r.M for r in ens])
    q10, q90 = np.quantile(moods, 0.10, axis=0), np.quantile(moods, 0.90, axis=0)

    days = np.arange(DAYS)
    expected = result.params.N * result.p

    # --- behavior directives ---------------------------------------------
    warmth, expressive, initiative = [], [], []
    for i, rec in enumerate(result.records):
        prev = result.records[i - 1] if i else None
        d = derive_behavior(rec, timing, hour=20.0, mood_scale=result.params.N, previous=prev)
        warmth.append(d.warmth)
        expressive.append(d.expressiveness)
        initiative.append(d.initiative)

    # --- proactive events --------------------------------------------------
    events_h = run_events.run(
        days=DAYS,
        seed=SEED,
        scores=np.asarray(result.score, dtype=float),
    )
    event_day_frac = (events_h % 168.0) / 24.0  # within-week position

    # hourly rate from the circadian envelope (the deterministic part of h)
    hours = np.linspace(0, 24, 200, endpoint=False)
    envelope = np.array([circadian.envelope(h, timing) for h in hours])

    # ---------------------------------------------------------------- plot
    fig, axes = plt.subplots(4, 1, figsize=(11.5, 12.5), sharex=False)
    fig.suptitle(
        f"LLM Behavioral Harness — one simulated week (seed {SEED}, "
        f"{len(ENSEMBLE_SEEDS)}-seed ensemble, {len(events_h)} proactive contacts)",
        fontsize=13,
    )

    def phase_band(ax):
        for i, rec in enumerate(result.records):
            ax.axvspan(i - 0.5, i + 0.5, color=PHASE_COLORS.get(rec.phase_label, "#eee"), alpha=0.55, zorder=0)

    # 1 — mood
    ax = axes[0]
    phase_band(ax)
    ax.fill_between(days, q10, q90, color="#4a7dbd", alpha=0.18, label="ensemble p10–p90")
    ax.plot(days, expected, "--", color="#2a5d9d", lw=1.6, label="expected N·p (latent)")
    ax.plot(days, result.M, "-o", color="#e07b39", lw=2.2, ms=6, label="sampled mood M")
    ax.axhline(result.params.N / 2, color="gray", lw=0.8, alpha=0.6)
    ax.set_ylabel("mood (0–10)")
    ax.set_ylim(-0.5, 10.5)
    ax.set_title("Mood: frozen stochastic draw vs latent expected value", fontsize=10, loc="left")
    ax.legend(fontsize=8, ncol=3, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)

    # 2 — energy
    ax = axes[1]
    e_m = [circadian.energy(9.0, r.phase_label, timing) for r in result.records]
    e_a = [circadian.energy(14.0, r.phase_label, timing) for r in result.records]
    e_e = [circadian.energy(20.0, r.phase_label, timing) for r in result.records]
    phase_band(ax)
    ax.plot(days, e_m, "-o", ms=4, color="#7aa874", label="09:00")
    ax.plot(days, e_a, "-o", ms=4, color="#c9834a", label="14:00")
    ax.plot(days, e_e, "-o", ms=4, color="#5d6fa8", label="20:00")
    ax.set_ylabel("energy (0–1)")
    ax.set_ylim(0, 1.05)
    ax.set_title("Circadian energy by time of day × cycle phase", fontsize=10, loc="left")
    ax.legend(fontsize=8, ncol=3, loc="upper right")
    ax.spines[["top", "right"]].set_visible(False)

    # 3 — behavior directives
    ax = axes[2]
    phase_band(ax)
    ax.plot(days, warmth, "-o", ms=4, color="#a85c5c", label="warmth")
    ax.plot(days, expressive, "-o", ms=4, color="#b8863b", label="expressiveness")
    ax.plot(days, initiative, "-o", ms=4, color="#5f8a5f", label="initiative")
    ax.set_ylabel("directive strength (0–1)")
    ax.set_ylim(0, 1.05)
    ax.set_title("Behavior directives rendered to the model (never raw numbers)", fontsize=10, loc="left")
    ax.legend(fontsize=8, ncol=3, loc="upper right")
    ax.spines[["top", "right"]].set_visible(False)

    # 4 — proactive contacts
    ax = axes[3]
    phase_band(ax)
    # quiet hours shading (23:00-08:00) — no contacts may land there
    ax.axvspan(0, 8, color="#555", alpha=0.15, zorder=1)
    ax.axvspan(23, 24, color="#555", alpha=0.15, zorder=1)
    # raster: one tick per accepted event
    hour_of = events_h % 24.0
    day_of = np.floor(events_h / 24.0).astype(int)
    rng_j = np.random.default_rng(7)
    jitter = rng_j.uniform(-0.25, 0.25, size=len(events_h))
    ax.scatter(hour_of, day_of + 0.5 + jitter, marker="|", s=260, color="#222",
               zorder=4, linewidths=2.5)
    ax.set_yticks(np.arange(DAYS) + 0.5)
    ax.set_yticklabels([f"day {i+1}" for i in range(DAYS)])
    ax.set_xlim(0, 24)
    ax.set_ylim(0, DAYS)
    ax.invert_yaxis()
    ax.set_xticks(range(0, 25, 2), minor=False)
    ax.set_xticks(range(0, 25, 1), minor=True)
    ax.set_xlabel("hour of day (gray = quiet hours, no contact allowed)")
    ax.set_title(f"Proactive contact times — Weibull hazard × circadian envelope × phase multiplier × score adjustment ({len(events_h)} events)",
                 fontsize=10, loc="left")
    ax.grid(axis="x", which="major", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)

    fig.align_ylabels(axes)
    fig.tight_layout(rect=[0, 0, 1, 0.97])

    out = Path("results/one-week-showcase.png")
    fig.savefig(out, dpi=150)
    print("saved", out.resolve())

    # machine-readable companion
    summary = {
        "seed": SEED,
        "ensemble_seeds": len(ENSEMBLE_SEEDS),
        "days": DAYS,
        "mood_sampled": result.M.tolist(),
        "mood_expected": np.round(expected, 3).tolist(),
        "phases": [r.phase_label for r in result.records],
        "proactive_events_hour_of_week": [round(float(t), 2) for t in events_h],
        "n_proactive": int(len(events_h)),
        "directives_day6_20h": {"warmth": round(warmth[-1], 3),
                                 "expressiveness": round(expressive[-1], 3),
                                 "initiative": round(initiative[-1], 3)},
    }
    spath = Path("results/one-week-showcase.json")
    spath.write_text(json.dumps(summary, indent=1))
    print("saved", spath.resolve())


if __name__ == "__main__":
    main()
