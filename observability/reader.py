"""Run-level read model: the overview payload the dashboard's page starts from.

This module owns the *run* questions (how old, how live, how much happened,
which invariants broke, what the clock says) and re-exports the call/event
read model so the HTTP layer has one import. Everything below is read-only.

Reused rather than re-implemented, so the app can never disagree with the CLI
inspectors:

* ``harness.trace.load_anchor`` / ``Anchor`` — virtual hour to local clock;
* ``harness.trace.collect_findings`` — the ``checks`` verdicts;
* ``harness.trace.CACHE_FLOOR`` — the "shared prefix should have cached" line;
* ``harness.spend.GroupStats`` — the spend formula and cache-hit rate;
* ``harness.tools.TOOL_SCHEMAS`` — the decide-leg schemas the store omits.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from harness.trace import Ctx, collect_findings, load_anchor

from observability.calls import (
    DEFAULT_CONTEXT_WINDOW,
    call_detail,
    call_payload,
    call_rows,
    context_payload,
    stamp,
    stats_json,
    tool_schemas_for,
    usage_payload,
)
from observability.db import (
    RunRef,
    count as _count,
    find_runs,
    live_processes,
    open_run,
    repo_root,
    rows as _rows,
)
from observability.events import (
    DEFAULT_LIMIT,
    collect_events,
    events_payload,
)

__all__ = [
    "DEFAULT_CONTEXT_WINDOW", "DEFAULT_LIMIT", "RunRef",
    "call_detail", "call_payload", "call_rows", "clock_payload", "collect_events",
    "context_payload", "counters", "events_payload", "find_runs", "latest_ids",
    "live_processes", "load_anchor", "open_run", "repo_root", "run_detail",
    "run_summary", "stats_json", "tool_schemas_for", "usage_payload",
]

#: Domain tables the counters panel reports (missing tables read 0).
COUNTER_TABLES: tuple[str, ...] = (
    "messages", "state_events", "schedule_events", "decision_records",
    "steering_queue", "proactive_intents", "judgements", "conversations",
    "conversation_turns", "agenda_items", "life_arcs", "memory_episodes",
    "user_model_assertions",
)


def counters(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per domain table (missing tables read 0, never raise)."""
    return {table: _count(conn, table) for table in COUNTER_TABLES}


def clock_payload(conn: sqlite3.Connection, anchor: Any) -> dict[str, Any]:
    """Virtual and real time for the run, plus its anchor."""
    found = _rows(
        conn,
        "select max(t_h) as t_h from ("
        " select max(t_h) as t_h from llm_calls"
        " union all select max(t_h) from messages"
        " union all select max(t_h) from state_events"
        " union all select max(t_h) from conversation_turns)",
    )
    current = float((found[0].get("t_h") if found else None) or 0.0)
    days = _rows(conn, "select max(day) as day from daily_state")
    tz = getattr(anchor, "tz", None)
    return {
        "anchor": None if anchor is None else {
            "tz": str(getattr(anchor, "tz", "UTC")),
            "epoch0_s": anchor.epoch0_s,
            "t_h0": anchor.t_h0,
        },
        "virtual_now": round(current, 3),
        "virtual_day": (days[0].get("day") if days else None),
        "real_now": stamp(anchor, current),
        "local_now": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tz": str(tz) if tz is not None else None,
    }


def latest_ids(conn: sqlite3.Connection) -> dict[str, Any]:
    """Newest row ids, so the front-end can detect movement cheaply."""
    calls = _rows(conn, "select max(id) as id from llm_calls")
    messages = _rows(conn, "select max(id) as id from messages")
    call_id = calls[0].get("id") if calls else None
    message_id = messages[0].get("id") if messages else None
    return {"call_id": int(call_id or 0), "message_id": int(message_id or 0)}


def checks_payload(conn: sqlite3.Connection, anchor: Any) -> dict[str, Any]:
    """The ``harness.trace checks`` verdicts, as data.

    A crashing invariant is reported as a finding instead of a 500: an
    observability page must still show the run it was asked about, and the
    crashed check is itself the most interesting thing about such a run.
    """
    try:
        findings = collect_findings(Ctx(conn=conn, anchor=anchor))
        raw = [
            {"severity": finding.severity.lower(), "code": finding.code,
             "message": finding.message, "detail": finding.detail}
            for finding in findings
        ]
    except Exception as exc:  # noqa: BLE001 - the checker, not the run, is broken
        raw = [{"severity": "error", "code": "checks-crashed",
                "message": f"{type(exc).__name__}: {exc}",
                "detail": "harness.trace.collect_findings raised on this run"}]
    counts = {"error": 0, "warn": 0, "info": 0}
    for finding in raw:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {"counts": counts, "findings": raw}


def run_summary(ref: RunRef, *, now: float, context_window: int) -> dict[str, Any]:
    """Headline facts about one run, without loading its history."""
    with open_run(ref.path) as conn:
        latest = _rows(conn, "select day, t_h, model, lane from llm_calls "
                             "order by id desc limit 1")
        head = latest[0] if latest else {}
        seeds = _rows(conn, "select seed from daily_state order by day desc limit 1")
        calls = _rows(conn, "select * from llm_calls order by id")
        pid = live_processes().get(str(ref.path.resolve()))
        return {
            "id": str(ref.path),
            "label": ref.label,
            "path": str(ref.path),
            "mtime": ref.mtime,
            "wal_mtime": ref.wal_mtime,
            "idle_s": round(ref.idle_seconds(now), 1),
            "active": ref.is_active(now),
            "process_up": pid is not None,
            "pid": pid,
            "model": head.get("model"),
            "lane": head.get("lane"),
            "seed": (seeds[0].get("seed") if seeds else None),
            "days": _count(conn, "daily_state"),
            "calls": len(calls),
            "messages": _count(conn, "messages"),
            "events": _count(conn, "state_events"),
            "context_window": context_window,
            "usage": usage_payload(calls)["totals"],
        }


def run_detail(ref: RunRef, *, context_window: int, call_id: int | None = None,
               limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Everything the overview screen needs for one run."""
    with open_run(ref.path) as conn:
        anchor = load_anchor(conn)
        rows = call_rows(conn)
        return {
            "summary": run_summary(ref, now=time.time(), context_window=context_window),
            "clock": clock_payload(conn, anchor),
            "usage": usage_payload(rows),
            "counters": counters(conn),
            "checks": checks_payload(conn, anchor),
            "latest": latest_ids(conn),
            "calls": [call_payload(row, anchor) for row in rows[-limit:]],
            "events": events_payload(conn, anchor, after=0, limit=limit),
            "context": context_payload(conn, anchor, call_id=call_id,
                                       context_window=context_window),
        }
