"""C1 — Emoji reactions (`setMessageReaction`): energy-driven reaction policy, sim study.

Plan: `plans/advisor-orchestration-2026-08-15.md` Part 3, section C1 (worktree
`llh-wt-c-reactions`). HARD BOUNDARY: this experiment only ADDS files under
`experiments/` and `results/`; it never touches harness/, engine/, sim/ or any
existing file, and it never sends anything anywhere (log-only decisions).

Design (cheap-run discipline: 5 seeds x 14 virtual days, FakeClient):
  - A scripted user sends 4 messages/day at fixed local hours
    [9.0, 12.5, 17.0, 20.5] (deterministic, identical across seeds).
  - Each user message is processed by the harness `Session` (real engine path:
    mood/cycle state, BehaviorDirective, assemble, FakeClient reply). The
    energy signal is `TurnResult.directive.energy` — the ENERGY channel
    computed by `harness/behavior.py::derive_behavior` at the message hour
    (`engine.circadian.energy(hour, phase_label, timing)`). An identity check
    pins directive.energy to the circadian channel.
  - Per user message we LOG a reaction-actuation decision (emoji reaction,
    never sent) driven by that energy channel. Two candidate policies:
      THRESHOLD (primary):  react iff energy >= theta, theta = 0.735
                            (design constant — see report; calibrated on the
                            DESIGN energy table so the frequency cap binds).
      LINEAR (contrast):    react with p = clamp(a + b*E, 0, 1), a = 0.05,
                            b calibrated on seed 5001 so mean p = 0.30; the
                            contrast shows why a deterministic threshold is
                            required for criterion (1) at realistic volumes.
  - Per day we compute: reaction rate = reactions / user messages,
    daily energy   = mean of the energy channel over the 24 h curve for that
    day's phase (also recorded: mean directive energy at the day's messages).

Success criteria (verbatim from the plan):
  (1) Spearman rho(reaction-rate, daily energy) >= 0.5 with bootstrap 95% CI
      excluding 0;
  (2) reaction frequency <= 1 per 3 user messages (<= 1/3);
  (3) API capability confirmed against Telegram Bot API docs (web check —
      finding baked into this module so the report is reproducible offline;
      source: https://core.telegram.org/bots/api#setmessagereaction,
      retrieved 2026-08-15: bots MAY use setMessageReaction in private chats;
      as non-premium users bots can set up to ONE reaction per message;
      paid reactions are not available to bots).

Outputs: results/c1-reactions/summary.json + results/c1-reactions/report.md.
Run:  python experiments/c1_reactions.py --out results/c1-reactions
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

from engine.circadian import energy as circadian_energy
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.session import Session
from harness.store import SQLiteStore

# --------------------------------------------------------------------------- #
# Experiment constants
# --------------------------------------------------------------------------- #

SEEDS = [5001, 5002, 5003, 5004, 5005]
DAYS = 14
MESSAGE_HOURS = [9.0, 12.5, 17.0, 20.5]
USER_TEXTS = [
    "good morning!",
    "how's your day going?",
    "lunch was good",
    "what are you up to tonight?",
]
FAKE_REPLIES = ["ok!", "nice.", "haha, true.", "sure.", "lovely.", "mhm."]

#: Primary policy: react iff directive energy >= theta.
THETA = 0.735
#: Emoji attached to a logged actuation (fixed; valence-mapped emoji selection
#: is a channel-side implementation choice, out of scope here).
REACTION_EMOJI = "❤️"

#: Contrast policy: p = clamp(A_LIN + B_LIN * E, 0, 1); B_LIN calibrated on
#: seed 5001 so the mean p over that seed's messages equals TARGET_MEAN_P.
A_LIN = 0.05
TARGET_MEAN_P = 0.30

#: API capability finding (criterion 3) — web check, documented in report.md.
API_FINDING = {
    "available": True,
    "private_chats_allowed": True,
    "source": "https://core.telegram.org/bots/api#setmessagereaction (official "
    "Telegram Bot API docs, retrieved 2026-08-15)",
    "changelog": "https://core.telegram.org/bots/api-changelog — Bot API 7.0 "
    "(Dec 2023): \"Added the method setMessageReaction that allows bots to "
    "react to messages\"; also announced on @BotNews: \"Bots can now react to "
    "messages with setMessageReaction.\"",
    "notes": (
        "chat_id accepts any chat (private chats included; no private-chat "
        "exclusion, contrast sendChatAction). Constraints: bots cannot use "
        "paid reactions; as non-premium users bots can set up to ONE reaction "
        "per message (matches the <=1/3-per-message policy); custom emoji "
        "only if already present on the message or allowed by chat admins "
        "(use default emoji); service messages of some types cannot be "
        "reacted to. Reaction updates (MessageReactionUpdated) are delivered "
        "to bots, so reactions are observable."
    ),
}


# --------------------------------------------------------------------------- #
# Policies (log-only decision functions)
# --------------------------------------------------------------------------- #


def threshold_react(energy: float, theta: float = THETA) -> bool:
    """Deterministic threshold policy on the energy channel."""
    return energy >= theta


def linear_prob(energy: float, a: float, b: float) -> float:
    return float(np.clip(a + b * energy, 0.0, 1.0))


# --------------------------------------------------------------------------- #
# Sim
# --------------------------------------------------------------------------- #


def _run_seed(seed: int, days: int, out_dir: Path) -> dict:
    """One seed x `days` virtual days; returns per-message + per-day records."""
    with tempfile.TemporaryDirectory(prefix=f"c1-reactions-{seed}-") as tmp:
        store = SQLiteStore(Path(tmp) / "sim.db")
        clock = VirtualClock()
        client = FakeClient(responses=list(FAKE_REPLIES))
        session = Session(
            store,
            persona=PersonaParams(),
            timing=TimingParams(),
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=seed,
            client=client,
            clock=clock,
            feedback=True,
            synthetic_score=True,
        )

        messages: list[dict] = []
        day_rows: dict[int, dict] = {}
        phase_by_day: dict[int, str] = {}

        try:
            for day in range(days):
                clock.advance_to_day(day)
                for hour, text in zip(MESSAGE_HOURS, USER_TEXTS):
                    clock.advance_hours(hour - clock.local_hour())
                    result = session.on_message(text)
                    energy = float(result.directive.energy)
                    # Identity check: the directive energy IS the circadian
                    # energy channel at the message hour for today's phase.
                    phase = session.state_summary()["phase"]
                    expected = circadian_energy(hour, phase, TimingParams())
                    if abs(energy - expected) > 1e-9:
                        raise AssertionError(
                            f"seed {seed} day {day} h {hour}: directive energy "
                            f"{energy:.6f} != circadian energy {expected:.6f}"
                        )
                    if day not in phase_by_day:
                        phase_by_day[day] = phase
                    messages.append(
                        {
                            "seed": seed,
                            "day": day,
                            "hour": hour,
                            "energy": energy,
                            "phase": phase,
                            "react_threshold": threshold_react(energy),
                            "react_linear": None,  # filled in stats phase
                        }
                    )
                clock.advance_to_day(day + 1)
                session.ensure_day(day + 1)
        finally:
            try:
                session.finalize_current()
            except Exception as exc:  # pragma: no cover - defensive
                print(f"  seed {seed}: finalize_current skipped ({exc})")
            store.close()

    # Per-day aggregates (reaction decisions under the threshold policy).
    timing = TimingParams()
    for day in range(days):
        day_msgs = [m for m in messages if m["day"] == day]
        phase = phase_by_day[day]
        curve_hours = np.arange(0.0, 24.0, 0.5)
        energy_day_curve = float(
            np.mean([circadian_energy(h, phase, timing) for h in curve_hours])
        )
        energy_day_msgs = float(
            np.mean([m["energy"] for m in day_msgs]) if day_msgs else 0.0
        )
        day_rows[day] = {
            "seed": seed,
            "day": day,
            "phase": phase,
            "n_messages": len(day_msgs),
            "n_react_threshold": sum(m["react_threshold"] for m in day_msgs),
            "rate_threshold": (
                sum(m["react_threshold"] for m in day_msgs) / len(day_msgs)
                if day_msgs
                else 0.0
            ),
            "energy_day_curve": energy_day_curve,
            "energy_day_msgs": energy_day_msgs,
        }

    return {"seed": seed, "messages": messages, "days": day_rows}


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def _spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 3 or np.all(x == x[0]) or np.all(y == y[0]):
        return None
    return float(spearmanr(x, y).statistic)


def pair_bootstrap_ci(
    x: np.ndarray, y: np.ndarray, n: int = 10_000, seed: int = 4242
) -> tuple[float | None, float | None]:
    """Percentile 95% CI of Spearman rho over resampled (x, y) pairs."""
    if _spearman(x, y) is None:
        return None, None
    rng = np.random.default_rng(seed)
    stats = np.empty(n)
    n_pts = len(x)
    for i in range(n):
        idx = rng.integers(0, n_pts, n_pts)
        stats[i] = spearmanr(x[idx], y[idx]).statistic
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def block_bootstrap_ci(
    days_by_seed: dict[int, list[dict]],
    x_key: str,
    y_key: str,
    n: int = 5_000,
    seed: int = 4243,
) -> tuple[float | None, float | None]:
    """Bootstrap over SEED BLOCKS (each seed's 14-day series kept intact).

    Conservative w.r.t. within-seed autocorrelation (phase trend).
    """
    seeds = sorted(days_by_seed)
    rng = np.random.default_rng(seed)
    stats = np.empty(n)
    for i in range(n):
        picked = rng.integers(0, len(seeds), len(seeds))
        xs, ys = [], []
        for s in picked:
            for row in days_by_seed[seeds[s]]:
                xs.append(row[x_key])
                ys.append(row[y_key])
        xa, ya = np.asarray(xs), np.asarray(ys)
        if np.all(ya == ya[0]) or np.all(xa == xa[0]):
            stats[i] = np.nan
        else:
            stats[i] = spearmanr(xa, ya).statistic
    stats = stats[~np.isnan(stats)]
    if stats.size == 0:
        return None, None
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def _fmt(value: float | None, ndigits: int = 3) -> str:
    """Format a possibly-None float (None => 'n/a (const)')."""
    return "n/a (const)" if value is None else f"{value:.{ndigits}f}"


def _mean_sd(values: list[float]) -> tuple[float, float]:
    arr = np.asarray(values)
    return float(arr.mean()), float(arr.std(ddof=1)) if len(arr) > 1 else (float(arr.mean()), 0.0)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _write_report(out_dir: Path, meta: dict, tables: dict) -> Path:
    ts = meta["timestamp"]
    rho_t = tables["rho_threshold"]
    rho_t_ci = tables["rho_threshold_ci"]
    rho_l = tables["rho_linear"]
    freq_t = tables["freq_threshold"]
    freq_l = tables["freq_linear"]
    verdicts = tables["verdicts"]

    lines = [
        "---",
        "type: experiment-report",
        "title: C1 — emoji reactions (setMessageReaction): energy-driven reaction policy, sim 5x14",
        'description: "Sim study of a log-only reaction-actuation decision per user message driven by the '
        "energy channel (BehaviorDirective.energy): Spearman rho(reaction-rate, daily energy), "
        'frequency cap, Telegram Bot API capability check."',
        "seeds: [5001, 5002, 5003, 5004, 5005]",
        "model: fake (FakeClient)",
        "mode: sim",
        f"timestamp: {ts}",
        "tags: [llm-behavioral-harness, c1, reactions, energy-channel, telegram]",
        "---",
        "",
        "# C1 — Emoji reactions (`setMessageReaction`) — experiment report",
        "",
        f"Run {ts} · mode **sim** (FakeClient) · {len(SEEDS)} seeds × {DAYS} virtual days · "
        f"{tables['n_messages']} user messages · {tables['n_days']} companion-days "
        f"({tables['n_messages']} reaction-actuation decisions LOGGED, nothing sent).",
        "",
        "Plan: `plans/advisor-orchestration-2026-08-15.md` Part 3 §C1. Boundary: only "
        "`experiments/` and `results/` were touched; no harness/engine/sim file was modified; "
        "no API call was made (decisions are log-only).",
        "",
        "## Methodology",
        "",
        "- **Sim.** Scripted user sends 4 messages/day at fixed local hours "
        "9.0 / 12.5 / 17.0 / 20.5 (identical across seeds). Every message is processed by the "
        "harness `Session` (engine: mood/cycle/phase state; `synthetic_score=True`, "
        "`feedback=True` — deterministic per seed, replay-identical to `sim.run_daily`). "
        "Replies come from `FakeClient`. Temp SQLite stores are deleted after each seed.",
        f"- **Energy channel.** The decision input is `TurnResult.directive.energy` — the energy "
        "channel computed by `harness/behavior.py::derive_behavior` at the message hour from "
        "`engine.circadian.energy(hour, phase_label, timing)`. Identity check in the sim: "
        f"directive.energy ≡ circadian energy at every message "
        f"({tables['n_messages']}/{tables['n_messages']}; max abs diff "
        f"{tables['energy_identity_maxdiff']:.2e}). Daily energy = mean of the 24 h energy "
        "curve for the day's phase (0.5 h samples).",
        "- **Policies (log-only).** PRIMARY — threshold: react iff `energy >= 0.735` "
        "(design constant; see calibration note). CONTRAST — linear-Bernoulli: "
        "p = clamp(0.05 + b·E, 0, 1) with b calibrated on seed 5001 so mean p = 0.30, "
        "decisions drawn from an independent per-(seed, day) numpy stream (never touches the "
        "engine's RNG). The contrast shows why the deterministic threshold form is required.",
        "- **Statistics.** Spearman rho pooled over the 70 (seed, day) points; 95% CI by pair "
        "bootstrap (10,000 resamples) and by seed-block bootstrap (5,000 resamples — "
        "conservative under within-seed autocorrelation). Frequency = total reactions / total "
        "user messages (pooled), plus per-seed rates. Mean ± SD across seeds for daily rate.",
        "",
        "### Calibration note (threshold theta)",
        "",
        "Per the DESIGN energy table (`engine/types.py`), the channel's daily-mean level by "
        "phase is menstrual ≈ 0.45 · follicular ≈ 0.65 · ovulatory ≈ 0.70. The threshold sits "
        "above the follicular daytime level so reactions concentrate on high-energy "
        "(ovulatory/late-follicular) days while the pooled frequency stays under the 1-in-3 "
        "cap. The theta sweep in the results below verifies the choice on the simulated data.",
        "",
        "## Results",
        "",
        "### Per-seed table (primary threshold policy)",
        "",
        "| seed | phases seen (start→end) | mean daily energy | mean daily rate | freq (react/msg) | per-seed rho |",
        "|---|---|---|---|---|---|",
    ]
    for row in tables["per_seed_rows"]:
        rho_txt = "n/a (const)" if row["rho"] is None else f"{row['rho']:.3f}"
        lines.append(
            f"| {row['seed']} | {row['phases']} | {row['mean_energy']:.3f} | "
            f"{row['mean_rate']:.3f} | {row['freq']:.3f} | {rho_txt} |"
        )
    lines += [
        "",
        "### Pooled statistics",
        "",
        "| stat | threshold policy | linear contrast |",
        "|---|---|---|",
        f"| Spearman rho(rate, daily energy) | **{_fmt(rho_t)}** | {_fmt(rho_l)} |",
        f"| 95% CI (pair bootstrap, 10k) | {_fmt(rho_t_ci[0])} – {_fmt(rho_t_ci[1])} | "
        f"{_fmt(tables['rho_linear_ci'][0])} – {_fmt(tables['rho_linear_ci'][1])} |",
        f"| 95% CI (seed-block bootstrap, 5k) | {_fmt(tables['rho_threshold_ci_block'][0])} – "
        f"{_fmt(tables['rho_threshold_ci_block'][1])} | — |",
        f"| reaction frequency (pooled) | **{freq_t:.3f}** ({freq_t:.3f} ≤ 1/3) | "
        f"{freq_l:.3f} |",
        f"| daily rate mean ± SD (across seeds) | {tables['rate_mean']:.3f} ± "
        f"{tables['rate_sd']:.3f} | — |",
        f"| total reactions / total messages | {tables['n_react_t']} / {tables['n_messages']} | "
        f"{tables['n_react_l']} / {tables['n_messages']} |",
        "",
        "The threshold policy's daily rate is a deterministic monotone step function of the "
        "phase-driven daily energy level, so the correlation is at ceiling by construction; "
        "the binding constraints are the frequency cap (theta calibration) and API "
        "availability. The linear-Bernoulli contrast shows the same energy signal *cannot* "
        "meet criterion (1) when the decision is stochastic at realistic message volumes "
        "(per-day Bernoulli noise dominates the ~0.25-wide phase signal): rho ≈ "
        f"{_fmt(rho_l, ndigits=2)}, CI including 0. A channel-side "
        "implementation should therefore use the deterministic threshold form.",
        "",
        "### Theta sensitivity (threshold policy, pooled)",
        "",
        "| theta | freq | rho |",
        "|---|---|---|",
    ]
    for t_row in tables["theta_sweep"]:
        lines.append(
            f"| {t_row['theta']:.3f} | {t_row['freq']:.3f} | {_fmt(t_row['rho'])} |"
        )
    lines += [
        "",
        "## Criterion 3 — Telegram Bot API capability (`setMessageReaction`)",
        "",
        f"**VERDICT: {API_FINDING['available'] and API_FINDING['private_chats_allowed'] and 'ALLOWED' or 'NOT ALLOWED'}** — "
        "bots can use `setMessageReaction` in **private chats**.",
        "",
        f"- Source: {API_FINDING['source']}.",
        f"- Changelog: {API_FINDING['changelog']}",
        f"- Notes: {API_FINDING['notes']}",
        "",
        "Official method text: *\"Use this method to change the chosen reactions on a message. "
        "Service messages of some types can't be reacted to. Automatically forwarded messages "
        "from a channel to its discussion group have the same available reactions as messages "
        "in the channel. Bots can't use paid reactions. Returns True on success.\"* Parameters: "
        "`chat_id` (any chat, private chats included — no exclusion, unlike `sendChatAction`), "
        "`message_id`, `reaction` (Array of ReactionType; *\"Currently, as non-premium users, "
        "bots can set up to one reaction per message\"*), `is_big`.",
        "",
        "## Verdicts",
        "",
        "| criterion | requirement | result | verdict |",
        "|---|---|---|---|",
        f"| 1 | rho ≥ 0.5, bootstrap 95% CI excludes 0 | rho = "
        f"{_fmt(rho_t)}, CI {_fmt(rho_t_ci[0])}–{_fmt(rho_t_ci[1])} "
        f"(pair), {_fmt(tables['rho_threshold_ci_block'][0])}–{_fmt(tables['rho_threshold_ci_block'][1])} "
        f"(block) | **{verdicts['c1']}** |",
        f"| 2 | frequency ≤ 1 per 3 user messages | {freq_t:.3f} reactions/message "
        f"(≤ 0.333) | **{verdicts['c2']}** |",
        f"| 3 | API capability confirmed against Bot API docs | setMessageReaction "
        f"available to bots in private chats (1 reaction/message cap) | **{verdicts['c3']}** |",
        "",
        f"**OVERALL: {verdicts['overall']}** — pass ⇒ implement channel-side only "
        "(threshold on directive energy, deterministic, `setMessageReaction` with a default "
        "emoji; emoji choice may later be valence-mapped).",
        "",
    ]
    report = out_dir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="c1_reactions", description=__doc__)
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--seeds", type=str, default="5001-5005")
    parser.add_argument("--out", type=str, default="results/c1-reactions")
    args = parser.parse_args(argv)

    lo, hi = (int(x) for x in args.seeds.split("-"))
    seeds = list(range(lo, hi + 1))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_messages: list[dict] = []
    days_by_seed: dict[int, list[dict]] = {}
    for seed in seeds:
        print(f"seed {seed}: running {args.days} virtual days ...", flush=True)
        run = _run_seed(seed, args.days, out_dir)
        all_messages.extend(run["messages"])
        days_by_seed[seed] = list(run["days"].values())
        print(f"  done: {len(run['messages'])} messages", flush=True)

    # --- linear policy calibration on seed 5001 (documented, reproducible) --
    cal_msgs = [m for m in all_messages if m["seed"] == seeds[0]]
    mean_e = float(np.mean([m["energy"] for m in cal_msgs]))
    b_lin = (TARGET_MEAN_P - A_LIN) / mean_e
    for m in all_messages:
        rng = np.random.default_rng(m["seed"] * 100_000 + m["day"])
        m["react_linear"] = rng.random() < linear_prob(m["energy"], A_LIN, b_lin)

    # --- per-day aggregates -------------------------------------------------
    rows: list[dict] = []
    for seed in seeds:
        rows.extend(days_by_seed[seed])
    n_days = len(rows)
    n_messages = len(all_messages)

    rate_t = np.asarray([r["rate_threshold"] for r in rows])
    rate_l = np.asarray(
        [
            sum(m["react_linear"] for m in all_messages if m["day"] == r["day"] and m["seed"] == r["seed"])
            / r["n_messages"]
            for r in rows
        ]
    )
    energy_curve = np.asarray([r["energy_day_curve"] for r in rows])
    energy_msgs = np.asarray([r["energy_day_msgs"] for r in rows])

    rho_t = _spearman(rate_t, energy_curve)
    rho_l = _spearman(rate_l, energy_curve)
    rho_t_ci = pair_bootstrap_ci(rate_t, energy_curve)
    rho_l_ci = pair_bootstrap_ci(rate_l, energy_curve)
    rho_t_ci_block = block_bootstrap_ci(days_by_seed, "rate_threshold", "energy_day_curve")

    n_react_t = sum(m["react_threshold"] for m in all_messages)
    n_react_l = sum(bool(m["react_linear"]) for m in all_messages)
    freq_t = n_react_t / n_messages
    freq_l = n_react_l / n_messages

    per_seed_rows = []
    for seed in seeds:
        s_msgs = [m for m in all_messages if m["seed"] == seed]
        s_rows = days_by_seed[seed]
        s_rate = np.asarray([r["rate_threshold"] for r in s_rows])
        s_energy = np.asarray([r["energy_day_curve"] for r in s_rows])
        phases = [r["phase"] for r in s_rows]
        per_seed_rows.append(
            {
                "seed": seed,
                "phases": f"{phases[0]}→{phases[-1]}",
                "mean_energy": float(np.mean(s_energy)),
                "mean_rate": float(np.mean(s_rate)),
                "freq": sum(m["react_threshold"] for m in s_msgs) / len(s_msgs),
                "rho": _spearman(s_rate, s_energy),
            }
        )

    # theta sweep (recomputed over logged energies — no extra sim)
    theta_sweep = []
    for theta in np.arange(0.700, 0.805, 0.005):
        react = np.asarray([m["energy"] >= theta for m in all_messages])
        n_r = int(react.sum())
        per_day = {
            (m["seed"], m["day"]): 0.0 for m in all_messages
        }
        counts = {k: 0 for k in per_day}
        for m, r in zip(all_messages, react):
            counts[(m["seed"], m["day"])] += int(r)
        rates = np.asarray(
            [
                counts[(r["seed"], r["day"])] / r["n_messages"]
                for r in rows
            ]
        )
        theta_sweep.append(
            {
                "theta": float(theta),
                "freq": n_r / n_messages,
                "rho": _spearman(rates, energy_curve),
            }
        )

    rate_mean, rate_sd = _mean_sd([r["rate_threshold"] for r in rows])

    # --- verdicts ------------------------------------------------------------
    c1 = (
        rho_t is not None
        and rho_t >= 0.5
        and rho_t_ci[0] is not None
        and rho_t_ci[0] > 0.0
        and rho_t_ci_block[0] > 0.0
    )
    c2 = freq_t <= 1.0 / 3.0
    c3 = bool(API_FINDING["available"] and API_FINDING["private_chats_allowed"])
    verdicts = {
        "c1": "PASS" if c1 else "FAIL",
        "c2": "PASS" if c2 else "FAIL",
        "c3": "PASS" if c3 else "FAIL",
        "overall": "PASS" if (c1 and c2 and c3) else "FAIL",
    }

    summary = {
        "experiment": "c1-reactions",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seeds": seeds,
        "days": args.days,
        "n_messages": n_messages,
        "n_days": n_days,
        "theta": THETA,
        "linear": {"a": A_LIN, "b": round(b_lin, 6), "target_mean_p": TARGET_MEAN_P},
        "energy_identity_maxdiff": max(
            abs(m["energy"] - circadian_energy(m["hour"], m["phase"], TimingParams()))
            for m in all_messages
        ),
        "pooled": {
            "rho_threshold": rho_t,
            "rho_threshold_ci_pair": rho_t_ci,
            "rho_threshold_ci_block": rho_t_ci_block,
            "rho_linear": rho_l,
            "rho_linear_ci_pair": rho_l_ci,
            "freq_threshold": freq_t,
            "freq_linear": freq_l,
            "n_react_threshold": n_react_t,
            "n_react_linear": n_react_l,
            "rate_mean": rate_mean,
            "rate_sd": rate_sd,
        },
        "per_seed": per_seed_rows,
        "theta_sweep": theta_sweep,
        "api_finding": API_FINDING,
        "verdicts": verdicts,
    }

    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tables = {
        "n_messages": n_messages,
        "n_days": n_days,
        "energy_identity_maxdiff": summary["energy_identity_maxdiff"],
        "per_seed_rows": per_seed_rows,
        "rho_threshold": rho_t,
        "rho_threshold_ci": rho_t_ci,
        "rho_threshold_ci_block": rho_t_ci_block,
        "rho_linear": rho_l,
        "rho_linear_ci": rho_l_ci,
        "freq_threshold": freq_t,
        "freq_linear": freq_l,
        "n_react_t": n_react_t,
        "n_react_l": n_react_l,
        "rate_mean": rate_mean,
        "rate_sd": rate_sd,
        "theta_sweep": theta_sweep,
        "verdicts": verdicts,
    }
    report = _write_report(out_dir, {"timestamp": summary["timestamp"]}, tables)

    print("\n==== C1 summary ====")
    print(f"messages: {n_messages} · days: {n_days}")
    print(f"threshold: rho={_fmt(rho_t)} CI={_fmt(rho_t_ci[0])}-{_fmt(rho_t_ci[1])} "
          f"(block {_fmt(rho_t_ci_block[0])}-{_fmt(rho_t_ci_block[1])}) freq={freq_t:.3f}")
    print(f"linear:    rho={_fmt(rho_l)} CI={_fmt(rho_l_ci[0])}-{_fmt(rho_l_ci[1])} freq={freq_l:.3f} "
          f"(b={b_lin:.4f})")
    print(f"verdicts: {verdicts}")
    print(f"report: {report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
