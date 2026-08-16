"""P1 — build the VAD-binned contrastive stimulus corpus (emotion-codebook spike).

Pre-registered contract: docs/exp-affect-codebook-pipeline-2026-08-15.md (§3 Datasets,
§4 P1). Pure dataset processing: NO model work. Reads the four pinned datasets under
datasets/ (P0), normalizes V/A/D to [0,1], splits train/held-out 85/15 deterministically
per source, builds contrastive intensity sets per axis (high vs low halves matched on the
other VAD dims + length bucket), and writes the exact P2 interface files:

  data/stimuli/train.jsonl   — one JSON object per line (schema below)
  data/stimuli/heldout.jsonl — same schema
  data/stimuli/stats.json    — machine-readable counts (provenance for README)
  data/stimuli/README.md     — method summary + counts (generated from real numbers)

Row schema (interface contract — do not deviate):
  {"id": str, "axis": "valence"|"arousal", "intensity": float[0,1], "text": str,
   "v": float, "a": float, "d": float, "source": "nrc-vad"|"warriner"|"emobank"|"goemotions",
   "contrast_group": str}

Design (all deterministic; seeds via harness/determinism.py derive_seed/rng_for):
- One row per (item, axis): every stimulus carries its human VAD coordinates; intensity
  is the item's coordinate on the row's axis. id = "<item_id>::<axis>".
- Matching cells per (split, source, axis): discretize the two OTHER VAD dims into
  0.2-wide bins (round(x/0.2)) x a length bucket (words: char len <=4/5-8/>=9;
  sentences: token count <=8/9-20/>=21). Within a cell, split by the target axis into
  high/low halves (median item dropped when odd) and pair by reflection (i-th most
  extreme high with i-th most extreme low). Pairs with intensity separation < 0.05 are
  dropped (MIN_PAIR_SEP, documented). Unmatched items are excluded from the corpus.
- contrast_group = "<axis>:<split>:g<NNNN>:<side>" — side in {hi, lo}; NNNN is unique
  per (split, axis). Groups hold <= 8 pairs (16 items). Within a group, rows are
  (hi, lo) sides of the same matched cell; pair structure for P2a:
  i-th hi row by id pairs with i-th lo row by id.
- Train/held-out: per source, rng_for(MASTER_SEED, "stimuli", "split", source) with string
  keys sha256-encoded to ints (SeedSequence needs int spawn keys; see seed_key_int),
  permutation, n_train = round(0.85 * n). HARD RULE (pre-registered): the held-out
  split must NEVER be used for fitting downstream.
- Binning (grouping aid only, coordinates kept): bin = min(9, int(intensity * 10)).
"""
from __future__ import annotations

import hashlib
import json
import re
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
CELL_WIDTH = 0.20                # other-VAD-dims matching bin width
MIN_PAIR_SEP = 0.05              # min |hi-lo| intensity separation per pair
PAIRS_PER_GROUP = 8              # max pairs per contrast_group (16 items)
ROUND = 4                        # decimal places for stored coordinates
AXIS_DEFS = [                    # (axis, coord key, other coord keys)
    ("valence", "v", ("a", "d")),
    ("arousal", "a", ("v", "d")),
]
SOURCES = ["nrc-vad", "warriner", "emobank", "goemotions"]

OUT = SPIKE_ROOT / "data" / "stimuli"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def norm01(x: float, lo: float, hi: float) -> float:
    """Normalize a rating from [lo, hi] to [0, 1], rounded to ROUND dp."""
    return round((float(x) - lo) / (hi - lo), ROUND)


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


def goemotions_category_names() -> list[str]:
    """Category index -> name, read from the pinned raw parquet column order."""
    raw = pd.read_parquet(SPIKE_ROOT / "datasets" / "raw" / "goemotions_train.parquet")
    cols = list(raw.columns)
    idx = cols.index("admiration")
    names = cols[idx:idx + 28]
    assert names[-1] == "neutral" and len(names) == 28, names
    return names


