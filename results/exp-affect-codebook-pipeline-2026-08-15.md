# Experiment brief — lexical affect codebook, pipeline validation

Date: 2026-08-15
Mode: orchestrator (subagents available)
Status: PRE-REGISTERED design. Gates are fixed before any run; the orchestrator
reports against them and does not move them post hoc.

## 0. Objective and scope

Validate — on small, locally-runnable models — the full pipeline that turns the
frozen engine's continuous affect values into a **model-native, empirically
derived lexical codebook** (value → words), with **no numbers in the prompt**.

This is a **method-validation spike**, NOT the production codebook. Under the
project's law (*research and product must never diverge*), the shipped codebook
must be extracted from the production model (DeepSeek-V4-Flash) on rented GPU.
This spike's only job is to prove the pipeline works and beats the current
renderer **before** that rental is committed. Its output is a **go/no-go** on the
rental plus the parameters to run it (resolution, which extraction method).

Explicitly OUT of scope: V4-Flash extraction; runtime activation steering (can't
run through the product API — deferred); production wiring; calibrating the
behavioral/social channels beyond keeping them separate from affect.

## 1. Hardware & model constraints (fixed)

- One GPU: **RTX PRO 1000, 8 GB** CUDA (Intel Arc unusable for autograd).
- Models (bf16 HF weights, autograd + hooks — **GGUF is disqualified**, it's
  inference-only):
  - **Primary: Qwen3-1.7B** (fits forward+backward in 8 GB).
  - **Replication: Gemma-3-1B** (or Gemma-2-2B if it fits with checkpointing).
  - Both families have published emotion-vector/valence-arousal replications, so
    they're the right method-validation targets.
- Memory tactics for the backward leg (J-lens): 128-token sequences, gradient
  checkpointing, layer-sharded fitting via the repo's `merge()`.
- Pin HF commit hashes, dataset versions, and all seeds. Fixed-seed +
  fixed-temperature decoding for reproducibility (mirror the harness repro rule).

## 2. Hypotheses and pre-registered gates

| # | Hypothesis | Gate (pass condition) |
|---|---|---|
| H1 | Extracted affect geometry recovers human VAD structure | valence axis ↔ human valence Pearson **r ≥ 0.60**; arousal **r ≥ 0.40**; 95% bootstrap CI excludes 0 |
| H1x | Method is not model-fragile | H1 passes on **both** Qwen and Gemma; if only one, flag "model-specific" |
| H2 | Codebook is a smooth, monotone function of the value | adjacent-bin JS divergence median **≤ 0.05**; Spearman ρ(grid value, lexical-centroid valence projection) **≥ 0.90**; **no off-axis outlier bins** |
| H2d | Doses are distinct (anti-degeneracy) | descriptors at 0.20 / 0.50 / 0.80 are pairwise different strings AND differ in top-k token sets |
| H3 | Codebook prompts produce judge-recoverable affect, blind to numbers | **independent** cross-family judge classifies {low/mid/high} valence bands at **acc ≥ 0.60** (3-way, chance 0.33), CI excludes chance, **K ≥ 30** generations/band/model |
| H4 | Codebook beats the current renderer where it matters | on levels the ±0.35/48-state renderer collapses (e.g. valence 0.6 vs 1.0), codebook generations are judge-distinguishable while renderer's are not; paired test, CI on the difference |
| H5 | Masking holds | assembled prompts contain **zero** numeric affect tokens (hard invariant); a judge cannot recover the numeric value better from the generation than from the descriptor words alone |
| Hres | Behavioral resolution (measurement, no pass/fail) | smallest step in {0.01, 0.05, 0.10, 0.20} at which adjacent generations are judge-distinguishable → the **recommended production resolution** |

## 3. Datasets (human-grounded reference — pinned)

Ground the affect scale on humans, not the model's self-report:
- **NRC-VAD** (~20k words, V/A/D ratings)
- **Warriner et al.** (13,915 lemmas, V/A/D)
- **EmoBank** (~10k sentences, writer+reader VAD)
- **GoEmotions** (58k texts, 27 categories) — for behavioral-eval contexts

Use a **train/held-out split**: fit the codebook on train, validate geometry (H1)
and behavior (H3/H4) on held-out. No fitting on the validation split.

## 4. Pipeline (phases → subagent units)

**P0 — Environment & provenance** (1 agent)
Acquire Qwen3-1.7B + Gemma bf16, pin revisions; acquire+version datasets; build
the deterministic harness (seeded, records revisions/seeds/configs). Barrier: no
downstream phase starts until models load and a forward+backward smoke test fits
in 8 GB.

**P1 — Stimulus construction** (1 agent, shared)
Build VAD-binned stimulus corpus from the datasets (contrastive intensity sets
per axis). Output: train/held-out stimulus sets with human VAD coordinates.

