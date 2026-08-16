"""P6 stats — consolidate judged JSONLs into the gate record (G-ABS, G-BEH).

Reads the judged per-band files produced by p6_judge.py and writes
diagnostics/p6-eval.json with, per the pre-registration
(docs/exp-affect-codebook-spike2-2026-08-16.md, Gates table):

  G-ABS (H3, support):  codebook generations judge-classified >= 0.60
                        (3-way, chance 0.33), 95% CI excludes chance,
                        K >= 30/band.
  G-BEH (H4, PRIMARY):  on the LARGEST local actor (qwen8b), codebook beats
                        the current 48-state renderer on judge separability
                        by DeltaAcc >= +0.10, 95% CI on the difference
                        excludes 0 (paired, same contexts/levels).

All CIs are seeded percentile bootstraps (10k resamples,
derive_seed(MASTER_SEED, 12, ...) — p6_common.bootstrap_ci). K<30 bands are
reported with a K-FAIL gate verdict (as the K=2 selftest expects) — the
plumbing verdicts are still computed and reported.

Usage:
  python scripts/p6_stats.py                 # all actors
  python scripts/p6_stats.py --actor qwen8b  # single actor
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from p6_common import (
    ACTOR_NAMES,
    BAND_ORDER,
    JUDGE_FOR,
    MODELS,
    SEED_KEY,
    SPIKE_ROOT,
    bootstrap_ci,
    derive_seed,
    judged_path,
    load_jsonl,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)  # unbuffered stdout

G_ABS_MIN_ACC = 0.60
G_ABS_CHANCE = 0.33
G_ABS_MIN_K = 30
G_BEH_MIN_DELTA = 0.10
BOOT_N = 10000
OUT = SPIKE_ROOT / "diagnostics" / "p6-eval.json"


def band_accuracy(rows: list[dict], seed: int) -> dict:
    """3-way accuracy + seeded 95% CI over the per-reply correctness."""
    accs = [1.0 if r["correct"] else 0.0 for r in rows]
    mean, lo, hi = bootstrap_ci(accs, seed)
    return {
        "n": len(accs),
        "acc": mean,
        "ci95": [lo, hi],
        "ci_excludes_chance": bool(lo > G_ABS_CHANCE or hi < G_ABS_CHANCE),
        "bootstrap_seed": seed,
    }


def gabs_verdict(stat: dict) -> dict:
    return {
        "gate": "G-ABS (H3)",
        "condition": "acc >= 0.60 AND 95% CI excludes chance (0.33) AND K>=30/band",
        "acc_ok": stat["acc"] >= G_ABS_MIN_ACC,
        "ci_ok": stat["ci_excludes_chance"],
        "k_ok": stat["n"] >= G_ABS_MIN_K,
        "pass": (stat["acc"] >= G_ABS_MIN_ACC and stat["ci_excludes_chance"]
                 and stat["n"] >= G_ABS_MIN_K),
    }


def pair_deltas(actor: str, judge: str, band: str) -> tuple[list[float], dict]:
    """Paired per-pair deltas (codebook_correct - renderer_correct) for one
    band. Pairing = same band, same sample index (seeds shared across
    variants by construction)."""
    r_rows = load_jsonl(judged_path(actor, judge, "renderer", band))
    c_rows = load_jsonl(judged_path(actor, judge, "codebook", band))
    r_by_id = {r["id"]: r for r in r_rows}
    c_by_id = {r["id"]: r for r in c_rows}
    common = sorted(set(r_by_id) & set(c_by_id))
    if len(common) == 0:
        return [], {"band": band, "n_pairs": 0, "note": "no paired judgments"}
    d = [float(c_by_id[i]["correct"]) - float(r_by_id[i]["correct"])
         for i in common]
    acc_r = float(np.mean([r_by_id[i]["correct"] for i in common]))
    acc_c = float(np.mean([c_by_id[i]["correct"] for i in common]))
    return d, {
        "band": band,
        "n_pairs": len(common),
        "acc_renderer": acc_r,
        "acc_codebook": acc_c,
        "delta_acc": acc_c - acc_r,
    }


def paired_delta(actor: str, judge: str, band: str) -> dict:
    """Per-band paired statistic with its seeded 95% bootstrap CI."""
    d, base = pair_deltas(actor, judge, band)
    if not d:
        return base
    mean, lo, hi = bootstrap_ci(
        d, derive_seed(SEED_KEY["boot"], 1, tuple(MODELS).index(actor),
                       BAND_ORDER.index(band)))
    base.update({
        "paired_delta_mean": mean,
        "ci95": [lo, hi],
        "ci_excludes_zero": bool(lo > 0 or hi < 0),
        "bootstrap": {"n": BOOT_N,
                      "seed": derive_seed(SEED_KEY["boot"], 1,
                                          tuple(MODELS).index(actor),
                                          BAND_ORDER.index(band)),
                      "method": "percentile 2.5/97.5 of resampled pair means"},
    })
    return base


def actor_record(actor: str) -> dict:
    judge = JUDGE_FOR[actor]
    actor_idx = tuple(MODELS).index(actor)
    bands: dict[str, dict] = {}
    for band in BAND_ORDER:
        band_idx = BAND_ORDER.index(band)
        c_rows = load_jsonl(judged_path(actor, judge, "codebook", band))
        r_rows = load_jsonl(judged_path(actor, judge, "renderer", band))
        bands[band] = {
            "judge": judge,
            "codebook": band_accuracy(
                c_rows, derive_seed(SEED_KEY["boot"], 0, actor_idx, band_idx)),
            "renderer": band_accuracy(
                r_rows, derive_seed(SEED_KEY["boot"], 0, actor_idx, band_idx)),
            "gabs": gabs_verdict(band_accuracy(
                c_rows, derive_seed(SEED_KEY["boot"], 0, actor_idx, band_idx))),
            "gbeh_paired": paired_delta(actor, judge, band),
        }
    # pooled (across bands) codebook accuracy for the overall G-ABS read
    all_c = [r for band in BAND_ORDER
             for r in load_jsonl(judged_path(actor, judge, "codebook", band))]
    all_r = [r for band in BAND_ORDER
             for r in load_jsonl(judged_path(actor, judge, "renderer", band))]
    pooled_seed = derive_seed(SEED_KEY["boot"], 0, actor_idx, 99)
    pooled = {
        "codebook": band_accuracy(all_c, pooled_seed),
        "renderer": band_accuracy(all_r, pooled_seed),
        "gabs": gabs_verdict(band_accuracy(all_c, pooled_seed)),
    }
    # G-BEH verdict: per-band + pooled over ALL paired deltas (bootstrap over
    # the pooled pair-level deltas, not over band means). PRIMARY gate = the
    # largest local actor (qwen8b).
    deltas = [bands[b]["gbeh_paired"] for b in BAND_ORDER]
    all_d: list[float] = []
    for b in BAND_ORDER:
        d, _ = pair_deltas(actor, judge, b)
        all_d.extend(d)
    if all_d:
        mean, lo, hi = bootstrap_ci(
            all_d, derive_seed(SEED_KEY["boot"], 2,
                               tuple(MODELS).index(actor)))
        gbeh_pooled = {
            "method": "bootstrap over pooled pair-level deltas (all bands)",
            "n_pairs": len(all_d),
            "delta_acc_mean": mean,
            "ci95": [lo, hi],
            "ci_excludes_zero": bool(lo > 0 or hi < 0),
        }
        gbeh_pass = (mean >= G_BEH_MIN_DELTA and (lo > 0 or hi < 0))
    else:
        gbeh_pooled = {"note": "no paired judgments"}
        gbeh_pass = False
    return {
        "actor": actor,
        "model": MODELS[actor]["id"],
        "revision": MODELS[actor]["revision"],
        "judge": judge,
        "bands": bands,
        "pooled": pooled,
        "gbeh": {
            "gate": "G-BEH (H4, PRIMARY)",
            "condition": ("DeltaAcc >= +0.10 AND 95% CI on the paired "
                          "difference excludes 0"),
            "per_band": deltas,
            "pooled": gbeh_pooled,
            "pass": gbeh_pass,
        },
        "largest_actor": actor == "qwen8b",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--actor", choices=ACTOR_NAMES, help="default: all actors")
    args = ap.parse_args()
    actors = [args.actor] if args.actor else list(ACTOR_NAMES)

    out = {
        "phase": "P6",
        "contract": "experiments/emotion-codebook-spike2/docs/"
                    "exp-affect-codebook-spike2-2026-08-16.md",
        "gates": {
            "G-ABS": "codebook generations judge-classified >= 0.60 (3-way, "
                     "chance 0.33), CI excludes chance, K>=30/band",
            "G-BEH": ("PRIMARY: on the largest local actor (qwen8b), codebook "
                      "beats the current 48-state renderer on judge "
                      "separability by DeltaAcc >= +0.10, 95% CI on the "
                      "difference excludes 0 (paired, same contexts/levels)"),
        },
        "chance": G_ABS_CHANCE,
        "bootstrap": {"n": BOOT_N,
                      "seed_keys": {"abs": derive_seed(SEED_KEY["boot"], 0, 0),
                                    "beh": derive_seed(SEED_KEY["boot"], 1, 0, 0)},
                      "method": "percentile 2.5/97.5, seeded"},
        "actors": {a: actor_record(a) for a in actors},
    }
    out["verdicts"] = {
        "gabs_per_actor": {a: {
            "pooled": out["actors"][a]["pooled"]["gabs"]["pass"],
            "per_band": {b: out["actors"][a]["bands"][b]["gabs"]["pass"]
                         for b in BAND_ORDER},
        } for a in actors},
        "gbeh_primary": out["actors"]["qwen8b"]["gbeh"]["pass"]
        if "qwen8b" in actors else None,
    }
    OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    for a in actors:
        rec = out["actors"][a]
        print(f"[{a}] judge={rec['judge']}")
        for b in BAND_ORDER:
            cb = rec["bands"][b]["codebook"]
            rb = rec["bands"][b]["renderer"]
            gv = rec["bands"][b]["gabs"]
            print(f"  {b}: codebook n={cb['n']} acc={cb['acc']:.3f} "
                  f"CI=[{cb['ci95'][0]:.3f},{cb['ci95'][1]:.3f}] "
                  f"G-ABS={'PASS' if gv['pass'] else 'FAIL'} "
                  f"(k_ok={gv['k_ok']}, acc_ok={gv['acc_ok']}, "
                  f"ci_ok={gv['ci_ok']}) | renderer acc={rb['acc']:.3f}")
        pl = rec["pooled"]["gabs"]
        print(f"  pooled codebook: n={rec['pooled']['codebook']['n']} "
              f"acc={rec['pooled']['codebook']['acc']:.3f} "
              f"CI=[{rec['pooled']['codebook']['ci95'][0]:.3f},"
              f"{rec['pooled']['codebook']['ci95'][1]:.3f}] "
              f"G-ABS={'PASS' if pl['pass'] else 'FAIL'}")
        gh = rec["gbeh"]
        print(f"  G-BEH: {'PASS' if gh['pass'] else 'FAIL'} "
              f"(pooled delta={gh['pooled'].get('delta_acc_mean', float('nan')):.3f} "
              f"CI={gh['pooled'].get('ci95')})")
    print(f"wrote -> {OUT.relative_to(SPIKE_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
