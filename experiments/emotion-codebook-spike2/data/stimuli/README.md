# Stimulus corpus — emotion-codebook spike 2 (P1)

Pre-registered: `docs/exp-affect-codebook-spike2-2026-08-16.md` (§ D real arousal reference; Orchestrator decisions §7 G-DATA). Phase P1 built this corpus; phases P2+ consume it. **HARD RULE (pre-registered): the `heldout.jsonl` split must NEVER be used for any fitting downstream** — geometry validation and behavior (H3/H4) only.

## Files

| file | contents |
|---|---|
| `train.jsonl` | one JSON object per line — train stimuli (row schema below) |
| `heldout.jsonl` | same schema — held-out stimuli (never fit on) |
| `stats.json` | machine-readable counts backing every number in this README |
| `READY` | empty marker — written last, after verification passed |

### Row schema (interface contract — exact)

```json
{"id": str, "axis": "valence"|"arousal", "intensity": float in [0,1], "text": str,
 "v": float, "a": float, "d": float, "source": "nrc-vad"|"warriner"|"emobank"|"goemotions",
 "contrast_group": str}
```

- One row per **(item × axis)** pair. `intensity` is the item's human VAD coordinate on that axis; the full `v/a/d` coordinates are kept on every row (the scale stays continuous — bins below are a grouping aid only).
- `id` = `<item_id>::<axis>`; strip the `::<axis>` suffix to recover the item id. Row ids are unique within a file and item ids are disjoint across files — **across axes too**: an item's valence and arousal rows are always in the same split (split seeds identical to spike 1).
- `contrast_group` = `<axis>:<split>:g<NNNN>:<side>`, `side` ∈ {`hi`, `lo`}. `NNNN` is unique per split (the counter runs across axes within a split; arousal numbering continues after the copied valence groups — train from g3531, heldout from g0672). Within a group, the i-th `hi` row by id pairs with the i-th `lo` row by id. A group holds ≤ 8 pairs (16 rows).

## Valence rows — copied verbatim from spike 1

The valence half of this corpus is **not rebuilt**: every `axis == "valence"` line of the committed spike 1 files (`../emotion-codebook-spike/data/stimuli/{train,heldout}.jsonl`) is carried over **byte-for-byte** (raw line preserved, same ids, same contrast_groups, same v/a/d). Spike 1's files total 87278 (train) / 15240 (heldout) lines across both axes; the valence subset is 55352 / 9716 rows. Source file sha256: `7b578a522ac885a6…` (train), `50dcbf7a24723d32…` (heldout) — full values in `stats.json`. This includes spike 1's GoEmotions-derived valence rows; GoEmotions is excluded from the AROUSAL half only (brief §D).

## Arousal rows — built NEW (G-DATA reference)

Arousal-graded stimuli are built from the three real-arousal sources (all carry direct human arousal ratings). **GoEmotions is NOT used for arousal** (brief §D — its category-level arousal clusters near mid-scale; spike 1 measured 0.054 surviving separation at a 0.05 filter). Pair-level intensity gap is **0.3** — the pre-registered G-DATA intensity-gap filter. G-DATA (hard gate): surviving separation = mean |intensity(hi) − intensity(lo)| over contrast groups passing the filter, measured on TRAIN, must be ≥ 0.30; evidence in `diagnostics/gdata-arousal.json` (written by `scripts/gdata_arousal.py`).

## G-DATA gate (pre-flight, hard) — VERDICT: PASS

Numbers RE-DERIVED from the built files by `scripts/gdata_arousal.py`
(`diagnostics/gdata-arousal.json`).

| metric | train (gate split) | heldout (reported, not gated) |
|---|---|---|
| contrast groups | **492** | 117 |
| rows | **6,700** (3,350 pairs) | 1,124 (562 pairs) |
| surviving separation mean | **0.4122** | 0.4077 |
| median | 0.3865 | 0.3863 |
| min | 0.3000 | 0.3012 |
| max | 0.7584 | 0.6265 |
| pairs below the 0.30 filter (re-derived) | 0 | 0 |
| separation deciles (p10/p50/p90) | 0.3104 / 0.3865 / 0.5575 | — |

**Gate: train mean surviving separation ≥ 0.30 → 0.4122 ≥ 0.30 → PASS.**
Compare spike 1's GoEmotions arousal reference: 0.054 surviving separation at a
0.05 filter — the new reference is ~7.6× the old surviving separation.

Arousal intensity-bin coverage (rows per bin, `bin = min(9, int(intensity×10))`):

| bin | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| train | 22 | 445 | 1563 | 951 | 290 | 600 | 1149 | 963 | 550 | 167 |
| heldout | 3 | 76 | 262 | 170 | 42 | 100 | 196 | 154 | 90 | 31 |

All 10 bins are non-empty in both splits (train min intensity 0.046, max 0.99;
heldout min 0.071, max 0.98).

## Sources & preprocessing (RE-DERIVED from the pinned files, 2026-08-16)

All raw files are the P0-pinned artifacts under `datasets/` (sha256 in `repro_bundle.json`). Parsing/normalization identical to spike 1.

| source | loaded | kept (after dedupe/drop) | normalization | notes |
|---|---|---|---|---|
| `nrc-vad` | 19974 | 19974 | already [0,1] | headerless TSV; 0 malformed lines skipped; 0 duplicate words kept-first |
| `warriner` | 13915 | 13915 | (x−1)/8 (1–9 scale) | 0 duplicate lemmas kept-first; 0 rows with NaN means skipped |
| `emobank` | 10062 | 9906 | (x−1)/4 (1–5 scale) | 0 empty texts skipped; 156 duplicate texts kept-first; EmoBank's own split column ignored |

