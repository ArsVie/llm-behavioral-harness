# Stimulus corpus — emotion-codebook spike (P1)

Pre-registered: `docs/exp-affect-codebook-pipeline-2026-08-15.md` (§3 Datasets, §4 P1). Phase P1 built this corpus; phases P2+ consume it. **HARD RULE (pre-registered): the `heldout.jsonl` split must NEVER be used for any fitting downstream** — geometry validation (H1) and behavior (H3/H4) only.

## Files

| file | contents |
|---|---|
| `train.jsonl` | one JSON object per line — train stimuli (row schema below) |
| `heldout.jsonl` | same schema — held-out stimuli (never fit on) |
| `stats.json` | machine-readable counts backing every number in this README |
| `READY` | empty marker — written last, after line-count + schema verification |

### Row schema (interface contract — exact)

```json
{"id": str, "axis": "valence"|"arousal", "intensity": float in [0,1], "text": str,
 "v": float, "a": float, "d": float, "source": "nrc-vad"|"warriner"|"emobank"|"goemotions",
 "contrast_group": str}
```

- One row per **(item × axis)** pair: every stimulus appears with `axis=valence` and/or `axis=arousal` (whichever axis it was contrastively matched on). `intensity` is the item's human VAD coordinate on that axis; the full `v/a/d` coordinates are kept on every row (the scale stays continuous — bins below are a grouping aid only).
- `id` = `<item_id>::<axis>`; strip the `::<axis>` suffix to recover the item id. Row ids are unique within a file and item ids are disjoint across files.
- `contrast_group` = `<axis>:<split>:g<NNNN>:<side>`, `side` ∈ {`hi`, `lo`}. `NNNN` is unique per split (the group counter runs across axes within a split). All rows sharing `<axis>:<split>:g<NNNN>` form one contrastive set: `hi` rows are the high-intensity half, `lo` rows the low-intensity half, matched on the other VAD dims (see below). Within a group, the i-th `hi` row by id pairs with the i-th `lo` row by id. A group holds ≤ 8 pairs (16 rows).

## Sources & preprocessing

All raw files are the P0-pinned artifacts under `datasets/` (sha256 in `repro_bundle.json`). Row counts below are **RE-DERIVED** from the pinned files by this build (2026-08-15).

| source | loaded | kept (after dedupe/drop) | normalization | notes |
|---|---|---|---|---|
| `nrc-vad` | 19974 | 19974 | already [0,1] | headerless TSV; 0 malformed lines skipped; 0 duplicate words kept-first |
| `warriner` | 13915 | 13915 | (x−1)/8 (1–9 scale) | 0 duplicate lemmas kept-first; 0 rows with NaN means skipped |
| `emobank` | 10062 | 9906 | (x−1)/4 (1–5 scale) | 0 empty texts skipped; 156 duplicate texts kept-first; EmoBank's own split column ignored (we use our own split) |
| `goemotions` | 54263 | 53994 | NRC-VAD means per category (see below) | 54263 rows → 53994 unique texts (269 duplicates kept-first) |

Warriner et al. 2013 has no frequency column in the pinned file (JULIELab distribution), so frequency matching is not possible; word **length** is used as the confound proxy (documented limitation).

## GoEmotions VAD mapping (documented method)

GoEmotions has no direct VAD; coordinates are transferred from NRC-VAD by a fully data-driven lexicon method: for each of the 28 categories (27 + `neutral`, index order = column order of the pinned raw parquet), the category VAD is the **mean NRC-VAD over all tokens of all texts carrying that label** (tokens = `[a-z']+` runs of the lowercased text; tokens absent from the NRC-VAD lexicon are skipped). Each text's VAD is then the mean over its labels' category VADs. Every category matched ≥ 1 token (no fallback was needed).

