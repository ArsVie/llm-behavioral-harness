"""Canonical conversation-log model (behavioral_signature).

A conversation log is an ordered stream of turns. Each turn has a speaker
(``user`` | ``companion``), the message text, and a time. Two time sources are
supported and are NEVER mixed (see ``time_kind``):

* ``t_h`` — absolute virtual hours since simulation start (the harness
  convention: t_h = 0.0 is day 0 at 00:00; ``int(t_h // 24)`` is the day).
  This is what the live trial DB stores today (pre-S1 schema).
* ``timestamp`` — a real wall-clock ``datetime`` (tz-aware UTC), used when the
  log carries real time (post-S1).

Time-source rule (deterministic by construction): time-based metrics use real
``timestamp`` values only when EVERY turn in the log carries one; otherwise
they fall back to ``t_h``. A log that mixes sources is treated as a ``t_h``
log; turns without the chosen source are skipped by time-based metrics.

``conversation_id`` on each turn is the conversation-boundary marker; the
stream may span multiple conversations (the metrics operate on the stream).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence, Union

SPEAKERS = ("user", "companion")

_TIME_KIND_T_H = "t_h"
_TIME_KIND_DATETIME = "datetime"


@dataclass(frozen=True)
class LogTurn:
    """One turn of a conversation log.

    speaker: 'user' | 'companion' (validated by LogRecord construction).
    text: the raw message text.
    t_h: virtual-hour timestamp (float), or None when unknown.
    timestamp: real wall-clock instant, or None when unavailable.
    turn_index: 0-based position within its conversation (provenance).
    conversation_id: conversation-boundary marker (provenance).
    """

    speaker: str
    text: str
    t_h: Union[float, None] = None
    timestamp: Union[datetime, None] = None
    turn_index: Union[int, None] = None
    conversation_id: Union[str, None] = None


@dataclass(frozen=True)
class LogRecord:
    """A whole conversation log: an ordered tuple of turns plus provenance.

    ``conversation_id`` labels the log (may be "" for anonymous/crafted logs).
    ``opened_t_h`` / ``closed_t_h`` carry the conversation's span when known.
    """

    conversation_id: str
    turns: Sequence[LogTurn] = ()
    opened_t_h: Union[float, None] = None
    closed_t_h: Union[float, None] = None

    def __post_init__(self) -> None:
        for t in self.turns:
            if t.speaker not in SPEAKERS:
                raise ValueError(f"unknown speaker {t.speaker!r}; expected one of {SPEAKERS}")


def time_kind(record: LogRecord) -> str:
    """Which time source the log uses: 'datetime' iff every turn has a real
    ``timestamp``, else 't_h'. Pure function of the data — deterministic."""
    if record.turns and all(t.timestamp is not None for t in record.turns):
        return _TIME_KIND_DATETIME
    return _TIME_KIND_T_H


# --------------------------------------------------------------------------- #
# canonical JSON codec (deterministic formatting)
# --------------------------------------------------------------------------- #


def _turn_to_dict(t: LogTurn) -> dict:
    d: dict = {"speaker": t.speaker, "text": t.text}
    if t.t_h is not None:
        d["t_h"] = t.t_h
    if t.timestamp is not None:
        d["timestamp"] = t.timestamp.isoformat(timespec="microseconds")
    if t.turn_index is not None:
        d["turn_index"] = t.turn_index
    if t.conversation_id is not None:
        d["conversation_id"] = t.conversation_id
    return d


def log_to_json(record: LogRecord) -> str:
    """Serialize a log to canonical JSON (fixed field order, fixed formatting).

    Byte-deterministic: the same LogRecord always serializes to the same
    string. ``None`` fields are omitted.
    """
    doc = {
        "conversation_id": record.conversation_id,
        "turns": [_turn_to_dict(t) for t in record.turns],
    }
    if record.opened_t_h is not None:
        doc["opened_t_h"] = record.opened_t_h
    if record.closed_t_h is not None:
        doc["closed_t_h"] = record.closed_t_h
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def log_from_json(text: str) -> LogRecord:
    """Parse canonical JSON back into a LogRecord (inverse of log_to_json)."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or "conversation_id" not in doc or "turns" not in doc:
        raise ValueError("not a canonical behavioral_signature log document")
    turns = []
    for i, td in enumerate(doc["turns"]):
        if not isinstance(td, dict) or "speaker" not in td or "text" not in td:
            raise ValueError(f"turn {i}: expected {{speaker, text, ...}}")
        ts = td.get("timestamp")
        turns.append(
            LogTurn(
                speaker=td["speaker"],
                text=td["text"],
                t_h=None if td.get("t_h") is None else float(td["t_h"]),
                timestamp=None if ts is None else datetime.fromisoformat(ts),
                turn_index=None if td.get("turn_index") is None else int(td["turn_index"]),
                conversation_id=td.get("conversation_id"),
            )
        )
    return LogRecord(
        conversation_id=doc["conversation_id"],
        turns=tuple(turns),
        opened_t_h=None if doc.get("opened_t_h") is None else float(doc["opened_t_h"]),
        closed_t_h=None if doc.get("closed_t_h") is None else float(doc["closed_t_h"]),
    )
