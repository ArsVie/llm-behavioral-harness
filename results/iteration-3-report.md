# Iteration-3 — perceptual validity: confirmatory report

Status: SKELETON — numbers are computed by `experiments.cvs_report.py`
(and the G6 driver) from artifacts; this file is assembled at G6 close.
Nothing in this report is handwritten except interpretation.

## 0. Headline

(Stated answer to DoD §11: is the endogenous stochastic state
perceptible to an independent observer? — filled after G6 judging.)

## 1. Gate ledger (it3)

| Gate | What | Status | Evidence |
|---|---|---|---|
| G1 | seam audit + generation integrity | PASS | (it3-b1 merges) |
| G2 | preflight gate, real claims, horizon split | PASS | 941/941 (main-g2close3.log) |
| G3 | real-model smoke (1 cell × 7 d) | — | results/it3-g3-smoke-night/ |
| G4 | manifest freeze (B10) | DONE | results/it3-g4-manifest-*.json (fingerprint ca357fde) |
| G5 | confirmatory matrix (35 cells × 30 d) | — | results/it3-g5-matrix/ |
| G6 | judge protocol v2 (both families) | — | results/it3-g6-judge/ |

## 2. G2 close (941/941) — horizon split

- Gate vs hypothesis threshold split (gate: GATE_MIN_DIVERGENCE 0.05
  channel-not-dormant; manifest: count 0.15 / gap 0.10, unchanged).
- min_days per claim (NO_LIFE 2, timing 4, SIMPLE_RAG 10); below-horizon
  claims report NOT EVALUABLE, never FAIL.
- Reconciliation: results/it3-g2-horizon-split-reconciliation-2026-08-10.md
  — the 14.4%-vs-15% discrepancy was a two-leg measurement error
  (positive control passes via the fired-schedule leg, 29.17%; the count
  leg 14.4% pooled remains below 0.15 → recorded at margin, no threshold
  moving).

## 3. G3 real-model smoke

- Command: `companion_vertical_slice vertical --seed 5001 --days 7`
- Result: (filled — cell dir, blank rate, days reached)
- Provider episode 2026-08-10 03:23+ (deepseek-v4-flash 100% empty):
  4 failed attempts before the hardened client + watchdog fallback;
  amendment results/it3-manifest-amendment-2026-08-10-model-fallback.md.

## 4. G4 manifest (B10) — frozen before generation

- results/it3-g4-manifest-*.json — conditions, seeds, thresholds, judge
  config, EVENT_CHAINS, reconciliation + SNS-at-margin decision.

## 5. G5 confirmatory matrix

- 7 conditions × 5 seeds × 30 days (real client, checkpoints on,
  perturbation per cell) — runner experiments/cvs_matrix.py.
- Cell-level retry/resume on provider empties; per-cell transcripts for
  the judge.
- Matrix audit: per-cell claims evaluated on real summaries (see
  report-data.json).

## 6. G6 judge protocol v2

- Both families (opencode-flash / opencode-luna), 2 passes, within-seed
  sampling, attention probes (degraded-transcript control pairs) resolved
  by BOTH families (DoD §11 item 6), BT aggregation per dimension per
  family.
- Dry-run verified 8/8 probes; real run on matrix transcripts.

## 7. DoD §11 recomputed from artifacts

1. Blank turns < 1% (hard invariant): report-data.json per_cell.
2. Ablations demonstrably ablate pre-generation: G2 gate (941/941) +
   matrix per-cell claim evaluations.
3. closing_tendency mechanically observable: turn counts per
   conversation (report-data.json conversations).
4. Latent state reaches the timing channel: structured_no_state_claim on
   real summaries (report-data.json timing_channel).
5. Memory conditions each through own lane with absolute CompleteChain:
   EVENT_CHAINS (manifest) resolved per lane.
6. Judge resolves a deliberately degraded transcript under both
   families: g6_report.json attention_probes_resolved.
7. Stated answer to the perceptual question: §0.

## 8. Limitations

(provider episode 2026-08-10; model fallback; judge family-1 dependency
on the degraded route — filled at close)

## 9. Artifacts

- results/it3-g4-manifest-*.json (freeze)
- results/it3-g2-horizon-split-reconciliation-2026-08-10.md
- results/it3-manifest-amendment-2026-08-10-model-fallback.md (if used)
- results/it3-g3-smoke-night/ (G3 cell)
- results/it3-g5-matrix/ (35 cells + transcripts/)
- results/it3-g6-judge/ (pairs, outcomes, g6_report.json)
- results/it3-report-data.json (DoD computations)
- experiments/cvs_matrix.py, cvs_g6.py, cvs_report.py, live_companion.py