| idx | category | texts | matched tokens | V | A | D |
|---|---|---|---|---|---|---|
| 0 | admiration | 5102 | 23029 | 0.6872 | 0.4739 | 0.5654 |
| 1 | amusement | 2891 | 11581 | 0.6192 | 0.4629 | 0.5143 |
| 2 | anger | 1953 | 8657 | 0.4853 | 0.5195 | 0.4922 |
| 3 | annoyance | 3086 | 15191 | 0.5248 | 0.4808 | 0.4923 |
| 4 | approval | 3680 | 17490 | 0.6271 | 0.442 | 0.537 |
| 5 | caring | 1366 | 7395 | 0.6244 | 0.454 | 0.528 |
| 6 | confusion | 1665 | 7780 | 0.5904 | 0.44 | 0.5189 |
| 7 | curiosity | 2716 | 11913 | 0.6036 | 0.4507 | 0.5239 |
| 8 | desire | 801 | 4214 | 0.6401 | 0.461 | 0.5316 |
| 9 | disappointment | 1581 | 7802 | 0.5408 | 0.4704 | 0.4924 |
| 10 | disapproval | 2577 | 12147 | 0.5683 | 0.461 | 0.5171 |
| 11 | disgust | 1008 | 4719 | 0.4889 | 0.497 | 0.4684 |
| 12 | embarrassment | 374 | 1868 | 0.5247 | 0.4747 | 0.4704 |
| 13 | excitement | 1037 | 4335 | 0.67 | 0.4866 | 0.5448 |
| 14 | fear | 759 | 3614 | 0.4803 | 0.5236 | 0.4898 |
| 15 | gratitude | 3304 | 14463 | 0.7135 | 0.4406 | 0.5378 |
| 16 | grief | 95 | 448 | 0.4894 | 0.4869 | 0.459 |
| 17 | joy | 1772 | 8381 | 0.6964 | 0.4908 | 0.555 |
| 18 | love | 2544 | 11888 | 0.6891 | 0.4564 | 0.5399 |
| 19 | nervousness | 208 | 1013 | 0.4961 | 0.5171 | 0.4801 |
| 20 | optimism | 1969 | 11311 | 0.6554 | 0.4425 | 0.55 |
| 21 | pride | 141 | 604 | 0.6824 | 0.4992 | 0.5953 |
| 22 | realization | 1381 | 6640 | 0.5939 | 0.4447 | 0.518 |
| 23 | relief | 182 | 828 | 0.6438 | 0.4538 | 0.5256 |
| 24 | remorse | 668 | 3215 | 0.5419 | 0.4396 | 0.4575 |
| 25 | sadness | 1618 | 7645 | 0.5276 | 0.4601 | 0.4671 |
| 26 | surprise | 1328 | 5596 | 0.6109 | 0.4689 | 0.5246 |
| 27 | neutral | 17714 | 75479 | 0.5831 | 0.4437 | 0.5122 |

## Contrastive group construction

Per (split, source, axis), items are grouped into **matching cells**: the two *other* VAD dims are binned at width 0.2 (`round(x/0.2)`, 6 bins each) plus a length bucket (words: ≤4 / 5–8 / ≥9 chars; sentences: ≤8 / 9–20 / ≥21 tokens). Within a cell, items are split into **high/low halves** by the target axis (stable sort by (coordinate, item id); the median item is dropped when the cell is odd) and paired by **reflection**: the i-th most extreme high pairs with the i-th most extreme low. Pairs with intensity separation < 0.05 are dropped (documented filter: a 'contrastive' pair must actually contrast). Pairs are chunked into contrast groups of ≤ 8 pairs. Items that end up unmatched (odd-median surplus, or dropped pairs) are **excluded from the corpus** — every row in the corpus belongs to a contrastive set, which is the corpus's purpose.

## Binning (grouping aid — coordinates are never discarded)

`bin = min(9, int(intensity × 10))`, width 0.1. Each row keeps its exact continuous `intensity`; bins only organize reporting below and for P2 subsampling.

## Train / held-out split

- Item-level (both axis rows of an item go to the same split), per source: `n_train = round(0.85 × n)`, remainder held out.
- Deterministic: `rng_for(MASTER_SEED, "stimuli", "split", <source>)` — string keys sha256-encoded to ints (`seed_key_int` in `scripts/build_stimuli.py`) because `np.random.SeedSequence` requires int spawn keys; actual split seeds in `stats.json` → `seeds`.
- Contrastive groups are built **within** each split, so no group straddles the split.
- EmoBank/GoEmotions internal dataset splits are ignored.

### Split counts (RE-DERIVED)

| source | train | heldout |
|---|---|---|
| `nrc-vad` | 16978 | 2996 |
| `warriner` | 11828 | 2087 |
| `emobank` | 8420 | 1486 |
| `goemotions` | 45895 | 8099 |

## Counts per axis / split / source (RE-DERIVED)

### valence — train

| source | rows | groups | matched items | dropped (unmatched) | dropped (sep<0.05) |
|---|---|---|---|---|---|
| `nrc-vad` | 14850 | 962 | 14850 | 10 | 2086 |
| `warriner` | 9766 | 629 | 9766 | 7 | 2036 |
| `emobank` | 5362 | 349 | 5362 | 5 | 3042 |
| `goemotions` | 25374 | 1590 | 25374 | 1 | 20514 |

Totals: **55352 rows, 3530 contrast groups**; intensity range [0.0, 1.0]; 10/10 non-empty bins; mean within-group hi−lo separation 0.1787.

