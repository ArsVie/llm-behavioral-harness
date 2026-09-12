"""The unified event stream: every durable row a run produced, in one order.

The run DB keeps its history in eight tables written by different layers
(messages, state_events, schedule_events, decision_records, steering_queue,
proactive_intents, judgements, conversation_turns). ``harness.trace`` renders
them per view; this module folds them into ONE chronological stream so a live
front-end can tail the run the way DeepSeek Harness' trajectory view tails a
session.

Ordering is a composite sequence number, monotonic in virtual time and stable
across polls:

    seq = round(t_h * 1e6) * 10 + rank

``rank`` breaks ties inside one virtual instant by lane, and each row's own id
breaks the remaining ties. The same row therefore keeps the same seq on every
poll, which is what makes ``?after=<seq>`` safe for incremental fetching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from observability.db import rows as _rows

#: One page of the stream.
DEFAULT_LIMIT: int = 400

#: Tie-break order for rows sharing one virtual instant (lower shows first).
RANK: dict[str, int] = {
    "judgement": 0,
    "message": 1,
    "conversation": 2,
    "decision": 3,
    "steer": 4,
    "proactive": 5,
    "schedule": 6,
    "agenda": 7,
    "state": 8,
    "call": 9,
    "memory": 10,
}

#: Substrings in an event/verdict name that mark a row worth flagging.
_ERROR_MARKERS = ("failed", "error", "refused")
_WARN_MARKERS = ("requeue", "omit", "suppress", "expired", "degraded")


def seq_of(t_h: float, rank: str, row_id: int) -> int:
    """The composite cursor for one row: stable across polls, ordered by time.

    Virtual time dominates, then the lane rank, then the row's own id — so two
    calls at the same instant (routine: a chat leg and its decide legs share
    one timestamp) keep distinct, stable cursors and ``?after=<seq>`` never
    swallows a sibling.
    """
    rank_value = RANK.get(rank, len(RANK))
    return (int(round(t_h * 1_000_000)) * 1_000_000
            + rank_value * 100_000 + (abs(int(row_id)) % 100_000))


def _severity(label: str, detail: str) -> str:
    """``error`` / ``warn`` / ``info`` from the row's own wording."""
    blob = f"{label} {detail}".lower()
    if any(marker in blob for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in blob for marker in _WARN_MARKERS):
        return "warn"
    return "info"


def _loads(text: Any) -> Any:
    """Tolerant JSON decode: non-JSON bodies come back as the raw string."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _clip(text: Any, limit: int = 240) -> str:
    """One-line digest of a detail body."""
    blob = str(text or "").replace("\n", " ⏎ ")
    return blob if len(blob) <= limit else blob[: limit - 1] + "…"


@dataclass(frozen=True)
class Event:
    """One row of the unified stream, already priced for display."""

    seq: int
    kind: str
    rank: str
    severity: str
    day: int
    t_h: float
    real: str | None
    label: str
    detail: str
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "severity": self.severity,
            "day": self.day,
            "t_h": round(self.t_h, 4),
            "real": self.real,
            "label": self.label,
            "detail": self.detail,
            "raw": self.raw,
        }


def _real(anchor: Any, t_h: float) -> str | None:
    """Local wall clock for a virtual hour, or None on an unanchored run."""
    if anchor is None:
        return None
    try:
        stamp: datetime = anchor.real(t_h)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    return stamp.isoformat(timespec="seconds")


def _row_event(anchor: Any, row: dict[str, Any], kind: str, rank: str,
               label: str, detail: str, raw: dict[str, Any]) -> Event:
    t_h = float(row.get("t_h") or 0.0)
    day_value = row.get("day")
    day = int(day_value) if day_value is not None else int(t_h // 24.0)
    return Event(
        seq=seq_of(t_h, rank, int(row.get("id") or 0)),
        kind=kind,
        rank=rank,
        severity=_severity(label, detail),
        day=day,
        t_h=t_h,
        real=_real(anchor, t_h),
        label=label,
        detail=detail,
        raw=raw,
    )


def from_messages(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Inbound and companion turns, including the plan/state cards."""
    out: list[Event] = []
    for row in rows:
        role = str(row.get("role") or "?")
        text = _clip(row.get("content"), 200)
        label = {"user": "user message", "assistant": "companion reply",
                 "system": "internal card"}.get(role, f"{role} message")
        out.append(_row_event(
            anchor, row, "message", "message", label, text,
            {"role": role, "proactive": bool(row.get("proactive")),
             "chars": len(str(row.get("content") or "")),
             "conversation_id": row.get("conversation_id"),
             "meta": _loads(row.get("meta"))},
        ))
    return out


