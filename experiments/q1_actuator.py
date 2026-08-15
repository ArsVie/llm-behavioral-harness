"""Q1 actuator experiment — RE-DERIVED from the lost original (G0 record).

STATUS: The orchestrator brief referenced ``scratchpad/q1_actuator.py``; that
file is LOST (verified absent from the repo, all worktrees, /tmp, /mnt/c, git
history, and session history on 2026-08-14). This module RE-DERIVES the
experiment from the brief's design spec. The brief's cited numbers are treated
as HYPOTHESES to confirm, not ground truth: if the re-run reproduces them, this
is the record; if not, the corrected numbers ARE the record (and the brief's
prose gets corrected).

Design (from the brief):
  Modulator = envelope(t % 24) x S_d, with S_d an imposed constant and
  phase/adj held at 1.0 (isolates S_d). mod_ub = S_d. Guards from TimingParams
  (min_gap 15 min, daily_cap 3, max_gap 48 h), mirrored in the loop; a skipped
  candidate does not advance t_last (silence keeps growing).

  Exp-1:  sweep S_d in {0.5, 0.75, 1.0, 1.25, 1.5, 2.0}, n=500 seeds, 30 days.
          Mean count +- 95% CI per arm; elasticity =
          (count@2.0 - count@0.5) / count@1.0.
  Exp-2a: same sweep, guards OFF (isolates whether the guards absorb it).
  Exp-3:  real S_d distribution, two ways:
            fresh mood engine: cycle.step -> mood.step -> derive_behavior ->
                state_vector -> clip(exp(w.(x-x0)), 0.5, 2) at the sample hour
            stored FULL run: committed it3-g5-matrix full/seed5001..5005 dbs
                via harness.scheduler.state_factor (load-bearing, headline)
          Fresh-vs-stored gap expected (momentum/previous-day + trajectory).
  Envelope floor/ceiling (15 / 90) reasoned ANALYTICALLY from the guards
  (max_gap 48h => at least ~15/30d; daily_cap 3 => at most 90/30d), NOT
  re-simulated at pathological S_d.

Floor: engine/ untouched (frozen engine.timing.next_event is driven read-only
through sim/run_events.run, the production thinning wrapper); harness/ is
read-only here; model-free; deterministic given seed.

Run:  .venv/bin/python experiments/q1_actuator.py [--seeds N] [--days D]
      [--smoke]  (--smoke = 5 seeds, quick sanity before the full sweep)
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from engine.types import (
    DEFAULT_PHASE_MULTIPLIERS,
    MoodVariant,
    PersonaParams,
    TimingParams,
)
from harness.behavior import derive_behavior
from harness.scheduler import (
    STATE_FACTOR_BOUNDS,
    STATE_NEUTRAL,
    STATE_WEIGHTS,
    state_factor,
)
from sim import run_daily, run_events

#: Sweep arms (S_d values), per brief.
S_D_SWEEP: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
#: Sample hour for the per-day state vector (diurnal peak, scheduler default).
SAMPLE_HOUR = 14.0
#: Seed convention: matrix seeds 5001..5005; sweep uses the same family.
SWEEP_SEED_BASE = 5001
#: Committed FULL-run dbs at base 653de09 (it3-g5-matrix).
FULL_DB_REL = "results/it3-g5-matrix/full/seed{seed}/cell_full_seed{seed}.db"
FULL_SEEDS = (5001, 5002, 5003, 5004, 5005)
REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Timing construction
# --------------------------------------------------------------------------- #

def _isolated_timing(guards: bool) -> TimingParams:
    """TimingParams with phase/adj pinned to 1.0 (isolates S_d).

    ``phase_multipliers`` all 1.0 and ``adj_bounds`` (1.0, 1.0) force the
    modulator composition envelope x phase x adj x state to collapse to
    envelope x S_d when state_factors = S_d const. With ``guards=False`` the
    three queue guards are relaxed (min_gap -> 0, daily_cap -> huge,
    max_gap -> huge) through the SAME production path (run_events.run).
    """
    return TimingParams(
        phase_multipliers={k: 1.0 for k in DEFAULT_PHASE_MULTIPLIERS},
        adj_bounds=(1.0, 1.0),
        min_gap_min=0.0 if not guards else 15.0,
        daily_cap=10**9 if not guards else 3,
        max_gap_h=10**9 if not guards else 48.0,
    )


def count_events(seed: int, days: int, s_d: float, guards: bool) -> int:
    """Accepted proactive-event count over `days` days at constant S_d.

    Drives the production thinning path (sim/run_events.run -> frozen
    engine.timing.next_event) with the modulator collapsed to
    envelope(t % 24) x S_d (phase/adj pinned to 1.0; mod_ub = S_d via the
    state_factors max). Guards on/off per the brief.
    """
    timing = _isolated_timing(guards)
    events = run_events.run(
        days,
        seed,
        persona=PersonaParams(),
        timing=timing,
        scores=None,
        state_factors=np.full(days, s_d, dtype=float),
    )
    return int(len(events))


def _arm_stats(counts: np.ndarray) -> dict:
    n = len(counts)
    mean = float(np.mean(counts))
    sd = float(np.std(counts, ddof=1)) if n > 1 else 0.0
    ci = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": round(mean, 4), "sd": round(sd, 4),
            "ci95": round(ci, 4), "min": int(np.min(counts)),
            "max": int(np.max(counts))}


def _elasticity(counts_by_sd: dict[float, np.ndarray]) -> float:
    c_lo = float(np.mean(counts_by_sd[0.5]))
    c_hi = float(np.mean(counts_by_sd[2.0]))
    c_ref = float(np.mean(counts_by_sd[1.0]))
    return (c_hi - c_lo) / c_ref


def sweep(seeds: list[int], days: int, guards: bool) -> dict:
    """Exp-1 / Exp-2a: per-arm stats + elasticity for one guard mode."""
    counts_by_sd: dict[float, np.ndarray] = {}
    for s_d in S_D_SWEEP:
        counts_by_sd[s_d] = np.asarray(
            [count_events(s, days, s_d, guards) for s in seeds], dtype=float
        )
    return {
        "guards": guards,
        "days": days,
        "n_seeds": len(seeds),
        "arms": {str(s_d): _arm_stats(counts_by_sd[s_d]) for s_d in S_D_SWEEP},
        "elasticity": round(_elasticity(counts_by_sd), 4),
        "count_at_0.5": round(float(np.mean(counts_by_sd[0.5])), 2),
        "count_at_2.0": round(float(np.mean(counts_by_sd[2.0])), 2),
        "count_at_1.0": round(float(np.mean(counts_by_sd[1.0])), 2),
    }


# --------------------------------------------------------------------------- #
# Exp-3: real S_d distribution
# --------------------------------------------------------------------------- #

def _factor_from_directive(directive) -> float:
    """clip(exp(w.(x - x0)), 0.5, 2) from a BehaviorDirective (same formula
    as harness.scheduler.state_factor, computed from an in-memory directive
    instead of a store row)."""
    x = (directive.energy, directive.initiative,
         directive.valence, directive.reactivity)
    exponent = sum(w * (xi - xn)
                   for w, xi, xn in zip(STATE_WEIGHTS, x, STATE_NEUTRAL))
    return float(np.clip(np.exp(exponent), *STATE_FACTOR_BOUNDS))


def exp3_fresh(seeds: list[int], days: int) -> dict:
    """Fresh mood engine: cycle.step -> mood.step -> derive_behavior ->
    state_vector -> clip(exp(w.(x-x0)), 0.5, 2) at the sample hour."""
    timing = TimingParams()
    factors: list[float] = []
    for seed in seeds:
        sim = run_daily.run(days, seed, MoodVariant.DECOUPLED_OFFSETS)
        recs = sim.records
        for i, rec in enumerate(recs):
            prev = recs[i - 1] if i > 0 else None
            directive = derive_behavior(rec, timing, hour=SAMPLE_HOUR,
                                        previous=prev)
            factors.append(_factor_from_directive(directive))
    arr = np.asarray(factors, dtype=float)
    return {
        "method": "fresh-engine (cycle.step -> mood.step -> derive_behavior)",
        "n": int(len(arr)),
        "days": days,
        "seeds": seeds,
        "mean": round(float(np.mean(arr)), 4),
        "sd": round(float(np.std(arr, ddof=1)), 4),
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
    }


def _extract_committed_db(seed: int, tmpdir: Path) -> Path:
    """Extract the COMMITTED FULL-run cell db (base 653de09) to a temp file,
    so the record reads the exact committed artifact, not the possibly-
    modified working-tree copy."""
    rel = FULL_DB_REL.format(seed=seed)
    out = tmpdir / f"cell_full_seed{seed}.db"
    r = subprocess.run(
        ["git", "show", f"653de09:{rel}"],
        capture_output=True,
        cwd=REPO_ROOT,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git show {rel}: {r.stderr.decode()[:200]}")
    out.write_bytes(r.stdout)
    return out


def exp3_stored(tmpdir: Path) -> dict:
    """Stored FULL run: per-day state_factor via harness.scheduler.state_factor
    over the committed it3-g5-matrix full/seed5001..5005 dbs (LOAD-BEARING)."""
    timing = TimingParams()
    factors: list[float] = []
    per_seed: dict[int, dict] = {}
    for seed in FULL_SEEDS:
        db = _extract_committed_db(seed, tmpdir)
        con = sqlite3.connect(db)
        days = [r[0] for r in con.execute("SELECT day FROM daily_state")]
        con.close()
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from harness.store import SQLiteStore
        store = SQLiteStore(db)
        seed_factors = [state_factor(store, d, timing) for d in days]
        per_seed[seed] = {
            "n_days": len(days),
            "mean": round(float(np.mean(seed_factors)), 4),
            "min": round(float(np.min(seed_factors)), 4),
            "max": round(float(np.max(seed_factors)), 4),
        }
        factors.extend(seed_factors)
    arr = np.asarray(factors, dtype=float)
    return {
        "method": "stored FULL run (committed 653de09 it3-g5-matrix full/seed5001..5005)",
        "n": int(len(arr)),
        "mean": round(float(np.mean(arr)), 4),
        "sd": round(float(np.std(arr, ddof=1)), 4),
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
        "per_seed": per_seed,
    }


# --------------------------------------------------------------------------- #
# Envelope (analytic, per brief — NOT simulated)
# --------------------------------------------------------------------------- #

def analytic_envelope(days: int = 30) -> dict:
    """Envelope floor/ceiling reasoned from the queue guards:
    max_gap_h 48h forces at least one contact per 48h window (=> ~days/2
    events at minimum); daily_cap 3 caps at 3 per day (=> 3*days at most).
    Analytic bounds, deliberately not re-simulated at pathological S_d.
    """
    return {
        "floor_analytic": max(1, days // 2),
        "ceiling_analytic": 3 * days,
        "basis": "max_gap_h=48h => ~1 per 2 days floor; daily_cap=3 => 3/day ceiling",
        "note": "analytic, not simulated (brief: nobody re-trips the slow path)",
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=500)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--smoke", action="store_true",
                    help="5 seeds only (quick sanity before the full sweep)")
    args = ap.parse_args(argv)

    n_seeds = 5 if args.smoke else args.seeds
    seeds = list(range(SWEEP_SEED_BASE, SWEEP_SEED_BASE + n_seeds))

    report: dict = {
        "record": "q1-actuator-rerun (RE-DERIVED, original lost)",
        "date": "2026-08-14",
        "n_seeds": n_seeds,
        "days": args.days,
        "s_d_sweep": list(S_D_SWEEP),
        "sample_hour": SAMPLE_HOUR,
        "elasticity_formula": "(count@2.0 - count@0.5) / count@1.0",
        "exp1_guards_on": sweep(seeds, args.days, guards=True),
        "exp2a_guards_off": sweep(seeds, args.days, guards=False),
        "exp3": {
            "fresh": exp3_fresh(seeds, 90),
            "stored": exp3_stored(Path(tempfile.mkdtemp(prefix="q1-stored-"))),
        },
        "envelope_analytic": analytic_envelope(args.days),
    }

    # Headline comparison vs the brief's cited claims.
    e_on = report["exp1_guards_on"]
    e_off = report["exp2a_guards_off"]
    s = report["exp3"]["stored"]
    f = report["exp3"]["fresh"]
    report["verification"] = {
        "elasticity_on": e_on["elasticity"],
        "elasticity_off": e_off["elasticity"],
        "guards_on_vs_off": round(e_on["elasticity"] - e_off["elasticity"], 4),
        "stored_mean": s["mean"],
        "stored_range": [s["min"], s["max"]],
        "fresh_mean": f["mean"],
        "fresh_vs_stored": round(f["mean"] - s["mean"], 4),
        "claims": {
            "elasticity_about_plus63pct": "REPRODUCED"
            if abs(e_on["elasticity"] - 0.63) < 0.10 else "CORRECTED",
            "stored_mean_about_1.28": "REPRODUCED"
            if abs(s["mean"] - 1.28) < 0.06 else "CORRECTED",
            "stored_range_0.56_2.0": "REPRODUCED"
            if abs(s["min"] - 0.56) < 0.05 and abs(s["max"] - 2.0) < 0.01
            else "CORRECTED",
        },
    }

    print(json.dumps(report, indent=2))
    out = REPO_ROOT / "results" / "q1-actuator-rerun-2026-08-14.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
