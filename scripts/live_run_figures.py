"""Day-by-day figures for the live run (results/live-companion/companion.db).

Same panels as the August two-week figures, rebuilt for the real-time
run that started 3 October 2026:

  docs/images/live-overview.png (4 panels)
    P1  sampled mood per day against its 3-day average, cycle phase shaded
    P2  her reach-outs per day (bars colored by phase)
    P3  fired intents: hour of day x that day's mood, by hook kind
    P4  messages per day x hour (his and hers)
  docs/images/live-week.png (3 panels)
    W1  his messages per day x hour     W2  her messages per day x hour
    W3  proactive pipeline per day: fired, suppressed, still open

Only counts, hours and the sampled mood are plotted. Hook text is never drawn:
in a live run it quotes real plans. The database stays local; run this
with it present (MPLBACKEND=Agg is set below).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "results" / "live-companion" / "companion.db"
OUT_DIR = REPO / "docs" / "images"

PHASE_COLORS = {
    "menstrual": "#f2c4cd",
    "follicular": "#cdebc9",
    "ovulatory": "#fce8b0",
    "luteal_early": "#c9d9f2",
    "luteal_late": "#d8cdef",
}
HOOK_COLORS = {"agenda": "#c9834a", "finished": "#5f8a5f", "callback": "#4a7dbd"}
HOOK_LABELS = {"agenda": "agenda item coming up or on now",
               "finished": "something she just finished",
               "callback": "callback to an earlier plan"}


def load(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    state = cur.execute(
        "SELECT day, M, phase_label FROM daily_state ORDER BY day").fetchall()
    msgs = cur.execute(
        "SELECT day, t_h, role, proactive, sent_at FROM messages "
        "WHERE role IN ('user', 'assistant')").fetchall()
    intents = cur.execute(
        "SELECT hook, created_t_h, status FROM proactive_intents").fetchall()
    n_convs = cur.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    con.close()
    return {"state": state, "msgs": msgs, "intents": intents, "n_convs": n_convs}


def hook_kind(hook: str) -> str:
    head = hook.split(":", 1)[0].strip().lower()
    if head == "finished":
        return "finished"
    if head == "callback":
        return "callback"
    return "agenda"


def heatmap(ax, fig, rows, n_days, day_labels, title):
    grid = np.zeros((n_days, 24))
    for day, t_h in rows:
        if 0 <= int(day) < n_days:
            grid[int(day)][int(t_h % 24)] += 1
    im = ax.imshow(grid, aspect="auto", cmap="viridis", origin="upper",
                   extent=[0, 24, n_days - 0.5, -0.5])
    ax.set_yticks(np.arange(n_days))
    ax.set_yticklabels(day_labels, fontsize=8)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xlabel("hour of day")
    ax.set_title(title, fontsize=10.5, loc="left")
    fig.colorbar(im, ax=ax, shrink=0.85).set_label("messages", fontsize=8)


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DB
    if not db.exists():
        sys.exit(f"no run database at {db}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load(db)

    state = data["state"]
    n_days = len(state)
    days = np.arange(n_days)
    M = np.array([float(s[1]) for s in state])
    phase = [s[2] for s in state]
    kernel = np.ones(3) / 3.0
    smoothed = np.convolve(np.pad(M, (1, 1), mode="edge"), kernel, mode="valid")

    first_sent = {}
    for day, _t, _r, _p, sent_at in data["msgs"]:
        if sent_at is not None:
            first_sent[int(day)] = min(first_sent.get(int(day), sent_at), sent_at)
    day_labels = [datetime.fromtimestamp(first_sent[d]).strftime("%a %d")
                  if d in first_sent else f"day {d + 1}" for d in range(n_days)]
    span = f"{day_labels[0]} to {day_labels[-1]}"
    year_month = (datetime.fromtimestamp(max(first_sent.values())).strftime("%B %Y")
                  if first_sent else "")

    his = [(d, t) for d, t, role, _p, _s in data["msgs"] if role == "user"]
    hers = [(d, t) for d, t, role, _p, _s in data["msgs"] if role == "assistant"]
    reach = Counter(int(d) for d, _t, role, pro, _s in data["msgs"]
                    if role == "assistant" and pro == 1)
    fired = [i for i in data["intents"] if i[2] == "fired"]
    suppressed = [i for i in data["intents"] if i[2] == "suppressed"]
    still_open = [i for i in data["intents"] if i[2] not in ("fired", "suppressed")]

    # live-overview.png
    fig = plt.figure(figsize=(13.5, 9))
    gs = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.25)
    ax1, ax2 = fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1])
    ax3, ax4 = fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])
    fig.suptitle(
        f"Live run, {span} {year_month}: {len(his) + len(hers)} messages, "
        f"{sum(reach.values())} of hers unprompted, "
        f"{len(fired)} intents fired and {len(suppressed)} held back",
        fontsize=13)

    ax = ax1
    for i, ph in enumerate(phase):
        ax.axvspan(i - 0.5, i + 0.5, color=PHASE_COLORS.get(ph, "#eee"),
                   alpha=0.55, zorder=0)
    ax.plot(days, smoothed, "--", color="#2a5d9d", lw=1.6, label="3-day average")
    ax.plot(days, M, "-o", color="#e07b39", lw=2.0, ms=4.5, label="sampled mood")
    ax.set_title("Mood per day, by cycle phase", fontsize=10.5, loc="left")
    ax.set_ylabel("mood (0 to 10)")
    ax.set_ylim(-0.6, 10.6)
    ax.set_xticks(days)
    ax.set_xticklabels(day_labels, fontsize=8)
    ax.legend(fontsize=8, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)

    ax = ax2
    ax.bar(days, [reach.get(int(d), 0) for d in days],
           color=[PHASE_COLORS.get(p, "#ccc") for p in phase],
           edgecolor="#777", lw=0.5)
    ax.set_title("Her unprompted messages per day (color = cycle phase)",
                 fontsize=10.5, loc="left")
    ax.set_ylabel("messages")
    ax.set_xticks(days)
    ax.set_xticklabels(day_labels, fontsize=8)
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.spines[["top", "right"]].set_visible(False)

    ax = ax3
    mood_at = {int(s[0]): float(s[1]) for s in state}
    seen = set()
    for hook, t_h, _status in fired:
        kind = hook_kind(hook)
        ax.scatter(float(t_h % 24), mood_at.get(int(t_h // 24), 5.0), s=70,
                   color=HOOK_COLORS[kind], alpha=0.85, edgecolor="white", lw=0.6,
                   label=HOOK_LABELS[kind] if kind not in seen else None)
        seen.add(kind)
    ax.axvspan(-0.5, 8, color="#555", alpha=0.12, zorder=0)
    ax.axvspan(23, 24.5, color="#555", alpha=0.12, zorder=0)
    ax.set_xlim(-0.5, 24.5)
    ax.set_xticks(range(0, 25, 3))
    ax.set_ylim(-0.6, 12.2)
    ax.set_yticks(range(0, 11, 2))
    ax.set_xlabel("hour she reached out (gray = quiet hours)")
    ax.set_ylabel("mood that day")
    ax.set_title("Why she reached out: hook kind, hour and mood",
                 fontsize=10.5, loc="left")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)

    heatmap(ax4, fig, his + hers, n_days, day_labels,
            "When the conversation happens: messages per day and hour")

    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out1 = OUT_DIR / "live-overview.png"
    fig.savefig(out1, dpi=150)
    print("saved", out1)

    # live-week.png
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.8))
    fig.suptitle(f"Live run, {span}: who writes when, and what happens to her "
                 f"intents ({data['n_convs']} conversations)", fontsize=13)
    heatmap(axes[0], fig, his, n_days, day_labels, f"His messages ({len(his)})")
    heatmap(axes[1], fig, hers, n_days, day_labels, f"Her messages ({len(hers)})")

    ax = axes[2]
    per = {name: Counter(int(i[1] // 24) for i in rows)
           for name, rows in (("fired", fired), ("suppressed", suppressed),
                              ("open", still_open))}
    bottom = np.zeros(n_days)
    for name, color, label in (("fired", "#5f8a5f", "fired"),
                               ("suppressed", "#b05a5a", "held back by the gate"),
                               ("open", "#bbbbbb", "still open")):
        vals = np.array([per[name].get(int(d), 0) for d in days], dtype=float)
        if vals.sum():
            ax.bar(days, vals, bottom=bottom, color=color, edgecolor="#777",
                   lw=0.5, label=label)
        bottom += vals
    ax.set_title("Intents per day", fontsize=10.5, loc="left")
    ax.set_ylabel("intents")
    ax.set_xticks(days)
    ax.set_xticklabels(day_labels, fontsize=8, rotation=45)
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=[0, 0, 1, 0.92])
    out2 = OUT_DIR / "live-week.png"
    fig.savefig(out2, dpi=150)
    print("saved", out2)

    print(json.dumps({
        "days": n_days, "span": span, "his": len(his), "hers": len(hers),
        "unprompted": sum(reach.values()), "conversations": data["n_convs"],
        "fired": len(fired), "suppressed": len(suppressed),
        "open": len(still_open), "mood": M.tolist(), "phases": phase,
    }))


if __name__ == "__main__":
    main()
