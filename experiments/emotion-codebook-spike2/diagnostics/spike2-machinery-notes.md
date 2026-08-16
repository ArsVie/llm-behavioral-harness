# Spike 2 — P2 extraction machinery notes (C1/C2/C3)

Date: 2026-08-16 · Task: P2-machinery · Pre-registered: `docs/exp-affect-codebook-spike2-2026-08-16.md`
(§ Prerequisite fixes C1/C2/C3; Orchestrator decisions 4–5).

## What changed vs spike 1

`scripts/p2_qwen_extract.py` (Qwen/Qwen3-1.7B @ 70d244cc86ccca08cf5af4e1e306ecf908b1ad5e) and
`scripts/p2_gemma_extract.py` (google/gemma-3-1b-pt @ fcf18a2a879aab110ca39f8bffbccd5d49d8eb29)
are the validated spike-1 scripts with exactly three machinery fixes (everything
else — gpu_clear, checkpoints, seeding, bf16, gradient checkpointing, 128-token
cap — carried over unchanged):

### C1 — J-lens polarity
- `stage_p2b` accumulation weight flipped: `w = ybar[a] - axis_value(r, a)`
  (spike 1: `axis_value(r, a) - ybar[a]`); helper `jlens_direction` updated to
  the same convention (`w = ȳ − y`). Equivalently the final directions are
  negated.
- Selftest now asserts the C1 convention: corrected JL recovers `+d_val`
  (cos > 0.9) while the spike-1 sign gives cos ≈ −0.98 (counter-check).
- Synthetic gradients in selftest model the real relationship (dL/dh opposes
  the feature direction — the empirically observed inversion that C1 fixes).

### C2 — single pre-registered layer rule (replaces per-model |r| argmax)
- `c2_band(n) = [n//3, (2n+2)//3)` — middle third of depth: Qwen [9,19),
  Gemma [8,18). Verified in bringup (hard assert per model).
- `c2_select`: within the band, pick the layer with the highest **signed**
  train r for the axis (contract: valence-r, NOT |r|). Same rule in P2a (EV),
  P2b (JL) and P3's layer choice. "norm" is excluded (not a layer).
- Full per-layer sensitivity (`train_r` per layer, incl. norm) is kept in the
  artifacts: P2a `axes[axis].layers`, P2b `axes[axis].direction_layers`.
  `best_layer_by_abs_train_r` is retained as a legacy diagnostic only, never
  used for selection.

### C3 — orthogonalization
- `orthogonalize(d_aro, d_val)` = unit(d_aro − (d_aro·d_val)·d_val) — Gram-Schmidt
  onto the valence-orthogonal complement (orchestrator decision 5), applied per
  layer to BOTH EV (P2a) and JL (P2b) arousal directions, valence first.
- P2a records per-layer `c3_angle_deg` (angle between raw arousal direction and
  valence direction) and both stages record `axes[axis].c3` applied/note.

## Verification (all real runs; RE-DERIVED values)

### CPU selftest (both models) — PASS
| metric | Qwen | Gemma |
|---|---|---|
| cos EV valence | 0.9779 | 0.9878 |
| cos EV arousal raw (entangled) | 0.8763 | 0.8840 |
| cos EV arousal after C3 | 0.9694 | 0.9816 |
| cos JL valence (C1) | 0.9815 | 0.9896 |
| cos JL arousal after C3 | 0.9733 | 0.9844 |
| spike-1 sign counter-check cos | −0.9815 | −0.9896 |
| r / CI (binning check) | 0.9991 [0.9988,0.9993] | 0.9991 [0.9988,0.9994] |

Raw arousal recovery 0.876/0.884 < 0.9 while post-C3 > 0.97: the entanglement
C3 removes is real in the synthetic test.

### GPU bringup (real models) — PASS both
| | Qwen | Gemma |
|---|---|---|
| layers / C2 band | 28 / [9,19) ✓ | 26 / [8,18) ✓ |
| analytic cos (sentence / 2-token / 1-token) | 0.999996 / 0.999997 / 0.999998 | 0.999996 / 0.999998 / 0.999997 |
| bwd peak VRAM | 4521 / 4495 / 4489–4492 MiB | 3189 / 3136 / 3131 MiB |

All ≤ 7000 MiB budget (of 8151). Gradient hooks match the analytic
head-input gradient to cos > 0.99999 on every sequence type.

### Mini C1 sign verification (Qwen — pre-registered gate) — **PASS**
n = 60 train + 60 held-out per axis, deterministic sampling
(derive_seed(MASTER_SEED, 7, axis); seeds: valence 2521371786, arousal 2885350259),
on the spike-2 corpus (`data/stimuli`, P1/G-DATA build). Fits EV (P2a) and JL
(P2b, corrected polarity) on the train sample; held-out correlations:

| axis | EV r (layer) | JL r (layer) | JL r @ norm | signs match |
|---|---|---|---|---|
| valence | +0.298 (17) | +0.101 (18) | +0.096 | **TRUE (gate PASS)** |
| arousal | −0.162 (9) | −0.039 (17) | +0.243 | FALSE (informational) |

Valence gate: corrected JL sign == EV sign ✓. Old spike-1 sign would have given
JL r ≈ −0.10 (anti-aligned). Spike-1 full-n (n≈17k held-out) JL valence r was
−0.49 with the old sign → corrected expectation ≈ +0.49, matching EV +0.48;
the mini n=60 r's are small but correctly signed.

Gemma mini run (same procedure, informational — the gate is Qwen-only):
valence EV +0.398 (8) vs JL +0.070 (10): C2-layer signs match; JL @ norm −0.120
is within noise of 0 at n=60 (spike-1 full-n data under the corrected sign
predicts norm ≈ +0.40, so no machinery issue — watch item for the full run:
confirm jlens norm-layer valence `train_r` sign at n=2000).

C2 selected layers (mini, RE-DERIVED at n=60): Qwen valence EV 17 / JL 18,
arousal EV 9 / JL 17; Gemma valence EV 8 / JL 10, arousal EV 10 / JL 17.
Full-run values supersede these (n=2000).

## Operational notes
- Stimuli: scripts read `data/stimuli/{train,heldout}.jsonl`; `selftest`/`bringup`
  run without them; `c1verify`/`all` fail cleanly if absent.
- During bringup the spike-1 corpus was accidentally copied over the freshly
  built spike-2 corpus; it was regenerated with `scripts/build_stimuli.py`
  (deterministic: seeded draws, sorted rows) and verified byte-equivalent
  (verify_stimuli.py PASS; stats.json identical modulo built_at; valence lines
  byte-identical to spike 1; G-DATA re-check 0.4122 ≥ 0.30).
- New stage: `c1verify` (mini C1 gate). Seed key 7 added (`c1`); pipeline seeds
  (1–6, 99) unchanged → comparable with spike 1.
- Mini-run checkpoint files written into `data/extractions/{qwen,gemma}/` by the
  c1verify stage were removed after the run; the full pipeline rewrites them.
