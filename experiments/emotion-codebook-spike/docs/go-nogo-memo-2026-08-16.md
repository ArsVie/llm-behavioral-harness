# Go/No-Go memo — V4-Flash rental (emotion-codebook spike)

Date: 2026-08-16 (00:30 local)
Status: **NO-GO** — per the pre-registered P3 barrier (brief §4, P3): "if H1 fails
on both families, STOP and report — the method doesn't transfer to small models
and the rental premise needs rethinking."

Pre-registration: docs/exp-affect-codebook-pipeline-2026-08-15.md
Repro bundle: repro_bundle.json (revisions, seeds, dataset versions)
All commits local (no push).

---

## 1. H1 gate results (held-out, all 15,240 rows, both families)

Gates: valence Pearson r >= 0.60, arousal r >= 0.40, bootstrap 95% CI excludes 0.

| family | axis  | method          | r       | 95% CI               | layer | verdict |
|--------|-------|-----------------|---------|----------------------|-------|---------|
| Qwen   | val   | emotion vectors | +0.478  | [0.463, 0.495]       | 0     | FAIL    |
| Qwen   | val   | j-lens          | -0.490  | [-0.505, -0.475]     | 17    | FAIL    |
| Qwen   | aro   | emotion vectors | +0.322  | [0.301, 0.342]       | 0     | FAIL    |
| Qwen   | aro   | j-lens          | -0.234  | [-0.260, -0.210]     | 21    | FAIL    |
| Gemma  | val   | emotion vectors | +0.456  | [0.440, 0.473]       | 13    | FAIL    |
| Gemma  | val   | j-lens          | -0.520  | [-0.534, -0.505]     | 13    | FAIL    |
| Gemma  | aro   | emotion vectors | +0.294  | [0.271, 0.319]       | 3     | FAIL    |
| Gemma  | aro   | j-lens          | -0.388  | [-0.411, -0.366]     | 11    | FAIL    |

All 8 CIs exclude 0 (axes are statistically real), none meets its r threshold.
H1x: consistent failure across families — flagged "method does not transfer as
a VAD-alignment method on small models".

## 2. What the spike DID validate (machinery)

- Full P0→P3 pipeline runs end-to-end on both Qwen3-1.7B and Gemma-3-1B in
  8 GB (fwd+bwd, J-lens sharded, gradient checkpointing, bf16). Barrier PASS
  (Qwen peak 5,674 MiB; Gemma peak 4,279 MiB; 8,151 MiB budget).
- Determinism held across re-runs: identical seeds, identical sample bins,
  byte-identical loss in smoke; all stochastic steps seeded from master
  20260815.
- Crash-resilience worked: 3 root-caused crashes (device mismatch, .numpy() on
  CUDA, negative SeedSequence key) each recovered from checkpoints; final runs
  exit 0 on both families.
- Held-out discipline held: directions fitted on train only, evaluated on
  held-out; EV held-out r ≈ train r (Qwen val 0.478 vs 0.479) — no overfit.

## 3. Findings that inform a rethink

1. **Signal is real but weak.** All axes are significant; the recovered geometry
   is stable and cross-family consistent (EV valence 0.478 vs 0.456; JL valence
   0.490 vs 0.520 magnitude), yet systematically below human-VAD alignment.
2. **J-lens polarity is inverted by construction** (see jlens-shim-qwen.md RUN
   LOG): accumulation w·∇L with w = y − ȳ, and dL/dh points away from the
   target token. Read |r| for magnitude; a polarity correction (−d, or w =
   ȳ − y) is required in any future run. Even corrected, |r| < 0.60 on both
   families.
3. **Best layer is not stable across families** (EV valence: Qwen layer 0,
   Gemma layer 13) — layer selection needs a principled rule, not per-model
   argmax, for the production run.
4. Arousal is the weaker channel everywhere (EV 0.32/0.29; JL |0.23|/|0.39|),
   consistent with P1's finding that arousal contrasts are data-limited
   (GoEmotions mid-scale; surviving separation ~0.054).

## 4. Rental decision

- **Recommendation: do not rent V4-Flash for the identical pipeline.** The
  spike's purpose was to validate the pipeline before committing the rental;
  the first scientific gate failed on both validation families. The premise
  "small-model validation predicts production viability" is unsupported in the
  alignment dimension.
- Conditional sizing (if a rethink changes the gate set): emotion-vectors
  (forward-only) ≈ hold the model (1× weights); +J-lens ≈ several× for
  gradients (backward pass + checkpointing ≈ 2-3× peak over forward-only on
  the 8 GB card).

## 5. Rethink options (not gate changes — proposals for the next pre-registration)

A. Larger local models (e.g. 3-8B class) before any rental: cheap check of the
   "bigger = more human-aligned" hypothesis.
B. Different grounding target: behavioral alignment (H3-style judge) instead of
   or alongside human VAD — the brief's own H3/H4 were judge-based; VAD may be
   the wrong reference for model-native geometry.
C. Extraction changes: polarity fix (above), multi-axis orthogonalization
   (valence/arousal directions are likely entangled), layer-selection rule.
D. Scale the stimulus corpus for arousal (GoEmotions is valence-rich only).

## 6. Where things stand

- P0 (env/provenance), P1 (stimuli), P2 (extraction, both families), P3
  (geometry, both families) — COMPLETE, committed (7e1f59f, d1c8df8).
- P4–P7 — NOT RUN (stopped per pre-registration).
- Voice alert fired at the gate; full numbers in diagnostics/geometry-{qwen,
  gemma}.json; raw runs in data/extractions/{qwen,gemma}/run.log.
