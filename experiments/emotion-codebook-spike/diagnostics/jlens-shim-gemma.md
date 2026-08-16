# J-lens shim — Gemma-3-1B (P2b) — adaptation of the Qwen shim, and run log

Status: PRE-REGISTERED design (written before the run); run log appended below.
Spike: emotion-codebook-spike (2026-08-15). Model: google/gemma-3-1b-pt @
pinned revision `fcf18a2a879aab110ca39f8bffbccd5d49d8eb29`
(repro_bundle.json, `models.gemma3-1b-pt`).
GPU: RTX PRO 1000, 8151 MiB budget. Script: `scripts/p2_gemma_extract.py`
(adaptation of `scripts/p2_qwen_extract.py`).

## 0. Relationship to the Qwen shim

The J-lens design, math, and layer-sharded fitting procedure are **identical**
to the Qwen leg — see `diagnostics/jlens-shim-qwen.md` §0–§3, which remains
the canonical statement (no `merge()` exists in the repo; the shim replaces
it, as documented there). This document records only the deltas of the Gemma
adaptation: architecture numbers, model-layout/tokenizer differences, code
changes, and the run log.

## 1. Architecture numbers (RE-DERIVED from config.json @ pinned revision)

`model_type: gemma3_text`, `architectures: [Gemma3ForCausalLM]`:

| quantity | value |
|---|---|
| num_hidden_layers | **26** |
| hidden_size | **1152** |
| vocab_size | **262144** |
| intermediate_size | 6912 |
| num_attention_heads / num_key_value_heads | 4 / 1 |
| head_dim | 256 |
| sliding_window | 512 |
| rope_theta | 1,000,000 |
| max_position_embeddings | 32768 |
| rms_norm_eps | 1e-6 |
| params (est. from config) | ~1.00B (embed 262144×1152 = 302M + 26×26.7M) |
| bf16 weights | 2,039,065,489 bytes (model.safetensors, sha256 ee5250f6eb1aa7cf…) |

- `lm_head` is **tied** to `embed_tokens` (verified by `data_ptr()` equality at
  load; `config.tie_word_embeddings` resolves True at runtime although the raw
  config.json omits the key). The analytic check and the vocabulary readout
  use `model.lm_head.weight`, which is valid tied or untied.
- `embed_tokens` is `Gemma3TextScaledWordEmbedding` (output scaled by
  √hidden_size ≈ 33.94). Irrelevant to the shim: the backward graph leaf is
  the embedding output, which the harness builds directly
  (`embed_tokens(input_ids).detach().requires_grad_(True)`), so the scale is
  applied consistently and drops out of the analytic check (which involves
  only the final norm output and `lm_head.weight`).

## 2. Model layout and code changes vs the Qwen script

1. **Model class**: `Gemma3ForCausalLM` (explicit import; Qwen used
   `AutoModelForCausalLM`). In transformers 5.15.0, `Gemma3ForCausalLM.model`
   **is** the text backbone (`Gemma3TextModel`) exposing `.layers` /
   `.embed_tokens` / `.norm` directly — the same names the Qwen code path
   used under `model.model.*`. `_resolve_backbone()` accepts both this layout
   and the older one-level-deeper layout (`model.model.text_model`) and fails
   loudly otherwise. Hook registration, the `_x0` leaf replacement, and the
   forward/backward position-mask conventions are unchanged.
2. **Tokenizer**: `gemma3_text` maps to `GemmaTokenizer` in transformers
   5.15.0 (the SentencePiece tokenizer shared by the gemma family; the class
   was historically named `Gemma3Tokenizer` — `AutoTokenizer` is used, with a
   runtime assertion that the resolved class starts with `Gemma`). BOS=2
   `<bos>`, EOS=1 `<eos>`, PAD=0 `<pad>`. `add_special_tokens=False` emits
   **content tokens only** (no implicit BOS), so the Qwen scheme — explicit
   BOS prepend + stimulus tokens as labels, variable length ≤ 128, batch 1 —
   applies **unchanged**. Verified tokenizations (pinned revision): the
   bring-up sentence → 13 content tokens; `"hurt"` → 1; `"joy"` → 1;
   `"hurt joy"` → 2 (2-token probe).