**P2 — Extraction** (2 agents, one per model, parallel)
- P2a **emotion vectors** (forward hooks, cheap, guaranteed to fit): contrastive
  activation differences per axis, per layer → candidate affect directions.
- P2b **J-lens** (backward, memory-tight): fit per model with the constraints in
  §1; read the vocabulary distribution for each binned activation.
  *Fallback:* if J-lens OOMs on 8 GB even sharded, proceed with emotion-vectors +
  nearest-VAD-label lexical readout, and record that J-lens vocabulary quality is
  deferred to rental scale. Report the fallback explicitly — do not silently drop.

**P3 — Geometry validation** (folds into the P2 agents) — **GATE H1/H1x**
Correlate the recovered valence/arousal axes with human VAD on held-out. Barrier:
if H1 fails on both families, STOP and report — the method doesn't transfer to
small models and the rental premise needs rethinking.

**P4 — Codebook construction** (2 agents, per model, parallel)
Fit the 0.01 grid by smoothing the measured lexical distributions (collect
continuous, then interpolate — do not query 101 points independently). Emit the
three-field artifact per bin:
```json
{ "value": 0.63,
  "tokens": [["content",0.91],["pleased",0.84],["warm",0.77]],
  "descriptor": "content, pleased, warm",
  "prompt_variants": ["quietly pleased and warm", "content, at ease", "..."] }
```
`tokens` = raw evidence; `descriptor` = normalized; `prompt_variants` = surface
realizations (deterministically chosen at runtime by `hash(seed,day,channel)` to
avoid repetition). An LLM may polish `descriptor`/`variants` surface only — it may
**never** define the scale (provenance rule).

**P5 — Quality tests** (1 agent) — **GATE H2/H2d/H5**
Smoothness curve, monotonicity, lexical-derivative (ΔP between adjacent bins:
which tokens enter/leave), degeneracy check, numeric-leakage scan. Barrier: fail
H2d (degenerate doses) or H5 (numbers in prompt) ⇒ STOP; those are the exact
failure modes that invalidated earlier work.

**P6 — Behavioral eval** (fan-out: 1 coordinator + up to 4 judge/generation
agents) — **GATE H3, measurement Hres**
Fixed conversation scaffold; only the affect descriptor + behavioral bearing
vary (no confounds). Generate K≥30 per band per model; score with an
**independent cross-family judge** (actor Qwen → judge Gemma/stronger, and vice
versa — never actor==judge). Sweep step sizes for Hres. Report acc + bootstrap CI.

**P7 — Comparison & synthesis** (1 agent) — **GATE H4**
Head-to-head vs the current 48-state renderer on collapse-prone level pairs.
Synthesize all gates → go/no-go on the V4-Flash rental, recommended resolution,
recommended extraction method (emotion-vectors vs +J-lens), and rental sizing.

## 5. Parallelization summary
Two model tracks (Qwen, Gemma) run P2→P4 concurrently; P1 shared upstream; P6
fans out judges; P3/P5/P7 are barriers/gates. Peak ~6 concurrent agents, within
the 10 budget.

## 6. Guardrails (pre-registered; violations invalidate the run)
- **Independent judge only** — never actor==judge (the it2 corpus judge lesson).
- **Report n and CIs everywhere**, bootstrap; no directional claim on K<30 (the
  tiny-n false-null lesson).
- **Held-out validation** — no fitting on the validation split.
- **Determinism** — pinned revisions/seeds/decoding; record a repro bundle.
- **Negative results reported** — family failures, J-lens fallback, collapsed
  bins: all surfaced, never dropped.
- **Provenance** — descriptors derive from measured distributions; the LLM
  polishes surface only.

## 7. Deliverables
1. `emotion_codebook/{qwen,gemma}/{valence,arousal}/…` — the three-field artifact.
2. `diagnostics/` — geometry correlations, smoothness/monotonicity, lexical
   derivatives, degeneracy, JND, all with CIs.
3. Behavioral-eval report (H3/H4) with judge separability vs renderer.
4. **Go/no-go memo** for the V4-Flash rental: recommended resolution (from Hres),
   extraction method, and rental sizing (forward-only emotion-vectors ≈ hold the
   model; +J-lens ≈ several× for gradients).
5. Repro bundle (seeds, HF commit hashes, dataset versions, scripts).

## 8. Success in one line
The spike passes if, on **both** Qwen and Gemma, the pipeline yields a smooth,
non-degenerate, human-VAD-aligned codebook whose prompts let an independent judge
recover affect it **cannot** recover from the current renderer — with no numbers
ever in the prompt. That result, and only that result, justifies renting GPU to
run the identical pipeline on V4-Flash.
