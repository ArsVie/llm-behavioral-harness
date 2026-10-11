"""Render the two README charts from data already committed under results/.

    MPLBACKEND=Agg .venv/bin/python scripts/readme_charts.py

Writes docs/images/judge-calibration.png and docs/images/decision-probe.png.
With a live run database at results/live-companion/companion.db (not
committed) it also writes docs/images/live-run.png, aggregate numbers only.
Every number is read from the run databases and probe records; nothing is
typed in by hand.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
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


def live_run(db: Path) -> None:
    """Aggregate numbers from the live companion run: no message content."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT day FROM messages ORDER BY day")]
    first = {d: con.execute("SELECT min(sent_at) FROM messages WHERE day = ?",
                            (d,)).fetchone()[0] for d in days}
    labels = [datetime.fromtimestamp(first[d]).strftime("%a %d") for d in days]
    per_day = {d: con.execute(
        "SELECT sum(role = 'user'), sum(role = 'assistant' AND proactive = 0), "
        "sum(role = 'assistant' AND proactive = 1) FROM messages WHERE day = ?",
        (d,)).fetchone() for d in days}
    lanes = con.execute(
        "SELECT role, count(*), sum(prompt_tokens), sum(cached_tokens) "
        "FROM llm_calls GROUP BY role ORDER BY sum(prompt_tokens)").fetchall()
    calls = con.execute(
        "SELECT called_at, role, prompt_tokens, cached_tokens FROM llm_calls "
        "WHERE prompt_tokens > 0 ORDER BY id").fetchall()
    verdicts: dict[str, Counter[str]] = defaultdict(Counter)
    for kind, raw in con.execute(
            "SELECT popup_kind, verdict_json FROM decision_records"):
        verdict = json.loads(raw) if raw else {}
        acted = any(verdict.get(k) for k in ("initiate", "reply", "answer"))
        verdicts[kind]["act" if acted else "hold"] += 1
    parse_failed = con.execute("SELECT count(*) FROM state_events "
                               "WHERE event = 'decision_parse_failed'").fetchone()[0]
    con.close()

    fig, axes = plt.subplots(2, 2, figsize=(11, 7.4))
    ax = axes[0, 0]
    xs = range(len(days))
    his = [per_day[d][0] or 0 for d in days]
    hers = [per_day[d][1] or 0 for d in days]
    out = [per_day[d][2] or 0 for d in days]
    ax.bar(xs, his, 0.7, color=GRAY, edgecolor=SURFACE, lw=1.5, label="his messages")
    ax.bar(xs, hers, 0.7, bottom=his, color=BLUE, edgecolor=SURFACE, lw=1.5,
           label="her replies")
    ax.bar(xs, out, 0.7, bottom=[a + b for a, b in zip(his, hers)], color=ORANGE,
           edgecolor=SURFACE, lw=1.5, label="her reach-outs (proactive)")
    ax.set_xticks(list(xs), labels, fontsize=8.5)
    ax.set_ylabel("messages (text and voice lines)")
    ax.set_title("Conversation per day", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left")

    ax = axes[0, 1]
    names = [r[0] for r in lanes]
    cached = [(r[3] or 0) / 1e6 for r in lanes]
    fresh = [((r[2] or 0) - (r[3] or 0)) / 1e6 for r in lanes]
    ys = range(len(names))
    ax.barh(ys, cached, 0.65, color=AQUA, edgecolor=SURFACE, lw=1.5, label="served from cache")
    ax.barh(ys, fresh, 0.65, left=cached, color=GRAY, edgecolor=SURFACE, lw=1.5,
            label="processed fresh")
    for y, r in zip(ys, lanes):
        hit = (r[3] or 0) / (r[2] or 1)
        ax.text((r[2] or 0) / 1e6 + 0.03, y, f"{r[1]} calls, {hit:.0%}",
                va="center", fontsize=8, color=INK_2)
    ax.set_yticks(list(ys), names, fontsize=8.5)
    ax.set_xlabel("prompt tokens (millions)")
    ax.set_xlim(0, max(r[2] or 0 for r in lanes) / 1e6 * 1.35)
    ax.set_title("Prompt tokens by call type", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8.5, loc="lower right")

    ax = axes[1, 0]
    when = [datetime.fromtimestamp(c[0]) for c in calls]
    share = [100 * (c[3] or 0) / c[2] for c in calls]
    main = [c[1] == "chat" or c[1].startswith("tool_decide") for c in calls]
    ax.scatter([w for w, m in zip(when, main) if m], [s for s, m in zip(share, main) if m],
               s=14, color=BLUE, alpha=0.75, label="her turns and decisions")
    ax.scatter([w for w, m in zip(when, main) if not m], [s for s, m in zip(share, main) if not m],
               s=22, color=ORANGE, marker="D", label="judge, planner, memory, face")
    ax.set_ylim(-3, 103)
    ax.set_ylabel("prompt served from cache (%)")
    ax.set_title("Cache hit per model call", loc="left", fontsize=11, color=INK)
    ax.xaxis.set_major_formatter(__import__("matplotlib.dates", fromlist=["x"]).DateFormatter("%a %d"))
    ax.tick_params(axis="x", labelsize=8.5)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")

    ax = axes[1, 1]
    kinds = sorted(verdicts, key=lambda k: -sum(verdicts[k].values()))
    ys = range(len(kinds))[::-1]
    act = [verdicts[k]["act"] for k in kinds]
    hold = [verdicts[k]["hold"] for k in kinds]
    ax.barh(list(ys), act, 0.62, color=BLUE, edgecolor=SURFACE, lw=1.5, label="act")
    ax.barh(list(ys), hold, 0.62, left=act, color=AQUA, edgecolor=SURFACE, lw=1.5,
            label="hold")
    for y, a, h in zip(ys, act, hold):
        ax.text(a + h + 0.6, y, f"{a} / {h}", va="center", fontsize=8.5, color=INK_2)
    ax.set_yticks(list(ys), kinds, fontsize=8.5)
    ax.set_xlabel(f"decisions (act / hold); {parse_failed} unparseable replies were re-asked")
    ax.set_title("Her decisions by pop-up", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8.5, loc="lower right")

    for a in axes.flat:
        a.grid(axis="y" if a in (axes[0, 0], axes[1, 0]) else "x", color=GRID, lw=0.8)
        a.set_axisbelow(True)
    start = datetime.fromtimestamp(min(first.values())).strftime("%d %b")
    end = datetime.fromtimestamp(calls[-1][0]).strftime("%d %b %Y")
    fig.suptitle(f"Live run, {start} to {end}: aggregate numbers",
                 x=0.01, ha="left", fontsize=12.5, color=INK)
    fig.text(0.01, 0.005, "seed 8001, deepseek-v4.1-flash. "
             "Data: results/live-companion/companion.db (not committed): counts and "
             "token totals only.", fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.025, 1, 0.97))
    fig.savefig(OUT / "live-run.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    judge_calibration()
    decision_probe()
    live_db = ROOT / "results" / "live-companion" / "companion.db"
    if live_db.exists():
        live_run(live_db)
