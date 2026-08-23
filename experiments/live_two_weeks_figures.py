"""Fresh visuals from the live two-week run (results/live-two-weeks/companion.db).

Same four-panel layout as the README hero (experiments/month_showcase.py) plus a
week view, rebuilt against the REAL live run instead of the backfill sims:

  overview.png (4 panels)
    P1  mood trajectory vs 7-day smoothed expectation, cycle-phase background
    P2  proactive messages per day (bars colored by phase)
    P3  decision hook x fire-hour x that day's mood (fired intents)
    P4  contact heatmap — messages per day x hour
  week-view.png (3 panels)
    W1  week 1 day-x-hour message heatmap      W2  week 2 day-x-hour heatmap
    W3  proactive pipeline per day: fired vs suppressed intents

Single-run data (seed 6001): no ensemble band exists — P1 shows the smoothed
expectation line only. Run with MPLBACKEND=Agg.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "results" / "live-two-weeks" / "companion.db"
OUT_DIR = REPO / "results" / "live-two-weeks" / "figures"

PHASE_COLORS = {
    "menstrual": "#f2c4cd",
    "follicular": "#cdebc9",
    "ovulatory": "#fce8b0",
    "luteal_early": "#c9d9f2",
    "luteal_late": "#d8cdef",
}
REASON_COLORS = {"schedule": "#c9834a", "event": "#5f8a5f", "memory": "#4a7dbd"}
N_DAYS = 14


def load() -> dict:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    state = cur.execute(
        "SELECT day, M, g, phase_label FROM daily_state ORDER BY day"
    ).fetchall()
    msgs = cur.execute("SELECT day, t_h, role, proactive FROM messages").fetchall()
    intents = cur.execute(
        "SELECT reason, hook, created_t_h, status FROM proactive_intents"
    ).fetchall()
    n_convs = cur.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    con.close()
    return {"state": state, "msgs": msgs, "intents": intents, "n_convs": n_convs}


def classify(reason: str) -> str:
    r = reason.lower()
    if "finished" in r or "completed" in r or "event" in r.split(":")[0]:
        return "event"
    return "schedule"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load()
    state = [s for s in data["state"] if s[0] < N_DAYS]
    days = np.arange(len(state))
    M = np.array([float(s[1]) for s in state])
    g = np.array([float(s[2]) for s in state])
    phase = [s[3] for s in state]

    kernel = np.ones(7) / 7.0
    expected = np.convolve(np.pad(M, (3, 3), mode="edge"), kernel, mode="valid")

    pro_msgs = [(day, t_h) for day, t_h, _, pro in data["msgs"] if pro == 1]
    all_msgs = [(day, t_h) for day, t_h, _, _ in data["msgs"]]
    per_day_pro = Counter(int(d) for d, _ in pro_msgs)

    fired = [i for i in data["intents"] if i[3] == "fired"]
    suppressed = [i for i in data["intents"] if i[3] == "suppressed"]

    # ================= overview.png (README-hero equivalent) =================
    fig = plt.figure(figsize=(13.5, 9))
    gs = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.25)
    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[0, 1])
    ax3 = fig.add_subplot(gs[1, 0])
    ax4 = fig.add_subplot(gs[1, 1])

    fig.suptitle(
        f"Two-week LIVE run — real model companion (seed 6001, {len(all_msgs)} messages, "
        f"{len(fired)} proactive intents fired / {len(suppressed)} suppressed)",
        fontsize=13,
    )

    # ---- P1: mood --------------------------------------------------------
    ax = ax1
    for i, ph in enumerate(phase):
        ax.axvspan(i - 0.5, i + 0.5, color=PHASE_COLORS.get(ph, "#eee"), alpha=0.55, zorder=0)
    ax.plot(days, expected, "--", color="#2a5d9d", lw=1.6, label="expected (7-day)")
    ax.plot(days, M, "-o", color="#e07b39", lw=2.0, ms=4.5, label="sampled mood M")
    ax.set_title("Mood — stochastic draw vs expectation, by cycle phase",
                 fontsize=10.5, loc="left")
    ax.set_ylabel("mood (0–10)")
    ax.set_ylim(-0.6, 10.6)
    ax.set_xlabel("day")
    ax.legend(fontsize=8, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)

    # ---- P2: proactive per day -------------------------------------------
    ax = ax2
    counts = np.array([per_day_pro.get(int(d), 0) for d in days])
    ax.bar(days, counts,
           color=[PHASE_COLORS.get(p_, "#ccc") for p_ in phase],
           edgecolor="#777", lw=0.5)
    ax.set_title("Proactive messages per day (bar color = cycle phase)",
                 fontsize=10.5, loc="left")
    ax.set_ylabel("proactive messages")
    ax.set_xlabel("day")
    ax.set_xticks(days)
    ax.set_xticklabels([str(d + 1) for d in days], fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # ---- P3: decision reason x fire-hour x mood --------------------------
    ax = ax3
    mood_at = {int(s[0]): float(s[1]) for s in state}
    seen = set()
    annotated = 0
    for reason, hook, t_h, _status in fired:
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
        if annotated < 7:
            txt = (hook[:30] + "…") if len(hook) > 32 else hook
            dy = -12 - (annotated % 3) * 9
            ax.annotate(txt, (ihour, m), textcoords="offset points",
                        xytext=(6 + (annotated % 2) * 14, dy),
                        fontsize=6.2, color="#555",
                        arrowprops=dict(arrowstyle="-", lw=0.4, color="#999"))
            annotated += 1
    ax.axvspan(-0.5, 8, color="#555", alpha=0.12, zorder=0)
    ax.axvspan(23, 24.5, color="#555", alpha=0.12, zorder=0)
    ax.set_xlim(-0.5, 24.5)
    ax.set_xticks(range(0, 25, 2))
    ax.set_ylim(-0.6, 12.2)
    ax.set_yticks(range(0, 11, 2))
    ax.set_xlabel("hour of day at fire time (gray = quiet hours)")
    ax.set_ylabel("mood that day (0–10)")
    ax.set_title("Why it reached out — decision hook × time × that day's mood",
                 fontsize=10.5, loc="left")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)

    # ---- P4: contact heatmap day x hour ----------------------------------
    ax = ax4
    grid = np.zeros((N_DAYS, 24))
    for day, t_h, _role, _pro in data["msgs"]:
        d = int(day)
        hour = int(t_h % 24)
        if d < N_DAYS:
            grid[d][hour] += 1
    im = ax.imshow(grid, aspect="auto", cmap="viridis", origin="upper",
                   extent=[0, 24, N_DAYS - 0.5, -0.5])
    ax.set_yticks(np.arange(N_DAYS))
    ax.set_yticklabels([str(d + 1) for d in range(N_DAYS)], fontsize=8)
    ax.set_ylabel("run day")
    ax.set_xticks(range(0, 25, 2))
    ax.set_xlabel("hour of day")
    ax.set_title("When does conversation happen — messages per day × hour",
                 fontsize=10.5, loc="left")
    cbar = fig.colorbar(im, ax=ax, shrink=0.85)
    cbar.set_label("messages", fontsize=8)

    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out1 = OUT_DIR / "overview.png"
    fig.savefig(out1, dpi=150)
    print("saved", out1)

    # ================= week-view.png =================
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.6))
    fig.suptitle(
        "Two-week LIVE run — week view & proactive pipeline "
        f"({data['n_convs']} conversations closed)", fontsize=13,
    )
    for w in (0, 1):
        ax = axes[w]
        wk_grid = np.zeros((7, 24))
        for day, t_h, role, pro in data["msgs"]:
            d = int(day)
            if w * 7 <= d < (w + 1) * 7:
                wk_grid[d - w * 7][int(t_h % 24)] += 1
        im = ax.imshow(wk_grid, aspect="auto", cmap="viridis", origin="upper",
                       extent=[0, 24, 6.5, -0.5])
        ax.set_yticks(range(7))
        ax.set_yticklabels([f"day {w * 7 + i + 1}" for i in range(7)], fontsize=8)
        ax.set_xticks(range(0, 25, 4))
        ax.set_xlabel("hour of day")
        ax.set_title(f"Week {w + 1} — messages per day × hour", fontsize=10.5, loc="left")
        fig.colorbar(im, ax=ax, shrink=0.85).set_label("messages", fontsize=8)

    ax = axes[2]
    f_per_day = Counter(int(i[2] // 24) for i in fired)
    s_per_day = Counter(int(i[2] // 24) for i in suppressed)
    base = np.zeros(len(days))
    fired_c = np.array([f_per_day.get(int(d), 0) for d in days], dtype=float)
    supp_c = np.array([s_per_day.get(int(d), 0) for d in days], dtype=float)
    ax.bar(days, fired_c, bottom=base, color="#5f8a5f", edgecolor="#777",
           lw=0.5, label="fired")
    ax.bar(days, supp_c, bottom=fired_c, color="#b05a5a", edgecolor="#777",
           lw=0.5, label="suppressed (gate)")
    ax.set_title("Proactive pipeline per day — fired vs suppressed intents",
                 fontsize=10.5, loc="left")
    ax.set_ylabel("intents")
    ax.set_xlabel("day")
    ax.set_xticks(days)
    ax.set_xticklabels([str(d + 1) for d in days], fontsize=8)
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=[0, 0, 1, 0.92])
    out2 = OUT_DIR / "week-view.png"
    fig.savefig(out2, dpi=150)
    print("saved", out2)

    summary = {
        "db": str(DB.relative_to(REPO)),
        "days_finalized": len(state),
        "messages_total": len(all_msgs),
        "proactive_messages": len(pro_msgs),
        "conversations": data["n_convs"],
        "intents_fired": len(fired),
        "intents_suppressed": len(suppressed),
        "mood_sampled": M.tolist(),
        "phases": phase,
    }
    (OUT_DIR / "figures-summary.json").write_text(json.dumps(summary, indent=1))
    print("saved", OUT_DIR / "figures-summary.json")


if __name__ == "__main__":
    main()
