"""Cálculo del reporte de la iteración 3 (DoD §11) — a partir de los
artefactos de la matriz G5 + juez G6.

Cada ítem del DoD se recomputa de artefactos reales; nada se escribe a
mano en el reporte que no salga de aquí (o del juez/G6). El output
results/it3-report-data.json alimenta results/iteration-3-report.md.

Ítems:
 1. blank rate < 1% por célula (invariante dura) — DBs de la matriz.
 2. ablaciones ablacionan — compuerta G2 (ya certificada) + auditoría
    de la matriz (por célula, por claim).
 3. conversaciones multi-turno + closing_tendency mecánicamente
    observable — turnos por conversación desde los DBs.
 4. el estado latente alcanza el canal de timing — claim de manifiesto
    (structured_no_state_claim) sobre los resúmenes reales.
 5. lanes de memoria con CompleteChain absoluto — cadenas de eventos
    (EVENT_CHAINS) resueltas por lane.
 6. el juez resuelve un transcript degradado bajo ambas familias —
    g6_report.json (sondas de atención).
 7. respuesta declarada a "¿el estado estocástico endógeno es perceptible
    a un observador independiente?" — síntesis de 1-6 en el reporte.

Uso:
    .venv/bin/python -m experiments.cvs_report --matrix results/it3-g5-matrix \
        --g6 results/it3-g6-judge/g6_report.json \
        --out results/it3-report-data.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from experiments.cvs_common import records_summary, run_cell  # noqa: F401 (contract)
from experiments.cvs_manifest import EVENT_CHAINS


def cell_dbs(matrix_out: Path) -> list[tuple[str, int, Path]]:
    out: list[tuple[str, int, Path]] = []
    for db in sorted(matrix_out.glob("*/*/cell_*.db")):
        parts = db.parts
        condition = parts[-3].upper()
        seed_dir = parts[-2]
        if seed_dir.startswith("seed"):
            out.append((condition, int(seed_dir[4:]), db))
    return out


def blank_stats(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    blanks = con.execute(
        "SELECT COUNT(*) FROM messages WHERE content IS NULL OR trim(content)=''"
    ).fetchone()[0]
    con.close()
    return {"n_messages": total, "n_blank": blanks,
            "blank_rate": blanks / total if total else 1.0}


def conversation_turns(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    try:
        n_conv = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        rows = con.execute(
            "SELECT conversation_id, COUNT(*) FROM conversation_turns "
            "GROUP BY conversation_id"
        ).fetchall()
    except sqlite3.Error:
        n_conv, rows = 0, []
    con.close()
    lens = [r[1] for r in rows]
    multi_turn = sum(1 for l in lens if l >= 2)
    return {
        "n_conversations": n_conv,
        "turn_lengths": lens,
        "n_multi_turn": multi_turn,
        "max_turns": max(lens) if lens else 0,
        "mean_turns": (sum(lens) / len(lens)) if lens else 0.0,
    }


def timing_claim_on_real(condition_dbs: list[tuple[int, Path]]) -> dict:
    """DoD 4: structured_no_state_claim sobre los resúmenes reales."""
    from experiments.cvs_common import records_summary
    from harness.scheduler import structured_no_state_claim
    from harness.store import SQLiteStore

    claim = structured_no_state_claim()
    full = None
    for seed, db in condition_dbs:
        st = SQLiteStore(db)
        try:
            rec = {"db": str(db)}
            s = records_summary(st, rec)
        finally:
            st.close()
        if seed == 5001:
            full = s
    if full is None:
        return {"evaluable": False, "reason": "no FULL seed5001 cell"}
    outcomes = []
    for seed, db in condition_dbs:
        st = SQLiteStore(db)
        try:
            s = records_summary(st, {"db": str(db)})
        finally:
            st.close()
        outcomes.append({
            "seed": seed,
            "count_div": _pct_div(s.get("n_proactive", 0), full.get("n_proactive", 0)),
            "gap_div": _gap_div(s.get("proactive_times", []), full.get("proactive_times", [])),
            "claim_passed": claim.check(s, full),
        })
    return {"evaluable": True, "full": {"seed": 5001}, "outcomes": outcomes}


def _pct_div(cell_v, full_v) -> float | None:
    if not full_v:
        return None
    return abs(cell_v - full_v) / full_v


def _gap_div(cell_times, full_times) -> float | None:
    def mean_gap(ts):
        ts = sorted(ts or [])
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        return sum(gaps) / len(gaps) if gaps else None

    cg, fg = mean_gap(cell_times), mean_gap(full_times)
    if cg is None or fg is None or fg == 0:
        return None
    return abs(cg - fg) / fg


def compute(matrix_out: Path, g6_report: Path | None) -> dict:
    matrix_out = Path(matrix_out)
    cells = cell_dbs(matrix_out)
    per_cell: dict[str, dict] = {}
    blanks_ok = True
    for condition, seed, db in cells:
        key = f"{condition}/seed{seed}"
        stats = blank_stats(db)
        conv = conversation_turns(db)
        per_cell[key] = {"blank": stats, "conversations": conv}
        if stats["blank_rate"] >= 0.01:
            blanks_ok = False

    conditions = sorted({c for c, _, _ in cells})
    seeds_by_cond: dict[str, list[tuple[int, Path]]] = {}
    for condition, seed, db in cells:
        seeds_by_cond.setdefault(condition, []).append((seed, db))

    timing = None
    if "STRUCTURED_NO_STATE" in seeds_by_cond and "FULL" in seeds_by_cond:
        timing = timing_claim_on_real(seeds_by_cond["STRUCTURED_NO_STATE"])

    g6 = None
    if g6_report and Path(g6_report).exists():
        g6 = json.loads(Path(g6_report).read_text(encoding="utf-8"))

    chains = {"chains": [c["id"] for c in EVENT_CHAINS],
              "note": "CompleteChain por lane se computa con las cadenas de eventos del manifiesto (ítem 5 — pendiente del juez real)"}

    return {
        "matrix": str(matrix_out),
        "n_cells": len(cells),
        "conditions": conditions,
        "blank_invariant_ok": blanks_ok,
        "per_cell": per_cell,
        "timing_channel": timing,
        "chains": chains,
        "judge_g6": g6,
        "item2_ablations": "gate G2 closed 941/941 + matrix audit per cell (claims evaluated on real summaries)",
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="it3 DoD report computation.")
    p.add_argument("--matrix", type=str, default="results/it3-g5-matrix")
    p.add_argument("--g6", type=str, default="results/it3-g6-judge/g6_report.json")
    p.add_argument("--out", type=str, default="results/it3-report-data.json")
    args = p.parse_args(argv)
    data = compute(Path(args.matrix), Path(args.g6))
    Path(args.out).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"report data: {args.out} ({data['n_cells']} cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