3. **Sequence scheme**: unchanged (BOS + stimulus tokens, labels = stimulus
   tokens, labeled positions 0..L−2, forward means exclude BOS, readout =
   logits at last real position). Gemma's BOS semantics do not require a
   scheme change; this is verified empirically by the analytic check on
   sentence, 2-token, and 1-token inputs (cos > 0.99 required; see RUN LOG).
4. **Selftest**: identical procedure; the synthetic hidden dimension constant
   is set to Gemma's `SELFTEST_HIDDEN = 1152` (was 2048).
5. **Bring-up probes**: Qwen's trio (sentence, `"hurt"`, `"joy"`) kept for
   comparability **plus** a 2-token probe (`"hurt joy"`) so all three sequence
   types (sentence / 2-token / 1-token) are exercised explicitly.
6. **Output names**: all `-gemma` (`emotion_vectors-gemma.json`,
   `emotion_vectors-gemma_dirs.npz`, `sample_ids-gemma.json`,
   `jlens-gemma_dirs.npz`, `geometry-gemma.json`);
   `data/extractions/gemma/jlens_{valence,arousal}.json` (per-axis checkpoint
   writes inside `stage_p2b`, same as Qwen).
7. **Copied verbatim from the Qwen script** (no regressions): the FIXED
   `gpu_clear()` (own-pid exclusion via `--query-compute-apps`, never
   total-memory), per-axis jlens checkpoint writes inside `stage_p2b`, the
   lm_head readout device fix (numpy direction `.to(wu.device)` before the
   matmul), gradient checkpointing, bf16, the 128-token cap, and the
   deterministic seed scheme — `derive_seed(MASTER_SEED=20260815, k)` with the
   **same keys** as Qwen (1 bringup, 2 p2a, 3 p2b, 4 p3, 5 boot, 6 sample,
   99 selftest) so runs are seed-comparable.
8. **One-token divergence from the Qwen readout (SURFACED BUG)**: the Qwen
   line `torch.softmax(wu @ numpy.to(wu.device), dim=-1).numpy()` still ends
   with `.numpy()` on a **CUDA** tensor and raises `TypeError: can't convert
   cuda:0 device type tensor to numpy` — observed live on the Qwen full run
   2026-08-15 (crash at the P2b valence readout after ~3400 backward stimuli;
   commit 1625f7a's "device fix" moved the numpy operand to CUDA but did not
   move the result back). The Gemma adaptation keeps the device fix and adds
   `.cpu()` before `.numpy()`. **Action for the Qwen track: apply the same
   `.cpu()` and re-run; the Qwen full run did NOT complete.**

## 3. Analytic self-check (unchanged math)

With the tied linear head, per position p: ∂L/∂z_p = softmax(z_p) − onehot(t_p),
∂L/∂h_p = W_Uᵀ(softmax(z_p) − onehot(t_p)), mean over labeled positions with the
1/n_labeled scaling of `cross_entropy(reduction='mean')` included. The
norm-hook autograd gradient must match this closed form (cos ≥ 0.99 gate).
CPU pre-check on the real pinned weights (before the GPU window) gave
cos ≥ 0.999996 on all four probes — see RUN LOG for GPU numbers.

## 4. Pre-registered decisions

Identical to the Qwen leg (jlens-shim-qwen.md §4, §4b): H1 primary method =
emotion vectors (P2a) with train-selected layer; J-lens secondary; gates
valence r ≥ 0.60 / arousal r ≥ 0.40 with 95% bootstrap CI excluding 0;
pre-registered J-lens fallback (nearest-VAD-label readout) if the backward
leg OOMs; no fitting on held-out; seeded stratified 2,000-row train samples;
bins width 0.10 with explicit `fallback` marking of empty bins. All H1x
(Gemma-family) numbers are measured and reported by P3, never assumed.

