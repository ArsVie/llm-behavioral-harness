"""P1 — G-DATA pre-flight gate evidence for the spike 2 arousal reference.

Pre-registered: docs/exp-affect-codebook-spike2-2026-08-16.md, Orchestrator
decisions §7: "surviving separation = mean |intensity(hi) - intensity(lo)| over
contrast groups passing the >= 0.30 intensity-gap filter on the TRAIN split;
reported with n groups, distribution, bins coverage."

This script RE-DERIVES everything from the built files
(data/stimuli/{train,heldout}.jsonl) — no build-state reuse. It computes:

- n contrast groups and n rows (arousal axis, per split)
- per-group separation = mean(hi intensity) - mean(lo intensity) within the
  group (pair structure: i-th hi row by id pairs with i-th lo row by id)
- surviving separation stats on TRAIN: mean / median / min / max
- distribution: 10 decile-width histogram bins over the observed train
  separation range + decile quantiles (p10..p90)
- pair-level filter pass counts (pairs with |hi-lo| >= 0.30 vs dropped),
  re-derived from the files, plus build-time drop counts from stats.json
- 10-bin intensity coverage per split (bin = min(9, int(intensity*10)))
- VERDICT PASS/FAIL: PASS iff train n_groups > 0 and mean surviving
  separation >= 0.30. The gate is fixed — no weakening on FAIL.

Writes diagnostics/gdata-arousal.json.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

import numpy as np

OUT = SPIKE_ROOT / "data" / "stimuli"
DIAG = SPIKE_ROOT / "diagnostics"
GATE_MIN_SEP = 0.30


def load_rows(split: str) -> list[dict]:
    rows = []
    with open(OUT / f"{split}.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["axis"] == "arousal":
                rows.append(r)
    return rows


def group_separations(rows: list[dict]) -> tuple[list[float], list[float], dict]:
    """Per-group mean hi-lo separation + per-pair separations.

    Pair structure: within a group, the i-th hi row by id pairs with the
    i-th lo row by id (same convention as spike 1).
    """
    grp: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grp[r["contrast_group"].rsplit(":", 1)[0]].append(r)
    sep_groups: list[float] = []
    sep_pairs: list[float] = []
    n_pairs = 0
    pairs_below = 0
    for gid in sorted(grp):
        grows = grp[gid]
        hi = sorted([r for r in grows if r["contrast_group"].endswith(":hi")],
                    key=lambda r: r["id"])
        lo = sorted([r for r in grows if r["contrast_group"].endswith(":lo")],
                    key=lambda r: r["id"])
        assert len(hi) == len(lo), f"group {gid} unbalanced"
        pair_seps = [h["intensity"] - l["intensity"] for h, l in zip(hi, lo)]
        sep_groups.append(float(np.mean(pair_seps)))
        sep_pairs.extend(pair_seps)
        n_pairs += len(pair_seps)
        pairs_below += sum(1 for s in pair_seps if s < GATE_MIN_SEP - 1e-9)
    return sep_groups, sep_pairs, {"n_groups": len(sep_groups), "n_pairs": n_pairs,
                                   "pairs_below_filter": pairs_below}


def bin_coverage(rows: list[dict]) -> dict:
    bins = defaultdict(int)
    for r in rows:
        bins[min(9, int(r["intensity"] * 10))] += 1
    return {str(b): bins.get(b, 0) for b in range(10)}


def decile_histogram(values: list[float], nbins: int = 10) -> dict:
    """Equal-width histogram over [min, max] with nbins decile-width bins."""
    lo, hi = min(values), max(values)
    width = (hi - lo) / nbins
    edges = [lo + i * width for i in range(nbins + 1)]
    counts = [0] * nbins
    for v in values:
        idx = min(nbins - 1, int((v - lo) / width)) if width > 0 else 0
        counts[idx] += 1
    return {
        "bin_width": round(width, 6),
        "bins": [
            {"range": f"[{edges[i]:.4f},{edges[i + 1]:.4f})", "count": counts[i]}
            for i in range(nbins)
        ],
    }


def main() -> None:
    DIAG.mkdir(parents=True, exist_ok=True)

    rows = {split: load_rows(split) for split in ["train", "heldout"]}
    seps = {}
    pairs = {}
    for split in ["train", "heldout"]:
        g, p, info = group_separations(rows[split])
        seps[split] = g
        pairs[split] = p
        print(f"[gdata] {split}: {info['n_groups']} groups, {info['n_pairs']} pairs, "
              f"{info['pairs_below_filter']} pairs below filter")

    train_seps = seps["train"]
    train_pairs = pairs["train"]
    ev = {
        "spike": "emotion-codebook-spike2",
        "gate": "G-DATA",
        "definition": ("surviving separation = mean |intensity(hi) - intensity(lo)| over "
                       "contrast groups passing the >= 0.30 intensity-gap filter, measured on "
                       "TRAIN; pair filter applied at build time (MIN_PAIR_SEP=0.30 in "
                       "scripts/build_stimuli.py); group separation = mean(hi) - mean(lo) "
                       "within the group, pair structure i-th hi by id with i-th lo by id "
                       "(spike 1 convention)."),
        "threshold": GATE_MIN_SEP,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "computed_from": "data/stimuli/{train,heldout}.jsonl (re-derived, no build-state reuse)",
    }

    # ---- train (the gate split) ----
    ev["train"] = {
        "n_groups": len(train_seps),
        "n_rows": len(rows["train"]),
        "n_pairs": len(train_pairs),
        "separation": {
            "mean": round(float(np.mean(train_seps)), 4),
            "median": round(float(np.median(train_seps)), 4),
            "min": round(float(np.min(train_seps)), 4),
            "max": round(float(np.max(train_seps)), 4),
        },
        "pair_separation": {
            "mean": round(float(np.mean(train_pairs)), 4),
            "median": round(float(np.median(train_pairs)), 4),
            "min": round(float(np.min(train_pairs)), 4),
            "max": round(float(np.max(train_pairs)), 4),
        },
        "deciles": {f"p{i * 10}": round(float(np.percentile(train_seps, i * 10)), 4)
                    for i in range(1, 10)},
        "histogram_deciles": decile_histogram(train_seps),
        "filter_pass": {
            "pairs_passing_ge_0.30": len(train_pairs),
            "pairs_below_filter": 0,
            "note": "pair filter applied at build time; below-filter count re-derived from "
                    "the files (0 expected)",
        },
        "bin_coverage": bin_coverage(rows["train"]),
    }
    # re-derived below-filter count (computed inside group_separations)
    ev["train"]["filter_pass"]["pairs_below_filter"] = sum(
        1 for s in train_pairs if s < GATE_MIN_SEP - 1e-9)

    # ---- heldout (reported for completeness; NOT the gate split) ----
    ev["heldout"] = {
        "n_groups": len(seps["heldout"]),
        "n_rows": len(rows["heldout"]),
        "n_pairs": len(pairs["heldout"]),
        "separation": {
            "mean": round(float(np.mean(seps["heldout"])), 4),
            "median": round(float(np.median(seps["heldout"])), 4),
            "min": round(float(np.min(seps["heldout"])), 4),
            "max": round(float(np.max(seps["heldout"])), 4),
        },
        "bin_coverage": bin_coverage(rows["heldout"]),
    }

    # ---- build-time drop counts (provenance, from stats.json) ----
    stats_path = OUT / "stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text())
        ev["build_time_drop_counts"] = {
            split: {name: stats["per_axis_per_split_per_source"]["arousal"][split][name]
                    for name in stats["params"]["arousal_sources"]}
            for split in ["train", "heldout"]
        }

    # ---- verdict (fixed gate; no weakening) ----
    mean_sep = ev["train"]["separation"]["mean"]
    verdict = "PASS" if (ev["train"]["n_groups"] > 0 and mean_sep >= GATE_MIN_SEP) else "FAIL"
    ev["verdict"] = verdict
    ev["verdict_rule"] = ("PASS iff train n_groups > 0 and train mean surviving separation "
                          ">= 0.30; on FAIL: STOP and fix the corpus, never weaken the filter.")

    out = DIAG / "gdata-arousal.json"
    out.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n")
    print(f"[gdata] VERDICT: {verdict} (train mean surviving separation "
          f"{ev['train']['separation']['mean']} >= {GATE_MIN_SEP}, "
          f"n_groups={ev['train']['n_groups']})")
    print(f"[gdata] wrote {out}")
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
