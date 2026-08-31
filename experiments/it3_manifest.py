"""Manifiesto de preregistro de la iteración 3 (Gate G4, B10) —
validez perceptual.

Congela —ANTES de la generación de la matriz— el diseño de la it3:
condiciones, semillas, horizonte, umbrales (SPLIT de compuerta vs
hipótesis), la reconciliación del margen de conteo (14.4% vs 0.15) y la
decisión STRUCTURED_NO_STATE-a-margen. Reutiliza las constantes del
manifiesto de la it2 (cvs_manifest) allí donde el diseño no cambió.

El output JSON se congela en results/it3-g4-manifest/manifest.json y se
commitea; ninguna hipótesis ni umbral cambia tras observar resultados sin
crear un experimento nuevo (invariante 19/20 del plan).

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from experiments.cvs_manifest import (
    DIMENSIONS,
    EXPERIMENT_NAME as _IT2_NAME,
    JUDGE_FAMILIES,
    JUDGE_MIN_FAMILIES_FINAL_SUBSET,
    JUDGE_PASSES,
    JUDGE_SHUFFLE_SEED_BASE,
    MATRIX_CONDITIONS,
    SEEDS,
    THRESHOLDS,
)
from experiments.cvs_preflight import CLAIMS, GATE_MIN_DIVERGENCE
from experiments.cvs_common import DEFAULT_CHECKPOINT_DAYS
from harness.scheduler import COUNT_DIVERGENCE_MIN, GAP_DIVERGENCE_MIN

EXPERIMENT_NAME = "it3-perceptual-validity"
SCHEMA_VERSION = "2.0"
DAYS = 30

# Margin decision 2026-08-10: count leg 14.4%, gap leg 12.5%, margin 0.15.
RECONCILIATION = {
    "record": "results/it3-g2-horizon-split-reconciliation-2026-08-10.md",
    "three_flags_two_causes": {
        "NO_LIFE": "broken ablation, fixed by goldfish c24d880 (a fix, not a horizon change)",
        "STRUCTURED_NO_STATE": "horizon artifact — score feedback cannot land before day 2-3; min_days=4",
        "SIMPLE_RAG": "horizon artifact — store below retrieval surface (limit=8) until ~day 5-10; min_days=10",
    },
    "which_leg_fired": {
        "n_fired_schedule": "48 vs 62 = 29.2% (seed 5001, 30d) — the leg that fired",
        "n_proactive_pooled": "222 vs 254 = 14.4% — below COUNT_DIVERGENCE_MIN",
        "mean_gap": "3.2h vs 2.8h = 12.5% — above GAP_DIVERGENCE_MIN",
    },
    "decision": (
        "COUNT_DIVERGENCE_MIN stays 0.15. The manifest claim is tested on "
        "the real matrix; if not met at margin, report not-met with the gap "
        "leg noted, or B5 strengthens coupling weights before G4-freeze "
        "review (user-approved options)."
    ),
}

# Split between the pre-flight divergence check and hypothesis thresholds.
THRESHOLD_SPLIT = {
    "gate_min_divergence": GATE_MIN_DIVERGENCE,
    "gate_role": "null-detector: channel not dormant; immune to post-hoc tuning",
    "hypothesis_thresholds": {
        "count": COUNT_DIVERGENCE_MIN,
        "gap": GAP_DIVERGENCE_MIN,
    },
    "hypothesis_role": (
        "effect-size claims, tested on the REAL matrix at G5; "
        "never tuned after seeing results"
    ),
}

# Minimum horizon per claim, in days.
CLAIM_MIN_DAYS = {c.condition: c.min_days for c in CLAIMS}

# Maximum blank-turn rate.
BLANK_RATE_HARD_INVARIANT = 0.01


def build_it3_manifest(*, repo_root: Path | None = None) -> dict:
    repo_root = Path(repo_root or Path(__file__).resolve().parent.parent)
    started_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "experiment": EXPERIMENT_NAME,
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "based_on": _IT2_NAME,
        "matrix": {
            "conditions": list(MATRIX_CONDITIONS),
            "seeds": list(SEEDS),
            "days": DAYS,
            "cells": len(MATRIX_CONDITIONS) * len(SEEDS),
            "checkpoints": list(DEFAULT_CHECKPOINT_DAYS),
            "perturbation": True,
            "real_client": "deepseek-v4-flash via commandcode (JUDGE_GENERATOR_TOKEN, research lane)",
            "retry_policy": (
                "client: 7 attempts x 2/4/8/16/32/64s backoff (G3 hardening); "
                "cell-level retry 3x with 2/4/8 min backoff (cvs_matrix)"
            ),
        },
        "gate": {
            "split": THRESHOLD_SPLIT,
            "min_days": CLAIM_MIN_DAYS,
            "claims": [
                {
                    "condition": c.condition,
                    "channel": c.channel,
                    "assertion": c.assertion,
                    "min_days": c.min_days,
                }
                for c in CLAIMS
            ],
            "evidence": "941/941 suite green at G2 close (commit 454a799 + test fix)",
        },
        "reconciliation": RECONCILIATION,
        "hard_invariants": {
            "blank_rate_max": BLANK_RATE_HARD_INVARIANT,
            "ablations_ablate": "every matrix condition demonstrably ablates its target channel, asserted before generation (G2 gate)",
        },
        "hypotheses": [
            "H1: endogenous stochastic state (mood/timing/initiative) is perceptible to an independent observer in the transcript",
            "H2: structured memory carries verifiable trajectory recall through its own lane",
            "H3: the timing channel (score feedback) measurably changes proactive behavior at 30 days",
            "H4: ablating life-state persistence (goldfish) changes arc identity continuity without breaking grounding",
            "H5: actuator controls visibly flatten generation controls when pinned",
            "H6: the positive control (NO_TIMING_FEEDBACK) is detectable — if the pipeline cannot see it, the pipeline is broken",
        ],
        "judge": {
            "dimensions": list(DIMENSIONS),
            "families": [
                {k: f[k] for k in ("id", "family", "model", "base_url", "env_key", "role")}
                for f in JUDGE_FAMILIES
            ],
            "min_families_for_final_subset": JUDGE_MIN_FAMILIES_FINAL_SUBSET,
            "passes": JUDGE_PASSES,
            "shuffle_seed_base": JUDGE_SHUFFLE_SEED_BASE,
            "protocol_v2": {
                "aggregation": "Bradley-Terry per dimension, per family; disagreement reported",
                "attention_probes": "control pairs (corrupt_transcript) resolved under both families",
                "degraded_transcript": "a deliberately degraded transcript must be resolved by both families (DoD item 6)",
            },
            "subset_rule": (
                "final/important subset = companion matrix conditions x seeds "
                "5001-5003, judged blind by >=2 independent judge families; "
                "per-dimension disagreement reported."
            ),
        },
        "metrics": ["matrix_audit_summary", "per-cell DBs", "blank rate", "conversation turns"],
        "thresholds": THRESHOLDS,
    }
    manifest["fingerprint"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return manifest


def main() -> int:
    out = Path("results/it3-g4-manifest")
    out.mkdir(parents=True, exist_ok=True)
    m = build_it3_manifest()
    (out / "manifest.json").write_text(
        json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"manifest written: {out / 'manifest.json'}")
    print(f"cells: {m['matrix']['cells']} | fingerprint: {m['fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
