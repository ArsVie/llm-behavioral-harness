# Qwen3-8B scale-point prep notes (spike 2, 2026-08-16)

Track: EV-only scale point, `experiments/emotion-codebook-spike2/scripts/p2_qwen8b_extract.py`.
Contract: `docs/exp-affect-codebook-spike2-2026-08-16.md` (Models; Orchestrator decisions 1, 2, 4, 5, 6).

## Pinned model (decision 1)

- repo_id: `Qwen/Qwen3-8B` (open weights, not gated)
- revision: `b968826d9c46dd6066d109eabc6255188de91218` — resolved 2026-08-16 via
  `HfApi.model_info(..., files_metadata=True)` (latest commit on default branch).
- Weights: **5 shards** (`model-00001..00005-of-00005.safetensors`) + index — the
  repo no longer ships a single `model.safetensors`. Shard sha256/sizes recorded in
  `repro_bundle.json` (LFS metadata) and verified locally post-download (`all_match`).
- Arch (from config.json): **36 layers, hidden 4096, vocab 151936**, 32 attn heads,
  8 KV heads, intermediate 12288, max pos 40960, rms_norm_eps 1e-6, rope_theta 1e6,
  torch_dtype bf16.

## Quantization (decision 6 — labeled confound for the SCALE diagnostic)

- Method: **bitsandbytes 0.50.1, NF4 4-bit**, `bnb_4bit_compute_dtype=bf16`,
  `bnb_4bit_use_double_quant=True`, `device_map="cuda"`, `low_cpu_mem_usage=True`.
  Installed into the spike2 venv (`uv pip install --python .venv/bin/python bitsandbytes`
  → bitsandbytes==0.50.1; venv is a symlink to the spike1 venv; torch 2.11.0+cu128,
  transformers 5.15.0).
- **lm_head stays UNQUANTIZED** (verified at load: float dtype, no `quant_state`),
  so the readout path `wu = model.lm_head.weight.detach().float()` is exact
  (shape (151936, 4096)); the bringup analytic check confirms
  `cos(lm_head(norm_last), logits_last) > 0.99`.
- If bitsandbytes had failed on cu128, fallback was torchao `int4_weight_only`
  (would have been documented here); not needed.
- Quantized (NF4) vs bf16 small models = documented confound for the SCALE
  diagnostic (decision 6); G-BEH stands on its own merits.

## Divergences from the small-model (spike 1) path

- **EV forward-only at 8B**: no P2b/J-lens backward pass (does not fit 8 GB;
  decision 2 — EV is the cleaner method regardless). Seed key 3 (p2b) unused.
- **C2 (decision 4)**: layer selected on TRAIN by |valence-r| within the middle
  third of depth `[floor(36/3), ceil(72/3)) = [12, 24)`; one layer per model,
  used for both axes. Full per-layer sensitivity reported.
- **C3 (decision 5)**: per layer, arousal direction = unit(GS projection of raw
  arousal contrastive direction onto the valence complement); `orth_keep_frac`
  and pre-orth cos with valence recorded per layer.
- **Readout (decision 2)**: bins from TRAIN projections onto the C2-selected EV
  direction (minmax → [0,1], 10 bins × 0.10 — same scheme as spike 1); per-bin
  token distribution = softmax of mean **last-position** logits; top-30 tokens/bin.
  Written to `data/extractions/qwen8b/ev_bins_{axis}.json`; per-axis bins are
  checkpointed BEFORE the readout (crash resilience).
- Output suffixes `-qwen8b`: `diagnostics/emotion_vectors-qwen8b.json`,
  `geometry-qwen8b.json`, `sample_ids-qwen8b.json`, `data/extractions/qwen8b/`.
- P3 is EV-only (valence + C3-orthogonalized arousal) and **diagnostic, not a
  gate** (spike 2 demoted H1); thresholds reported for continuity.
- Seed keys mirror spike 1 (bringup 1, p2a 2, p3 4, boot 5, sample 6, selftest 99).
- `gpu_clear()` fixed version carried over (counts only OTHER compute-app pids —
  self-wait deadlock observed 2026-08-15 avoided); `.cpu()` before `.numpy()`
  everywhere; 128-token cap, batch 1.

## Verification

- selftest (CPU, seeded): PASS — EV recovery cos 0.9246, C3 orth recovery
  0.9112 (cos with valence −0.0), r 0.9947 with CI containing r, c2_band(36)=[12,24).
- bringup (GPU, 2026-08-16): PASS on RTX PRO 1000 (8151 MiB usable).
  - 4-bit NF4 load in ~6 s (cached) / 64 s (cold), **alloc after load 5887.6 MiB**;
    forward peak **5815.8–5819.4 MiB**; overall run peak 5887.6 MiB — all ≤ 7000 MiB target.
  - layers=36, hidden=4096, vocab=151936; lm_head **bf16, unquantized**
    (`quantized=False` — readout path exact).
  - analytic check (EV-only analogue of spike 1's backward check): cos of
    `lm_head(norm_last)` vs model logits at the last position = **1.000009 /
    1.000001** on 3 sequence types (long sentence, "hurt", "joy") — > 0.99 ✓.
  - readout sanity: vocab-size (151936,) finite logits; top-5 decodes are real
    tokens (" The", " A", … for the sentence; "joy", "Joy", … for "joy").
  - Full JSON in the bringup run output; recorded in `repro_bundle.json`
    (`smoke.qwen3-8B`).
- Not run: full P2a/P3 pipeline (waits on G-DATA / stimuli READY — orchestrator's gate).