def from_state_events(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Engine/state rows: lifecycle, memory, cache and parse outcomes."""
    return [
        _row_event(anchor, row, "state", "state", str(row.get("event") or "?"),
                   _clip(row.get("detail")), {"raw": _loads(row.get("detail"))})
        for row in rows
    ]


def from_decisions(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Decision pop-ups: what was asked, what the model answered."""
    out: list[Event] = []
    for row in rows:
        verdict = _loads(row.get("verdict_json"))
        summary = verdict if isinstance(verdict, dict) else row.get("raw_reply")
        out.append(_row_event(
            anchor, row, "decision", "decision", str(row.get("popup_kind") or "?"),
            _clip(summary, 200),
            {"source": row.get("source"), "transport": row.get("transport"),
             "event_label": row.get("event_label"), "state_label": row.get("state_label"),
             "verdict": verdict, "inputs": _loads(row.get("inputs_json")),
             "raw_reply": _clip(row.get("raw_reply"), 400),
             "replay_id": row.get("replay_id"),
             "budget_consumed": row.get("budget_consumed")},
        ))
    return out


def from_steering(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Steering rows: what was queued for the next generation boundary."""
    out: list[Event] = []
    for row in rows:
        payload = _loads(row.get("payload_json"))
        status = str(row.get("status") or "?")
        out.append(_row_event(
            anchor, row, "steer", "steer", f"{row.get('kind')} ({status})",
            _clip(payload, 200),
            {"status": status, "boundary": row.get("boundary"),
             "delivered_t_h": row.get("delivered_t_h"), "payload": payload,
             "seen_turn_id": row.get("seen_turn_id")},
        ))
    return out


def from_proactive(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Proactive intents: the hook, its validity window and its fate."""
    out: list[Event] = []
    for row in rows:
        t_h = float(row.get("created_t_h") or 0.0)
        status = str(row.get("status") or "?")
        out.append(Event(
            seq=seq_of(t_h, "proactive", abs(hash(str(row.get("id")))) % 10_000),
            kind="proactive",
            rank="proactive",
            severity=_severity(status, str(row.get("hook") or "")),
            day=int(t_h // 24.0),
            t_h=t_h,
            real=_real(anchor, t_h),
            label=f"intent {row.get('id')} ({status})",
            detail=_clip(row.get("hook"), 200),
            raw={"reason": row.get("reason"), "source_type": row.get("source_type"),
                 "source_id": row.get("source_id"),
                 "valid_until_t_h": row.get("valid_until_t_h"),
                 "salience": row.get("salience"), "evidence": _loads(row.get("evidence")),
                 "status": status},
        ))
    return out


def from_schedule(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Planned proactive firings and whether they fired or expired."""
    out: list[Event] = []
    for row in rows:
        status = str(row.get("status") or "?")
        caused = row.get("caused_by") if hasattr(row, "get") else None
        planned = float(row.get("t_h") or 0.0)
        fired = row.get("fired_t_h")
        detail = f"{row.get('reason')} — {status}"
        if fired is not None:
            detail += f" at t_h {float(fired):.2f}"
        if caused:
            detail += f" ({caused})"
        out.append(_row_event(anchor, row, "schedule", "schedule",
                              _clip(row.get("reason")), detail,
                              {"status": status, "fired_t_h": fired,
                               "planned_t_h": planned, "seed": row.get("seed"),
                               "caused_by": caused}))
    return out


def from_conversations(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Conversation lifecycles: who opened it, why it closed."""
    out: list[Event] = []
    for row in rows:
        opened = float(row.get("opened_t_h") or 0.0)
        closed = row.get("closed_t_h")
        detail = f"opened by {row.get('opened_by')}"
        if closed is not None:
            detail += f" · closed ({row.get('close_reason')})"
        out.append(Event(
            seq=seq_of(float(closed if closed is not None else opened),
                       "conversation", abs(hash(str(row.get("id")))) % 10_000),
            kind="conversation",
            rank="conversation",
            severity="info",
            day=int(opened // 24.0),
            t_h=opened,
            real=_real(anchor, opened),
            label=f"conversation {row.get('id')}",
            detail=detail,
            raw={"opened_by": row.get("opened_by"),
                 "closed_t_h": closed, "close_reason": row.get("close_reason")},
        ))
    return out


def from_judgements(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Daily judge scores, shadow or live."""
    out: list[Event] = []
    for row in rows:
        day = int(row.get("day") or 0)
        t_h = day * 24.0
        mode = "shadow" if row.get("shadow") else "live"
        out.append(Event(
            seq=seq_of(t_h, "judgement", day),
            kind="judgement",
            rank="judgement",
            severity="info",
            day=day,
            t_h=t_h,
            real=_real(anchor, t_h),
            label=f"judge score {row.get('score')} ({mode})",
            detail=_clip(row.get("justification"), 200),
            raw={"score": row.get("score"), "model": row.get("model"),
                 "shadow": bool(row.get("shadow"))},
        ))
    return out


def from_calls(anchor: Any, rows: Iterable[dict[str, Any]]) -> list[Event]:
    """Model calls: the request that went out and what it cost."""
    out: list[Event] = []
    for row in rows:
        prompt = int(row.get("prompt_tokens") or 0)
        cached = int(row.get("cached_tokens") or 0)
        completion = int(row.get("completion_tokens") or 0)
        hit = (cached / prompt * 100.0) if prompt else 0.0
        out.append(_row_event(
            anchor, row, "call", "call",
            f"{row.get('role')} call · {row.get('lane')}",
            f"prompt {prompt} ({hit:.0f}% cached) · out {completion}",
            {"model": row.get("model"), "prompt_tokens": prompt,
             "cached_tokens": cached, "cache_miss_tokens": row.get("cache_miss_tokens"),
             "completion_tokens": completion, "total_tokens": row.get("total_tokens"),
             "raw_cost": row.get("raw_cost"),
             "has_repro": row.get("repro_json") is not None,
             "response_head": _clip(row.get("response"), 160)},
        ))
    return out


def collect_events(conn: Any, anchor: Any) -> list[Event]:
    """Every table's rows as one sorted stream, oldest first."""
    collected: list[Event] = []
    collected += from_messages(anchor, _rows(conn, "select * from messages order by id"))
    collected += from_state_events(anchor, _rows(conn, "select * from state_events order by id"))
    collected += from_decisions(anchor, _rows(conn, "select * from decision_records order by id"))
    collected += from_steering(anchor, _rows(conn, "select * from steering_queue order by id"))
    collected += from_proactive(anchor, _rows(conn, "select * from proactive_intents"))
    collected += from_schedule(anchor, _rows(conn, "select * from schedule_events order by id"))
    collected += from_conversations(anchor, _rows(conn, "select * from conversations"))
    collected += from_judgements(anchor, _rows(conn, "select * from judgements order by day"))
    collected += from_calls(anchor, _rows(conn, "select * from llm_calls order by id"))
    collected.sort(key=lambda event: event.seq)
    return collected


def events_payload(conn: Any, anchor: Any, *, after: int = 0,
                   limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """The unified event stream, oldest first, cursor-filtered by ``after``."""
    collected = collect_events(conn, anchor)
    newest = collected[-1].seq if collected else 0
    fresh = [event for event in collected if event.seq > after]
    page = fresh[-limit:] if len(fresh) > limit else fresh
    return {
        "events": [event.to_json() for event in page],
        "latest_seq": newest,
        "returned": len(page),
        "truncated": len(fresh) > len(page),
    }
