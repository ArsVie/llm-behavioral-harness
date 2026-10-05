"""Metrics + tables for the live-call replay (pre-reg: results/replay-live-31b-2026-10-03/README.md).

Covers the main 31B run (A/B/B2/C/D at disp), the mult-dose extension (C2/D2 at s=73.2),
the cross-model extension (e2b/e4b/12b at A/B2/C), and the dose sweep (C at 21.2..56.5).

Run: .venv/bin/python -m experiments.replay_live_31b.analyze
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RES = ROOT / "results" / "replay-live-31b-2026-10-03"
LEX = Path("/home/vruizes/.hermes/projects/emotion-codebook/emotion-codebook-spike/"
           "datasets/nrc-vad/NRC-VAD-Lexicon/NRC-VAD-Lexicon.txt")

SPRITES = {"smug", "neutral", "smile", "laugh", "sad", "crying", "angry", "pout",
           "worried", "surprised", "embarrassed", "flustered"}
NAMES_RE = re.compile(r"\b(dummy|silly|stupid|perv)\b", re.I)
TAG_RE = re.compile(r"\[([a-zA-Z]+)\]")
WORD_RE = re.compile(r"[a-z][a-z']+")
LEAK_RE = re.compile(r"state update|bearing|agenda|availability|readily|opening up|grounded", re.I)
KAOMOJI_CHARS = set("∀ω◕◔◠◡╥﹏╯￣∇°▽≧≦・｡･✧ᴗᵕᴖᵔ๑٩۶")


def load_lexicon() -> dict[str, tuple[float, float]]:
    lex = {}
    for line in LEX.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                lex[parts[0].lower()] = (float(parts[1]), float(parts[2]))
            except ValueError:
                pass
    return lex


def rep3(words: list[str]) -> float:
    """Fraction of repeated word-3-grams (0 = all unique; ~1 = degenerate loops)."""
    grams = [tuple(words[i:i + 3]) for i in range(max(0, len(words) - 2))]
    return round(1.0 - len(set(grams)) / len(grams), 3) if grams else 0.0


def metrics(text: str, lex: dict) -> dict:
    words = WORD_RE.findall(text.lower())
    vs = [lex[w][0] for w in words if w in lex]
    ars = [lex[w][1] for w in words if w in lex]
    tags = TAG_RE.findall(text)
    first_tag = tags[0].lower() if tags else None
    return {
        "chars": len(text),
        "words": len(words),
        "first_tag": first_tag,
        "sprite_ok": first_tag in SPRITES if first_tag else False,
        "n_tags": len(tags),
        "hearts": text.count("♡"),
        "name_calls": len(NAMES_RE.findall(text)),
        "kaomoji": any(c in KAOMOJI_CHARS for c in text),
        "emoji": any(ord(c) >= 0x1F000 for c in text),
        "leaks": len(LEAK_RE.findall(text)),
        "nrc_valence": round(sum(vs) / len(vs), 3) if vs else None,
        "nrc_arousal": round(sum(ars) / len(ars), 3) if ars else None,
        "nrc_matched": len(vs),
        "rep3": rep3(words),
    }


def _agg(rows: list[dict]) -> dict:
    vals = [g["nrc_valence"] for g in rows if g["nrc_valence"] is not None]
    return {
        "n": len(rows),
        "sprite_ok_rate": round(sum(g["sprite_ok"] for g in rows) / len(rows), 2) if rows else None,
        "mean_valence": round(sum(vals) / len(vals), 3) if vals else None,
        "mean_chars": round(sum(g["chars"] for g in rows) / len(rows)) if rows else None,
        "mean_rep3": round(sum(g["rep3"] for g in rows) / len(rows), 3) if rows else None,
        "hearts": sum(g["hearts"] for g in rows),
        "name_calls": sum(g["name_calls"] for g in rows),
        "emoji_total": sum(g["emoji"] for g in rows),
        "leak_total": sum(g["leaks"] for g in rows),
        "tags": [g["first_tag"] for g in rows],
    }


def main() -> None:
    lex = load_lexicon()
    raw = RES / "raw"
    scen = {s["call_id"]: s for s in json.loads((raw / "scenarios.json").read_text())}
    gens = json.loads((raw / "generations.json").read_text())
    mult = json.loads((raw / "generations-mult.json").read_text()) if (raw / "generations-mult.json").exists() else None
    xm = json.loads((raw / "generations-xmodels.json").read_text()) if (raw / "generations-xmodels.json").exists() else None
    dose = json.loads((raw / "generations-dose.json").read_text()) if (raw / "generations-dose.json").exists() else None
    smp_files = sorted(raw.glob("generations-samples*.json"))

    out = {"per_generation": [], "aggregates": {}, "deltas": {}, "recorded": {}}
    for r in gens["results"]:
        out["per_generation"].append({**{k: r[k] for k in ("call_id", "condition", "steer", "n_in", "seconds")},
                                      **metrics(r["text"], lex), "text": r["text"]})
    if mult:
        out["mult_scale_s"] = mult["scale_s"]
        for r in mult["results"]:
            out["per_generation"].append({**{k: r[k] for k in ("call_id", "steer", "n_in", "seconds")},
                                          "condition": r["condition"] + "2",
                                          **metrics(r["text"], lex), "text": r["text"]})

    conds = ["A", "B", "B2", "C", "D"] + (["C2", "D2"] if mult else [])
    for c in conds:
        out["aggregates"][c] = _agg([g for g in out["per_generation"] if g["condition"] == c])
    a = out["aggregates"]

    def d(x, y):  # noqa: E741
        vx, vy = a[x]["mean_valence"], a[y]["mean_valence"]
        return round(vx - vy, 3) if vx is not None and vy is not None else None

    out["deltas"] = {"A_minus_B2_card_effect": d("A", "B2"), "C_minus_B2_steering_effect": d("C", "B2"),
                     "D_minus_A_steering_on_card": d("D", "A"), "B_minus_B2_latest_only": d("B", "B2")}
    if mult:
        out["deltas"].update({"C2_minus_B2_steering_mult": d("C2", "B2"),
                              "D2_minus_A_steering_mult_on_card": d("D2", "A")})
    for sid, s in scen.items():
        out["recorded"][str(sid)] = metrics(s["recorded_response"], lex)

    if xm:
        out["xmodels"] = {}
        for r in xm["results"]:
            out["xmodels"].setdefault(r["model"], {}).setdefault(r["condition"], []).append(
                {**{k: r[k] for k in ("call_id", "steer", "n_in", "seconds")}, **metrics(r["text"], lex), "text": r["text"]})
        out["xmodels_agg"] = {m: {c: _agg(rows) for c, rows in cds.items()} for m, cds in out["xmodels"].items()}
    if dose:
        out["dose"] = {}
        for r in dose["results"]:
            out["dose"].setdefault(r["scale_s"], []).append(
                {**{k: r[k] for k in ("call_id", "seconds")}, **metrics(r["text"], lex), "text": r["text"]})
        out["dose_agg"] = {s_: _agg(rows) for s_, rows in out["dose"].items()}
    if smp_files:
        import statistics as st
        out["samples"] = {}
        for fp in smp_files:
            smp = json.loads(fp.read_text())
            tkey = f"{smp['temperature']:g}"
            by_cond: dict = {}
            for r in smp["results"]:
                by_cond.setdefault(r["condition"], []).append(
                    {**{k: r[k] for k in ("call_id", "sample", "seed", "seconds")}, **metrics(r["text"], lex), "text": r["text"]})
            # paired per-call analysis: mean per call, then deltas across calls
            calls = sorted({r["call_id"] for r in smp["results"]})
            per_call = {c: {sid: [g["nrc_valence"] for g in rows if g["call_id"] == sid] for sid in calls}
                        for c, rows in by_cond.items()}
            def cmean(c, sid):
                vs = [v for v in per_call[c][sid] if v is not None]
                return sum(vs) / len(vs) if vs else None
            d_card = [cmean("A", s) - cmean("B2", s) for s in calls]
            d_dose = [cmean("C42", s) - cmean("B2", s) for s in calls]
            d_card = [x for x in d_card if x is not None]
            d_dose = [x for x in d_dose if x is not None]
            out["samples"][tkey] = {
                "temperature": smp["temperature"], "n_samples": smp["n_samples"],
                "master_seed": smp["master_seed"], "file": fp.name,
                "by_cond": by_cond, "agg": {c: _agg(rows) for c, rows in by_cond.items()},
                "deltas": {
                    "card_A_minus_B2_mean": round(sum(d_card) / len(d_card), 3),
                    "card_sem": round(st.stdev(d_card) / len(d_card) ** 0.5, 3) if len(d_card) > 1 else None,
                    "dose_C42_minus_B2_mean": round(sum(d_dose) / len(d_dose), 3),
                    "dose_sem": round(st.stdev(d_dose) / len(d_dose) ** 0.5, 3) if len(d_dose) > 1 else None,
                    "per_call_card": [round(x, 3) for x in d_card],
                    "per_call_dose": [round(x, 3) for x in d_dose],
                },
            }

    (RES / "metrics.json").write_text(json.dumps(out, indent=1) + "\n")

    print(f"{'cond':5s} {'sprite_ok':9s} {'valence':8s} {'rep3':6s} {'chars':6s} {'hearts':6s} {'names':5s} {'emoji':5s} {'leaks':5s} tags")
    for c in conds:
        g = a[c]
        print(f"{c:5s} {g['sprite_ok_rate']:9.2f} {str(g['mean_valence']):8s} {g['mean_rep3']:6.3f} {g['mean_chars']:6d} "
              f"{g['hearts']:6d} {g['name_calls']:5d} {g['emoji_total']:5d} {g['leak_total']:5d} {g['tags']}")
    print("deltas:", json.dumps(out["deltas"]))
    print("recorded deepseek valence:", [out["recorded"][str(s)]["nrc_valence"] for s in scen])
    if xm:
        print("\n== xmodels ==")
        for m, cds in out["xmodels_agg"].items():
            for c, g in cds.items():
                print(f"{m:12s} {c:4s} sprite={g['sprite_ok_rate']:.2f} val={g['mean_valence']} rep3={g['mean_rep3']} chars={g['mean_chars']} tags={g['tags']}")
    if dose:
        print("\n== dose sweep ==")
        for s_, g in out["dose_agg"].items():
            print(f"s={s_:6.1f} val={g['mean_valence']} rep3={g['mean_rep3']} chars={g['mean_chars']} sprite={g['sprite_ok_rate']:.2f}")
    for tkey, s_ in (out.get("samples") or {}).items():
        print("\n== samples temp %s (n=%d/cell) ==" % (tkey, s_["n_samples"]))
        for c, g in s_["agg"].items():
            print(f"{c:4s} val={g['mean_valence']} rep3={g['mean_rep3']} chars={g['mean_chars']} sprite={g['sprite_ok_rate']:.2f}")
        print("paired deltas:", json.dumps(s_["deltas"]))

    lines = ["---",
             "type: reference",
             "title: Replay side-by-side (generated)",
             "description: \"Generated side-by-side of every replay generation vs the recorded replies, per call and condition.\"",
             "tags: [llm-behavioral-harness, replay, generated]",
             "timestamp: 2026-10-03",
             "---",
             "",
             "# Replay side-by-side (generations vs recorded deepseek reply)", ""]
    for sid, s in scen.items():
        lines.append(f"## Call {sid} (day {s['day']}, t={s['t_h']:.2f}) — recorded: `{s['meta']['last_user'][:90]}`")
        lines.append("")
        rec = out["recorded"][str(sid)]
        lines.append(f"- **recorded deepseek** (val={rec['nrc_valence']}, tag={rec['first_tag']}, "
                     f"chars={rec['chars']}): {s['recorded_response'][:400]}")
        for g in out["per_generation"]:
            if g["call_id"] == sid:
                lines.append(f"- **{g['condition']}** (val={g['nrc_valence']}, rep3={g['rep3']}, tag={g['first_tag']}, "
                             f"chars={g['chars']}): {g['text'][:400]}")
        lines.append("")
    if xm:
        lines.append("# Cross-model extension (A / B2 / C at each model's own disp scale)")
        lines.append("")
        for m, cds in out["xmodels"].items():
            for c in ("A", "B2", "C"):
                for g in cds.get(c, []):
                    lines.append(f"- **{m} {c}** (call {g['call_id']}, val={g['nrc_valence']}, rep3={g['rep3']}, "
                                 f"tag={g['first_tag']}): {g['text'][:300]}")
            lines.append("")
    if dose:
        lines.append("# Dose sweep (condition C; s=14.12 and s=73.2 endpoints from the main runs)")
        lines.append("")
        for s_, rows in out["dose"].items():
            for g in rows:
                lines.append(f"- **s={s_} call {g['call_id']}** (val={g['nrc_valence']}, rep3={g['rep3']}): {g['text'][:300]}")
            lines.append("")
    for tkey, s_ in (out.get("samples") or {}).items():
        lines.append(f"# Samples (temp {tkey}, {s_['n_samples']} per cell, seeds fixed)")
        lines.append("")
        for c, rows in s_["by_cond"].items():
            for g in rows:
                lines.append(f"- **{c} call {g['call_id']} s{g['sample']}** (seed={g['seed']}, val={g['nrc_valence']}): {g['text'][:300]}")
            lines.append("")
    (RES / "table.md").write_text("\n".join(lines) + "\n")
    print("\nwrote", RES / "metrics.json", "and", RES / "table.md")


if __name__ == "__main__":
    main()
