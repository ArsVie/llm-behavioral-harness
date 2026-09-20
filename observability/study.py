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
from typing import Any

from engine.circadian import energy as circadian_energy
from engine.types import PHASE_FRACTIONS, TimingParams
from harness.trace import load_anchor

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


def day_dates(conn: sqlite3.Connection) -> dict[int, dict[str, str]]:
    """The real date each run day began, from the run's own anchor.

    The calendar is a real calendar: a day number alone cannot place a cell in
    a month grid or a weekday column. Unanchored runs read empty, and the
    front-end falls back to "day N" labels.
    """
    anchor = load_anchor(conn)
    if anchor is None:
        return {}
    dates: dict[int, dict[str, str]] = {}
    for day in day_numbers(conn):
        started = anchor.real(day * DAY_H)
        dates[day] = {"date": started.date().isoformat(),
                      "weekday": started.strftime("%a"),
                      "month": started.strftime("%Y-%m")}
    return dates


def rhythm_curves(conn: sqlite3.Connection,
                  timing: TimingParams | None = None) -> dict[str, Any]:
    """One energy curve per cycle phase the run has lived, oldest phase first.

    The panel marks the reach-outs on these curves, so it needs the engine's
    own function, not a redrawing of it.
    """
    timing = timing or TimingParams()
    phases: list[str] = []
    for row in conn.execute(
            "select day, phase_label from daily_state order by day"):
        phase = str(row["phase_label"] or "")
        if phase and phase not in phases:
            phases.append(phase)
    latest = conn.execute(
        "select phase_label from daily_state order by day desc limit 1").fetchone()
    return {
        "current": str(latest["phase_label"]) if latest is not None else "",
        "curves": [{"phase": phase, "points": _curve(phase, timing)}
                   for phase in phases],
        "quiet_hours": [float(x) for x in timing.quiet_hours],
    }


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


def _mood_series(conn: sqlite3.Connection) -> dict[str, Any]:
    """The run's mood line and the 7-day expectation it deviates from.

    Same kernel as the README's mood panel (a padded 7-wide box mean), so the
    app draws the analysis figure's line, not its own smoothing.
    """
    rows = [
        dict(row) for row in conn.execute(
            "select day, M, m_level, phase_label from daily_state order by day")
    ]
    values = [float(row["M"]) for row in rows if row["M"] is not None]
    expected: list[float | None] = []
    window = 7
    half = window // 2
    if values:
        padded = [values[0]] * half + values + [values[-1]] * half
        expected = [
            round(sum(padded[i + k] for k in range(window)) / window, 3)
            for i in range(len(values))
        ]
    return {
        "days": [int(row["day"]) for row in rows],
        "M": [row["M"] for row in rows],
        "m_level": [row["m_level"] for row in rows],
        "phase": [str(row["phase_label"] or "") for row in rows],
        "expected": expected,
        "window": window,
    }


def _panels(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the study panels mark: every reach-out decision, and when the two
    of them talked — both run-wide, because a panel reads the whole run, not
    the day the calendar happens to show."""
    proactive = []
    for row in conn.execute(
            "select t_h, verdict_json, inputs_json from decision_records "
            "where popup_kind = 'tool_decide_proactive' order by t_h"):
        verdict = _loads(row["verdict_json"])
        inputs = _loads(row["inputs_json"])
        proactive.append({
            "day": int(float(row["t_h"]) // DAY_H),
            "hour": round(float(row["t_h"]) % DAY_H, 3),
            "fired": bool(verdict.get("initiate")),
            "hook": str(inputs.get("hook") or inputs.get("reason") or ""),
            "reason": str(verdict.get("reason") or ""),
        })

    days = day_numbers(conn)
    index = {day: i for i, day in enumerate(days)}
    contact = [[[0, 0, 0] for _ in range(24)] for _ in days]
    for row in conn.execute("select t_h, role, proactive from messages"):
        day = int(float(row["t_h"]) // DAY_H)
        slot = index.get(day)
        if slot is None:
            continue
        hour = int(float(row["t_h"]) % DAY_H)
        who = 2 if row["proactive"] else (0 if str(row["role"]) == "user" else 1)
        contact[slot][hour][who] += 1
    return {"proactive": proactive, "contact": contact, "contact_days": days}


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
    dates = day_dates(conn)
    span = _day_span(conn)
    for entry in span:
        entry.update(dates.get(int(entry["day"]), {}))
    return {
        "day": day,
        # The now-line belongs to the day the run is actually in; on another
        # day there is no "now" to draw.
        "now_hour": (round(now - start, 3)
                     if now is not None and start <= now < end else None),
        "today": latest_day(conn),
        "date": dates.get(day, {}).get("date"),
        "weekday": dates.get(day, {}).get("weekday"),
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
        "mood_series": _mood_series(conn),
        "circadian": _curve(phase, timing),
        "rhythm": rhythm_curves(conn, timing),
        "panels": _panels(conn),
        "quiet_hours": [float(x) for x in timing.quiet_hours],
        "days": day_numbers(conn),
        "dates": dates,
        "span": span,
    }
