"""P1 — spike 2 stimulus corpus: valence copied verbatim from spike 1, arousal built NEW.

Pre-registered contract: docs/exp-affect-codebook-spike2-2026-08-16.md
(§ D real arousal reference; Orchestrator decisions §7 G-DATA definition).

The corpus has two parts:

1. VALENCE — copied VERBATIM from the committed spike 1 corpus
   (../emotion-codebook-spike/data/stimuli/{train,heldout}.jsonl): every line
   whose object has "axis": "valence" is carried over byte-for-byte (the raw
   line is preserved, not re-serialized). NOT rebuilt from datasets. Same ids,
   same contrast_groups, same v/a/d — identical valence corpus. Spike 1's files
   total 87,278 (train) / 15,240 (heldout) lines across BOTH axes; the valence
   subset is 55,352 / 9,716 rows (RE-DERIVED from spike 1 stats.json).

2. AROUSAL — built NEW from the three real-arousal sources (NRC-VAD 19,974
   words, Warriner 13,915 lemmas, EmoBank 10,062 sentences). GoEmotions is
   EXCLUDED (brief §D: it carries almost no arousal signal — spike 1 measured
   0.054 surviving separation at a 0.05 filter). Pair-level intensity gap
   MIN_PAIR_SEP = 0.30 — the pre-registered G-DATA intensity-gap filter
   (G-DATA: mean surviving separation over TRAIN contrast groups >= 0.30).

Parsing/normalization, cell matching (0.2-wide bins on the other VAD dims +
length bucket), reflection pairing, 10-bin (0.1 width) stratification, and the
85/15 per-source seeded split are exactly spike 1's (scripts/build_stimuli.py
of the spike 1 experiment). The split RNG keys are IDENTICAL to spike 1's, so
every item lands in the same train/heldout split as its spike-1 valence row —
item-level disjointness holds ACROSS axes (an item's valence and arousal rows
are always in the same file).

Group ids: within a split, the group counter continues AFTER the valence groups
copied from spike 1 (train: valence g0001..g3530; heldout: g0001..g0671), so
group ids stay unique per split across axes exactly like spike 1.

Row schema (interface contract — do not deviate):
  {"id": str, "axis": "valence"|"arousal", "intensity": float[0,1], "text": str,
   "v": float, "a": float, "d": float, "source": "nrc-vad"|"warriner"|"emobank"|"goemotions",
   "contrast_group": str}
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

import numpy as np
import pandas as pd

from harness.determinism import MASTER_SEED, derive_seed, rng_for  # noqa: E402

# ---------------------------------------------------------------------------
# Parameters (documented in README; pre-registration-consistent choices)
# ---------------------------------------------------------------------------
SPLIT_TRAIN_FRAC = 0.85          # train/held-out split ratio (per source)
BIN_WIDTH = 0.10                 # intensity bin width (grouping aid only)
CELL_WIDTH = 0.20                # other-VAD-dims matching bin width (spike 1's)
MIN_PAIR_SEP = 0.30              # G-DATA intensity gap: min |hi-lo| per pair (spike 1: 0.05)
PAIRS_PER_GROUP = 8              # max pairs per contrast_group (16 items)
ROUND = 4                        # decimal places for stored coordinates
AROUSAL_SOURCES = ["nrc-vad", "warriner", "emobank"]   # GoEmotions excluded (brief §D)

SPIKE1_DIR = SPIKE_ROOT.parent / "emotion-codebook-spike" / "data" / "stimuli"
OUT = SPIKE_ROOT / "data" / "stimuli"

# Spike 1 valence row counts per split (RE-DERIVED from spike 1 stats.json
# per_axis_per_split; used as a hard assertion on the verbatim copy).
VALENCE_EXPECT = {"train": 55352, "heldout": 9716}
# Spike 1 valence group counts per split (group counter continuation).
VALENCE_GROUPS = {"train": 3530, "heldout": 671}


# ---------------------------------------------------------------------------
# Loaders (identical parsing/normalization to spike 1)
# ---------------------------------------------------------------------------

def norm01(x: float, lo: float, hi: float) -> float:
    """Normalize a rating from [lo, hi] to [0, 1], rounded to ROUND dp."""
    return round((float(x) - lo) / (hi - lo), ROUND)


def word_len_bucket(n: int) -> int:
    return 0 if n <= 4 else (1 if n <= 8 else 2)


def sent_len_bucket(n: int) -> int:
    return 0 if n <= 8 else (1 if n <= 20 else 2)


def load_nrc_vad() -> tuple[list[dict], dict]:
    """NRC-VAD English lexicon: headerless TSV word<V<A<D, V/A/D in [0,1]."""
    p = SPIKE_ROOT / "datasets" / "nrc-vad" / "NRC-VAD-Lexicon" / "NRC-VAD-Lexicon.txt"
    items: list[dict] = []
    bad = 0
    seen: set[str] = set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 4:
                bad += 1
                continue
            word, v, a, d = parts
            if word in seen:
                continue
            seen.add(word)
            items.append({
                "item_id": f"nrc-vad:{word}", "text": word, "source": "nrc-vad",
                "v": round(float(v), ROUND), "a": round(float(a), ROUND),
                "d": round(float(d), ROUND),
                "n_tokens": len(word.split()), "len_bucket": word_len_bucket(len(word)),
            })
    return items, {"rows_raw": len(items), "bad_lines": bad, "dup_words": len(seen) - len(items)}


def load_warriner() -> tuple[list[dict], dict]:
    """Warriner et al. 2013 (JULIELab secondary distribution): 1-9 scale."""
    p = SPIKE_ROOT / "datasets" / "raw" / "Ratings_Warriner_et_al.csv"
    df = pd.read_csv(p)   # column 0 is a row-number; the word is the "Word" column
    items: list[dict] = []
    seen: set[str] = set()
    skipped_nan = 0
    for _, row in df.iterrows():
        word = str(row["Word"]).strip()
        if word in seen:
            continue
        seen.add(word)
        try:
            v = norm01(row["V.Mean.Sum"], 1, 9)
            a = norm01(row["A.Mean.Sum"], 1, 9)
            d = norm01(row["D.Mean.Sum"], 1, 9)
        except (TypeError, ValueError):
            skipped_nan += 1
            continue
        items.append({
            "item_id": f"warriner:{word}", "text": word, "source": "warriner",
            "v": v, "a": a, "d": d,
            "n_tokens": len(word.split()), "len_bucket": word_len_bucket(len(word)),
        })
    return items, {"rows_raw": len(items), "dup_words": len(seen) - len(items), "skipped_nan": skipped_nan}


def load_emobank() -> tuple[list[dict], dict]:
    """EmoBank sentences: V/A/D on 1-5 scale; split column ignored (our own split)."""
    p = SPIKE_ROOT / "datasets" / "raw" / "emobank.csv"
    df = pd.read_csv(p)
    rows_raw = len(df)
    df = df.sort_values("id").reset_index(drop=True)
    items: list[dict] = []
    seen_text: set[str] = set()
    empty = 0
    dup_text = 0
    for _, row in df.iterrows():
        text = str(row["text"]).strip()
        if not text:
            empty += 1
            continue
        if text in seen_text:
            dup_text += 1
            continue
        seen_text.add(text)
        items.append({
            "item_id": f"emobank:{row['id']}", "text": text, "source": "emobank",
            "v": norm01(row["V"], 1, 5), "a": norm01(row["A"], 1, 5),
            "d": norm01(row["D"], 1, 5),
            "n_tokens": len(text.split()), "len_bucket": sent_len_bucket(len(text.split())),
        })
    return items, {"rows_raw": rows_raw, "empty_text": empty, "dup_text": dup_text}


# ---------------------------------------------------------------------------
# Split + matching (identical scheme to spike 1)
# ---------------------------------------------------------------------------

def cell_bin(x: float) -> int:
    """0.2-wide bin index for a [0,1] coordinate: round(x / CELL_WIDTH)."""
    return min(5, int(np.floor(x / CELL_WIDTH + 0.5)))


def seed_key_int(s: str) -> int:
    """Stable 32-bit int encoding of a string seed key (see spike 1 build)."""
    return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "big")


def split_items(items: list[dict], source: str) -> tuple[list[dict], list[dict], int]:
    """Deterministic 85/15 item-level split per source.

    Same RNG keys as spike 1 -> same item->split assignment, so an item's
    arousal row always lands in the same file as its spike-1 valence row.
    """
    rng = rng_for(MASTER_SEED, seed_key_int("stimuli"), seed_key_int("split"),
                  seed_key_int(source))
    n = len(items)
    n_train = int(round(SPLIT_TRAIN_FRAC * n))
    order = rng.permutation(n)
    train_ids = {items[i]["item_id"] for i in order[:n_train]}
    train = [it for it in items if it["item_id"] in train_ids]
    heldout = [it for it in items if it["item_id"] not in train_ids]
    seed = derive_seed(MASTER_SEED, seed_key_int("stimuli"), seed_key_int("split"),
                       seed_key_int(source))
    return train, heldout, seed


def build_axis_rows(items: list[dict], axis: str, coord: str, others: tuple[str, str],
                    split: str, start_group: int,
                    min_sep: float = MIN_PAIR_SEP) -> tuple[list[dict], dict, int]:
    """Contrastive matching for one (split, axis): cells -> halves -> reflection pairs.

    Returns (rows, stats, next_group_index). stats keys: groups, matched_items,
    dropped_unmatched, dropped_low_sep, sep_mean (mean pair separation),
    low_sep_pairs, sep_min, sep_max.
    """
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        key = (cell_bin(it[others[0]]), cell_bin(it[others[1]]), it["len_bucket"])
        cells[key].append(it)

    rows: list[dict] = []
    g = start_group
    matched = 0
    dropped_unmatched = 0
    dropped_low_sep = 0
    seps: list[float] = []
    low_sep_pairs = 0
    for key in sorted(cells):
        cell = sorted(cells[key], key=lambda it: (it[coord], it["item_id"]))
        n = len(cell)
        if n < 2:
            dropped_unmatched += n
            continue
        mid = n // 2
        low = cell[:mid]
        high = cell[mid + (n % 2):]          # odd n: median item dropped
        high_desc = sorted(high, key=lambda it: (-it[coord], it["item_id"]))
        low_asc = sorted(low, key=lambda it: (it[coord], it["item_id"]))
        pairs: list[tuple[dict, dict]] = []
        for h, l in zip(high_desc, low_asc):
            sep = h[coord] - l[coord]
            if sep < min_sep:
                dropped_low_sep += 2
                low_sep_pairs += 1
                continue
            pairs.append((h, l))
            seps.append(sep)
        # chunk pairs into contrast groups of <= PAIRS_PER_GROUP pairs
        for gi in range(0, len(pairs), PAIRS_PER_GROUP):
            chunk = pairs[gi:gi + PAIRS_PER_GROUP]
            g += 1
            gid = f"{axis}:{split}:g{g:04d}"
            for h, l in chunk:
                for it, side in ((h, "hi"), (l, "lo")):
                    rows.append({
                        "id": f"{it['item_id']}::{axis}",
                        "axis": axis,
                        "intensity": it[coord],
                        "text": it["text"],
                        "v": it["v"], "a": it["a"], "d": it["d"],
                        "source": it["source"],
                        "contrast_group": f"{gid}:{side}",
                    })
                matched += 2
    stats = {
        "groups": g - start_group,
        "matched_items": matched,
        "dropped_unmatched": dropped_unmatched,
        "dropped_low_sep": dropped_low_sep,
        "low_sep_pairs": low_sep_pairs,
        "sep_mean": round(float(np.mean(seps)), 4) if seps else None,
        "sep_min": round(float(np.min(seps)), 4) if seps else None,
        "sep_max": round(float(np.max(seps)), 4) if seps else None,
    }
    return rows, stats, g


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def bin_of(intensity: float) -> int:
    return min(9, int(intensity * 10))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # ---- 1. load + split (3 real-arousal sources; same split keys as spike 1) --
    loaders = {"nrc-vad": load_nrc_vad, "warriner": load_warriner, "emobank": load_emobank}
    sources: dict[str, tuple[list[dict], dict]] = {}
    splits: dict[str, tuple[list[dict], list[dict], int]] = {}
    for name in AROUSAL_SOURCES:
        items, stat = loaders[name]()
        items = sorted(items, key=lambda it: it["item_id"])
        train, heldout, seed = split_items(items, name)
        sources[name] = (items, stat)
        splits[name] = (train, heldout, seed)
        print(f"[load] {name}: {len(items)} items  {stat}")
        print(f"[split] {name}: train {len(train)} / heldout {len(heldout)} (seed {seed})")

    # ---- 2. valence: copy verbatim from spike 1 (raw lines preserved) ---------
    valence_raw: dict[str, dict[str, str]] = {}
    spike1_sha: dict[str, str] = {}
    spike1_lines: dict[str, int] = {}
    for split in ["train", "heldout"]:
        p = SPIKE1_DIR / f"{split}.jsonl"
        spike1_sha[split] = sha256_file(p)
        lines: dict[str, str] = {}
        n_lines = 0
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                n_lines += 1
                r = json.loads(line)
                if r["axis"] == "valence":
                    lines[r["id"]] = line
        spike1_lines[split] = n_lines
        assert len(lines) == VALENCE_EXPECT[split], \
            f"spike 1 valence rows {split}: {len(lines)} != {VALENCE_EXPECT[split]}"
        valence_raw[split] = lines
        print(f"[valence] {split}: copied {len(lines)} verbatim rows "
              f"from spike 1 {p.name} ({n_lines} lines total in spike 1 file)")

    # ---- 3. arousal: contrastive matching per (split, source) ------------------
    all_rows: dict[str, list[tuple[tuple[str, str], str]]] = {"train": [], "heldout": []}
    per_axis: dict[str, dict[str, dict]] = {}
    group_counter = {"train": VALENCE_GROUPS["train"], "heldout": VALENCE_GROUPS["heldout"]}
    for split in ["train", "heldout"]:
        for name in AROUSAL_SOURCES:
            items = splits[name][0] if split == "train" else splits[name][1]
            rows, st, group_counter[split] = build_axis_rows(
                items, "arousal", "a", ("v", "d"), split, group_counter[split],
                min_sep=MIN_PAIR_SEP)
            for r in rows:
                all_rows[split].append(((r["contrast_group"], r["id"]),
                                        json.dumps(r, ensure_ascii=False)))
            per_axis.setdefault(split, {})[name] = st
            print(f"[match] {split}/arousal/{name}: {st}")
        print(f"[match] {split}/arousal TOTAL: {len(all_rows[split])} rows, "
              f"last group {group_counter[split]:04d}")

    # ---- 4. combine with valence lines, sort deterministically, write ----------
    for split in ["train", "heldout"]:
        combined: list[tuple[tuple[str, str], str]] = list(all_rows[split])
        for line in valence_raw[split].values():
            r = json.loads(line)
            combined.append(((r["contrast_group"], r["id"]), line))
        combined.sort(key=lambda t: t[0])
        with open(OUT / f"{split}.jsonl", "w", encoding="utf-8") as f:
            for _, line in combined:
                f.write(line + "\n")
        print(f"[write] {split}.jsonl: {len(combined)} rows "
              f"({len(all_rows[split])} new arousal + {len(valence_raw[split])} copied valence)")

    # ---- 5. stats ---------------------------------------------------------------
    per_axis_summary: dict = {}
    bin_counts: dict = {}
    for split in ["train", "heldout"]:
        for axis, coord in (("valence", "v"), ("arousal", "a")):
            rows = []
            with open(OUT / f"{split}.jsonl", encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    if r["axis"] == axis:
                        rows.append(r)
            ints = [r["intensity"] for r in rows]
            grp = defaultdict(list)
            for r in rows:
                grp[r["contrast_group"].rsplit(":", 1)[0]].append(r)
            sep_groups = []
            for gid, grows in grp.items():
                hi = [r["intensity"] for r in grows if r["contrast_group"].endswith(":hi")]
                lo = [r["intensity"] for r in grows if r["contrast_group"].endswith(":lo")]
                sep_groups.append(float(np.mean(hi)) - float(np.mean(lo)))
            bins = defaultdict(int)
            for r in rows:
                bins[bin_of(r["intensity"])] += 1
                bin_counts.setdefault(axis, {}).setdefault(split, {})[bin_of(r["intensity"])] = \
                    bin_counts.setdefault(axis, {}).setdefault(split, {}).get(bin_of(r["intensity"]), 0) + 1
            per_axis_summary.setdefault(axis, {})[split] = {
                "rows": len(rows),
                "groups": len(grp),
                "nonempty_bins": len(bins),
                "min_intensity": round(min(ints), ROUND) if ints else None,
                "max_intensity": round(max(ints), ROUND) if ints else None,
                "mean_group_sep": round(float(np.mean(sep_groups)), 4) if sep_groups else None,
            }

    stats = {
        "spike": "emotion-codebook-spike2", "phase": "P1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "master_seed": MASTER_SEED,
        "params": {
            "split_train_frac": SPLIT_TRAIN_FRAC, "bin_width": BIN_WIDTH,
            "cell_width": CELL_WIDTH, "min_pair_sep": MIN_PAIR_SEP,
            "pairs_per_group": PAIRS_PER_GROUP, "round": ROUND,
            "arousal_sources": AROUSAL_SOURCES,
        },
        "seeds": {"split": {name: s for name, (_, _, s) in splits.items()}},
        "sources": {name: {"loaded": len(items), **stat}
                    for name, (items, stat) in sources.items()},
        "split_counts": {name: {"train": len(splits[name][0]), "heldout": len(splits[name][1])}
                         for name in AROUSAL_SOURCES},
        "valence_copied_from_spike1": {
            "train_rows": VALENCE_EXPECT["train"], "heldout_rows": VALENCE_EXPECT["heldout"],
            "spike1_train_file_lines": spike1_lines["train"],
            "spike1_heldout_file_lines": spike1_lines["heldout"],
            "spike1_train_sha256": spike1_sha["train"],
            "spike1_heldout_sha256": spike1_sha["heldout"],
            "note": "verbatim lines (axis==valence) from committed spike 1 files; not rebuilt",
        },
        "per_axis_per_split": per_axis_summary,
        "per_axis_per_split_per_source": {
            "arousal": {split: {name: per_axis[split][name] for name in AROUSAL_SOURCES}
                        for split in ["train", "heldout"]},
        },
        "bin_counts": bin_counts,
    }
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    print("[write] stats.json")

    # ---- 6. README (generated from the real numbers above) -----------------------
    (OUT / "README.md").write_text(render_readme(stats, sources, splits), encoding="utf-8")
    print("[write] README.md")
    print("DONE")


def render_readme(stats: dict, sources: dict, splits: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# Stimulus corpus — emotion-codebook spike 2 (P1)")
    A("")
    A("Pre-registered: `docs/exp-affect-codebook-spike2-2026-08-16.md` (§ D real arousal "
      "reference; Orchestrator decisions §7 G-DATA). Phase P1 built this corpus; phases P2+ "
      "consume it. **HARD RULE (pre-registered): the `heldout.jsonl` split must NEVER be used "
      "for any fitting downstream** — geometry validation and behavior (H3/H4) only.")
    A("")
    A("## Files")
    A("")
    A("| file | contents |")
    A("|---|---|")
    A("| `train.jsonl` | one JSON object per line — train stimuli (row schema below) |")
    A("| `heldout.jsonl` | same schema — held-out stimuli (never fit on) |")
    A("| `stats.json` | machine-readable counts backing every number in this README |")
    A("| `READY` | empty marker — written last, after verification passed |")
    A("")
    A("### Row schema (interface contract — exact)")
    A("")
    A("```json")
    A('{"id": str, "axis": "valence"|"arousal", "intensity": float in [0,1], "text": str,')
    A(' "v": float, "a": float, "d": float, "source": "nrc-vad"|"warriner"|"emobank"|"goemotions",')
    A(' "contrast_group": str}')
    A("```")
    A("")
    A("- One row per **(item × axis)** pair. `intensity` is the item's human VAD coordinate on "
      "that axis; the full `v/a/d` coordinates are kept on every row (the scale stays continuous "
      "— bins below are a grouping aid only).")
    A("- `id` = `<item_id>::<axis>`; strip the `::<axis>` suffix to recover the item id. "
      "Row ids are unique within a file and item ids are disjoint across files — **across axes "
      "too**: an item's valence and arousal rows are always in the same split (split seeds "
      "identical to spike 1).")
    A("- `contrast_group` = `<axis>:<split>:g<NNNN>:<side>`, `side` ∈ {`hi`, `lo`}. `NNNN` is "
      "unique per split (the counter runs across axes within a split; arousal numbering "
      f"continues after the copied valence groups — train from g{VALENCE_GROUPS['train'] + 1:04d}, "
      f"heldout from g{VALENCE_GROUPS['heldout'] + 1:04d}). Within a group, the i-th `hi` row by "
      "id pairs with the i-th `lo` row by id. A group holds ≤ 8 pairs (16 rows).")
    A("")
    A("## Valence rows — copied verbatim from spike 1")
    A("")
    A("The valence half of this corpus is **not rebuilt**: every `axis == \"valence\"` line of "
      "the committed spike 1 files "
      "(`../emotion-codebook-spike/data/stimuli/{train,heldout}.jsonl`) is carried over "
      "**byte-for-byte** (raw line preserved, same ids, same contrast_groups, same v/a/d). "
      "Spike 1's files total "
      f"{stats['valence_copied_from_spike1']['spike1_train_file_lines']} (train) / "
      f"{stats['valence_copied_from_spike1']['spike1_heldout_file_lines']} (heldout) lines "
      "across both axes; the valence subset is "
      f"{stats['valence_copied_from_spike1']['train_rows']} / "
      f"{stats['valence_copied_from_spike1']['heldout_rows']} rows. Source file sha256: "
      f"`{stats['valence_copied_from_spike1']['spike1_train_sha256'][:16]}…` (train), "
      f"`{stats['valence_copied_from_spike1']['spike1_heldout_sha256'][:16]}…` (heldout) — full "
      "values in `stats.json`. This includes spike 1's GoEmotions-derived valence rows; "
      "GoEmotions is excluded from the AROUSAL half only (brief §D).")
    A("")
    A("## Arousal rows — built NEW (G-DATA reference)")
    A("")
    A("Arousal-graded stimuli are built from the three real-arousal sources (all carry direct "
      "human arousal ratings). **GoEmotions is NOT used for arousal** (brief §D — its "
      "category-level arousal clusters near mid-scale; spike 1 measured 0.054 surviving "
      "separation at a 0.05 filter). Pair-level intensity gap is "
      f"**{stats['params']['min_pair_sep']}** — the pre-registered G-DATA intensity-gap filter. "
      "G-DATA (hard gate): surviving separation = mean |intensity(hi) − intensity(lo)| over "
      "contrast groups passing the filter, measured on TRAIN, must be ≥ 0.30; evidence in "
      "`diagnostics/gdata-arousal.json` (written by `scripts/gdata_arousal.py`).")
    A("")
    A("## Sources & preprocessing (RE-DERIVED from the pinned files, 2026-08-16)")
    A("")
    A("All raw files are the P0-pinned artifacts under `datasets/` (sha256 in "
      "`repro_bundle.json`). Parsing/normalization identical to spike 1.")
    A("")
    A("| source | loaded | kept (after dedupe/drop) | normalization | notes |")
    A("|---|---|---|---|---|")
    for name in AROUSAL_SOURCES:
        items, stat = sources[name]
        if name == "nrc-vad":
            norm = "already [0,1]"
            note = f"headerless TSV; {stat['bad_lines']} malformed lines skipped; {stat['dup_words']} duplicate words kept-first"
        elif name == "warriner":
            norm = "(x−1)/8 (1–9 scale)"
            note = f"{stat['dup_words']} duplicate lemmas kept-first; {stat['skipped_nan']} rows with NaN means skipped"
        else:
            norm = "(x−1)/4 (1–5 scale)"
            note = f"{stat['empty_text']} empty texts skipped; {stat['dup_text']} duplicate texts kept-first; EmoBank's own split column ignored"
        A(f"| `{name}` | {stat['rows_raw']} | {len(items)} | {norm} | {note} |")
    A("")
    A("Warriner et al. 2013 has no frequency column in the pinned file, so frequency matching is "
      "not possible; word **length** is used as the confound proxy (documented limitation, same "
      "as spike 1).")
    A("")
    A("## Contrastive group construction")
    A("")
    A(f"Per (split, source, axis), items are grouped into **matching cells**: the two *other* "
      f"VAD dims are binned at width {CELL_WIDTH} (`round(x/{CELL_WIDTH})`, 6 bins each) plus a "
      f"length bucket (words: ≤4 / 5–8 / ≥9 chars; sentences: ≤8 / 9–20 / ≥21 tokens). Within a "
      f"cell, items are split into **high/low halves** by the target axis (stable sort by "
      f"(coordinate, item id); the median item is dropped when the cell is odd) and paired by "
      f"**reflection**: the i-th most extreme high pairs with the i-th most extreme low. Pairs "
      f"with intensity separation < {MIN_PAIR_SEP} are dropped (the G-DATA intensity-gap "
      f"filter: a 'contrastive' arousal pair must actually contrast by ≥ 0.30). Pairs are "
      f"chunked into contrast groups of ≤ {PAIRS_PER_GROUP} pairs. Items that end up unmatched "
      f"(odd-median surplus, or dropped pairs) are **excluded from the corpus** — every row in "
      f"the corpus belongs to a contrastive set, which is the corpus's purpose.")
    A("")
    A("## Binning (grouping aid — coordinates are never discarded)")
    A("")
    A(f"`bin = min(9, int(intensity × 10))`, width {BIN_WIDTH}. Each row keeps its exact "
      "continuous `intensity`; bins only organize reporting below and for P2 subsampling.")
    A("")
    A("## Train / held-out split")
    A("")
    A(f"- Item-level, per source: `n_train = round({SPLIT_TRAIN_FRAC} × n)`, remainder held out.")
    A("- Deterministic: `rng_for(MASTER_SEED, \"stimuli\", \"split\", <source>)` — **identical "
      "keys to spike 1**, so every item lands in the same split as its spike-1 valence row "
      "(item-level disjointness across axes). String keys sha256-encoded to ints "
      "(`seed_key_int` in `scripts/build_stimuli.py`); actual split seeds in `stats.json` → `seeds`.")
    A("- Contrastive groups are built **within** each split, so no group straddles the split.")
    A("- EmoBank's internal dataset split is ignored.")
    A("")
    A("### Split counts (RE-DERIVED)")
    A("")
    A("| source | train | heldout |")
    A("|---|---|---|")
    for name in AROUSAL_SOURCES:
        A(f"| `{name}` | {stats['split_counts'][name]['train']} | {stats['split_counts'][name]['heldout']} |")
    A("")
    A("## Counts per axis / split (RE-DERIVED)")
    A("")
    for axis in ["valence", "arousal"]:
        for split in ["train", "heldout"]:
            s = stats["per_axis_per_split"][axis][split]
            A(f"### {axis} — {split}")
            A("")
            A(f"**{s['rows']} rows, {s['groups']} contrast groups**; intensity range "
              f"[{s['min_intensity']}, {s['max_intensity']}]; {s['nonempty_bins']}/10 non-empty "
              f"bins; mean within-group hi−lo separation {s['mean_group_sep']}.")
            A("")
    A("### Arousal rows per source (RE-DERIVED)")
    A("")
    A("| split | source | rows | groups | dropped (unmatched) | dropped (sep < 0.30) |")
    A("|---|---|---|---|---|---|")
    for split in ["train", "heldout"]:
        for name in AROUSAL_SOURCES:
            st = stats["per_axis_per_split_per_source"]["arousal"][split][name]
            A(f"| {split} | `{name}` | {st['matched_items']} | {st['groups']} | "
              f"{st['dropped_unmatched']} | {st['dropped_low_sep']} |")
    A("")
    A("### Bin coverage per axis/split (rows per bin, RE-DERIVED)")
    A("")
    A("| bin (intensity range) | valence train | valence heldout | arousal train | arousal heldout |")
    A("|---|---|---|---|---|")
    for b in range(10):
        lo_, hi_ = b / 10, (b + 1) / 10
        cells = []
        for axis in ["valence", "arousal"]:
            for split in ["train", "heldout"]:
                cells.append(stats["bin_counts"].get(axis, {}).get(split, {}).get(b, 0))
        A(f"| [{lo_:.1f}, {hi_:.1f}) | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
    A("")
    A("## Determinism")
    A("")
    A(f"- Master seed `{MASTER_SEED}` (pre-registered); every random draw goes through "
      "`harness/determinism.py` `derive_seed`/`rng_for` — no global RNG state, no "
      "unseeded `default_rng`.")
    A("- File row order is deterministic (sorted by contrast_group, then id).")
    A("- Rebuild: `.venv/bin/python scripts/build_stimuli.py`; verify: "
      "`.venv/bin/python scripts/verify_stimuli.py` (re-reads the jsonl files independently).")
    A("")
    A("## Verification")
    A("")
    A("`scripts/verify_stimuli.py` re-reads both jsonl files and checks: line counts vs "
      "`stats.json`; the exact 9-key schema on every row; id uniqueness; train/heldout item "
      "disjointness (across axes); axis/source/contrast_group formats; intensity ∈ [0,1] and == "
      "the axis coordinate; per-group hi mean ≥ lo mean (arousal groups additionally ≥ 0.30); "
      "**valence rows byte-identical to spike 1** (raw-line equality + field equality by id); "
      "bin coverage per (split, axis). `READY` was written only after verification passed.")
    A("")
    A("## Known limitations")
    A("")
    A("- Matching cells are 0.2-wide on the other VAD dims: residual confound within ±0.2 on "
      "matched dims is possible; pairwise reflection pairing maximizes target-axis separation.")
    A("- The 0.30 intensity gap is much stricter than spike 1's 0.05: the arousal corpus is "
      "correspondingly smaller, and low-arousal bins (0–2) are sparsely covered (see bin table).")
    A("- Corpus = contrastively-matched items only; unmatched items are excluded by design.")
    A("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
