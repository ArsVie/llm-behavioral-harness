"""Inspect P1 stimulus schema (read-only)."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "stimuli"

for f in ("train", "heldout"):
    rows = [json.loads(l) for l in (ROOT / f"{f}.jsonl").read_text().splitlines() if l.strip()]
    print(f, "n=", len(rows))
    keys = Counter(k for r in rows for k in r)
    print("  keys:", dict(keys))
    print("  axes:", Counter(str(r.get("axis")) for r in rows).most_common())
    print("  contrast_group:", Counter(str(r.get("contrast_group")) for r in rows).most_common())
    print("  sample:", json.dumps(rows[0])[:400])
    vs = [r["v"] for r in rows if r.get("v") is not None]
    as_ = [r["a"] for r in rows if r.get("a") is not None]
    print(f"  v range: {min(vs):.3f}..{max(vs):.3f}  a range: {min(as_):.3f}..{max(as_):.3f}")
    tl = sorted(len(r["text"]) for r in rows)
    print(f"  text len p50/p95/max: {tl[len(tl)//2]}/{tl[int(len(tl)*0.95)]}/{tl[-1]}")
    ids = [r.get("id") for r in rows]
    print("  unique ids:", len(set(ids)), " dup ids:", len(ids) - len(set(ids)))