Warriner et al. 2013 has no frequency column in the pinned file, so frequency matching is not possible; word **length** is used as the confound proxy (documented limitation, same as spike 1).

## Contrastive group construction

Per (split, source, axis), items are grouped into **matching cells**: the two *other* VAD dims are binned at width 0.2 (`round(x/0.2)`, 6 bins each) plus a length bucket (words: ≤4 / 5–8 / ≥9 chars; sentences: ≤8 / 9–20 / ≥21 tokens). Within a cell, items are split into **high/low halves** by the target axis (stable sort by (coordinate, item id); the median item is dropped when the cell is odd) and paired by **reflection**: the i-th most extreme high pairs with the i-th most extreme low. Pairs with intensity separation < 0.3 are dropped (the G-DATA intensity-gap filter: a 'contrastive' arousal pair must actually contrast by ≥ 0.30). Pairs are chunked into contrast groups of ≤ 8 pairs. Items that end up unmatched (odd-median surplus, or dropped pairs) are **excluded from the corpus** — every row in the corpus belongs to a contrastive set, which is the corpus's purpose.

## Binning (grouping aid — coordinates are never discarded)

`bin = min(9, int(intensity × 10))`, width 0.1. Each row keeps its exact continuous `intensity`; bins only organize reporting below and for P2 subsampling.

## Train / held-out split

- Item-level, per source: `n_train = round(0.85 × n)`, remainder held out.
- Deterministic: `rng_for(MASTER_SEED, "stimuli", "split", <source>)` — **identical keys to spike 1**, so every item lands in the same split as its spike-1 valence row (item-level disjointness across axes). String keys sha256-encoded to ints (`seed_key_int` in `scripts/build_stimuli.py`); actual split seeds in `stats.json` → `seeds`.
- Contrastive groups are built **within** each split, so no group straddles the split.
- EmoBank's internal dataset split is ignored.

### Split counts (RE-DERIVED)

| source | train | heldout |
|---|---|---|
| `nrc-vad` | 16978 | 2996 |
| `warriner` | 11828 | 2087 |
| `emobank` | 8420 | 1486 |

## Counts per axis / split (RE-DERIVED)

### valence — train

**55352 rows, 3530 contrast groups**; intensity range [0.0, 1.0]; 10/10 non-empty bins; mean within-group hi−lo separation 0.1787.

### valence — heldout

**9716 rows, 671 contrast groups**; intensity range [0.016, 1.0]; 10/10 non-empty bins; mean within-group hi−lo separation 0.1782.

### arousal — train

**6700 rows, 492 contrast groups**; intensity range [0.046, 0.99]; 10/10 non-empty bins; mean within-group hi−lo separation 0.4122.

### arousal — heldout

**1124 rows, 117 contrast groups**; intensity range [0.071, 0.98]; 10/10 non-empty bins; mean within-group hi−lo separation 0.4077.

### Arousal rows per source (RE-DERIVED)

| split | source | rows | groups | dropped (unmatched) | dropped (sep < 0.30) |
|---|---|---|---|---|---|
| train | `nrc-vad` | 4678 | 328 | 8 | 12254 |
| train | `warriner` | 1802 | 139 | 4 | 9996 |
| train | `emobank` | 220 | 25 | 5 | 8178 |
| heldout | `nrc-vad` | 780 | 76 | 11 | 2182 |
| heldout | `warriner` | 318 | 35 | 5 | 1742 |
| heldout | `emobank` | 26 | 6 | 7 | 1444 |

### Bin coverage per axis/split (rows per bin, RE-DERIVED)

| bin (intensity range) | valence train | valence heldout | arousal train | arousal heldout |
|---|---|---|---|---|
| [0.0, 0.1) | 588 | 85 | 22 | 3 |
| [0.1, 0.2) | 1525 | 274 | 445 | 76 |
| [0.2, 0.3) | 2472 | 433 | 1563 | 262 |
| [0.3, 0.4) | 3544 | 630 | 951 | 170 |
| [0.4, 0.5) | 6482 | 1160 | 290 | 42 |
| [0.5, 0.6) | 19232 | 3404 | 600 | 100 |
| [0.6, 0.7) | 14789 | 2548 | 1149 | 196 |
| [0.7, 0.8) | 4988 | 867 | 963 | 154 |
| [0.8, 0.9) | 1308 | 234 | 550 | 90 |
| [0.9, 1.0) | 424 | 81 | 167 | 31 |

## Determinism

- Master seed `20260815` (pre-registered); every random draw goes through `harness/determinism.py` `derive_seed`/`rng_for` — no global RNG state, no unseeded `default_rng`.
- File row order is deterministic (sorted by contrast_group, then id).
- Rebuild: `.venv/bin/python scripts/build_stimuli.py`; verify: `.venv/bin/python scripts/verify_stimuli.py` (re-reads the jsonl files independently).

## Verification

`scripts/verify_stimuli.py` re-reads both jsonl files and checks: line counts vs `stats.json`; the exact 9-key schema on every row; id uniqueness; train/heldout item disjointness (across axes); axis/source/contrast_group formats; intensity ∈ [0,1] and == the axis coordinate; per-group hi mean ≥ lo mean (arousal groups additionally ≥ 0.30); **valence rows byte-identical to spike 1** (raw-line equality + field equality by id); bin coverage per (split, axis). `READY` was written only after verification passed.

## Known limitations

- Matching cells are 0.2-wide on the other VAD dims: residual confound within ±0.2 on matched dims is possible; pairwise reflection pairing maximizes target-axis separation.
- The 0.30 intensity gap is much stricter than spike 1's 0.05: the arousal corpus is correspondingly smaller, and low-arousal bins (0–2) are sparsely covered (see bin table).
- Corpus = contrastively-matched items only; unmatched items are excluded by design.
