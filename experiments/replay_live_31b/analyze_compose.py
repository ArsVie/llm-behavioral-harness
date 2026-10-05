"""Composition/interference analysis: do stacked directions (V+A+L) stay additive?

Per condition: NRC valence + arousal means; deltas vs B2; interference = combo delta minus the
corresponding single-direction delta, per axis. Cross-talk check: single-direction steering
against the OTHER axis.

Run: .venv/bin/python -m experiments.replay_live_31b.analyze_compose
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

from experiments.replay_live_31b.analyze import _agg, load_lexicon, metrics

ROOT = Path(__file__).resolve().parent.parent.parent
RES = ROOT / "results" / "replay-live-31b-2026-10-03"

CONDS = ["B2", "V42", "A42", "L42", "VA42", "VAL42"]


def main() -> None:
    smp = json.loads((RES / "raw" / "generations-compose.json").read_text())
    lex = load_lexicon()
    by_cond: dict = {}
    for r in smp["results"]:
        by_cond.setdefault(r["condition"], []).append(
            {**{k: r[k] for k in ("call_id", "sample", "seed")}, **metrics(r["text"], lex),
             "text": r["text"]})
    agg = {c: _agg(rows) for c, rows in by_cond.items()}
    calls = sorted({r["call_id"] for r in smp["results"]})

    def cmean(c, sid, key):
        vs = [g[key] for g in by_cond[c] if g["call_id"] == sid and g[key] is not None]
        return sum(vs) / len(vs) if vs else None

    def deltas(c, key):
        d = [cmean(c, s, key) - cmean("B2", s, key) for s in calls]
        d = [x for x in d if x is not None]
        return (round(sum(d) / len(d), 3), round(st.stdev(d) / len(d) ** 0.5, 3)
                if len(d) > 1 else None, [round(x, 3) for x in d])

    out = {"temp": smp["temp"], "n_samples": smp["n_samples"], "agg": {}, "deltas": {}}
    for c in CONDS:
        ars = [x["nrc_arousal"] for x in by_cond[c] if x["nrc_arousal"] is not None]
        out["agg"][c] = {**agg[c], "mean_arousal": round(sum(ars) / len(ars), 3) if ars else None}
        if c != "B2":
            out["deltas"][c] = {"valence": deltas(c, "nrc_valence"),
                                "arousal": deltas(c, "nrc_arousal")}
    # interference: combo minus single, per axis
    inter = {}
    for combo, singles in (("VA42", {"valence": "V42", "arousal": "A42"}),
                           ("VAL42", {"valence": "V42", "arousal": "A42"})):
        for axis, single in singles.items():
            key = "nrc_valence" if axis == "valence" else "nrc_arousal"
            cm, _, _ = deltas(combo, key)
            sm, _, _ = deltas(single, key)
            inter[f"{combo}_minus_{single}_{axis}"] = round(cm - sm, 3)
    out["interference"] = inter
    (RES / "compose.json").write_text(json.dumps(out, indent=1) + "\n")

    print(f"compose — temp {smp['temp']}, {smp['n_samples']} samples/cell")
    print(f"{'cond':8s} {'valence':8s} {'arousal':8s} {'rep3':6s} {'sprite':7s} {'chars':6s} hearts tags")
    for c in CONDS:
        g = out["agg"][c]
        print(f"{c:8s} {g['mean_valence']!s:8s} {g['mean_arousal']!s:8s} {g['mean_rep3']!s:6s} "
              f"{g['sprite_ok_rate']:.2f}    {g['mean_chars']!s:6s} {g['hearts']:5d} {sorted(set(g['tags']))}")
    print()
    for c in CONDS[1:]:
        dv, ev, _ = out["deltas"][c]["valence"]
        da, ea, _ = out["deltas"][c]["arousal"]
        print(f"{c:8s} dVal {dv:+.3f} +/- {ev} | dAro {da:+.3f} +/- {ea}")
    print()
    print("interference:", json.dumps(inter, indent=1))


if __name__ == "__main__":
    main()
