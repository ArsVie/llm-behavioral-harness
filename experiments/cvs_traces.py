"""Causal traces (plan §13, deliverable 10): machine-generated provenance
walk for spontaneous proactive messages.

For each proactive message the walk resolves:
  MESSAGE -> ProactiveIntent (reason) -> source (AgendaItem/LifeArc/
  IndependentInterest) -> parent chain (arc -> interest), plus
  TIMING (schedule row: planned vs fired, window), BEHAVIOR (controls:
  max_tokens / response_delay_s / closing_tendency, initiative), and
  MEMORY CONTEXT (the intent's persisted evidence string + any L4 episode
  rows referenced inside it). The persisted ``messages.intent_id`` is the
  root of every walk.

Usage:
  python -m experiments.cvs_traces --db <cell.db> --records <records.json> \\
      --n 5 --out <trace_report.md>
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

EP_ID_RE = re.compile(r"(ep-[\w-]+|\bepisode[_-][\w-]+)")


def _row(conn: sqlite3.Connection, table: str, col: str, val) -> dict | None:
    try:
        r = conn.execute(
            f"SELECT * FROM {table} WHERE {col}=?", (val,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if r is None:
        return None
    return dict(zip([d[1] for d in conn.execute(
        f"PRAGMA table_info({table})").fetchall()], r))


def _chain(conn: sqlite3.Connection, src_type: str, src_id: str) -> list[dict]:
    """AgendaItem -> (LifeArc -> IndependentInterest) / IndependentInterest."""
    steps: list[dict] = []
    item = _row(conn, "agenda_items", "id", src_id)
    if item:
        steps.append({"type": "AgendaItem", "id": item["id"],
                      "activity": item["activity"], "status": item["status"],
                      "window": f"{item['start_t_h']:.2f}..{item['end_t_h']:.2f}h",
                      "source_type": item["source_type"]})
        if item["source_type"] == "arc":
            arc = _row(conn, "life_arcs", "id", item["source_id"])
            if arc:
                steps.append({"type": "LifeArc", "id": arc["id"],
                              "name": arc["name"], "interest": arc["interest"],
                              "progress": arc["progress"], "status": arc["status"]})
                if arc["interest"]:
                    steps.append({"type": "IndependentInterest",
                                  "id": arc["interest"]})
        elif item["source_type"] == "interest":
            steps.append({"type": "IndependentInterest", "id": item["source_id"]})
        elif item["source_type"] == "routine":
            steps.append({"type": "Routine", "id": item["source_id"]})
    else:
        # life_event / check_in sources reference the agenda item id directly;
        # if not found, the intent's own source is the terminal step.
        steps.append({"type": src_type, "id": src_id})
    return steps


def _timing(conn: sqlite3.Connection, intent: dict, msg_t_h: float) -> dict:
    day = int(msg_t_h // 24.0)
    sched = conn.execute(
        "SELECT * FROM schedule_events "
        "WHERE day=? ORDER BY ABS(t_h - ?) LIMIT 1", (day, msg_t_h)
    ).fetchone()
    if sched is None:
        return {"planned_t_h": None, "fired_t_h": None, "delay_h": None,
                "window": f"{intent['created_t_h']:.2f}..{intent['valid_until_t_h']:.2f}h"}
    s = dict(zip(sched.keys(), sched))
    fired = s.get("fired_t_h")
    delay = round(fired - s["t_h"], 3) if fired is not None else None
    return {"planned_t_h": s["t_h"], "fired_t_h": fired, "delay_h": delay,
            "schedule_status": s["status"],
            "window": f"{intent['created_t_h']:.2f}..{intent['valid_until_t_h']:.2f}h"}


def _memory_context(conn: sqlite3.Connection, evidence: str) -> list[dict]:
    eps: list[dict] = []
    for m in EP_ID_RE.findall(evidence or ""):
        ep = _row(conn, "memory_episodes", "id", m)
        if ep:
            eps.append({"id": ep["id"], "kind": ep.get("kind") or ep.get("category"),
                        "day": ep.get("day"), "summary": (ep.get("summary") or "")[:120]})
    return eps


def _behavior(records: dict, msg_id: int) -> dict:
    c = records.get("controls_by_message", {}).get(str(msg_id), {})
    d = records.get("directives_by_message", {}).get(str(msg_id), {})
    return {
        "max_tokens": c.get("max_tokens"),
        "response_delay_s": c.get("response_delay_s"),
        "closing_tendency": c.get("closing_tendency"),
        "initiative": d.get("initiative"),
        "length_scale": d.get("response_length_scale"),
    }


def build_traces(db: str, records_path: str, n: int) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    records = json.loads(Path(records_path).read_text(encoding="utf-8"))
    msgs = [dict(r) for r in conn.execute(
        "SELECT id, role, content, t_h, day, intent_id FROM messages "
        "WHERE proactive=1 AND intent_id IS NOT NULL ORDER BY t_h")]
    traces: list[dict] = []
    for m in msgs[:n]:
        intent = _row(conn, "proactive_intents", "id", m["intent_id"]) or {}
        steps = _chain(conn, intent.get("source_type", ""), intent.get("source_id", ""))
        traces.append({
            "message_id": m["id"],
            "t_h": round(m["t_h"], 3),
            "day": m["day"],
            "message": (m["content"] or "")[:140],
            "walk": [
                {"type": "OutgoingMessage", "id": m["id"]},
                {"type": "ProactiveIntent", "id": m["intent_id"],
                 "reason": intent.get("reason"), "status": intent.get("status")},
                *steps,
            ],
            "timing": _timing(conn, intent, m["t_h"]),
            "behavior": _behavior(records, m["id"]),
            "memory_context": _memory_context(conn, intent.get("evidence", "")),
            "persisted_intent_id": m["intent_id"],
            "evidence": intent.get("evidence", ""),
        })
    conn.close()
    return traces


def render_md(traces: list[dict]) -> str:
    lines = [
        "---",
        "type: causal-traces",
        "title: \"Causal traces — spontaneous proactive messages (plan §13, deliverable 10)\"",
        "description: \"Machine-generated provenance walks: message -> intent -> source -> parent chain + timing + behavior + memory context\"",
        "tags: [traces, auditability]",
        "timestamp: 2026-08-09T00:00:00+00:00",
        "---",
        "",
        "# Causal traces",
        "",
    ]
    for i, t in enumerate(traces, 1):
        lines += [f"## Trace {i} — message #{t['message_id']} (day {t['day']}, t={t['t_h']}h)",
                  "", f"> {t['message']}", ""]
        for step in t["walk"]:
            parts = [step["type"], step["id"]]
            for k in ("reason", "activity", "name", "interest", "status"):
                if step.get(k) is not None:
                    parts.append(f"{k}={step[k]}")
            lines.append(f"- {step['type']} **{step['id']}** ({', '.join(parts[2:])})")
        lines.append("")
        tm = t["timing"]
        lines.append(f"**Timing:** planned {tm['planned_t_h']} / fired {tm['fired_t_h']} "
                     f"(delay {tm['delay_h']}h), window {tm['window']}, "
                     f"schedule {tm['schedule_status']}")
        b = t["behavior"]
        lines.append(f"**Behavior:** max_tokens={b['max_tokens']} delay={b['response_delay_s']}s "
                     f"closing={b['closing_tendency']} initiative={b['initiative']} "
                     f"length_scale={b['length_scale']}")
        if t["memory_context"]:
            lines.append("**Memory context (L4):** " + ", ".join(
                f"{e['id']} ({e.get('kind')})" for e in t["memory_context"]))
        lines.append(f"**Persisted intent_id:** `{t['persisted_intent_id']}`")
        lines.append(f"**Evidence:** `{t['evidence'][:220]}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--records", required=True)
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--out", default="results/companion-vertical-slice/traces.md")
    args = p.parse_args(argv)
    traces = build_traces(args.db, args.records, args.n)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(traces), encoding="utf-8")
    print(json.dumps({"n_traces": len(traces), "out": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
