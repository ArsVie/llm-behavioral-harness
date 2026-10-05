"""Steering-battery analysis: ±dominance / ±intensity / ±arousal vs B2.

Readouts: NRC valence/arousal/dominance of replies, assertiveness vs hedging markers,
sprite/rep3/hearts integrity. Deltas are per-call paired against B2 (5 calls).

Run: .venv/bin/python -m experiments.replay_live_31b.analyze_steer
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

from experiments.replay_live_31b.analyze import LEX, metrics, rep3

ROOT = Path(__file__).resolve().parent.parent.parent
RES = ROOT / "results" / "replay-live-31b-2026-10-03"
CONDS = ["B2", "Dp42", "Dm42", "Ip42", "Im42", "Ap42", "Am42"]

ASSERT = ["i want", "i will", "you will", "you should", "you need to", "do it", "right now",
          "listen to me", "i said", "don't you dare", "you're mine", "come here", "stop it",
          "tell me", "give me", "i decide", "as i said", "enough"]
HEDGE = ["maybe", "perhaps", "i guess", "i think", "kind of", "sort of", "i hope",
         "if you want", "if you'd", "i'm sorry", "sorry", "i don't know", "might",
         "could be", "not sure", "i suppose", "just wondering", "no pressure",
         "if that's okay", "if that is okay"]


def load_lex3() -> dict:
    lex = {}
    for line in LEX.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            try:
                lex[parts[0].lower()] = (float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                pass
    return lex


def main() -> None:
    smp = json.loads((RES / "raw" / "generations-steer.json").read_text())
    lex = load_lex3()
    by_cond: dict = {}
    for r in smp["results"]:
        t = r["text"]
        m = metrics(t, lex)
        words = t.lower().split()
        m["nrc_dominance"] = None
        ds = [lex[w][2] for w in [w.strip(".,!?;:\"'()[]") for w in words] if w in lex]
        if ds:
            m["nrc_dominance"] = round(sum(ds) / len(ds), 3)
        low = t.lower()
        m["assert_n"] = sum(low.count(p) for p in ASSERT)
        m["hedge_n"] = sum(low.count(p) for p in HEDGE)
        m.update({k: r[k] for k in ("call_id", "sample", "seed")})
        by_cond.setdefault(r["condition"], []).append(m)
    calls = sorted({r["call_id"] for r in smp["results"]})

    def cmean(c, sid, key):
        vs = [g[key] for g in by_cond[c] if g["call_id"] == sid and g[key] is not None]
        return sum(vs) / len(vs) if vs else None

    def deltas(c, key):
        d = [cmean(c, s, key) - cmean("B2", s, key) for s in calls]
        d = [x for x in d if x is not None]
        return (round(st.mean(d), 3), round(st.pstdev(d) / max(1, len(d)) ** 0.5, 3)) if d else (None, None)

    print(f"{'cond':6s} {'n':>3s} {'val':>6s} {'aro':>6s} {'dom':>6s} {'assert':>6s} {'hedge':>6s} "
          f"{'sprite':>6s} {'rep3':>5s} {'chars':>5s} | paired deltas (val, aro, dom)")
    summary = {}
    for c in CONDS:
        rows = by_cond.get(c, [])
        if not rows:
            continue
        mv = st.mean([g["nrc_valence"] for g in rows if g["nrc_valence"] is not None])
        ma = st.mean([g["nrc_arousal"] for g in rows if g["nrc_arousal"] is not None])
        md = st.mean([g["nrc_dominance"] for g in rows if g["nrc_dominance"] is not None])
        ass = sum(g["assert_n"] for g in rows)
        hed = sum(g["hedge_n"] for g in rows)
        spr = sum(g["sprite_ok"] for g in rows) / len(rows)
        rep = st.mean([g["rep3"] for g in rows])
        chs = st.mean([g["chars"] for g in rows])
        dv, sv = deltas(c, "nrc_valence")
        da, sa = deltas(c, "nrc_arousal")
        dd, sd = deltas(c, "nrc_dominance")
        print(f"{c:6s} {len(rows):3d} {mv:6.3f} {ma:6.3f} {md:6.3f} {ass:6d} {hed:6d} "
              f"{spr:6.2f} {rep:5.3f} {chs:5.0f} | dv {dv:+.3f}±{sv:.3f}  da {da:+.3f}±{sa:.3f}  "
              f"dd {dd:+.3f}±{sd:.3f}")
        summary[c] = {"n": len(rows), "val": round(mv, 3), "aro": round(ma, 3), "dom": round(md, 3),
                      "assert": ass, "hedge": hed, "sprite": round(spr, 2),
                      "rep3": round(rep, 3), "chars": round(chs),
                      "d_val": [dv, sv], "d_aro": [da, sa], "d_dom": [dd, sd]}
    out = RES / "steer.json"
    out.write_text(json.dumps({"agg": summary, "complete": smp.get("complete")}, indent=1) + "\n")
    print("\nwrote", out)
    # assert/hedge ratio per direction sign
    print("\nmarker summary (sums over 15 gens/cond):")
    for c in CONDS:
        if c in summary:
            s = summary[c]
            print(f"  {c:6s} assert {s['assert']:3d} hedge {s['hedge']:3d} ratio "
                  f"{s['assert'] / max(1, s['hedge']):.2f}")


if __name__ == "__main__":
    main()
