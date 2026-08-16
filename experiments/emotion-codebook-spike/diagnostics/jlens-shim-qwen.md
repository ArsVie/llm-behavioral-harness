# J-lens shim — Qwen3-1.7B (P2b) — design, math, and run log

Status: PRE-REGISTERED design (written before the run); run log appended below.
Spike: emotion-codebook-spike (2026-08-15). Model: Qwen/Qwen3-1.7B @ pinned
revision `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` (repro_bundle.json).
GPU: RTX PRO 1000, 8151 MiB budget. Script: `scripts/p2_qwen_extract.py`.

## 0. Why a shim exists

The pre-registered brief (§1, §P2b) says the backward leg uses "the repo's
`merge()`" for layer-sharded fitting. **The repo has no such code** — P0 verified
no `merge()` exists anywhere (only a doc reference and an unrelated
`merge_left_tail` in `tests/test_mood.py:61`). This document defines the
replacement shim: a layer-sharded J-lens fitter built from scratch, with the
math stated explicitly. (P0 report, "No `merge()`/layer-shard helper exists".)

## 1. What J-lens means here (adaptation of Lester & Arora 2023)

J-Lens computes, for a target token t, the gradient of log p(t | h) with
respect to the residual stream h at each layer — a direction that points
"toward the representation that makes the model emit t". We adapt this to a
continuous affect axis a ∈ {valence, arousal}:

- Per stimulus i and layer ℓ, the per-position gradient
  g_{i,ℓ,p} = ∂L_i / ∂h_{i,ℓ,p}, where L_i is the mean next-token
  cross-entropy of the stimulus over its labeled positions (0..n_real−2) and
  h_{i,ℓ,p} is the output residual stream of layer ℓ at position p.
