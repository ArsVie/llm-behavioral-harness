"""Render the two README charts from data already committed under results/.

    MPLBACKEND=Agg .venv/bin/python scripts/readme_charts.py

Writes docs/images/judge-calibration.png and docs/images/decision-probe.png.
Every number is read from the run databases and probe records; nothing is
typed in by hand.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
BLUE, ORANGE, AQUA, RED, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#e34948", "#a9a8a3"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.edgecolor": GRID, "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 10,
})


def _rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def judge_calibration() -> None:
    """Judge rubric v1 vs v2 on the live 3x2 ablation (harness-ON cells)."""
    runs = [("Rubric v1: companion perspective", "e2e-ablation-live"),
            ("Rubric v2: user-treatment anchor", "e2e-ablation-v2")]
    cells = [("perfect_on", "perfect month", BLUE),
             ("flat_on", "flat month", AQUA),
             ("horrible_on", "horrible month", ORANGE)]
    fig, axes = plt.subplots(2, 2, figsize=(10, 6.4), sharex=True, sharey="row")
    for col, (title, run) in enumerate(runs):
        for cell, label, color in cells:
            db = ROOT / "results" / run / f"cell_{cell}.db"
            judged = _rows(db, "SELECT day, score FROM judgements ORDER BY day")
            mood = _rows(db, "SELECT day, mu FROM daily_state ORDER BY day")
            axes[0, col].plot(*zip(*judged), color=color, lw=2, marker="o",
                              ms=6, mec=SURFACE, mew=1.5, label=label)
            axes[1, col].plot(*zip(*mood), color=color, lw=2, marker="o",
                              ms=6, mec=SURFACE, mew=1.5, label=label)
        axes[0, col].set_title(title, fontsize=11, loc="left", color=INK)
    for ax in axes.flat:
        ax.axhline(0, color=INK_2, lw=0.8)
        ax.grid(axis="y", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
    axes[0, 0].set_ylim(-1, 1)
    axes[0, 0].set_ylabel("judge score of the day\n(-1 .. +1)")
    axes[1, 0].set_ylabel("engine mood baseline μ\n(driven by the judge)")
    for ax in axes[1]:
        ax.set_xlabel("day of run")
    handles, names = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, names, frameon=False, ncol=3, fontsize=9.5,
               loc="upper right", bbox_to_anchor=(0.995, 0.975))
    fig.suptitle("The LLM judge closes the loop: a rubric fix separates the months",
                 x=0.01, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.005, "Live run, deepseek-v4-flash, seed 1001, 7 days per cell. "
             "Data: results/e2e-ablation-live and results/e2e-ablation-v2 "
             "(judgements, daily_state).", fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.03, 1, 0.93))
    fig.savefig(OUT / "judge-calibration.png", dpi=160)
    plt.close(fig)


def decision_probe() -> None:
    """Decision-protocol outcomes per transport over three real probe runs."""
    runs = [("run 1", "decision-probe-real-2026-08-14"),
            ("run 2", "decision-probe-real-2026-08-14-r2"),
            ("run 3", "decision-probe-real-2026-08-14-r3")]
    kinds = [("yes", "verdict: act", BLUE),
             ("no", "verdict: hold, with a reason", AQUA),
             ("no-empty", "verdict: hold, empty reason", GRAY),
             ("fail", "parse failure", RED)]
    labels, counts = [], []
    for name, run in runs:
        probe = json.loads((ROOT / "results" / run / "probe.json").read_text())
        tally: Counter[tuple[str, str]] = Counter()
        for ev in probe["evaluations"]:
            if ev["source"] != "model":
                continue  # seeded server draws are not model calls
            if ev["parse_failure"]:
                kind = "fail"
            elif ev["reply"]:
                kind = "yes"
            else:
                kind = "no" if (ev["reason"] or "").strip() else "no-empty"
            tally[(ev["transport"], kind)] += 1
        for transport in ("native", "textual"):
            labels.append(f"{name}  {transport}")
            counts.append([tally[(transport, k)] for k, *_ in kinds])
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ys = list(range(len(labels)))[::-1]
    for y, row in zip(ys, counts):
        left = 0
        for value, (_, _, color) in zip(row, kinds):
            if value:
                ax.barh(y, value, left=left, height=0.62, color=color,
                        edgecolor=SURFACE, linewidth=2)
                if value >= 4:
                    ax.text(left + value / 2, y, str(value), ha="center",
                            va="center", fontsize=9,
                            color="white" if color in (BLUE, RED) else INK)
                left += value
    for (_, text, color) in kinds:
        ax.barh(0, 0, color=color, label=text)
    ax.set_yticks(ys, labels)
    ax.set_xlim(0, 45)
    ax.set_xlabel("model calls (15 scenarios x 3 mood states = 45 per transport)")
    ax.legend(frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.2),
              fontsize=9)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.suptitle("Decision probe: run 1 returned no usable verdict on either transport",
                 x=0.01, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.01, "deepseek-v4-flash, seed 20260814. Data: "
             "results/decision-probe-real-2026-08-14{,-r2,-r3}/probe.json "
             "(server draws excluded).", fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUT / "decision-probe.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    judge_calibration()
    decision_probe()
