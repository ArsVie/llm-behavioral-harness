"""README hero: 30-day engine view in the spirit of Ars's chat-analysis figures.

Four panels, one representative FULL simulation (seed 5001) pooled with the
4 sibling seeds where noted:
  P1  mood trajectory vs smoothed expectation + 5-seed ensemble band,
      cycle-phase colored background
  P2  proactive messages per day (bars colored by phase)
  P3  decision reason (agenda / life event / memory hook) plotted at fire
      hour vs the mood of that day — "why it reached out"
  P4  hour-of-day x weekday heatmap of ALL messages — "when does contact
      happen" (the circadian signature)

All data from real engine runs stored in results/it3-backfill-2026-08-09/dbs/FULL.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BASE = Path("results/it3-backfill-2026-08-09/dbs/FULL")
SEEDS = [5001, 5002, 5003, 5004, 5005]
REPRESENTATIVE = 5001

PHASE_COLORS = {
    "menstrual": "#f2c4cd",
    "follicular": "#cdebc9",
    "ovulatory": "#fce8b0",
    "luteal_early": "#c9d9f2",
    "luteal_late": "#d8cdef",
}
REASON_COLORS = {"schedule": "#c9834a", "event": "#5f8a5f", "memory": "#4a7dbd"}


def load(seed: int) -> dict:
    con = sqlite3.connect(BASE / f"seed{seed}.db")
    cur = con.cursor()
    state = cur.execute(
        "SELECT day, M, phase_label, score FROM daily_state ORDER BY day"
    ).fetchall()
    msgs = cur.execute("SELECT day, t_h, role, proactive FROM messages").fetchall()
    intents = cur.execute(
        "SELECT reason, hook, created_t_h, status FROM proactive_intents WHERE status='fired'"
    ).fetchall()
    con.close()
    return {"state": state, "msgs": msgs, "intents": intents}


def classify(reason: str) -> str:
    r = reason.lower()
    if "finished" in r or "completed" in r or "event" in r.split(":")[0]:
        return "event"
    return "schedule"


def main() -> None:
    data = {s: load(s) for s in SEEDS}
    rep = data[REPRESENTATIVE]

    state = rep["state"]
    days30 = np.arange(len(state))
    M = np.array([float(s[1]) for s in state])
    phase = [s[2] for s in state]

    # 7-day centred moving average (smoothed expectation)
    kernel = np.ones(7) / 7.0
    expected = np.convolve(np.pad(M, (3, 3), mode="edge"), kernel, mode="valid")

    ens_M = np.stack([np.array([float(x[1]) for x in data[s]["state"]]) for s in SEEDS])
    q10, q90 = np.quantile(ens_M, 0.10, axis=0), np.quantile(ens_M, 0.90, axis=0)

    pro_msgs = [(day, t_h) for day, t_h, role, pro in rep["msgs"] if pro == 1]
    per_day = Counter(int(day) for day, _ in pro_msgs)

    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9))
    fig.suptitle(
        f"One 30-day run of the behavioral engine (seed {REPRESENTATIVE}, "
        f"{len(SEEDS)}-seed ensemble band, {len(pro_msgs)} proactive messages)",
        fontsize=13,
    )

    # P1: mood
    ax = axes[0][0]
    for i, ph in enumerate(phase):
        ax.axvspan(i - 0.5, i + 0.5, color=PHASE_COLORS.get(ph, "#eee"), alpha=0.55, zorder=0)
    ax.fill_between(days30, q10[: len(days30)], q90[: len(days30)],
                    color="#4a7dbd", alpha=0.18, label="ensemble p10–p90")
    ax.plot(days30, expected, "--", color="#2a5d9d", lw=1.6, label="expected (7-day)")
    ax.plot(days30, M, "-o", color="#e07b39", lw=2.0, ms=4.5, label="sampled mood M")
    ax.set_title("Mood — stochastic draw vs expectation, by cycle phase",
                 fontsize=10.5, loc="left")
    ax.set_ylabel("mood (0–10)")
    ax.set_ylim(-0.6, 10.6)
    ax.set_xlabel("day")
    ax.legend(fontsize=8, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)

    # P2: proactive per day
    ax = axes[0][1]
    counts = np.array([per_day.get(int(d), 0) for d in days30])
    ax.bar(days30, counts,
           color=[PHASE_COLORS.get(p_, "#ccc") for p_ in phase],
           edgecolor="#777", lw=0.5)
    ax.set_title("Proactive messages per day (bar color = cycle phase)",
                 fontsize=10.5, loc="left")
    ax.set_ylabel("proactive messages")
    ax.set_xlabel("day")
    ax.set_xticks(days30[::4])
    ax.set_xticklabels([str(d + 1) for d in days30[::4]], fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # P3: decision reason x fire-hour x mood
    ax = axes[1][0]
    mood_at = {int(s[0]): float(s[1]) for s in state}
    seen = set()
    for reason, hook, t_h, status in rep["intents"]:
        kind = classify(reason)
        iday = int(t_h // 24)
        ihour = float(t_h % 24)
        m = mood_at.get(iday, 5.0)
        label = {"schedule": "agenda/schedule hook",
                 "event": "life-event hook"}[kind]
        ax.scatter(ihour, m, s=70, color=REASON_COLORS[kind],
                   alpha=0.85, edgecolor="white", lw=0.6,
                   label=label if kind not in seen else None)
        seen.add(kind)
    # quiet hours (23-24 and 0-8)
    ax.axvspan(-0.5, 8, color="#555", alpha=0.12, zorder=0)
    ax.axvspan(23, 24.5, color="#555", alpha=0.12, zorder=0)
    ax.set_xlim(-0.5, 24.5)
    ax.set_xticks(range(0, 25, 2))
    ax.set_ylim(-0.6, 10.6)
    ax.set_yticks(range(0, 11, 2))
    ax.set_xlabel("hour of day at fire time (gray = quiet hours)")
    ax.set_ylabel("mood that day (0–10)")
    ax.set_title("Why it reached out — decision hook × time × that day's mood",
                 fontsize=10.5, loc="left")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)

    # P4: heatmap hour x day-of-sim-week
    ax = axes[1][1]
    grid = np.zeros((5, 24))  # 5 "weeks" of the 30-day run
    for day, t_h, role, pro in rep["msgs"]:
        week = int(day // 7)
        hour = int(t_h % 24)
        if week < 5:
            grid[week][hour] += 1
    im = ax.imshow(grid, aspect="auto", cmap="viridis", origin="upper")
    ax.set_yticks(range(5))
    ax.set_yticklabels([f"week {i+1}" for i in range(5)], fontsize=8)
    ax.set_xticks(range(0, 25, 2))
    ax.set_xlabel("hour of day")
    ax.set_title("When does conversation happen — all messages per week × hour",
                 fontsize=10.5, loc="left")
    cbar = fig.colorbar(im, ax=ax, shrink=0.85)
    cbar.set_label("messages", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out = Path("results/one-month-showcase.png")
    fig.savefig(out, dpi=150)
    print("saved", out.resolve())


if __name__ == "__main__":
    main()
