"""P1 — independent verification of spike 2 data/stimuli/{train,heldout}.jsonl.

Re-reads the emitted files from disk (no build-state reuse) and checks:

- exact 9-key schema on every row; axis/source validity (per axis: arousal rows
  NEVER come from goemotions); contrast_group format; numeric ranges;
  intensity == axis coordinate; axis/split consistency; id suffix; text.
- id uniqueness within a file; item disjointness between train and heldout
  (item = id minus ::axis suffix — covers BOTH axes).
- per-group hi/lo balance, <= 8 pairs, mean hi >= mean lo; arousal groups
  additionally mean hi - mean lo >= 0.30 (the G-DATA filter, re-derived).
- VALENCE IDENTICAL TO SPIKE 1: for every valence row in spike 2 the raw line
  must be byte-identical to spike 1's line for the same id, and vice versa
  (every spike 1 valence id present, same split); counts 55,352 / 9,716.
  Spike 1 source files must have 87,278 / 15,240 lines and sha256 as recorded
  in stats.json (provenance of the verbatim copy).
- line counts vs stats.json; bin coverage per (split, axis).
- G-DATA gate re-check on TRAIN arousal groups: mean surviving separation
  >= 0.30 (hard, pre-registered).

Exits non-zero on any failure.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
OUT = SPIKE_ROOT / "data" / "stimuli"
SPIKE1_DIR = SPIKE_ROOT.parent / "emotion-codebook-spike" / "data" / "stimuli"

AXES = {"valence", "arousal"}
SOURCES_BY_AXIS = {
    "valence": {"nrc-vad", "warriner", "emobank", "goemotions"},
    "arousal": {"nrc-vad", "warriner", "emobank"},   # GoEmotions excluded (brief §D)
}
KEYS = {"id", "axis", "intensity", "text", "v", "a", "d", "source", "contrast_group"}
GROUP_RE = re.compile(r"^(valence|arousal):(train|heldout):g\d{4}:(hi|lo)$")
GATE_MIN_SEP = 0.30
VALENCE_EXPECT = {"train": 55352, "heldout": 9716}
SPIKE1_FILE_LINES = {"train": 87278, "heldout": 15240}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_file_lines(split: str) -> list[str]:
    path = OUT / f"{split}.jsonl"
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def check(split: str) -> tuple[list[dict], list[str], dict]:
    lines = read_file_lines(split)
    rows: list[dict] = []
    for ln, line in enumerate(lines, 1):
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            return [], [f"line {ln}: invalid JSON: {e}"], {}
        rows.append(r)
    errs: list[str] = []

    # schema + ranges
    for i, r in enumerate(rows):
        if set(r.keys()) != KEYS:
            errs.append(f"row {i}: keys {sorted(r.keys())} != {sorted(KEYS)}")
            continue
        if r["axis"] not in AXES:
            errs.append(f"row {i}: bad axis {r['axis']!r}")
        if r["source"] not in SOURCES_BY_AXIS.get(r["axis"], set()):
            errs.append(f"row {i}: bad source {r['source']!r} for axis {r['axis']!r}")
        if not GROUP_RE.match(r["contrast_group"]):
            errs.append(f"row {i}: bad contrast_group {r['contrast_group']!r}")
        for k in ("intensity", "v", "a", "d"):
            val = r[k]
            if not (isinstance(val, (int, float)) and not isinstance(val, bool) and 0.0 <= val <= 1.0):
                errs.append(f"row {i}: {k}={val!r} not in [0,1]")
        axis_coord = {"valence": "v", "arousal": "a"}[r["axis"]]
        if abs(r["intensity"] - r[axis_coord]) > 1e-9:
            errs.append(f"row {i}: intensity {r['intensity']} != {axis_coord} {r[axis_coord]}")
        if r["axis"] != r["contrast_group"].split(":")[0]:
            errs.append(f"row {i}: axis {r['axis']} != group axis {r['contrast_group']}")
        if split != r["contrast_group"].split(":")[1]:
            errs.append(f"row {i}: split {split} != group split {r['contrast_group']}")
        if not r["id"].endswith(f"::{r['axis']}"):
            errs.append(f"row {i}: id {r['id']!r} lacks ::{r['axis']} suffix")
        if not isinstance(r["text"], str) or not r["text"]:
            errs.append(f"row {i}: bad text")

    # id uniqueness
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        dup = {x for x in ids if ids.count(x) > 1}
        errs.append(f"duplicate row ids: {sorted(dup)[:5]}")

    # per-group balance + hi>=lo (+ arousal >= GATE_MIN_SEP)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["contrast_group"].rsplit(":", 1)[0]].append(r)
    for gid, grows in groups.items():
        hi = [r for r in grows if r["contrast_group"].endswith(":hi")]
        lo = [r for r in grows if r["contrast_group"].endswith(":lo")]
        if len(hi) != len(lo) or len(hi) == 0:
            errs.append(f"group {gid}: unbalanced sides hi={len(hi)} lo={len(lo)}")
            continue
        if len(hi) > 8:
            errs.append(f"group {gid}: {len(hi)} pairs > 8")
        m_hi = sum(r["intensity"] for r in hi) / len(hi)
        m_lo = sum(r["intensity"] for r in lo) / len(lo)
        if m_hi < m_lo:
            errs.append(f"group {gid}: mean hi {m_hi:.4f} < mean lo {m_lo:.4f}")
        if gid.startswith("arousal:") and m_hi - m_lo < GATE_MIN_SEP - 1e-9:
            errs.append(f"group {gid}: arousal mean hi-lo {m_hi - m_lo:.4f} < {GATE_MIN_SEP}")

    # bin coverage per axis
    cov = {}
    for axis in sorted(AXES):
        ints = [r["intensity"] for r in rows if r["axis"] == axis]
        bins = {min(9, int(x * 10)) for x in ints}
        cov[axis] = {
            "rows": len(ints),
            "groups": sum(1 for g in groups if g.startswith(axis + ":")),
            "nonempty_bins": len(bins),
            "min": round(min(ints), 4) if ints else None,
            "max": round(max(ints), 4) if ints else None,
        }
    return rows, errs, cov


def check_valence_identical() -> tuple[list[str], dict]:
    """Every spike 2 valence line byte-identical to spike 1's, same split, both directions."""
    errs: list[str] = []
    counts = {}
    for split in ["train", "heldout"]:
        s1: dict[str, str] = {}
        with open(SPIKE1_DIR / f"{split}.jsonl", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                r = json.loads(line)
                if r["axis"] == "valence":
                    s1[r["id"]] = line
        s2: dict[str, str] = {}
        with open(OUT / f"{split}.jsonl", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                r = json.loads(line)
                if r["axis"] == "valence":
                    s2[r["id"]] = line
        counts[split] = (len(s1), len(s2))
        if len(s1) != VALENCE_EXPECT[split]:
            errs.append(f"spike 1 valence rows {split}: {len(s1)} != {VALENCE_EXPECT[split]}")
        if len(s2) != len(s1):
            errs.append(f"spike 2 valence rows {split}: {len(s2)} != spike 1 {len(s1)}")
        missing = sorted(set(s1) - set(s2))[:5]
        extra = sorted(set(s2) - set(s1))[:5]
        if missing:
            errs.append(f"valence {split}: spike 1 ids missing in spike 2: {missing}")
        if extra:
            errs.append(f"valence {split}: spike 2 ids not in spike 1: {extra}")
        for i, (k, line1) in enumerate(s1.items()):
            line2 = s2.get(k)
            if line2 is None:
                continue
            if line1 != line2:
                errs.append(f"valence {split}: line differs for {k!r}")
                if len(errs) > 30:
                    break
        # field-level equality (paranoid double check)
        for k, line1 in s1.items():
            if k not in s2:
                continue
            r1, r2 = json.loads(line1), json.loads(s2[k])
            for field in ("id", "axis", "intensity", "text", "v", "a", "d", "source", "contrast_group"):
                if r1[field] != r2[field]:
                    errs.append(f"valence {split}: field {field} differs for {k!r}")
    return errs, counts


def main() -> None:
    ok = True
    all_items: dict[str, str] = {}
    counts = {}
    for split in ["train", "heldout"]:
        res = check(split)
        rows, errs, cov = res
        counts[split] = len(rows)
        for r in rows:
            item_id = r["id"].rsplit("::", 1)[0]
            other = all_items.get(item_id)
            if other is not None and other != split:
                errs.append(f"item {item_id!r} appears in both {other} and {split}")
            all_items[item_id] = split
        print(f"== {split}.jsonl: {len(rows)} rows ==")
        for axis, c in cov.items():
            print(f"   {axis}: rows={c['rows']} groups={c['groups']} nonempty_bins="
                  f"{c['nonempty_bins']}/10 min={c['min']} max={c['max']}")
        if errs:
            ok = False
            print(f"   FAILURES ({len(errs)}):")
            for e in errs[:40]:
                print("   -", e)

    # valence identical to spike 1 (byte-for-byte + counts)
    verrs, vcounts = check_valence_identical()
    for split, (n1, n2) in vcounts.items():
        print(f"== valence {split}: spike 1 {n1} rows / spike 2 {n2} rows "
              f"(byte-identical lines: {n1 == n2}) ==")
    if verrs:
        ok = False
        print(f"   VALENCE FAILURES ({len(verrs)}):")
        for e in verrs[:40]:
            print("   -", e)

    # spike 1 source files: line counts + sha256 vs stats.json
    stats_path = OUT / "stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text())
        for split in ["train", "heldout"]:
            exp = stats["per_axis_per_split"]["valence"][split]["rows"] + \
                  stats["per_axis_per_split"]["arousal"][split]["rows"]
            if exp != counts[split]:
                ok = False
                print(f"count mismatch {split}: file {counts[split]} vs stats {exp}")
            else:
                print(f"line count {split}: {counts[split]} matches stats.json")
            p = SPIKE1_DIR / f"{split}.jsonl"
            n_lines = sum(1 for _ in open(p, encoding="utf-8"))
            if n_lines != SPIKE1_FILE_LINES[split]:
                ok = False
                print(f"spike 1 {split}.jsonl lines {n_lines} != {SPIKE1_FILE_LINES[split]}")
            else:
                print(f"spike 1 {split}.jsonl lines: {n_lines} matches expected")
            sha = sha256_file(p)
            if sha != stats["valence_copied_from_spike1"][f"spike1_{split}_sha256"]:
                ok = False
                print(f"spike 1 {split}.jsonl sha256 mismatch vs stats.json")
            else:
                print(f"spike 1 {split}.jsonl sha256 matches stats.json ({sha[:16]}…)")
    else:
        print("WARNING: stats.json missing — count cross-check skipped")
        ok = False

    # G-DATA gate re-check on TRAIN (hard, pre-registered)
    from collections import defaultdict as _dd
    grp = _dd(list)
    with open(OUT / "train.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["axis"] == "arousal":
                grp[r["contrast_group"].rsplit(":", 1)[0]].append(r)
    seps = []
    for gid, grows in grp.items():
        hi = [r["intensity"] for r in grows if r["contrast_group"].endswith(":hi")]
        lo = [r["intensity"] for r in grows if r["contrast_group"].endswith(":lo")]
        seps.append(sum(hi) / len(hi) - sum(lo) / len(lo))
    mean_sep = sum(seps) / len(seps) if seps else 0.0
    print(f"G-DATA re-check (train arousal): n_groups={len(seps)} "
          f"mean surviving separation={mean_sep:.4f} (gate >= {GATE_MIN_SEP})")
    if len(seps) == 0 or mean_sep < GATE_MIN_SEP:
        ok = False
        print("   G-DATA re-check FAIL")

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
