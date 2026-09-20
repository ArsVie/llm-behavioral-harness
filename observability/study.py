"""Read-only study panels: the day's agenda, the reach-outs, the cycles, the levers.

Every builder takes an OPEN run connection and returns plain JSON-ready dicts,
so the app renders them without knowing any SQL. Nothing here writes, and
nothing needs the live process: the tables already carry all of it.

Times are DAY-LOCAL hours (0..24) unless a field says ``t_h`` — the run's
absolute clock stays behind the wall of this module.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

from engine.circadian import energy as circadian_energy
from engine.types import PHASE_FRACTIONS, TimingParams

from observability.calls import usage_payload
from observability.db import rows as db_rows

#: Samples per day on the circadian curve (one point every half hour).
CURVE_STEP_H = 0.5
DAY_H = 24.0


def _loads(raw: Any) -> dict[str, Any]:
    """JSON text -> dict, tolerantly: a missing or foreign blob reads empty."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def run_now(conn: sqlite3.Connection) -> float | None:
    """The run's present: the newest ``t_h`` any table carries."""
    row = conn.execute(
        "select max(t_h) from ("
        "  select max(t_h) as t_h from messages"
        "  union all select max(t_h) from state_events"
        "  union all select max(t_h) from llm_calls)"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def latest_day(conn: sqlite3.Connection) -> int:
    """The day the run is currently in (0 when the run has nothing yet)."""
    now = run_now(conn)
    return int(now // DAY_H) if now is not None else 0


def daily_row(conn: sqlite3.Connection, day: int) -> dict[str, Any] | None:
    """The day's engine row: mood, cycle, the model's own parameters."""
    row = conn.execute(
        "select day, M, m_level, g, p, arg, mu, eta, cycle_day, phase_label, "
        "seed, score from daily_state where day = ?",
        (day,),
    ).fetchone()
    return dict(row) if row is not None else None


def day_numbers(conn: sqlite3.Connection) -> list[int]:
    """Days the run has something to draw for, oldest first."""
    days: set[int] = set()
    for row in conn.execute("select distinct day from agenda_items"):
        days.add(int(row[0]))
    for row in conn.execute("select distinct day from daily_state"):
        days.add(int(row[0]))
    for row in conn.execute("select distinct t_h from messages"):
        days.add(int(float(row[0]) // DAY_H))
    return sorted(days)


def _curve(phase_label: str, timing: TimingParams) -> list[list[float]]:
    """The day's energy rhythm, sampled — the same call behavior.py makes."""
    points: list[list[float]] = []
    steps = int(round(DAY_H / CURVE_STEP_H))
    for i in range(steps + 1):
        hour = i * CURVE_STEP_H
        points.append([round(hour, 2),
                       round(circadian_energy(hour, phase_label, timing), 4)])
    return points


def _day_span(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One compact roll-up per day: what the week and month views draw.

    The day view needs the full detail of one day; the wider views need a
    little of every day, so this is small on purpose — blocks as bare
    start/end/status, reach-outs as the hours they were decided at.
    """
    days = day_numbers(conn)
    if not days:
        return []
    span: dict[int, dict[str, Any]] = {
        day: {"day": day, "agenda": [], "reach_outs": [], "sent": 0,
              "suppressed": 0, "messages": 0, "M": None, "m_level": None,
              "cycle_day": None, "phase_label": ""}
        for day in days
    }
    for row in conn.execute(
            "select start_t_h, end_t_h, status, source_type from agenda_items"):
        day = int(float(row["start_t_h"]) // DAY_H)
        entry = span.get(day)
        if entry is None:
            continue
        entry["agenda"].append({
            "hour": round(float(row["start_t_h"]) - day * DAY_H, 3),
            "end": round(float(row["end_t_h"]) - day * DAY_H, 3),
            "status": str(row["status"] or ""),
            "source": str(row["source_type"] or ""),
        })
    for row in conn.execute(
            "select t_h, verdict_json from decision_records "
            "where popup_kind = 'tool_decide_proactive'"):
        day = int(float(row["t_h"]) // DAY_H)
        entry = span.get(day)
        if entry is None:
            continue
        fired = bool(_loads(row["verdict_json"]).get("initiate"))
        entry["reach_outs"].append(
            {"hour": round(float(row["t_h"]) - day * DAY_H, 3), "fired": fired})
        entry["sent" if fired else "suppressed"] += 1
    for row in conn.execute("select t_h, role from messages"):
        day = int(float(row["t_h"]) // DAY_H)
        entry = span.get(day)
        if entry is not None:
            entry["messages"] += 1
    for row in conn.execute(
            "select day, M, m_level, cycle_day, phase_label from daily_state"):
        entry = span.get(int(row["day"]))
        if entry is None:
            continue
        entry.update(M=row["M"], m_level=row["m_level"],
                     cycle_day=row["cycle_day"],
                     phase_label=str(row["phase_label"] or ""))
    return [span[day] for day in days]


def day_view(conn: sqlite3.Connection, *, day: int,
             timing: TimingParams | None = None) -> dict[str, Any]:
    """One day as the calendar's Day view draws it: agenda, reach-outs,
    message ticks, the two cycles, the now-line."""
    timing = timing or TimingParams()
    start, end = day * DAY_H, (day + 1) * DAY_H
    state = daily_row(conn, day) or {}
    phase = str(state.get("phase_label") or "")

    agenda = [
        dict(row) for row in conn.execute(
            "select id, activity, start_t_h, end_t_h, source_type, source_id, "
            "salience, status, outcome from agenda_items where day = ? "
            "order by start_t_h", (day,))
    ]

    # A reach-out reads as a marker whether or not it was sent: the decision
    # record says which, and the reason says why.
    proactive: list[dict[str, Any]] = []
    for row in conn.execute(
        "select t_h, verdict_json, inputs_json from decision_records "
        "where popup_kind = 'tool_decide_proactive' and t_h >= ? and t_h < ? "
        "order by t_h", (start, end)):
        verdict = _loads(row["verdict_json"])
        inputs = _loads(row["inputs_json"])
        proactive.append({
            "hour": round(float(row["t_h"]) - start, 3),
            "fired": bool(verdict.get("initiate")),
            "hook": str(inputs.get("hook") or inputs.get("reason") or ""),
            "reason": str(verdict.get("reason") or ""),
            "source_id": str(inputs.get("source_id") or ""),
        })

    messages = [
        {"hour": round(float(row["t_h"]) - start, 3),
         "role": str(row["role"]),
         "proactive": bool(row["proactive"])}
        for row in conn.execute(
            "select t_h, role, proactive from messages where t_h >= ? and t_h < ? "
            "order by t_h", (start, end))
    ]

    now = run_now(conn)
    return {
        "day": day,
        # The now-line belongs to the day the run is actually in; on another
        # day there is no "now" to draw.
        "now_hour": (round(now - start, 3)
                     if now is not None and start <= now < end else None),
        "agenda": agenda,
        "proactive": proactive,
        "messages": messages,
        "cycle": {"cycle_day": state.get("cycle_day"),
                  "phase_label": phase,
                  # The engine draws each cycle's length afresh and does not
                  # persist it, so the wheel shows the phases themselves —
                  # the engine's own table — not absolute days.
                  "phases": [{"label": label, "from": round(start, 4),
                              "to": round(end, 4)}
                             for label, start, end in PHASE_FRACTIONS]},
        "mood": {"M": state.get("M"), "m_level": state.get("m_level")},
        "circadian": _curve(phase, timing),
        "quiet_hours": [float(x) for x in timing.quiet_hours],
        "days": day_numbers(conn),
        "span": _day_span(conn),
    }


def _hist(counter: Counter, *, order: list[str] | None = None) -> list[list[Any]]:
    """A histogram as ordered ``[label, count]`` pairs — charts want order."""
    if order is not None:
        items = [(str(key), counter.get(key, 0)) for key in order]
    else:
        items = sorted(((str(k), v) for k, v in counter.items()),
                       key=lambda pair: (-pair[1], pair[0]))
    return [[label, count] for label, count in items if count]


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "min": None, "max": None}
    return {"n": len(values), "mean": round(sum(values) / len(values), 3),
            "min": round(min(values), 3), "max": round(max(values), 3)}


def levers(conn: sqlite3.Connection) -> dict[str, Any]:
    """The distribution levers: one histogram + summary per subject.

    Descriptive only — counts, means, spreads. Every lever carries its own
    ``n``, because a shape drawn over four rows is not a distribution.
    """
    agenda = [
        dict(row) for row in conn.execute(
            "select day, start_t_h, end_t_h, source_type, status, salience "
            "from agenda_items")
    ]
    status_order = ["completed", "planned", "skipped", "shifted", "in_progress"]
    source_counter = Counter(str(r["source_type"] or "") for r in agenda)
    hour_counter = Counter(int(float(r["start_t_h"]) % DAY_H) for r in agenda)
    durations = [float(r["end_t_h"]) - float(r["start_t_h"]) for r in agenda]
    saliences = [float(r["salience"]) for r in agenda if r["salience"] is not None]

    proactive = [
        dict(row) for row in conn.execute(
            "select t_h, verdict_json, inputs_json from decision_records "
            "where popup_kind = 'tool_decide_proactive' order by t_h")
    ]
    sent = suppressed = 0
    reason_counter: Counter = Counter()
    reach_hours: Counter = Counter()
    for row in proactive:
        verdict = _loads(row["verdict_json"])
        if verdict.get("initiate"):
            sent += 1
        else:
            suppressed += 1
            reason_counter[str(verdict.get("reason") or "(no reason)")] += 1
        reach_hours[int(float(row["t_h"]) % DAY_H)] += 1

    mood_rows = [
        dict(row) for row in conn.execute(
            "select day, M, m_level, phase_label from daily_state order by day")
    ]
    phase_counter: Counter = Counter(
        str(r["phase_label"] or "") for r in mood_rows)
    by_phase_mood: dict[str, list[float]] = {}
    for row in mood_rows:
        if row["M"] is not None:
            by_phase_mood.setdefault(str(row["phase_label"] or ""), []).append(
                float(row["M"]))

    # Spend is the harness' own math (per-model pricing), the same numbers the
    # calls card shows: reuse it rather than derive a second truth.
    spend = usage_payload(db_rows(conn, "select * from llm_calls"))

    return {
        "window": {"days": day_numbers(conn),
                   "n_agenda": len(agenda), "n_reach_outs": len(proactive)},
        "agenda": {
            "by_status": _hist(Counter(str(r["status"] or "") for r in agenda),
                               order=status_order),
            "by_source": _hist(source_counter),
            "by_hour": [[str(h), c] for h, c in sorted(hour_counter.items())],
            "duration": _summary(durations),
            "salience": _summary(saliences),
        },
        "proactive": {
            "sent": sent,
            "suppressed": suppressed,
            "by_hour": [[str(h), c] for h, c in sorted(reach_hours.items())],
            "suppressed_reasons": _hist(reason_counter),
        },
        "mood": {
            "series": [[int(r["day"]), r["M"], r["m_level"]] for r in mood_rows],
            "M": _summary([float(r["M"]) for r in mood_rows if r["M"] is not None]),
        },
        "cycle": {
            "phase_days": _hist(phase_counter),
            "mood_by_phase": [
                [phase, _summary(values)]
                for phase, values in sorted(by_phase_mood.items())
            ],
        },
        "spend": spend,
    }
