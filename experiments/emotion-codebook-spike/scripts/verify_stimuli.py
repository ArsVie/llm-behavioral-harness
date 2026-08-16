"""P1 — independent verification of data/stimuli/{train,heldout}.jsonl.

Re-reads the emitted files from disk (no build-state reuse): checks schema on
every row, id uniqueness, train/heldout item disjointness, field formats and
ranges, intensity == axis coordinate, per-group hi/lo balance, bin coverage,
and line counts vs stats.json. Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
OUT = SPIKE_ROOT / "data" / "stimuli"

AXES = {"valence", "arousal"}
SOURCES = {"nrc-vad", "warriner", "emobank", "goemotions"}
KEYS = {"id", "axis", "intensity", "text", "v", "a", "d", "source", "contrast_group"}
GROUP_RE = re.compile(r"^(valence|arousal):(train|heldout):g\d{4}:(hi|lo)$")


def check(split: str) -> tuple[list[dict], list[str], dict]:
    path = OUT / f"{split}.jsonl"
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
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
        if r["source"] not in SOURCES:
            errs.append(f"row {i}: bad source {r['source']!r}")
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

    # per-group balance + hi>=lo
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["contrast_group"].rsplit(":", 1)[0]].append(r)
    low_sep_groups = 0
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
        if m_hi - m_lo < 0.05:
            low_sep_groups += 1

    # per (axis) bin coverage / min / max
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
            "low_sep_groups": low_sep_groups,
        }
    return rows, errs, cov


def main() -> None:
    ok = True
    all_items: dict[str, str] = {}          # item_id -> split (disjointness check)
    counts = {}
    for split in ["train", "heldout"]:
        res = check(split)
        rows = res[0]
        errs = res[1]
        cov = res[2]
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
        print(f"   groups with hi-lo sep < 0.05: {cov['valence']['low_sep_groups']} "
              f"(valence) / {cov['arousal']['low_sep_groups']} (arousal)")
        if errs:
            ok = False
            print(f"   FAILURES ({len(errs)}):")
            for e in errs[:40]:
                print("   -", e)

    # line counts vs stats.json
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
    else:
        print("WARNING: stats.json missing — count cross-check skipped")

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