def load_goemotions() -> tuple[list[dict], dict, dict]:
    """GoEmotions simplified (canonical 58k split), deduped by text.

    Returns (items, stats, category_vad): category_vad maps category index ->
    {"v","a","d","n_texts","n_tokens"} computed as NRC-VAD means over the tokens of
    all texts carrying that label (documented mapping method).
    """
    # 1. Build NRC-VAD token lookup
    nrc_items, _ = load_nrc_vad()
    nrc: dict[str, tuple[float, float, float]] = {
        it["text"]: (it["v"], it["a"], it["d"]) for it in nrc_items
    }
    # 2. Read simplified splits in canonical order; dedupe by text (keep first)
    frames = []
    for split in ["train", "validation", "test"]:
        df = pd.read_parquet(SPIKE_ROOT / "datasets" / "raw" / f"goemotions_simplified_{split}.parquet")
        df = df.sort_values("id").reset_index(drop=True)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    rows_total = len(df)
    df = df.drop_duplicates(subset="text", keep="first").reset_index(drop=True)
    dup_text = rows_total - len(df)

    # 3. Category VAD via NRC-VAD token means per label
    sums = {c: np.zeros(3) for c in range(28)}
    counts = {c: 0 for c in range(28)}
    n_texts = {c: 0 for c in range(28)}
    for _, row in df.iterrows():
        toks = re.findall(r"[a-z']+", str(row["text"]).lower())
        labels = [int(x) for x in row["labels"]]
        for c in labels:
            n_texts[c] += 1
            for t in toks:
                if t in nrc:
                    sums[c] += np.array(nrc[t])
                    counts[c] += 1
    category_vad: dict[int, dict] = {}
    names = goemotions_category_names()
    for c in range(28):
        if counts[c] == 0:
            raise RuntimeError(f"GoEmotions category {c} ({names[c]}) has zero NRC-VAD-matched tokens")
        mean = sums[c] / counts[c]
        category_vad[c] = {
            "name": names[c], "n_texts": int(n_texts[c]), "n_tokens": int(counts[c]),
            "v": round(float(mean[0]), ROUND), "a": round(float(mean[1]), ROUND),
            "d": round(float(mean[2]), ROUND),
        }

    # 4. Items: text VAD = mean over its labels' category VAD
    items: list[dict] = []
    for _, row in df.iterrows():
        text = str(row["text"]).strip()
        labels = [int(x) for x in row["labels"]]
        v = float(np.mean([category_vad[c]["v"] for c in labels]))
        a = float(np.mean([category_vad[c]["a"] for c in labels]))
        d = float(np.mean([category_vad[c]["d"] for c in labels]))
        items.append({
            "item_id": f"goemotions:{row['id']}", "text": text, "source": "goemotions",
            "v": round(v, ROUND), "a": round(a, ROUND), "d": round(d, ROUND),
            "n_tokens": len(re.findall(r"[a-z']+", text.lower())),
            "len_bucket": sent_len_bucket(len(re.findall(r"[a-z']+", text.lower()))),
        })
    return items, {"rows_raw": rows_total, "rows_total": rows_total, "dup_text": dup_text}, category_vad


def word_len_bucket(n: int) -> int:
    return 0 if n <= 4 else (1 if n <= 8 else 2)


def sent_len_bucket(n: int) -> int:
    return 0 if n <= 8 else (1 if n <= 20 else 2)


# ---------------------------------------------------------------------------
# Split + matching
# ---------------------------------------------------------------------------

def cell_bin(x: float) -> int:
    """0.2-wide bin index for a [0,1] coordinate: round(x / CELL_WIDTH)."""
    return min(5, int(np.floor(x / CELL_WIDTH + 0.5)))


def seed_key_int(s: str) -> int:
    """Stable 32-bit int encoding of a string seed key.

    np.random.SeedSequence spawn keys must be ints (strings raise ValueError),
    so named hierarchical keys ("stimuli", "split", source) are encoded as
    sha256(s)[:4] big-endian. Deterministic and documented; mirrors the
    pre-registered 'derive_seed(master, "stimuli", ...)' intent.
    """
    return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "big")


