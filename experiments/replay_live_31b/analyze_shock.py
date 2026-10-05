"""Shock-variant analysis: WHEN does the steering act? Constant vs prefill-only vs
last-position vs decay (temp 0.95, 4 samples/cell, base condition C42 = B2 + valence s=42.4).

Run: .venv/bin/python -m experiments.replay_live_31b.analyze_shock
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

from experiments.replay_live_31b.analyze import _agg, load_lexicon, metrics

ROOT = Path(__file__).resolve().parent.parent.parent
RES = ROOT / "results" / "replay-live-31b-2026-10-03"


def main() -> None:
    smp = json.loads((RES / "raw" / "generations-shock.json").read_text())
    lex = load_lexicon()
    by_cond: dict = {}
    for r in smp["results"]:
        by_cond.setdefault(r["condition"], []).append(
            {**{k: r[k] for k in ("call_id", "sample", "seed")}, **metrics(r["text"], lex),
             "text": r["text"]})
    agg = {c: _agg(rows) for c, rows in by_cond.items()}

    def cmean(c, sid, key):
        vs = [g[key] for g in by_cond[c] if g["call_id"] == sid and g[key] is not None]
        return sum(vs) / len(vs) if vs else None

    calls = sorted({r["call_id"] for r in smp["results"]})
    out = {"temp": smp["temp"], "n_samples": smp["n_samples"], "decay": smp["decay"], "agg": agg,
           "deltas": {}}
    for c in by_cond:
        if c == "B2":
            continue
        for key, tag in (("nrc_valence", "valence"), ("nrc_arousal", "arousal")):
            d = [cmean(c, s, key) - cmean("B2", s, key) for s in calls]
            d = [x for x in d if x is not None]
            out["deltas"].setdefault(c, {})[tag] = {
                "mean": round(sum(d) / len(d), 3) if d else None,
                "sem": round(st.stdev(d) / len(d) ** 0.5, 3) if len(d) > 1 else None,
                "per_call": [round(x, 3) for x in d],
            }
    (RES / "shock.json").write_text(json.dumps(out, indent=1) + "\n")

    print(f"shock variants — temp {smp['temp']}, {smp['n_samples']} samples/cell, "
          f"decay {smp['decay']}")
    print(f"{'cond':10s} {'valence':8s} {'arousal':8s} {'rep3':6s} {'sprite':7s} {'chars':6s} tags")
    for c in ("B2", "C42", "C42pre", "C42last", "C42decay"):
        g = agg[c]
        ars = [x["nrc_arousal"] for x in by_cond[c] if x["nrc_arousal"] is not None]
        aro = round(sum(ars) / len(ars), 3) if ars else "-"
        print(f"{c:10s} {g['mean_valence']!s:8s} {aro!s:8s} {g['mean_rep3']!s:6s} "
              f"{g['sprite_ok_rate']:.2f}    {g['mean_chars']!s:6s} {sorted(set(g['tags']))}")
    print()
    for c, d in out["deltas"].items():
        print(f"{c:10s} dVal {d['valence']['mean']:+.3f} +/- {d['valence']['sem']} "
              f"| dAro {d['arousal']['mean']:+.3f} +/- {d['arousal']['sem']}")


if __name__ == "__main__":
    main()