- Per-stimulus direction: d_{i,ℓ} = mean over labeled positions of g_{i,ℓ,p}.
- Axis direction (value-centered J-lens direction):
  **D_ℓ^a = Σ_i (y_i^a − ȳ^a) · d_{i,ℓ}**, where y_i^a is the human VAD value
  of stimulus i on axis a (from P1's train stimuli) and ȳ^a its mean.
  Positive weight on high-value stimuli ⇒ D_ℓ^a points toward representations
  associated with high human value on axis a (sign fixed by construction;
  a negative held-out correlation is a surfaced negative result, never a
  silent flip).

Rationale (documented decision): the per-position gradient of next-token CE
w.r.t. the residual stream is the J-lens quantity; centering by (y−ȳ) turns the
set of per-stimulus directions into one contrastive axis direction without any
probe fitting on the validation split (train only).

## 2. Layer-sharded fitting procedure (memory-tight)

Qwen3-1.7B: 28 decoder layers, hidden 2048, vocab 151936, tied embeddings.

Key memory result (RE-DERIVED from the P0 smoke numbers): the P0 smoke's
7806.8 MiB peak was dominated by **parameter gradients** (1.72B params × 2 B ≈
3.4 GB). The J-lens leg needs gradients w.r.t. residual streams only, so the
graph leaf is the embedding output:

1. `x0 = embed_tokens(input_ids).detach().requires_grad_(True)` — a fresh leaf.
2. `model.requires_grad_(False)` — no parameter grad buffers are ever created.
3. Forward with `use_cache=False`, then CE loss over labeled positions
   (padding labeled −100), then `loss.backward()`.
4. Per-layer gradients are captured with `Module.register_full_backward_hook`
   on each decoder layer and on the final RMSNorm. Under gradient checkpointing
   the forward is recomputed per layer during backward, so at any instant only
   one layer's activations live: **this is the sharding** — the backward is
   processed as 28 sequential layer shards, each bounded in memory, and each
   shard's contribution is accumulated into the shared per-layer direction
   accumulators `acc[ℓ] += (y_i − ȳ)·d_{i,ℓ}` (one accumulator per axis).
5. `torch.cuda.empty_cache()` every 25 stimuli; batch size 1; 128-token
   sequences (truncate, pad with eos-as-pad; labels −100 on padding).

Convention: layer ℓ output = residual stream after layer ℓ (input to layer
ℓ+1); the final RMSNorm output is the head input h_L. Forward hooks average
over all real tokens; backward hooks average over labeled positions so the
analytic check below is exact.

### Analytic self-check of the shim (final layer)

With a linear head (tied W_U, no bias), per position p:
∂L/∂z_p = softmax(z_p) − onehot(t_p), and ∂L/∂h_p = W_Uᵀ(softmax(z_p) − onehot(t_p)).
The norm-hook autograd gradient must match this closed form (mean over labeled
positions). The run log records cos ≥ 0.99 — this proves the hook capture is
correct end-to-end (checkpointing + backward hooks + leaf graph).

## 3. Binning and vocabulary readout (feeds P4's three-field artifact)

- Binning signal: projection p_i = ⟨h̄_i, D̂⟩ of the **post-norm** (head-input)
  activation h̄_i onto the unit axis direction D̂ (post-norm space is the space
  W_U maps to logits, so it is the right space for both binning and readout).
- Map: p̃_i = (p_i − p_min)/(p_max − p_min) using **train** min/max → [0,1];
  bins of width 0.10 (10 bins), floor(p̃_i / 0.10), clamped to bin 9.
- Per-bin vocabulary distribution: V_b = softmax( (1/n_b) Σ_{i∈b} z_i ),
  where z_i = the stimulus's **last-real-position** logits and T = 1.0
  (documented; the model's own next-token distribution conditioned on the
  internal state in that bin — the raw evidence for P4's `tokens` field).
- Axis-level J-lens token scores (secondary): softmax(W_U D̂) — the tokens the
  recovered direction points at in the embedding space.
- Empty bins (n=0): filled from the nearest non-empty bin, explicitly marked
  `fallback` in the artifact (pre-registered: collapsed bins are surfaced, not
  dropped). Full-bin coverage is reported (`empty_bins`).

## 4. Pre-registered decisions (fixed before the run)

1. H1 primary method = **emotion vectors (P2a)**, evaluated with the layer
   that maximizes |train Pearson r| of projections vs human value (selection on
   TRAIN only; held-out r is the reported gate number). J-lens is the secondary
   method with the same selection rule. Gate: valence r ≥ 0.60 AND arousal
   r ≥ 0.40 AND 95% bootstrap CI (n=1000, seeded `rng_for(master, 5, axis,
   method, layer)`) excludes 0. A negative held-out r for a direction is
   reported as a reversed layer — never flipped after the fact.
2. Fallback (only if the backward leg OOMs on 8 GB even sharded): keep P2a
   emotion vectors for geometry, and write the P2b artifacts with
   **nearest-VAD-label lexical readout** (NRC-VAD lexicon words ranked by
   |score − bin center|), marking `fallback: true` in the JSON and this doc.
   J-lens vocabulary quality is then deferred to rental scale. Never dropped
   silently.
3. No fitting on held-out data anywhere; held-out used once, for evaluation.
4. Seeds: bringup=1, p2a=2, p2b=3, p3=4, boot=5, sample=6 via
   derive_seed(20260815, k); seed_everything() before every stochastic stage
   (here: no sampling, dropout 0.0 in config — determinism is structural).

## 4b. Amendments after P1 stimuli landed (2026-08-15, before the P2 run)

- **Sequence scheme**: BOS + stimulus tokens, variable length (no padding,
  batch 1, L ≤ 128). Labels = stimulus tokens (next-token prediction from BOS
  context) so single-word NRC-VAD stimuli ("hurt") are usable. Labeled
  positions 0..L−2; forward activation means exclude the BOS position;
  vocabulary readout = logits at the last real position. The analytic check
  (§2) is unchanged and remains exact (verified cos > 0.99999 on sentence,
  2-token and 1-token inputs; backward peak 4520.9 MiB worst case).
- **High/low split**: `contrast_group` side suffix (`:hi`/`:lo`) — P1's own
  contrastive construction (per-group hi mean ≥ lo mean, verified by P1).
  Fallbacks: intensity median, then axis-value median.
- **Seeded stratified sampling for fitting passes** (efficiency, budget):
  the full train corpus (87,278 rows) cannot be backward-passed on 8 GB in
  reasonable time, so P2a/P2b fit on up to 2,000 rows per axis sampled
  deterministically (`rng_for(master, 6, axis)`) stratified by P1's intensity
  bins (width 0.1) to preserve coverage across [0,1]; sample ids + counts are
  recorded in `diagnostics/sample_ids-qwen.json`. P3 evaluates ALL held-out
  rows (15,240). Directions/readouts are sample-conditional; this is recorded
  and reproducible, not a data modification.
- The vocabulary readout per bin is the model's own next-token distribution at
  the last real position, softmax-mean over stimuli in the bin (T=1.0).

---

## RUN LOG (appended after execution)

(filled in after the run — see report below)