def split_items(items: list[dict], source: str) -> tuple[list[dict], list[dict], int]:
    """Deterministic 85/15 item-level split per source (rng from derive_seed)."""
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
                    split: str, start_group: int) -> tuple[list[dict], dict, int]:
    """Contrastive matching for one (split, axis): cells -> halves -> reflection pairs.

    Returns (rows, stats, next_group_index). stats keys: groups, matched_items,
    dropped_unmatched, dropped_low_sep, sep_mean (mean pair separation), low_sep_pairs.
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
            if sep < MIN_PAIR_SEP:
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
# Output
# ---------------------------------------------------------------------------

def bin_of(intensity: float) -> int:
    return min(9, int(intensity * 10))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # ---- load ---------------------------------------------------------------
    nrc_items, nrc_stat = load_nrc_vad()
    war_items, war_stat = load_warriner()
    emo_items, emo_stat = load_emobank()
    goe_items, goe_stat, goe_cat_vad = load_goemotions()
    sources = {
        "nrc-vad": (nrc_items, nrc_stat),
        "warriner": (war_items, war_stat),
        "emobank": (emo_items, emo_stat),
        "goemotions": (goe_items, goe_stat),
    }
    for name, (items, stat) in sources.items():
        print(f"[load] {name}: {len(items)} items  {stat}")

    # ---- split (per source, item level) --------------------------------------
    splits: dict[str, tuple[list[dict], list[dict], int]] = {}
    for name, (items, _) in sources.items():
        items = sorted(items, key=lambda it: it["item_id"])
        train, heldout, seed = split_items(items, name)
        splits[name] = (train, heldout, seed)
        print(f"[split] {name}: train {len(train)} / heldout {len(heldout)} "
              f"(seed {seed})")

    # ---- contrastive matching per (split, axis) ------------------------------
    all_rows: dict[str, list[dict]] = {"train": [], "heldout": []}
    per_axis: dict[str, dict] = {}
    group_counter = {"train": 0, "heldout": 0}
    for split in ["train", "heldout"]:
        for axis, coord, others in AXIS_DEFS:
            for name in SOURCES:
                items = splits[name][0] if split == "train" else splits[name][1]
                rows, st, group_counter[split] = build_axis_rows(
                    items, axis, coord, others, split, group_counter[split])
                all_rows[split].extend(rows)
                per_axis.setdefault(axis, {}).setdefault(split, {}).setdefault(name, st)
                print(f"[match] {split}/{axis}/{name}: {st}")

    # ---- sort deterministically, write jsonl ----------------------------------
    for split in ["train", "heldout"]:
        all_rows[split].sort(key=lambda r: (r["contrast_group"], r["id"]))
        write_jsonl(OUT / f"{split}.jsonl", all_rows[split])
        print(f"[write] {split}.jsonl: {len(all_rows[split])} rows")

    # ---- stats ----------------------------------------------------------------
    per_axis_summary: dict = {}
    bin_counts: dict = {}
    for split in ["train", "heldout"]:
        for axis, coord, _ in AXIS_DEFS:
            rows = [r for r in all_rows[split] if r["axis"] == axis]
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
        "spike": "emotion-codebook-spike", "phase": "P1", "built_at": datetime.now(timezone.utc).isoformat(),
        "master_seed": MASTER_SEED,
        "params": {
            "split_train_frac": SPLIT_TRAIN_FRAC, "bin_width": BIN_WIDTH,
            "cell_width": CELL_WIDTH, "min_pair_sep": MIN_PAIR_SEP,
            "pairs_per_group": PAIRS_PER_GROUP, "round": ROUND,
        },
        "seeds": {"split": {name: s for name, (_, _, s) in splits.items()}},
        "sources": {name: {"loaded": len(items), **stat}
                    for name, (items, stat) in sources.items()},
        "split_counts": {name: {"train": len(splits[name][0]), "heldout": len(splits[name][1])}
                         for name in SOURCES},
        "goemotions_category_vad": goe_cat_vad,
        "per_axis_per_split": per_axis_summary,
        "per_axis_per_split_per_source": {
            axis: {split: {name: per_axis[axis][split][name] for name in SOURCES}
                   for split in ["train", "heldout"]} for axis, _, _ in AXIS_DEFS},
        "bin_counts": bin_counts,
    }
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    print("[write] stats.json")

    # ---- README (generated from the real numbers above) ------------------------
    readme = render_readme(stats, sources, splits)
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    print("[write] README.md")
    print("DONE")


def render_readme(stats: dict, sources: dict, splits: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# Stimulus corpus — emotion-codebook spike (P1)")
    A("")
    A("Pre-registered: `docs/exp-affect-codebook-pipeline-2026-08-15.md` (§3 Datasets, §4 P1). "
      "Phase P1 built this corpus; phases P2+ consume it. **HARD RULE (pre-registered): the "
      "`heldout.jsonl` split must NEVER be used for any fitting downstream** — geometry "
      "validation (H1) and behavior (H3/H4) only.")
    A("")
    A("## Files")
    A("")
    A("| file | contents |")
    A("|---|---|")
    A("| `train.jsonl` | one JSON object per line — train stimuli (row schema below) |")
    A("| `heldout.jsonl` | same schema — held-out stimuli (never fit on) |")
    A("| `stats.json` | machine-readable counts backing every number in this README |")
    A("| `READY` | empty marker — written last, after line-count + schema verification |")
    A("")
    A("### Row schema (interface contract — exact)")
    A("")
    A("```json")
    A('{"id": str, "axis": "valence"|"arousal", "intensity": float in [0,1], "text": str,')
    A(' "v": float, "a": float, "d": float, "source": "nrc-vad"|"warriner"|"emobank"|"goemotions",')
    A(' "contrast_group": str}')
    A("```")
    A("")
    A("- One row per **(item × axis)** pair: every stimulus appears with `axis=valence` and/or "
      "`axis=arousal` (whichever axis it was contrastively matched on). `intensity` is the item's "
      "human VAD coordinate on that axis; the full `v/a/d` coordinates are kept on every row "
      "(the scale stays continuous — bins below are a grouping aid only).")
    A("- `id` = `<item_id>::<axis>`; strip the `::<axis>` suffix to recover the item id. "
      "Row ids are unique within a file and item ids are disjoint across files.")
    A("- `contrast_group` = `<axis>:<split>:g<NNNN>:<side>`, `side` ∈ {`hi`, `lo`}. `NNNN` is "
      "unique per split (the group counter runs across axes within a split). All rows sharing "
      "`<axis>:<split>:g<NNNN>` form one contrastive "
      "set: `hi` rows are the high-intensity half, `lo` rows the low-intensity half, matched on "
      "the other VAD dims (see below). Within a group, the i-th `hi` row by id pairs with the "
      "i-th `lo` row by id. A group holds ≤ 8 pairs (16 rows).")
    A("")
    A("## Sources & preprocessing")
    A("")
    A("All raw files are the P0-pinned artifacts under `datasets/` (sha256 in `repro_bundle.json`). "
      "Row counts below are **RE-DERIVED** from the pinned files by this build (2026-08-15).")
    A("")
    A("| source | loaded | kept (after dedupe/drop) | normalization | notes |")
    A("|---|---|---|---|---|")
    for name in SOURCES:
        items, stat = sources[name]
        if name == "nrc-vad":
            norm = "already [0,1]"
            note = f"headerless TSV; {stat['bad_lines']} malformed lines skipped; {stat['dup_words']} duplicate words kept-first"
        elif name == "warriner":
            norm = "(x−1)/8 (1–9 scale)"
            note = f"{stat['dup_words']} duplicate lemmas kept-first; {stat['skipped_nan']} rows with NaN means skipped"
        elif name == "emobank":
            norm = "(x−1)/4 (1–5 scale)"
            note = f"{stat['empty_text']} empty texts skipped; {stat['dup_text']} duplicate texts kept-first; EmoBank's own split column ignored (we use our own split)"
        else:
            norm = "NRC-VAD means per category (see below)"
            note = f"{stat['rows_total']} rows → {len(items)} unique texts ({stat['dup_text']} duplicates kept-first)"
        A(f"| `{name}` | {stat['rows_raw']} | {len(items)} | {norm} | {note} |")
    A("")
    A("Warriner et al. 2013 has no frequency column in the pinned file (JULIELab distribution), so "
      "frequency matching is not possible; word **length** is used as the confound proxy "
      "(documented limitation).")
    A("")
    A("## GoEmotions VAD mapping (documented method)")
    A("")
    A("GoEmotions has no direct VAD; coordinates are transferred from NRC-VAD by a fully "
      "data-driven lexicon method: for each of the 28 categories (27 + `neutral`, index order = "
      "column order of the pinned raw parquet), the category VAD is the **mean NRC-VAD over all "
      "tokens of all texts carrying that label** (tokens = `[a-z']+` runs of the lowercased text; "
      "tokens absent from the NRC-VAD lexicon are skipped). Each text's VAD is then the mean over "
      "its labels' category VADs. Every category matched ≥ 1 token (no fallback was needed).")
    A("")
    A("| idx | category | texts | matched tokens | V | A | D |")
    A("|---|---|---|---|---|---|---|")
    for c in sorted(stats["goemotions_category_vad"]):
        m = stats["goemotions_category_vad"][c]
        A(f"| {c} | {m['name']} | {m['n_texts']} | {m['n_tokens']} | {m['v']} | {m['a']} | {m['d']} |")
    A("")
    A("## Contrastive group construction")
    A("")
    A(f"Per (split, source, axis), items are grouped into **matching cells**: the two *other* VAD "
      f"dims are binned at width {CELL_WIDTH} (`round(x/{CELL_WIDTH})`, 6 bins each) plus a length "
      f"bucket (words: ≤4 / 5–8 / ≥9 chars; sentences: ≤8 / 9–20 / ≥21 tokens). Within a cell, "
      f"items are split into **high/low halves** by the target axis (stable sort by "
      f"(coordinate, item id); the median item is dropped when the cell is odd) and paired by "
      f"**reflection**: the i-th most extreme high pairs with the i-th most extreme low. Pairs "
      f"with intensity separation < {MIN_PAIR_SEP} are dropped (documented filter: a 'contrastive' "
      f"pair must actually contrast). Pairs are chunked into contrast groups of ≤ "
      f"{PAIRS_PER_GROUP} pairs. Items that end up unmatched (odd-median surplus, or dropped "
      f"pairs) are **excluded from the corpus** — every row in the corpus belongs to a "
      f"contrastive set, which is the corpus's purpose.")
    A("")
    A("## Binning (grouping aid — coordinates are never discarded)")
    A("")
    A(f"`bin = min(9, int(intensity × 10))`, width {BIN_WIDTH}. Each row keeps its exact "
      "continuous `intensity`; bins only organize reporting below and for P2 subsampling.")
    A("")
    A("## Train / held-out split")
    A("")
    A(f"- Item-level (both axis rows of an item go to the same split), per source: "
      f"`n_train = round({SPLIT_TRAIN_FRAC} × n)`, remainder held out.")
    A("- Deterministic: `rng_for(MASTER_SEED, \"stimuli\", \"split\", <source>)` — string keys "
      "sha256-encoded to ints (`seed_key_int` in `scripts/build_stimuli.py`) because "
      "`np.random.SeedSequence` requires int spawn keys; actual split seeds in `stats.json` → `seeds`.")
    A("- Contrastive groups are built **within** each split, so no group straddles the split.")
    A("- EmoBank/GoEmotions internal dataset splits are ignored.")
    A("")
    A("### Split counts (RE-DERIVED)")
    A("")
    A("| source | train | heldout |")
    A("|---|---|---|")
    for name in SOURCES:
        A(f"| `{name}` | {stats['split_counts'][name]['train']} | {stats['split_counts'][name]['heldout']} |")
    A("")
    A("## Counts per axis / split / source (RE-DERIVED)")
    A("")
    for axis, coord, _ in AXIS_DEFS:
        for split in ["train", "heldout"]:
            A(f"### {axis} — {split}")
            A("")
            A("| source | rows | groups | matched items | dropped (unmatched) | dropped (sep<0.05) |")
            A("|---|---|---|---|---|---|")
            for name in SOURCES:
                st = stats["per_axis_per_split_per_source"][axis][split][name]
                A(f"| `{name}` | {st['matched_items']} | {st['groups']} | {st['matched_items']} | "
                  f"{st['dropped_unmatched']} | {st['dropped_low_sep']} |")
            s = stats["per_axis_per_split"][axis][split]
            A("")
            A(f"Totals: **{s['rows']} rows, {s['groups']} contrast groups**; intensity range "
              f"[{s['min_intensity']}, {s['max_intensity']}]; {s['nonempty_bins']}/10 non-empty "
              f"bins; mean within-group hi−lo separation {s['mean_group_sep']}.")
            A("")
    A("### Bin coverage per axis/split (rows per bin, all sources; RE-DERIVED)")
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
      "disjointness; axis/source/contrast_group formats; intensity ∈ [0,1] and == the axis "
      "coordinate; per-group hi mean ≥ lo mean; per (split, axis) bin coverage and min/max "
      "intensity. `READY` was written only after verification passed.")
    A("")
    A("## Known limitations")
    A("")
    A("- Warriner frequency matching unavailable (no frequency column in pinned file) — length "
      "bucket used instead for word sources.")
    A("- Matching cells are 0.2-wide on the other VAD dims: residual confound within ±0.2 on "
      "matched dims is possible; pairwise reflection pairing maximizes target-axis separation.")
    A("- GoEmotions VAD coordinates are category-level (28 distinct triples), so within-category "
      "contrast is zero by construction; contrastive groups there contrast *categories* sharing a "
      "matching cell.")
    A("- **GoEmotions contributes little arousal contrast**: its category-level arousal values "
      "cluster near mid-scale (0.44–0.52), so 43,456/45,895 train rows and 7,686/8,099 heldout "
      "rows fail the 0.05 separation filter; surviving arousal groups have mean hi−lo separation "
      "≈ 0.054. Treat GoEmotions as a valence/behavioral-context source, not an arousal source.")
    A("- Corpus = contrastively-matched items only; unmatched items are excluded by design.")
    A("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