## 5. GPU etiquette caveat (observed, surfaced)

2026-08-15: while the Qwen full run was in progress, `nvidia-smi
--query-compute-apps=pid,used_memory` reported the Qwen pid's memory as
`[N/A]`. The FIXED `gpu_clear()` parses that field with `int()`, skips
unparseable rows, and would therefore **not wait** for a process whose
memory reads `[N/A]` (it proceeds immediately). The prep run therefore waited
for the Qwen process to exit *before* launching the Gemma bring-up (no
concurrent GPU use — the card's 8151 MiB cannot hold both models' backward
legs). Orchestrator note: launch the Gemma `all` run only after the Qwen run
has exited, or rely on `gpu_clear()` in the normal case where compute-apps
memory is numeric.

---

## RUN LOG (appended after execution)

### Selftest (CPU, no model) — 2026-08-15
```
{"selftest": "PASS", "cos_ev": 0.9482, "cos_jl": 0.9571, "r": 0.9957, "ci": [0.9945, 0.9968]}
```
Seed key 99 (derive_seed(20260815, 99)); synthetic hidden dim 1152. Same
procedure/keys as Qwen; PASS.

### CPU pre-check (real pinned weights, CPU) — 2026-08-15
Full hook/leaf/analytic path on CPU before the GPU window:
layers=26 hidden=1152 vocab=262144 lm_tied=True; 27 layers captured fwd+bwd;
analytic cos: sentence 0.999996, "hurt joy" (2-token) 0.999997, "hurt"
0.999998, "joy" 0.999998. (Scratch script `scripts/_cpu_gemma_check.py`.)

### Bring-up (GPU: RTX PRO 1000, 8151 MiB) — 2026-08-15
Total wall 11.3 s (incl. load 0.7 s). gpu_clear() waited 0 s — the Qwen run
had already crashed and released the card (see §2.8; the Qwen full run did
NOT complete). Sequence types exercised: sentence (13 content tokens),
2-token ("hurt joy"), 1-token ("hurt", "joy").

| probe | L (content) | fwd peak MiB | bwd peak MiB | analytic cos | rel_diff |
|---|---|---|---|---|---|
| sentence | 14 (13) | 2069 | **3188.9** | 0.9999956 | 0.00405 |
| "hurt joy" (2-token) | 3 (2) | 2071 | 3136.3 | 0.9999980 | 0.00202 |
| "hurt" (1-token) | 2 (1) | 2071 | 3130.8 | 0.9999972 | 0.00244 |
| "joy" (1-token) | 2 (1) | 2071 | 3130.8 | 0.9999961 | 0.00283 |

Verdict: **BRINGUP PASS** — analytic cos > 0.99 on all three sequence types;
backward peak 3188.9 MiB worst case, far under the ≤ ~7000 MiB gate on the
8151 MiB card (~5 GB headroom — the full run's backward leg has ample
margin; expect P2b peak ≈ 3.2–4 GB at 128 tokens).

### Readout fix check (GPU) — 2026-08-15
The exact stage_p2b readout statement (with the `.cpu()` fix, §2.8) on a
synthetic direction: scores shape (262144,), sum 1.0, 0.16 s. PASS — the
Qwen crash path is verified fixed in the Gemma script.

### Status
- Selftest PASS, bringup PASS, readout fix verified. Full pipeline (stage
  `all`) NOT run — readiness handoff to the orchestrator (2026-08-15).
- repro_bundle.json updated: `models.gemma3-1b-pt` pinned to
  fcf18a2a879aab110ca39f8bffbccd5d49d8eb29 (mirrors the qwen entry shape:
  files/sha256/snapshot_path/total_bytes; model.safetensors 2,039,065,489
  bytes, sha256 ee5250f6eb1aa7cfb729dfd4dc8d9964fd772324776c6d00bf2bc674c069cb27).
