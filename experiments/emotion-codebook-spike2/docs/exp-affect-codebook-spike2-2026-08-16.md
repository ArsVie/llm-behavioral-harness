# Experiment brief — affect codebook, spike 2 (behavioral re-gate)

Date: 2026-08-16
Mode: orchestrator (subagents)
Precedent: spike 1 (`exp-affect-codebook-pipeline-2026-08-15.md`) → **NO-GO**;
memo `experiments/emotion-codebook-spike/docs/go-nogo-memo-2026-08-16.md`.
Status: PRE-REGISTERED. Gates fixed before any run; reported against, never moved.

## Why a second spike, and why the gate changes

Spike 1's gate was **H1 — VAD-alignment** (recovered axis vs human lexical VAD).
It failed on both families (valence |r|≈0.46–0.52 < 0.60; arousal ≈0.29–0.39 <
0.40), all CIs excluding zero — the axes are real but under the bar.

That gate was a **cheap proxy** for the question we actually care about, which is
H4: *do the model's own affect words produce more behaviorally distinguishable
Lily than the current hand-designed 48-state renderer?* A model's native
vocabulary can carve behavior-relevant distinctions that do not line up with
human *lexical* VAD ratings — so H1 failing does not answer H4. Spike 1 also left
three confounds that make 0.48 a pessimistic floor: a J-lens polarity inversion,
a per-model layer argmax (unstable across families), and an arousal reference
(GoEmotions) with almost no arousal signal (~0.054 surviving separation).

**Spike 2 therefore (a) fixes the confounds, (b) demotes VAD-alignment from gate
to diagnostic, and (c) makes the PRIMARY gate behavioral — codebook vs the
current renderer.** This is a corrected question, not a lowered bar: the
behavioral gate can still fail, and the pre-committed decision on failure is
SHELVE (§ Decision matrix). All local; no rental; builds on spike 1's validated,
checkpointed machinery.

## Carried over unchanged
Frozen engine; additive-only; determinism/seeding; held-out discipline;
independent cross-family judge (never actor==judge); K≥30/band with bootstrap
CIs; negative results reported; provenance rule (descriptors from measured
distributions, LLM polishes surface only); 8 GB budget.

## Models
- **Small (full pipeline, incl. J-lens backward — validated in spike 1):**
  Qwen3-1.7B, Gemma-3-1B.
- **Larger (scale point, EV forward-only in 4-bit — fits 8 GB):** one 7–8B model
  (Qwen3-8B or Gemma-2-9B). J-lens backward won't fit 8 GB at this size; EV is the
  cleaner method regardless.

## Prerequisite fixes (each with its own verify-gate, before extraction)

- **C1 — J-lens polarity.** Correct the sign (`w = ȳ − y`, or negate `d`).
  Verify-gate: on Qwen, corrected JL valence sign matches EV sign.
- **C2 — principled layer rule (replaces per-model argmax).** Pre-register ONE
  rule applied identically to every model: select the layer on the **train** split
  by valence-r, **constrained to the middle third of depth** (per the workspace
  paper: J-space is a mid-block phenomenon); report full per-layer sensitivity.
  No post-hoc per-model layer picking.
- **C3 — orthogonalization.** Extract valence, then extract arousal in the
  valence-orthogonal complement (the two directions are likely entangled).
