"""Ensambla results/iteration-3-report.md desde los datos computados.

Toma results/it3-report-data.json (cvs_report) + g6_report.json y
rellena las secciones numéricas del esqueleto del reporte. La sección 0
(headline / respuesta declarada al DoD §11) la escribe el orquestador al
revisar — este script solo mecaniza los números.

Uso: .venv/bin/python -m experiments.it3_assemble_report
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "results/it3-report-data.json"
REPORT = REPO / "results/iteration-3-report.md"


def git_ledger() -> str:
    out = subprocess.run(
        ["git", "log", "--oneline", "-14"],
        cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout.strip()
    return out or "(no git history)"


def fmt_pct(v) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.1f}%"


def assemble(data: dict, g6: dict | None) -> str:
    lines: list[str] = []
    a = lines.append
    a("# Iteration-3 — perceptual validity: confirmatory report")
    a("")
    a("Assembled: (timestamp) — numbers from experiments/cvs_report.py "
      "and the G6 driver; interpretation by orchestrator review.")
    a("")
    a("## 0. Headline")
    a("")
    a("(stated answer to DoD §11 — filled by orchestrator at review)")
    a("")
    a("## 1. Gate ledger (it3)")
    a("")
    a("| Gate | What | Status | Evidence |")
    a("|---|---|---|---|")
    a("| G1 | seam audit + generation integrity | PASS | it3-b1 merges |")
    a("| G2 | preflight gate, real claims, horizon split | PASS | 941/941 (main-g2close3.log) |")
    a(f"| G3 | real-model smoke | PASS | {DATA.parent}/it3-g3-smoke-night/ |")
    a(f"| G4 | manifest freeze (B10) | DONE | results/it3-g4-manifest-*.json |")
    a(f"| G5 | confirmatory matrix | {'PASS' if data.get('n_cells', 0) > 0 and data.get('blank_invariant_ok') else 'REVIEW'} | results/it3-g5-matrix/ |")
    a(f"| G6 | judge protocol v2 | {'PASS' if g6 and g6.get('ok') else 'REVIEW'} | results/it3-g6-judge/ |")
    a("")
    a("## 2. G2 close (941/941) — horizon split")
    a("")
    a("Gate vs hypothesis threshold split; min_days per claim; "
      "below-horizon claims NOT EVALUABLE. Reconciliation: "
      "results/it3-g2-horizon-split-reconciliation-2026-08-10.md "
      "(14.4%-vs-15% = two-leg measurement; fired-schedule leg 29.17% "
      "carries the gate; count leg 14.4% at margin).")
    a("")
    a("## 3. G3 real-model smoke")
    a("")
    a("- Cell: results/it3-g3-smoke-night/ (seed 5001, 7 days, real client)")
    a("- Provider episode 2026-08-10 03:23+ (deepseek-v4-flash 100% empty): "
      "4 failed attempts; hardened client (7 attempts, 2.0s base) + "
      "watchdog fallback; amendment: "
      "results/it3-manifest-amendment-2026-08-10-model-fallback.md")
    a("")
    a("## 4. G4 manifest (B10)")
    a("")
    a("results/it3-g4-manifest-*.json — conditions, seeds, thresholds, "
      "judge config, EVENT_CHAINS, reconciliation + SNS-at-margin decision.")
    a("")
    a("## 5. G5 confirmatory matrix")
    a("")
    a(f"- {data.get('n_cells', 0)} cells ({', '.join(data.get('conditions', []))} "
      f"x 5 seeds x 30 days), real client, checkpoints on, perturbation per cell.")
    a(f"- Blank invariant (<1% per cell): "
      f"{'PASS' if data.get('blank_invariant_ok') else 'FAIL'}")
    for key in sorted(data.get("per_cell", {})):
        cell = data["per_cell"][key]
        b = cell["blank"]
        c = cell["conversations"]
        a(f"  - {key}: messages={b['n_messages']} blank_rate={b['blank_rate']:.4f} "
          f"convs={c['n_conversations']} multi_turn={c['n_multi_turn']} "
          f"mean_turns={c['mean_turns']:.1f}")
    a("")
    a("## 6. G6 judge protocol v2")
    a("")
    if g6:
        a(f"- Families: {', '.join(g6.get('families', []))}; passes: {g6.get('passes')}")
        a(f"- Attention probes: {g6.get('attention_probes_resolved')}/"
          f"{g6.get('attention_probes_total')} resolved "
          f"({'PASS' if g6.get('ok') else 'FAIL'})")
        if g6.get("family_errors"):
            a(f"- Family errors: {g6['family_errors']}")
        for fam, fd in (g6.get("per_family") or {}).items():
            a(f"- {fam}: outcomes={fd.get('n_outcomes')} "
              f"dims={sorted((fd.get('dims') or {}).keys())}")
    else:
        a("(g6 report missing — judge run pending)")
    a("")
    a("## 7. DoD §11 recomputed from artifacts")
    a("")
    a("1. Blank turns < 1% (hard invariant): see §5 per-cell rates.")
    a("2. Ablations ablate pre-generation: G2 gate 941/941 + matrix "
      "per-cell claim evaluations (timing channel below).")
    t = data.get("timing_channel") or {}
    if t.get("evaluable"):
        a("3. closing_tendency mechanically observable: mean turns per "
          "conversation (§5); multi-turn share recorded per cell.")
        a("4. Latent state reaches the timing channel "
          "(STRUCTURED_NO_STATE vs FULL):")
        for o in t.get("outcomes", []):
            a(f"   - seed {o['seed']}: count_div={fmt_pct(o.get('count_div'))} "
              f"gap_div={fmt_pct(o.get('gap_div'))} "
              f"claim={'PASS' if o.get('claim_passed') else 'not-met'}")
    else:
        a(f"3-4. timing claim: not evaluable ({t.get('reason', 'no data')})")
    a("5. Memory lanes with absolute CompleteChain: EVENT_CHAINS per "
      "manifest; per-lane resolution from matrix artifacts.")
    a("6. Judge resolves a deliberately degraded transcript under both "
      "families: §6 attention probes.")
    a("7. Stated answer: §0.")
    a("")
    a("## 8. Limitations")
    a("")
    a("- Provider episode 2026-08-10 (deepseek-v4-flash 100% empty for "
      "hours; gpt-5.6-luna fallback used if the amendment fired).")
    a("- Judge family-1 (opencode-flash) depends on the degraded route; "
      "family errors listed in §6 if any.")
    a("")
    a("## 9. Artifacts")
    a("")
    a("- results/it3-g4-manifest-*.json")
    a("- results/it3-g2-horizon-split-reconciliation-2026-08-10.md")
    a("- results/it3-manifest-amendment-2026-08-10-model-fallback.md (if used)")
    a("- results/it3-g5-matrix/ (cells + transcripts/)")
    a("- results/it3-g6-judge/ (pairs, outcomes, g6_report.json)")
    a("- results/it3-report-data.json")
    a("")
    a("## 10. Commit ledger (recent)")
    a("")
    a("```")
    a(git_ledger())
    a("```")
    a("")
    return "\n".join(lines)


def main() -> int:
    if not DATA.exists():
        print(f"missing {DATA} — run cvs_report first")
        return 1
    data = json.loads(DATA.read_text(encoding="utf-8"))
    g6 = data.get("judge_g6")
    REPORT.write_text(assemble(data, g6), encoding="utf-8")
    print(f"assembled: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
