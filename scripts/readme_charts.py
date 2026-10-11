"""Render the two README charts from data already committed under results/.

    MPLBACKEND=Agg .venv/bin/python scripts/readme_charts.py

Writes docs/images/judge-calibration.png, docs/images/decision-probe.png and
docs/images/ablation.png. With a live run database at
results/live-companion/companion.db (not committed) it also writes
docs/images/live-cache.png, token totals per call lane only.
Every number is read from the run databases and probe records; nothing is
typed in by hand.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
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


#: What each model-call lane does, in plain words.
LANES = {
    "chat": "her messages",
    "tool_decide_event": "whether to write at an agenda boundary",
    "tool_decide_reply": "whether to reply now",
    "tool_decide_proactive": "whether to reach out",
    "tool_decide_call": "whether to pick up his call",
    "aux_day_planner": "planning her day",
    "aux_judge": "judging the day",
    "aux_memory": "memory summaries",
    "face_pick": "picking a facial expression",
}


def live_cache(db: Path) -> None:
    """Prompt tokens per model-call lane in the live run, cached vs fresh."""
    lanes = _rows(db, "SELECT role, count(*), sum(prompt_tokens), sum(cached_tokens) "
                      "FROM llm_calls WHERE prompt_tokens > 0 GROUP BY role "
                      "ORDER BY sum(prompt_tokens)")
    first, last = _rows(db, "SELECT min(called_at), max(called_at) FROM llm_calls")[0]
    total = sum(r[2] for r in lanes)
    cached_total = sum(r[3] or 0 for r in lanes)
    fig, ax = plt.subplots(figsize=(10, 4.3))
    ys = range(len(lanes))
    cached = [(r[3] or 0) / 1e6 for r in lanes]
    fresh = [(r[2] - (r[3] or 0)) / 1e6 for r in lanes]
    ax.barh(ys, cached, 0.66, color=AQUA, edgecolor=SURFACE, lw=1.5,
            label="served from the provider's cache")
    ax.barh(ys, fresh, 0.66, left=cached, color=GRAY, edgecolor=SURFACE, lw=1.5,
            label="processed fresh")
    for y, r in zip(ys, lanes):
        ax.text(r[2] / 1e6 + 0.02, y, f"{(r[3] or 0) / r[2]:.0%} cached, {r[1]} calls",
                va="center", fontsize=9, color=INK_2)
    ax.set_yticks(list(ys), [LANES.get(r[0], r[0]) for r in lanes], fontsize=9.5)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("prompt tokens (millions)")
    ax.set_xlim(0, max(r[2] for r in lanes) / 1e6 * 1.32)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    start = datetime.fromtimestamp(first)
    end = datetime.fromtimestamp(last)
    fig.suptitle(f"The live run: {cached_total / total:.0%} of {total / 1e6:.1f}M prompt "
                 "tokens were served from cache", x=0.01, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.005, f"Live run, {start:%-d} to {end:%-d %B %Y}, seed 8001, "
             "deepseek-v4.1-flash. Token totals per lane only. Data: "
             "results/live-companion/companion.db (not committed), llm_calls.",
             fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUT / "live-cache.png", dpi=160)
    plt.close(fig)


#: The 35-cell matrix (results/it2-g6-matrix): each condition removes one channel.
ABLATIONS = [
    ("NO_TIMING_FEEDBACK", "no timing feedback"),
    ("NO_ACTUATORS", "no behavior actuators"),
    ("NO_LIFE", "no life of her own"),
    ("STRUCTURED_NO_STATE", "no internal state"),
    ("RAW_HISTORY", "raw history, no memory"),
    ("SIMPLE_RAG", "plain retrieval memory"),
]
SEEDS = (5001, 5002, 5003, 5004, 5005)


def ablation() -> None:
    """Paired change against FULL, per seed, with a 95% t-interval of the mean."""
    from scipy import stats

    base = ROOT / "results" / "it2-g6-matrix"

    def counts(cond: str, seed: int) -> tuple[int, int, int]:
        db = next((base / cond / f"seed{seed}").glob("*.db"))
        (pro, total, his), = _rows(db, "SELECT sum(proactive = 1), count(*), "
                                       "sum(role = 'user') FROM messages")
        return pro, total, his

    full = {s: counts("FULL", s) for s in SEEDS}
    metrics = [("her unprompted messages", 0), ("all messages", 1),
               ("his messages delivered", 2)]
    fig, axes = plt.subplots(1, 3, figsize=(10, 4.2), sharey=True)
    ys = list(range(len(ABLATIONS)))[::-1]
    summary = {}
    for ax, (title, k) in zip(axes, metrics):
        ax.axvline(0, color=INK_2, lw=0.9)
        for y, (cond, _label) in zip(ys, ABLATIONS):
            diffs = [counts(cond, s)[k] - full[s][k] for s in SEEDS]
            mean = sum(diffs) / len(diffs)
            sd = (sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)) ** 0.5
            half = stats.t.ppf(0.975, len(diffs) - 1) * sd / len(diffs) ** 0.5
            summary[(cond, k)] = (diffs, mean, half)
            colour = ORANGE if cond == "NO_TIMING_FEEDBACK" else BLUE
            ax.plot([mean - half, mean + half], [y, y], color=colour, lw=2.2,
                    solid_capstyle="round", alpha=0.45)
            ax.scatter(diffs, [y + 0.22] * len(diffs), s=13, color=colour, alpha=0.8,
                       edgecolor="none")
            ax.scatter([mean], [y], s=48, color=colour, edgecolor=SURFACE, lw=1.2, zorder=3)
        ax.set_title(title, loc="left", fontsize=10.5, color=INK)
        ax.grid(axis="x", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", length=0)
        ax.set_xlabel("change vs full system, 30 days")
    axes[0].set_yticks(ys, [label for _c, label in ABLATIONS])
    for ax in axes[1:]:
        ax.spines["left"].set_visible(False)
    unchanged = sum(all(summary[(c, k)][0][i] == 0 for k in (0, 1))
                    for c, _l in ABLATIONS if c != "NO_TIMING_FEEDBACK"
                    for i in range(len(SEEDS)))
    fig.suptitle("Ablations: removing one channel at a time barely moved how much she "
                 "talked", x=0.01, y=0.985, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.885, f"Five ablations matched the full system in {unchanged} of 25 seed "
             "pairs. Removing timing feedback moved every seed, but in both directions,\n"
             "and most of those cells also received fewer of the scripted user's messages: "
             "every interval crosses zero.", fontsize=9.5, color=INK_2, va="top",
             linespacing=1.4)
    fig.text(0.01, 0.008, "Big dot: mean change over 5 seeds; bar: 95% t-interval; small dots: "
             "each seed. 30 simulated days per cell, deepseek-v4-flash. "
             "Data: results/it2-g6-matrix.", fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.04, 1, 0.84))
    fig.savefig(OUT / "ablation.png", dpi=160)
    plt.close(fig)
    for (cond, k), (diffs, mean, half) in summary.items():
        if cond == "NO_TIMING_FEEDBACK":
            print(f"{cond} metric {k}: diffs {diffs} mean {mean:+.1f} +/- {half:.1f}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    judge_calibration()
    decision_probe()
    ablation()
    live_db = ROOT / "results" / "live-companion" / "companion.db"
    if live_db.exists():
        live_cache(live_db)
