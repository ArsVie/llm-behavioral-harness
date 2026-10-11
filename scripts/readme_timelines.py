"""Render the README timelines: her mood, her reach-outs and his messages.

    MPLBACKEND=Agg .venv/bin/python scripts/readme_timelines.py

Writes docs/images/month-timeline.png from the committed 30-day simulated
run (results/it3-backfill-2026-08-09/dbs/FULL/seed5001.db, the run behind
the old one-month showcase). With the live run database present at
results/live-companion/companion.db (never committed) it also writes
docs/images/live-timeline.png in the same design.

Only times, counts, the daily mood draw, cycle phases and the kind of
trigger are drawn. No message, hook or agenda text is read into a figure:
in the live run those quote real plans.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from readme_charts import (BLUE, GRAY, GRID, INK, INK_2, ORANGE, OUT, ROOT,  # noqa: E402
                           SURFACE)

MONTH_DB = ROOT / "results/it3-backfill-2026-08-09/dbs/FULL/seed5001.db"
LIVE_DB = ROOT / "results/live-companion/companion.db"

PHASES = {  # label, band colour
    "menstrual": ("period", "#f6dde3"),
    "follicular": ("follicular", "#e1f0e4"),
    "ovulatory": ("ovulation", "#f6edd2"),
    "luteal_early": ("early luteal", "#e0e9f5"),
    "luteal_late": ("late luteal", "#ebe3f3"),
}
#: Why she wrote first, as the figures name it.
KINDS = {
    "start": ("an item on her agenda", BLUE, "o"),
    "finished": ("an item she just finished", ORANGE, "D"),
    "call": ("his call", "#8c5c9c", "s"),
}


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _kind_of_hook(hook: str) -> str:
    """Kind of a proactive intent from its hook prefix only ('Finished: ...')."""
    return "finished" if hook.split(":", 1)[0].strip().lower() == "finished" else "start"


def load(db: Path) -> dict:
    """Mood per day, his message times, her reach-outs by kind, held-back intents.

    A reach-out is a proactive message. It carries either a proactive intent
    (its hook prefix gives the kind) or, in newer runs, was sent from an
    agenda-boundary decision at the same instant (the item starts there), or
    came with his call (a call decision at the same instant).
    """
    con = _ro(db)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    tables = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type = 'table'")}
    mood = q("SELECT day, M, phase_label FROM daily_state ORDER BY day")
    his = [t for (t,) in q("SELECT t_h FROM messages WHERE role = 'user'")]
    reach: list[tuple[float, str]] = []
    for t_h, intent in q("SELECT t_h, intent_id FROM messages "
                         "WHERE role = 'assistant' AND proactive = 1 ORDER BY t_h"):
        kind = None
        if intent:
            (hook,), = q("SELECT hook FROM proactive_intents WHERE id = ?", intent)
            kind = _kind_of_hook(hook)
        elif "decision_records" in tables:
            near = q("SELECT popup_kind, event_id FROM decision_records "
                     "WHERE abs(t_h - ?) < 0.01", t_h)
            kinds = {p for p, _ in near}
            if "tool_decide_event" in kinds:
                kind = "start"
            elif "tool_decide_call" in kinds:
                kind = "call"
        if kind is None:
            raise SystemExit(f"unclassified reach-out at t_h={t_h:.3f}")
        reach.append((t_h, kind))
    held = [t for (t,) in q("SELECT created_t_h FROM proactive_intents "
                            "WHERE status = 'suppressed'")]
    first_sent = {}
    if "sent_at" in {r[1] for r in q("PRAGMA table_info(messages)")}:
        for day, sent in q("SELECT day, min(sent_at) FROM messages "
                           "WHERE sent_at IS NOT NULL GROUP BY day"):
            first_sent[int(day)] = sent
    con.close()
    return {"mood": mood, "his": his, "reach": reach, "held": held, "first_sent": first_sent}


def timeline(d: dict, out: Path, title: str, subtitle: str, footer: str,
             n_days: int, day_labels: list[str] | None = None) -> None:
    fig, (ax, lane) = plt.subplots(
        2, 1, figsize=(10, 5.1), sharex=True,
        gridspec_kw={"height_ratios": [3.1, 1.25], "hspace": 0.06})

    # Cycle-phase bands, one per run of equal phase, named at the top.
    mood = [(int(day), m, ph) for day, m, ph in d["mood"] if int(day) < n_days]
    start = 0
    for i in range(1, len(mood) + 1):
        if i == len(mood) or mood[i][2] != mood[start][2]:
            name, colour = PHASES.get(mood[start][2], (mood[start][2], GRID))
            x0, x1 = mood[start][0], mood[i - 1][0] + 1
            for a in (ax, lane):
                a.axvspan(x0, x1, color=colour, lw=0, zorder=0)
            if x1 - x0 >= 2:
                ax.text((x0 + x1) / 2, 10.9, name, ha="center", va="bottom",
                        fontsize=9, color=INK_2)
            start = i

    xs = [day + 0.5 for day, _, _ in mood]
    ax.plot(xs, [m for _, m, _ in mood], color=INK, lw=1.8, zorder=3)
    ax.scatter(xs, [m for _, m, _ in mood], s=26, color=INK, edgecolor=SURFACE,
               linewidth=1.2, zorder=4)
    ax.set_ylim(-0.5, 10.6)
    ax.set_yticks([0, 5, 10])
    ax.set_ylabel("her mood that day\n(0 to 10)")
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="x", length=0)

    # Lane: her reach-outs (by trigger), held-back intents, his messages.
    Y_SENT, Y_HELD, Y_HIS = 2.0, 1.0, 0.0
    for kind, (_label, colour, marker) in KINDS.items():
        pts = [t / 24 for t, k in d["reach"] if k == kind]
        if pts:
            lane.scatter(pts, [Y_SENT] * len(pts), s=46, color=colour, marker=marker,
                         edgecolor=SURFACE, linewidth=1.0, zorder=4)
    lane.scatter([t / 24 for t in d["held"]], [Y_HELD] * len(d["held"]), s=26,
                 facecolor="none", edgecolor=GRAY, linewidth=1.1, zorder=3)
    lane.scatter([t / 24 for t in d["his"]], [Y_HIS] * len(d["his"]), marker="|",
                 s=70, color=INK_2, linewidth=1.0, zorder=3)
    lane.set_ylim(-0.6, 2.6)
    lane.set_yticks([Y_SENT, Y_HELD, Y_HIS],
                    ["she wrote first", "held back", "his messages"], fontsize=9)
    lane.tick_params(axis="y", length=0)
    lane.spines["left"].set_visible(False)
    lane.set_xlim(0, n_days)
    if day_labels:
        lane.set_xticks([i + 0.5 for i in range(n_days)], day_labels, fontsize=9)
        lane.tick_params(axis="x", length=0)
    else:
        lane.set_xticks(range(0, n_days + 1, 5))
        lane.set_xlabel("day of the run", color=INK_2)

    counts = Counter(k for _, k in d["reach"])
    handles = [Line2D([], [], ls="", marker=m, color=c, markeredgecolor=SURFACE, ms=7,
                      label=f"{label} ({counts[k]})")
               for k, (label, c, m) in KINDS.items() if counts[k]]
    handles.append(Line2D([], [], ls="", marker="o", markerfacecolor="none",
                          markeredgecolor=GRAY, ms=6,
                          label=f"reason raised, held back ({len(d['held'])})"))
    fig.legend(handles=handles, frameon=False, ncol=len(handles), fontsize=8.8,
               loc="lower left", bbox_to_anchor=(0.005, 0.045), handletextpad=0.3,
               columnspacing=1.2)
    fig.suptitle(title, x=0.01, y=0.985, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.935, subtitle, ha="left", va="top", fontsize=9.5, color=INK_2,
             linespacing=1.4)
    fig.text(0.01, 0.008, footer, fontsize=8, color=INK_2)
    fig.subplots_adjust(left=0.115, right=0.99, top=0.79, bottom=0.19)
    fig.savefig(out, dpi=180)
    plt.close(fig)


def month() -> None:
    d = load(MONTH_DB)
    n_days = 30
    by_phase: dict[str, list[int]] = {}
    for day, m, ph in d["mood"]:
        if int(day) < n_days:
            by_phase.setdefault(ph, []).append(m)
    avg = {ph: sum(v) / len(v) for ph, v in by_phase.items()}
    n = len(d["reach"])
    tied = sum(k in ("start", "finished") for _, k in d["reach"])
    assert tied == n, "title claims every reach-out is tied to her agenda"
    timeline(
        d, OUT / "month-timeline.png",
        f"A simulated month of her life: {n} times she wrote first, every one "
        "tied to her own agenda",
        f"Mood is drawn once a day; in this run it averaged {avg['follicular']:.1f} in the "
        f"follicular phase and {avg['luteal_late']:.1f} late in the luteal phase.\n"
        f"The gate held back {len(d['held'])} other reasons to write.",
        "Simulated 30-day run, seed 5001, scripted user. "
        "Data: results/it3-backfill-2026-08-09/dbs/FULL/seed5001.db "
        "(daily_state, messages, proactive_intents).",
        n_days)


def live() -> None:
    d = load(LIVE_DB)
    n_days = len(d["mood"])
    labels = [datetime.fromtimestamp(d["first_sent"][i]).strftime("%a %d")
              if i in d["first_sent"] else f"day {i}" for i in range(n_days)]
    n = len(d["reach"])
    tied = sum(k in ("start", "finished") for _, k in d["reach"])
    first = datetime.fromtimestamp(min(d["first_sent"].values()))
    last = datetime.fromtimestamp(max(d["first_sent"].values()))
    timeline(
        d, OUT / "live-timeline.png",
        f"The live run, {first:%-d} to {last:%-d %B}: she wrote first {n} times, "
        f"{tied} of them tied to her agenda",
        f"Real time, one real user: {len(d['his'])} lines from him (text and voice), mood drawn "
        "once a day.\nTimes and counts only: no message or plan text is drawn.",
        "Live run, seed 8001, deepseek-v4.1-flash. Data: results/live-companion/"
        "companion.db (not committed): daily_state, messages, proactive_intents, "
        "decision_records.",
        n_days, labels)


if __name__ == "__main__":
    month()
    if LIVE_DB.exists():
        live()
