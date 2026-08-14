"""B6 re-backfill: lane-routed + fair-probe memory metrics over the 35 archived
iteration-2 matrix DBs.

EXPLORATORY ONLY — the archived corpus is a BROKEN CORPUS (28% blank assistant
turns); this re-computation corrects the METRICS (F5), not the corpus. Results
are not confirmatory evidence.

Usage:
    python backfill_b6.py <repo-root> <out-dir>
"""
from __future__ import annotations

import glob
import json
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(sys.argv[1])
OUT = Path(sys.argv[2])

sys.path.insert(0, str(REPO))

from harness.store import SQLiteStore  # noqa: E402
from experiments import cvs_common  # noqa: E402
from experiments.cvs_manifest import git_info  # noqa: E402

CONDITIONS = [
    "FULL", "NO_ACTUATORS", "NO_LIFE", "NO_TIMING_FEEDBACK",
    "RAW_HISTORY", "SIMPLE_RAG", "STRUCTURED_NO_STATE",
]


def blank_rate(db_path: Path) -> dict:
    """Blank assistant-turn rate of one cell (evidence for the broken-corpus label)."""
    con = sqlite3.connect(str(db_path))
    try:
        n_assistant = con.execute(
            "SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0]
        n_blank = con.execute(
            "SELECT COUNT(*) FROM messages WHERE role='assistant' "
            "AND (content IS NULL OR trim(content) = '')").fetchone()[0]
    finally:
        con.close()
    return {"n_assistant": n_assistant, "n_blank": n_blank,
            "blank_rate": round(n_blank / n_assistant, 4) if n_assistant else 0.0}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db_glob = str(REPO / "results/it2-g6-matrix" / "*" / "seed*" / "cell_*.db")
    dbs = sorted(glob.glob(db_glob))
    assert len(dbs) == 35, f"expected 35 archived cell DBs, found {len(dbs)}"

    per_cell: dict = {}
    per_condition: dict = {}
    blanks: dict = {}

    for db in dbs:
        p = Path(db)
        cond = p.parent.parent.name
        seed = p.parent.name
        assert cond in CONDITIONS, cond
        # Copy the archived DB so the committed corpus stays pristine.
        local = OUT / "dbs" / cond / f"{seed}.db"
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, local)
        store = SQLiteStore(local)
        chains = cvs_common.event_chain_metrics(store, condition=cond)
        recall = cvs_common.recall_probe_metrics(store, condition=cond)
        agg = cvs_common.aggregate_chain_metrics(chains)
        store.close()
        per_cell[f"{cond}/{seed}"] = {
            "condition": cond, "seed": seed,
            "chains": chains, "aggregate": agg,
            "M3_recall": recall["M3_recall"],
            "M4_false_recall": recall["M4_false_recall"],
            "probe_detail": recall["detail"],
        }
        blanks[f"{cond}/{seed}"] = blank_rate(p)

    # Pooled rates per condition over chain-cells (3 chains x 5 seeds = 15).
    for cond in CONDITIONS:
        cells = [per_cell[k] for k in per_cell if k.startswith(cond + "/")]
        n = sum(c["aggregate"]["n_chains"] for c in cells)
        def pooled(key: str) -> float:
            return round(
                sum(1 for c in cells
                    for ch in c["chains"].values() if ch[key]) / n, 4) if n else 0.0
        per_condition[cond] = {
            "n_chain_cells": n,
            "AnyEvidence": pooled("AnyEvidence"),
            "LatestEvidence": pooled("LatestEvidence"),
            "CompleteChain": pooled("CompleteChain"),
            "M3_recall_mean": round(
                sum(c["M3_recall"] for c in cells) / len(cells), 4) if cells else 0.0,
            "probe_lanes": sorted({ch["probe_lane"]
                                   for c in cells for ch in c["chains"].values()}),
        }

    gap = round(per_condition["FULL"]["CompleteChain"]
                - per_condition["RAW_HISTORY"]["CompleteChain"], 4)
    n_blank_all = sum(b["n_blank"] for b in blanks.values())
    n_assistant_all = sum(b["n_assistant"] for b in blanks.values())
    payload = {
        "title": "B6 re-backfill — lane-routed memory metrics over the 35 archived "
                 "iteration-2 matrix DBs (EXPLORATORY, broken corpus)",
        "corpus_status": "BROKEN CORPUS — exploratory only, not confirmatory",
        "blank_assistant_turns": {
            "n_blank": n_blank_all, "n_assistant": n_assistant_all,
            "blank_rate": round(n_blank_all / n_assistant_all, 4),
        },
        "commit": git_info(REPO),
        "method": {
            "lane_routing": "event_chain_metrics/recall_probe_metrics route through "
                            "_memory_for(condition) — the lane each cell ran with "
                            "(FULL/NO_*/STRUCTURED_NO_STATE -> MemoryAgent structured; "
                            "SIMPLE_RAG -> SimpleRagMemory; RAW_HISTORY -> fair raw "
                            "context probe)",
            "raw_history_fair_probe": cvs_common.RAW_HISTORY_FAIR_PROBE,
            "absolute_reporting": "aggregate_chain_metrics reports absolute "
                                  "AnyEvidence/LatestEvidence/CompleteChain rates",
        },
        "per_condition": per_condition,
        "per_cell": per_cell,
        "complete_chain_gap_full_vs_raw_history": gap,
        "threshold_gap_0_2_met": gap >= 0.2,
        "blank_rate_by_cell": blanks,
    }
    (OUT / "backfill_metrics.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "cells": len(dbs),
        "per_condition": per_condition,
        "gap": gap, "threshold_met": gap >= 0.2,
        "blank_rate": payload["blank_assistant_turns"],
        "out": str(OUT),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