- **D — real arousal reference.** Replace GoEmotions for the arousal axis with
  arousal-graded stimuli from **NRC-VAD + Warriner + EmoBank** (all carry real
  arousal ratings). **Data-side pre-flight gate G-DATA:** the arousal stimulus set
  must show surviving separation **≥ 0.30** before extraction — if the reference
  itself has no arousal signal, STOP and fix the corpus (don't blame the model).

## Pipeline (reuses spike 1 P0–P3 machinery)
P0 env/provenance → P1 stimuli (with D + G-DATA) → P2 extraction (with C1/C2/C3,
per model) → P3 geometry (diagnostic now, not a gate) → **P4 build codebook**
(0.01 grid, smoothing, 3-field artifact) → **P5 quality** (H2/H2d/H5) → **P6
behavioral eval** (the primary gate) → **P7 vs current renderer + synthesis**.

## Gates (pre-registered)

| Gate | Type | Condition |
|---|---|---|
| **G-DATA** | pre-flight, hard | arousal reference surviving separation ≥ 0.30 |
| **G-MASK** (H5) | hard invariant | zero engine numbers in any assembled prompt |
| **G-SMOOTH/G-DEGEN** (H2/H2d) | quality | adjacent-bin JS ≤ 0.05; monotone (Spearman ≥ 0.90); descriptors at 0.2/0.5/0.8 pairwise distinct |
| **G-ABS** (H3) | support | codebook generations judge-classified ≥ 0.60 (3-way, chance 0.33), CI excludes chance, K≥30/band |
| **G-BEH** (H4) | **PRIMARY** | on the **largest local actor**, codebook beats the current 48-state renderer on judge separability by **Δacc ≥ +0.10**, 95% CI on the difference excludes 0 (paired, same contexts/levels) |
| **SCALE** (A) | diagnostic, no pass/fail | valence VAD-r vs model size (1.7B → 8B): report the slope |

Behavioral eval: actor = each model (codebook affect-bearing vs current-renderer
affect-bearing, identical scaffold, only the affect section differs); judge =
different family (or the hosted API model), never actor's family/size.

## Decision matrix (pre-committed — no post-hoc reinterpretation)

| G-BEH | SCALE slope | Decision |
|---|---|---|
| PASS | rising (1.7B→8B climbs) | **GO** — rent V4-Flash, run the corrected pipeline at production scale |
| PASS | flat | codebook helps but production gain uncertain → evaluate the **self-hostable-model** path (ship a model you can extract from) instead of the V4-Flash rental |
| FAIL | any | **SHELVE** codebook; keep the current renderer indefinitely; redirect to S4 (memory) / S5 (cognition) |

G-ABS fail (codebook can't beat chance) ⇒ SHELVE regardless of G-BEH.

## Parallelization
Track per model (Qwen-1.7B, Gemma-1B: full; 7–8B: EV-only) run P2→P5 concurrently;
P1 shared; P6 fans out actor-generation + judge across affect bands; P3/P5/P7
barriers. Peak ~5–6 agents.

## Deliverables
1. Corrected extraction (polarity, layer rule, orthogonalization) + per-layer
   sensitivity curves.
2. Arousal reference with G-DATA evidence.
3. Codebooks per model; quality diagnostics (H2/H2d).
4. Behavioral eval: codebook vs renderer separability with CIs (G-BEH/G-ABS);
   scale-trend curve (SCALE).
5. **Decision memo** applying the pre-committed matrix → GO / self-host / SHELVE.
6. Repro bundle (seeds, revisions, dataset versions).

## Out of scope
V4-Flash extraction (gated on this spike's GO); runtime steering; production
wiring; anything in wave-1 (proceeds independently — the renderer is unchanged in
production regardless of this spike's outcome).

## One line
Spike 1 proved the proxy (VAD-alignment) was the wrong gate; spike 2 fixes the
confounds and asks the real question — **does the model's own affect vocabulary
beat our hand-built renderer, and does the signal grow with scale** — with the
GO / self-host / SHELVE decision pre-committed before a single run.

---

## Orchestrator decisions (2026-08-16, mine unless marked)

1. **Larger model: Qwen/Qwen3-8B** (open weights, no gate; same family as
   Qwen3-1.7B → within-family SCALE slope; Gemma-2-9B would confound size with
   family). Revision pinned by the 8B-prep agent via HfApi, recorded in
   repro_bundle.json.
2. **Codebook readout method (all actors identical):** bin by EV projection at
   the C2-selected layer; token distribution = softmax of last-position logits
   per bin (forward-only, works at 4-bit). J-lens vocabulary readout kept for
   small models as a secondary artifact (polarity-corrected).
3. **Judges:** Qwen3-1.7B and Qwen3-8B actors → Gemma-3-1B judge; Gemma-3-1B
   actor → Qwen3-1.7B judge. Never actor's family. Local, deterministic.
4. **C2 band:** middle third of depth = layers [⌊N/3⌋, ⌈2N/3⌉) (Qwen3-1.7B:
   [9,19); Gemma-3-1B: [8,18); Qwen3-8B: [12,24)). Selection on TRAIN by
   valence-r within band; full per-layer sensitivity reported.
5. **C3 implementation:** per layer, arousal direction = unit(proj of raw
   arousal direction onto complement of valence direction) — Gram-Schmidt, then
   unit-normalize; applied to both EV and JL directions.
6. **4-bit caveat (labeled):** the 8B SCALE point is quantized (NF4) vs bf16
   small models — a documented confound for the SCALE diagnostic; G-BEH is on
   the 8B actor and stands on its own merits.
7. **G-DATA definition (operational):** surviving separation = mean |intensity
   (hi) − intensity(lo)| over contrast groups passing the ≥ 0.30 intensity-gap
   filter on the TRAIN split; reported with n groups, distribution, bins
   coverage. Same definition as spike 1's measurement for the GoEmotions case
   (0.054), so the two are comparable.