### valence — heldout

| source | rows | groups | matched items | dropped (unmatched) | dropped (sep<0.05) |
|---|---|---|---|---|---|
| `nrc-vad` | 2574 | 197 | 2574 | 10 | 384 |
| `warriner` | 1718 | 125 | 1718 | 10 | 340 |
| `emobank` | 990 | 69 | 990 | 7 | 480 |
| `goemotions` | 4434 | 280 | 4434 | 0 | 3660 |

Totals: **9716 rows, 671 contrast groups**; intensity range [0.016, 1.0]; 10/10 non-empty bins; mean within-group hi−lo separation 0.1782.

### arousal — train

| source | rows | groups | matched items | dropped (unmatched) | dropped (sep<0.05) |
|---|---|---|---|---|---|
| `nrc-vad` | 14322 | 931 | 14322 | 8 | 2610 |
| `warriner` | 9500 | 618 | 9500 | 4 | 2298 |
| `emobank` | 5670 | 372 | 5670 | 5 | 2728 |
| `goemotions` | 2434 | 154 | 2434 | 0 | 43456 |

Totals: **31926 rows, 2075 contrast groups**; intensity range [0.046, 0.99]; 10/10 non-empty bins; mean within-group hi−lo separation 0.1949.

### arousal — heldout

| source | rows | groups | matched items | dropped (unmatched) | dropped (sep<0.05) |
|---|---|---|---|---|---|
| `nrc-vad` | 2466 | 185 | 2466 | 11 | 496 |
| `warriner` | 1652 | 122 | 1652 | 5 | 408 |
| `emobank` | 998 | 75 | 998 | 7 | 472 |
| `goemotions` | 408 | 27 | 408 | 0 | 7686 |

Totals: **5524 rows, 409 contrast groups**; intensity range [0.071, 0.98]; 10/10 non-empty bins; mean within-group hi−lo separation 0.1873.

### Bin coverage per axis/split (rows per bin, all sources; RE-DERIVED)

| bin (intensity range) | valence train | valence heldout | arousal train | arousal heldout |
|---|---|---|---|---|
| [0.0, 0.1) | 588 | 85 | 22 | 3 |
| [0.1, 0.2) | 1525 | 274 | 476 | 84 |
| [0.2, 0.3) | 2472 | 433 | 3466 | 613 |
| [0.3, 0.4) | 3544 | 630 | 5536 | 966 |
| [0.4, 0.5) | 6482 | 1160 | 10070 | 1683 |
| [0.5, 0.6) | 19232 | 3404 | 6771 | 1185 |
| [0.6, 0.7) | 14789 | 2548 | 3107 | 579 |
| [0.7, 0.8) | 4988 | 867 | 1470 | 250 |
| [0.8, 0.9) | 1308 | 234 | 781 | 120 |
| [0.9, 1.0) | 424 | 81 | 227 | 41 |

## Determinism

- Master seed `20260815` (pre-registered); every random draw goes through `harness/determinism.py` `derive_seed`/`rng_for` — no global RNG state, no unseeded `default_rng`.
- File row order is deterministic (sorted by contrast_group, then id).
- Rebuild: `.venv/bin/python scripts/build_stimuli.py`; verify: `.venv/bin/python scripts/verify_stimuli.py` (re-reads the jsonl files independently).

## Verification

`scripts/verify_stimuli.py` re-reads both jsonl files and checks: line counts vs `stats.json`; the exact 9-key schema on every row; id uniqueness; train/heldout item disjointness; axis/source/contrast_group formats; intensity ∈ [0,1] and == the axis coordinate; per-group hi mean ≥ lo mean; per (split, axis) bin coverage and min/max intensity. `READY` was written only after verification passed.

## Known limitations

- Warriner frequency matching unavailable (no frequency column in pinned file) — length bucket used instead for word sources.
- Matching cells are 0.2-wide on the other VAD dims: residual confound within ±0.2 on matched dims is possible; pairwise reflection pairing maximizes target-axis separation.
- GoEmotions VAD coordinates are category-level (28 distinct triples), so within-category contrast is zero by construction; contrastive groups there contrast *categories* sharing a matching cell.
- **GoEmotions contributes little arousal contrast**: its category-level arousal values cluster near mid-scale (0.44–0.52), so 43,456/45,895 train rows and 7,686/8,099 heldout rows fail the 0.05 separation filter; surviving arousal groups have mean hi−lo separation ≈ 0.054. Treat GoEmotions as a valence/behavioral-context source, not an arousal source.
- Corpus = contrastively-matched items only; unmatched items are excluded by design.
