# P0 — Environment & provenance (emotion-codebook spike)

Date: 2026-08-15 · Phase: P0 · Pre-registered contract: docs/exp-affect-codebook-pipeline-2026-08-15.md
Repro bundle: `repro_bundle.json` (machine-readable provenance; this report mirrors it).

## Environment
- Spike root: `experiments/emotion-codebook-spike/` (repo convention: experiments only
  add under `experiments/` + `results/`).
- venv: `experiments/emotion-codebook-spike/.venv` (uv, CPython 3.12.3). Repo `.venv`
  was checked first — it has NO torch (engine/sim/harness only), so a dedicated spike
  venv was created; repo venv untouched.
- torch 2.11.0+cu128 (CUDA 12.8 build), transformers 5.15.0, datasets 5.0.1,
  numpy 2.5.2, scipy 1.18.0, huggingface_hub 1.27.0.
- `torch.cuda.is_available()` = True; device = NVIDIA RTX PRO 1000 Blackwell Laptop GPU
  (sm_120), 8151 MiB, driver 591.64 (verified via nvidia-smi).

## Models
| Model | Revision (pinned) | Status |
|---|---|---|
| Qwen/Qwen3-1.7B | `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` | ✓ downloaded, 3.8 GB cache, weights 3,441,185,608 + 622,329,984 B safetensors (bf16), load 2.9 s |
| google/gemma-3-1b-pt | `fcf18a2a879aab110ca39f8bffbccd5d49d8eb29` | ✗ BLOCKED — gated (manual), no HF token on machine |
| google/gemma-3-1b-it (alt) | `dcc83ea841ab6100d6b47a070329e1ba4cf78752` | same blocker |
| google/gemma-2-2b (fallback) | `c5ebcd40d208330abc697524c919956e692655cf` | same blocker — token gates ALL Gemma repos; size-viability moot until token |

Gemma handling per pre-registered rule: no browser auth attempted (user forbids);
user must supply token/auth link. Revision pins are recorded and ready.

## Datasets (all canonical, sha256-pinned — see `datasets/README.md` + bundle)
- NRC-VAD: saifmohammad.com NRC-VAD-Lexicon.zip → English lexicon **19,974 words**
  (word V A D, [0,1]).
- Warriner et al. 2013: JULIELab/XANEW distribution (canonical CSV) — **13,915 lemmas**
  + header (V/A/D means, SDs, demographics).
- EmoBank: JULIELab/EmoBank `corpus/emobank.csv` — **10,062 sentences**, V/A/D 1–5,
  splits train 8,062 / dev 1,000 / test 1,000 (RE-DERIVED).
- GoEmotions: HF `google-research-datasets/go_emotions` pinned rev
  `add492243ff905527e67aeb8b80c082af02207c3` — raw per-rater **211,225 rows**;
  simplified split version train 43,410 / validation 5,426 / test 5,427
  (**54,263 rows, 53,994 unique texts**, RE-DERIVED).
- No substitutions: two 404s (kabartolo Warriner mirror; goemotions GitHub raw) were
  replaced by canonical alternates (JULIELab; HF Hub) — documented in README.

## Seed policy (pre-registered)
- Master seed `20260815`; per-run seeds = `derive_seed(master, *key)` via
  SeedSequence (mirrors engine/rng.py); `seed_everything()` before any sampling;
  fixed-temperature `DecodingConfig` (temp 0.8, top_p 0.9, top_k 40, do_sample).
- Module: `harness/determinism.py` (seeded RNG + decoding helper + ProvenanceRecorder).

## Smoke test (barrier) — Qwen3-1.7B: **PASS**
- 128-token seq, bf16, gradient checkpointing, full forward+backward, CUDA.
- Peak alloc **7806.8 MiB** (fwd leg 3344.4 MiB) vs budget 8151 MiB → fits, ~4% headroom.
- 310/310 grad tensors finite (autograd alive), grad norm 88.3, loss 13.4375
  (identical across two runs with same seed — determinism confirmed).
- Timing: load 2.9 s, fwd 0.56 s, bwd 0.29 s, total ~7 s. No OOM, no sharding needed.
- No `merge()`/layer-shard helper exists in the repo (see pointers) — not needed for Qwen.

## Blockers / notes for orchestrator
1. **Gemma: needs HF token** (or auth link) from user. All Gemma repos gated.
   Pins ready. Gemma smoke = same script, one command once token present.
2. No J-lens/`merge()` code exists in the repo today (doc §1 references "repo's
   `merge()`" — aspirational). P2 must build layer-sharded fitting; Qwen smoke shows
   headroom is thin (4%), sharding likely needed for Gemma-3-1B backward at 128 tokens.
3. Memory headroom at 7806.8 MiB peak: J-lens P2b on Qwen at 128 tokens should fit
   (smoke ≈ worst case for the same shape), but activation-hook P2a is the cheap path.

## Paths located for later phases
- J-lens / merge(): **does not exist** — only doc reference
  (`docs/exp-affect-codebook-pipeline-2026-08-15.md` §1, §P2b) and an unrelated
  `merge_left_tail` helper in `tests/test_mood.py:61`.
- Engine affect contract (frozen): `engine/types.py` — `DayRecord` (t, m, g, arg, p,
  M, score, mu, eta, …) + `PersonaParams`; runtime affect: `harness/behavior.py`
  `BehaviorDirective.expressiveness` (line 41) + `derive_behavior` (line 133).
- 48-state renderer: `harness/prompts.py` STATE CARD + `harness/assembler.py`;
  rationale/quantization fix: `docs/architecture-and-results-2026-08-15.md` §3.2
  (8×6 = 48 band grid; anti-collapse test `tests/test_renderer_anticollapse.py`).
