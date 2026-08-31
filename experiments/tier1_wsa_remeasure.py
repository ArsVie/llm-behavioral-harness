"""WS-A re-measure (model-free): FULL vs STRUCTURED_NO_STATE proactive counts.

Drives ``experiments.cvs_common.run_cell`` with the deterministic fake client
(fake=True) over a shared seed set (default 5001..5500 — the same base the
G0 re-derivation used), 30 days, perturb=True, default checkpoints
(DEFAULT_CHECKPOINT_DAYS), default memory policy — the same cell recipe as the
it3 confirmatory matrix, minus the LLM. Counts flow through the real
engine.timing.next_event path (AsyncRuntime/FakeChannel); no LLM, no judge run,
no independent judge. Deterministic given seed.

Purpose: measure the FULL − SNS proactive-count delta THROUGH the fixed
day-0 planning path (commit 772b0f0: session.ensure_day(0) + real
day_scores(store, 0, timing) instead of scores=None) and test the
≈ +10% prediction (stored-run S_d mean ≈ 1.3123 ⇒ FULL should exceed SNS).

Usage (repo root):
    .venv/bin/python -m experiments.tier1_wsa_remeasure --smoke
    .venv/bin/python -m experiments.tier1_wsa_remeasure --seeds 500 --workers 8

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CONDITIONS_DEFAULT = ("FULL", "STRUCTURED_NO_STATE")
SEED_BASE_DEFAULT = 5001
DAYS_DEFAULT = 30

Z95 = 1.959963984540054  # normal approx; n >= 30 per arm here


def _run_one(condition: str, seed: int, db_root: Path, days: int) -> dict:
    """One cell through run_cell (fake client). Import inside worker."""
    from experiments.cvs_common import run_cell

    out_dir = db_root / f"{condition.lower()}-seed{seed}"
    record = run_cell(condition, seed, out_dir, days=days, fake=True,
                      perturb=True)
    return {
        "condition": condition,
        "seed": seed,
        "n_proactive": int(record["n_proactive"]),
        "n_messages": int(record["n_messages"]),
        "db": str(record["db"]),
    }


def _ci_t(values: list[float], alpha: float = 0.05) -> tuple[float, float]:
    """Two-sided confidence interval for the mean (t distribution)."""
    from scipy import stats

    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    half = float(stats.t.ppf(1.0 - alpha / 2.0, n - 1)) * sd / math.sqrt(n)
    return float(mean - half), float(mean + half)


def _stats(values: list[float]) -> dict:
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    lo, hi = _ci_t(values)
    return {
        "n": n,
        "mean": round(mean, 6),
        "sd": round(sd, 6),
        "se": round(sd / math.sqrt(n), 6) if n else None,
        "ci95": [round(lo, 6), round(hi, 6)],
        "min": round(min(values), 6) if n else None,
        "max": round(max(values), 6) if n else None,
    }


def run_sweep(conditions: tuple[str, ...], seeds: list[int], db_root: Path,
              days: int, workers: int) -> list[dict]:
    import multiprocessing as mp

    jobs = [(c, s, db_root, days) for c in conditions for s in seeds]
    if workers <= 1:
        results = [_run_one(*j) for j in jobs]
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(workers) as pool:
            results = pool.starmap(_run_one, jobs)
    results.sort(key=lambda r: (r["condition"], r["seed"]))
    return results


def analyze(results: list[dict], conditions: tuple[str, ...]) -> dict:
    """Per-arm stats + paired FULL−SNS delta (absolute and relative)."""
    arms: dict[str, dict] = {}
    for cond in conditions:
        vals = [r["n_proactive"] for r in results if r["condition"] == cond]
        arms[cond] = {"n_proactive": _stats(vals),
                      "per_seed": {str(r["seed"]): r["n_proactive"]
                                   for r in results
                                   if r["condition"] == cond}}
    full, sns = conditions[0], conditions[1]
    fv = [r["n_proactive"] for r in results if r["condition"] == full]
    sv = [r["n_proactive"] for r in results if r["condition"] == sns]
    pairs = sorted(zip(fv, sv))
    diffs = [f - s for f, s in pairs]
    ratios = [f / s - 1.0 for f, s in pairs if s > 0]
    delta_abs = statistics.fmean(diffs)
    delta_rel_mean = statistics.fmean(fv) / statistics.fmean(sv) - 1.0
    delta_abs_stats = _stats(diffs)
    delta_rel_stats = _stats(ratios)

    lo_a, hi_a = delta_abs_stats["ci95"]
    lo_r, hi_r = delta_rel_stats["ci95"]
    target = 0.10
    if lo_a > 0.0 and lo_r <= target <= hi_r:
        verdict = "confirmed"
    elif not (lo_r <= target <= hi_r):
        verdict = "refuted"
    else:
        verdict = "at-margin"

    return {
        "arms": arms,
        "delta_abs": {**delta_abs_stats, "mean": round(delta_abs, 6)},
        "delta_rel_mean": round(delta_rel_mean, 6),
        "delta_rel_paired": delta_rel_stats,
        "prediction": {"target_relative": target,
                       "basis": "stored-run S_d mean 1.3123 (G0 rerun 2026-08-14)"},
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed-base", type=int, default=SEED_BASE_DEFAULT)
    ap.add_argument("--seeds", type=int, default=500)
    ap.add_argument("--days", type=int, default=DAYS_DEFAULT)
    ap.add_argument("--conditions", type=str, default=",".join(CONDITIONS_DEFAULT))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--smoke", action="store_true",
                    help="5 seeds only (sanity + timing before the sweep)")
    ap.add_argument("--db-root", type=str, default=None,
                    help="where cell DBs go (default: fresh temp dir)")
    ap.add_argument("--out", type=str,
                    default="results/tier1-masking-wsa-remeasure-2026-08-15.json")
    ap.add_argument("--md", type=str,
                    default="results/tier1-masking-wsa-remeasure-2026-08-15.md")
    args = ap.parse_args(argv)

    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    if len(conditions) != 2:
        print("need exactly two conditions (delta is paired FULL minus SNS)",
              file=sys.stderr)
        return 2
    n_seeds = 5 if args.smoke else args.seeds
    seeds = list(range(args.seed_base, args.seed_base + n_seeds))
    db_root = Path(args.db_root) if args.db_root else Path(
        tempfile.mkdtemp(prefix="wsa-remeasure-"))
    db_root.mkdir(parents=True, exist_ok=True)

    print(f"[wsa] {len(conditions)} conditions x {n_seeds} seeds x {args.days} "
          f"days, fake client, workers={args.workers}, db_root={db_root}",
          flush=True)
    t0 = time.time()
    results = run_sweep(conditions, seeds, db_root, args.days, args.workers)
    dt = time.time() - t0
    analysis = analyze(results, conditions)

    report = {
        "record": "tier1-masking-wsa-remeasure-2026-08-15",
        "date": "2026-08-15",
        "branch": "wip/tier1-masking",
        "head": None,  # filled by the caller if available
        "path": "fixed day-0 plan (ensure_day(0) + day_scores(store,0,timing)); "
                "pre-fix was plan_and_persist(scores=None)",
        "model_free": True,
        "fake_client": "experiments.cvs_common.DeterministicClient (scripted "
                       "replies, seeded)",
        "judge": "experiments.cvs_common.DeterministicJudge (scripted, "
                 "perturbation-aware)",
        "engine_path": "real AsyncRuntime + FakeChannel -> "
                       "engine.timing.next_event (untouched)",
        "driver": "experiments/tier1_wsa_remeasure.py",
        "conditions": list(conditions),
        "seeds": seeds,
        "seed_base": args.seed_base,
        "n_seeds": n_seeds,
        "days": args.days,
        "checkpoints": [7, 14, 21, 26, 29],
        "perturb": True,
        "memory_policy": "default (None -> structured_memory)",
        "elapsed_s": round(dt, 2),
        "workers": args.workers,
        "per_cell": results,
        "analysis": analysis,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md_path = REPO_ROOT / args.md
    md_path.write_text(_render_md(report), encoding="utf-8")

    a = analysis
    print(json.dumps({
        "elapsed_s": report["elapsed_s"],
        "FULL": a["arms"][conditions[0]]["n_proactive"],
        "SNS": a["arms"][conditions[1]]["n_proactive"],
        "delta_abs": a["delta_abs"],
        "delta_rel_mean": a["delta_rel_mean"],
        "delta_rel_paired": a["delta_rel_paired"],
        "verdict": a["verdict"],
    }, indent=2))
    print(f"\nwrote {out}\nwrote {md_path}")
    return 0


def _render_md(report: dict) -> str:
    a = report["analysis"]
    c = report["conditions"]
    f, s = a["arms"][c[0]]["n_proactive"], a["arms"][c[1]]["n_proactive"]
    da, dr = a["delta_abs"], a["delta_rel_paired"]
    lines = [
        f"# WS-A re-measure: FULL vs STRUCTURED_NO_STATE (model-free, n={report['n_seeds']})",
        "",
        f"- Date: {report['date']} — branch {report['branch']} (fixed day-0 plan path)",
        f"- Driver: `{report['driver']}` → `experiments.cvs_common.run_cell` (fake client, perturb=True, "
        f"{report['days']} days, checkpoints {report['checkpoints']}, default memory policy)",
        f"- Model-free: deterministic fake client + scripted judge; counts flow through the real "
        f"AsyncRuntime → engine.timing.next_event path; engine/ and harness/ untouched.",
        f"- Seeds: {report['seed_base']}..{report['seed_base'] + report['n_seeds'] - 1} "
        f"(same base as the G0 500-seed rerun); paired by seed.",
        f"- Elapsed: {report['elapsed_s']} s (workers={report['workers']}).",
        "",
        "## Per-arm proactive counts (per 30-day run)",
        "",
        "| Arm | mean | sd | 95% CI | min | max |",
        "|---|---|---|---|---|---|",
        f"| {c[0]} | {f['mean']} | {f['sd']} | {f['ci95'][0]} – {f['ci95'][1]} | {f['min']} | {f['max']} |",
        f"| {c[1]} | {s['mean']} | {s['sd']} | {s['ci95'][0]} – {s['ci95'][1]} | {s['min']} | {s['max']} |",
        "",
        "## Delta (FULL − SNS, paired by seed)",
        "",
        f"- Absolute: mean {a['delta_abs']['mean']} proactives, 95% CI "
        f"[{a['delta_abs']['ci95'][0]}, {a['delta_abs']['ci95'][1]}]",
        f"- Relative (mean-based): {a['delta_rel_mean'] * 100:.2f}%",
        f"- Relative (paired ratios): mean {a['delta_rel_paired']['mean'] * 100:.2f}%, "
        f"95% CI [{a['delta_rel_paired']['ci95'][0] * 100:.2f}%, "
        f"{a['delta_rel_paired']['ci95'][1] * 100:.2f}%]",
        "",
        "## Verdict on the ≈ +10% prediction",
        "",
        f"- Prediction: FULL exceeds SNS by ~+10% (basis: stored-run S_d mean 1.3123, G0 rerun).",
        f"- Verdict: **{a['verdict'].upper()}** "
        f"(target 10% ∈ paired-ratio CI: {'yes' if a['delta_rel_paired']['ci95'][0] <= 0.10 <= a['delta_rel_paired']['ci95'][1] else 'no'}; "
        f"delta>0 at 95%: {'yes' if a['delta_abs']['ci95'][0] > 0 else 'no'}).",
        "",
        "## Model-free note",
        "",
        "All counts come from the deterministic fake client path (`run_cell(fake=True)`, "
        "`DeterministicClient` + `DeterministicJudge`). These are NOT LLM outputs; they measure "
        "the engine/scheduler behavior (state → plan → proactive firing) under each condition. "
        "The it3 free-check showed fake-client proactive counts match real-cell counts "
        "(FULL=48, SNS=45, NTF=54 at seeds 5001–5005). The day-0 plan here goes through the "
        "FIXED path: `session.ensure_day(0)` then `day_scores(store, 0, timing)` (commit 772b0f0); "
        "the pre-fix path planned day 0 with `scores=None` (neutral, all arms identical on day 0).",
        "",
        "Full per-seed data: `tier1-masking-wsa-remeasure-2026-08-15.json`.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
