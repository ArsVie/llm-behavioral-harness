# P6 machinery — behavioral eval (G-ABS / G-BEH), spike 2

Date: 2026-08-16 · Builder: P6-machinery subagent · Contract:
`docs/exp-affect-codebook-spike2-2026-08-16.md` (Gates table; Orchestrator
decision 3 = cross-family judge).

## Scripts (experiments/emotion-codebook-spike2/scripts/)

| File | Role |
|---|---|
| `p6_common.py` | shared machinery: model registry, band table, real-renderer + codebook prompt assembly, P6 token renderer, G-MASK scan, masked-diff invariant, judge rubric/parse, seeded bootstrap CIs, paths |
| `p6_prompts.py` | `--variant renderer\|codebook --actor … --band …` prints the assembled prompt; `--check` asserts the masked-diff invariant + G-MASK per (actor, band) |
| `p6_generate.py` | `--actor … --variant … --k N --bands … [--device cpu\|cuda]` generates K replies per band, checkpointed per band (JSONL), deterministic per-sample seeds |
| `p6_judge.py` | cross-family judge (default per decision 3), reply-only 3-way classification, greedy; `--paired` computes the paired G-BEH statistic + CIs |
| `p6_stats.py` | consolidates judged JSONLs → `diagnostics/p6-eval.json` with G-ABS and G-BEH verdicts per pre-registration |

## Prompt variants (identical scaffold, only the affect slot differs)

Both variants go through the REAL harness assembly path
(`harness.assembler.assemble_snapshot` → three-tier prompt:
`SYSTEM_CORE_WITH_TOOLS` + day block + state card):

- **Renderer variant**: `harness.behavior.derive_behavior(DayRecord,
  TimingParams, hour)` → `BehaviorDirective.prompt_brief` (the 8×6 = 48-state
  renderer's mood brief) → `assemble_snapshot(prompt_brief=…)`. No
  hand-rolled lookalike: the renderer's exact thresholds/band templates are
  used, and the shared `BehaviorBrief` also produces the availability line
  (`_availability_line`) and the BEHAVIORAL BEARING section.
- **Codebook variant**: the SAME scaffold (same `BehaviorBrief` → identical
  availability + BEHAVIORAL BEARING + all other sections), with the mood
  brief slot filled by the **P6 token renderer v1** from the actor's OWN
  valence codebook (`data/codebooks/<actor>/valence_codebook.json`):
  top-k (k=10) node candidates by prob desc (tie: token asc), filtered
  (non-empty; no digits; ≥1 alphanumeric; no forbidden engine substring),
  deduped keep-first, lowercase-folded, space-joined, prose shape
  `"Current bearing: <words>."`. Deterministic, RNG-free, provenance rule
  (tokens from node candidate lists only). No LLM polish at build time
  (P7's concern).

## Band table (pre-registered in p6_common.BANDS)

3-way eval bands realized on the renderer's integer mood scale
(mood_scale=10, valence = 2·M/10 − 1); energy/arousal held FIXED at 0.55
(mid) in both variants (the judge's task is a valence-band classification;
arousal is not manipulated in P6 — documented).

| band | M | renderer valence | renderer bearing | codebook coord ((v+1)/2) | codebook node |
|---|---|---|---|---|---|
| low | 1 | −0.80 | somber | 0.10 | j=10 |
| mid | 6 | +0.20 | even | 0.60 | j=60 |
| high | 9 | +0.80 | buoyant | 0.90 | j=90 |

Energy 0.55 is realized on the REAL renderer path by a deterministic hour
search (0.25 h grid, phase follicular → hour 0.25, energy ≈ 0.5491,
"lively" pace, mid availability). The hour never appears in the prompt (no
temporal section rendered).

## G-MASK (hard invariant)

Zero engine numbers in any assembled prompt: digits, `m=`/`g=`/`arg=`/`p=`/
`v=`/`a=`/`d=` substrings, `mu`/`eta`/`cycle`/`hormone`/`t_h`/phase labels
substrings, standalone `g` (test_snapshot battery semantics). Scanned on
both variants, the fixed USER_LINE, the COMPANION_PREFIX and the judge
rubric. Result at build time: PASS for all 3 actors × 3 bands.

## Invariant check (--check)

The two variants must be byte-identical except the AFFECTIVE BEARING
section (masked diff; availability line also byte-identical by design).
Result at build time: PASS for qwen, gemma, qwen8b × low/mid/high.

## Determinism & pairing

- Seeds: `derive_seed(MASTER_SEED, 10, actor_idx, band_idx, i)` per
  generated sample — the seed does NOT depend on the variant, so renderer
  and codebook runs at the same band share the same sampling stream
  (paired contexts/levels per G-BEH; pairs = same band, same index i).
- Decoding: pre-registered `DecodingConfig` (temperature 0.8, top_p 0.9,
  top_k 40, do_sample, 128 new tokens), `seed_everything` immediately
  before each `generate`.

## Orchestrator decision 8 (2026-08-16, after selftest) — judge-input normalization

The K=2 CPU selftest (qwen actor) showed base-model actors echo the state
card verbatim at the start of replies (`[SOMBER, LIVELY]`,
`[State Card: Affective Bearing: ...]`, `(card: neutral, ...)`) and then
role-play further `User:` / `Nova:` turns. Judging raw replies measures
LABEL LEAKAGE, not affect-bearing behavior — and the leakage is asymmetric:
renderer labels are the rubric's own words (trivial for the judge), codebook
tokens are not. Fix (implemented in `p6_judge.judged_text`, applied IDENTICALLY
to both variants, full raw reply retained in JSONL for provenance):
  1. truncate every line at its first `[` or `(card` (case-insensitive);
  2. cut at the first line starting with `User:`.
Verified on all selftest replies: zero card echoes remain in judged text.
Gate thresholds untouched — this is measurement hygiene, not a gate change.
- Judge: greedy (`do_sample=False`) — deterministic by construction; seed
  recorded for provenance.
- Bootstraps: percentile 2.5/97.5, 10k resamples, `rng_for(MASTER_SEED, 12,
  …)`.

## Judge design

Cross-family per decision 3 (qwen/qwen8b actors → gemma judge; gemma actor
→ qwen judge; `p6_judge.py` refuses actor==judge and same-family). The
judge gets the RAW REPLY ONLY (3-way rubric, G-MASK clean, no numbers, no
affect tokens). Per-reply 3-way judgment is the uniform choice for BOTH
gates (defined for all bands incl. mid; shared with G-ABS). A direction-
aware "which reply is more extreme" pairwise prompt was considered and
rejected: ill-defined for the mid band and not shared with G-ABS
(documentation in the --paired output).

## Loaders

Reused EXACTLY from the p2 extraction scripts: qwen/gemma bf16
(`AutoModelForCausalLM`/`Gemma3ForCausalLM` + `model.to(device)`), qwen8b
BitsAndBytes NF4 4-bit with device_map="cuda" (plus the lm_head-unquantized
guard). Pinned revisions identical to repro_bundle.json. qwen8b is CUDA-only
(no CPU NF4).

## Selftest (CPU, K=2) — plumbing proof

`p6_prompts.py --check` (all actors), `p6_generate.py --actor qwen --k 2
--device cpu` (both variants), `p6_judge.py --actor qwen --k 2 --device cpu
--paired`, `p6_stats.py`. Expected: invariant + G-MASK PASS; generation and
judging produce real rows; stats report G-ABS/G-BEH FAIL on K (K=2 < 30 —
expected; plumbing proven). Full K≥30 runs are launched by the orchestrator
on GPU.
