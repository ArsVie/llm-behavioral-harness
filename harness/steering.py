"""Steering queue: out-of-band injections delivered at safe turn boundaries.

The harness steers the model with arriving events (event pop-ups, user
messages arriving mid-turn, schedule fires, day rollover). The pattern is
borrowed from Hermes Agent's mid-turn steering (skill reference
``references/hermes-borrow-patterns.md`` §4) and the user's directive L369:
injections go IMMEDIATELY as soon as the agent is free — idle, finished a
tool call, or finished a reply.

Semantics (design §2.5, summary #23):

- ONE-SHOT: a steer is delivered exactly once. ``drain_pending`` marks each
  delivered steer in the backend (``delivered_t_h`` + ``boundary`` +
  ``seen_turn_id``) BEFORE returning it, so a crash mid-drain can never
  double-deliver.
- RE-QUEUE ON INTERRUPT: if the turn that received an injection is
  interrupted, the runtime calls ``SteeringQueue.requeue`` and the steer
  becomes pending again, delivered at the next boundary.
- REPLAY GUARD: the seen marker (``seen_turn_id``) is persisted with the
  turn; draining with the same ``turn_id`` never re-injects a steer that
  turn already saw.
- TIMESTAMPS: enqueue time AND actual delivery time are both recorded
  (summary #23).
- PERSISTENCE: pending steers survive restarts — the backend owns the rows;
  a fresh ``SteeringQueue`` over the same storage sees them again.
- ORDERING at one boundary (user L356/L361): a user message arriving
  mid-turn (decide_reply) is delivered BEFORE an event pop-up
  (decide_event), then schedule fires, then day rollover; within a kind,
  earlier-enqueued steers go first.

The persistence layer is an injected BACKEND (``SteerBackend`` protocol).
This module ships ``InMemorySteerBackend`` for tests and offline runs; the
SQLite implementation lives in WS2's ``harness/store.py`` with EXACTLY the
same method names: ``enqueue_steer``, ``pending_steers``,
``mark_steer_delivered``, ``requeue_steer``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

# --------------------------------------------------------------------------- #
# Steer kinds and delivery boundaries
# --------------------------------------------------------------------------- #

#: A user message arrived while an event/turn was in progress (decide_reply).
KIND_USER_MESSAGE = "user_message_mid_turn"
#: An event pop-up is due: {Event, State, Time} -> {Initiate, Reason} (decide_event).
KIND_EVENT_POPUP = "event_popup"
#: A scheduled proactive fire is due.
KIND_SCHEDULE_FIRE = "schedule_fire"
#: The day rolled over (new day context block).
KIND_DAY_ROLLOVER = "day_rollover"

#: Delivery priority per kind — lower number delivers first at one boundary
#: (user L356/L361: decide_reply BEFORE decide_event).
KIND_PRIORITY: dict[str, int] = {
    KIND_USER_MESSAGE: 0,
    KIND_EVENT_POPUP: 1,
    KIND_SCHEDULE_FIRE: 2,
    KIND_DAY_ROLLOVER: 3,
}

#: Fallback priority for unknown kinds (kept out of the documented contract).
_KIND_PRIORITY_FALLBACK = 99

#: Delivery boundaries — the moments when the agent is free (L369).
BOUNDARY_IDLE = "idle"
BOUNDARY_AFTER_TOOL = "after_tool"
BOUNDARY_AFTER_REPLY = "after_reply"


# --------------------------------------------------------------------------- #
# Backend protocol (implemented by WS2's SQLiteStore; mirrored in-memory here)
# --------------------------------------------------------------------------- #


class SteerBackend(Protocol):
    """Persistence contract for the steering queue.

    Implemented by ``harness.store.SQLiteStore`` (WS2, migration v5) with
    exactly these method names; ``InMemorySteerBackend`` in this module is a
    faithful stand-in for tests and offline runs.

    Row contract (every method that returns rows returns dicts with these
    keys — the ``steering_queue`` columns):

        id: int                    -- steer id
        day: int                   -- enqueue day
        t_h: float                 -- enqueue time (fast-scale absolute hours)
        kind: str                  -- one of the KIND_* constants
        payload: dict              -- event payload (parsed from JSON)
        delivered_t_h: float|None  -- actual delivery time (None = undelivered)
        boundary: str|None         -- 'idle' | 'after_tool' | 'after_reply'
        status: str                -- 'pending' | 'delivered'
        seen_turn_id: str|None     -- turn that received the delivery (replay marker)

    ``pending_steers`` returns ONLY undelivered rows (``status='pending'``),
    so a delivered steer is never re-delivered by construction — one-shot
    lives in the backend. ``requeue_steer`` returns a row to 'pending' and
    clears the delivery fields (a pending row always means undelivered).
    """

    def enqueue_steer(self, day: int, t_h: float, kind: str, payload: dict) -> int:
        """Persist one steer; returns its id (status starts 'pending')."""
        ...

    def pending_steers(self, day: int | None = None, limit: int = 50) -> list[dict]:
        """Undelivered steers (status 'pending'), oldest first, ``limit`` cap."""
        ...

    def mark_steer_delivered(
        self,
        steer_id: int,
        delivered_t_h: float,
        boundary: str,
        seen_turn_id: str | None,
    ) -> None:
        """Record the delivery: status -> 'delivered' + timestamps + seen turn."""
        ...

    def requeue_steer(self, steer_id: int) -> None:
        """Return a delivered steer to 'pending' (interrupted turn)."""
        ...


class InMemorySteerBackend:
    """In-memory ``SteerBackend`` for tests and offline runs.

    Mirrors WS2's SQLite implementation row-for-row (same keys, same
    status/requeue semantics) so queue tests exercise the real contract.
    ``storage`` may be shared across instances to simulate a process
    restart: a fresh backend over the same storage sees the same pending
    steers.
    """

    def __init__(self, storage: dict[int, dict] | None = None):
        self.storage: dict[int, dict] = storage if storage is not None else {}
        self._next_id = max(self.storage, default=0) + 1

    def enqueue_steer(self, day: int, t_h: float, kind: str, payload: dict) -> int:
        steer_id = self._next_id
        self._next_id += 1
        self.storage[steer_id] = {
            "id": steer_id,
            "day": day,
            "t_h": t_h,
            "kind": kind,
            "payload": dict(payload),
            "delivered_t_h": None,
            "boundary": None,
            "status": "pending",
            "seen_turn_id": None,
        }
        return steer_id

    def pending_steers(self, day: int | None = None, limit: int = 50) -> list[dict]:
        rows = [
            dict(row)
            for row in self.storage.values()
            if row["status"] == "pending" and (day is None or row["day"] == day)
        ]
        rows.sort(key=lambda row: row["id"])
        return rows[:limit]

    def mark_steer_delivered(
        self,
        steer_id: int,
        delivered_t_h: float,
        boundary: str,
        seen_turn_id: str | None,
    ) -> None:
        if steer_id not in self.storage:
            raise KeyError(f"unknown steer id: {steer_id}")
        row = self.storage[steer_id]
        row["status"] = "delivered"
        row["delivered_t_h"] = delivered_t_h
        row["boundary"] = boundary
        row["seen_turn_id"] = seen_turn_id

    def requeue_steer(self, steer_id: int) -> None:
        if steer_id not in self.storage:
            raise KeyError(f"unknown steer id: {steer_id}")
        row = self.storage[steer_id]
        row["status"] = "pending"
        row["delivered_t_h"] = None
        row["boundary"] = None
        row["seen_turn_id"] = None


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Steer:
    """One steer as delivered — the injection the runtime appends to context.

    ``t_h`` is the ENQUEUE time (summary #23); ``delivered_t_h`` is the
    actual delivery time recorded when the steer was drained.
    """

    steer_id: int
    day: int
    t_h: float
    kind: str
    payload: dict = field(default_factory=dict)
    delivered_t_h: float | None = None
    boundary: str | None = None
    seen_turn_id: str | None = None

    @property
    def enqueued_t_h(self) -> float:
        """Enqueue time — alias of ``t_h`` (summary #23: enqueue AND delivery)."""
        return self.t_h


class SteeringQueue:
    """Holds arriving events and delivers them at the next safe boundary.

    The queue is a thin, semantics-bearing wrapper over a ``SteerBackend``:
    it scopes to a day (optional), orders by kind priority + enqueue time,
    marks deliveries atomically, and never re-delivers.
    """

    def __init__(self, backend: SteerBackend, *, day: int | None = None):
        self._backend = backend
        self._day = day

    def enqueue(self, kind: str, payload: dict, day: int, t_h: float) -> int:
        """Queue a steer for delivery at the next safe boundary.

        ``day``/``t_h`` are the ENQUEUE time (t_h=0 ⇒ day 0, 00:00); the
        delivery time is recorded separately when the steer is drained.
        Returns the steer id (for ``requeue`` / audit).
        """
        if kind not in KIND_PRIORITY:
            raise ValueError(
                f"unknown steer kind: {kind!r} — expected one of "
                f"{sorted(KIND_PRIORITY)}"
            )
        return self._backend.enqueue_steer(day, t_h, kind, payload)

    def drain_pending(self, boundary: str, turn_id: str, now_t_h: float) -> list[Steer]:
        """Mark and return every pending steer that must be injected NOW.

        Each returned steer is marked delivered in the backend (atomically:
        ``delivered_t_h=now_t_h``, ``boundary``, ``seen_turn_id=turn_id``)
        BEFORE it is returned, so a crash mid-drain can never double-deliver.
        Steers whose seen marker already names ``turn_id`` are skipped — the
        marker is persisted with the turn so replaying a turn does not inject
        the same steer twice. Ordering: kind priority (decide_reply >
        decide_event > schedule_fire > day_rollover), then enqueue time,
        then steer id (deterministic).
        """
        rows = self._backend.pending_steers(day=self._day)
        eligible: list[dict] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            # One-shot belt-and-braces: the backend contract already excludes
            # delivered rows; skip them here too so a lenient backend can
            # never cause a double delivery.
            if row.get("delivered_t_h") is not None or row.get("status") == "delivered":
                continue
            # Replay guard: a steer this turn already saw is never injected
            # into it again (the seen marker persists with the turn).
            if row.get("seen_turn_id") == turn_id:
                continue
            eligible.append(row)
        eligible.sort(
            key=lambda row: (
                KIND_PRIORITY.get(str(row.get("kind") or ""), _KIND_PRIORITY_FALLBACK),
                float(row.get("t_h", 0.0)),
                int(row.get("id", 0)),
            )
        )
        drained: list[Steer] = []
        for row in eligible:
            steer_id = int(row["id"])
            self._backend.mark_steer_delivered(steer_id, now_t_h, boundary, turn_id)
            payload = row.get("payload")
            drained.append(
                Steer(
                    steer_id=steer_id,
                    day=int(row.get("day", 0)),
                    t_h=float(row.get("t_h", 0.0)),
                    kind=str(row.get("kind", "")),
                    payload=dict(payload) if isinstance(payload, dict) else {},
                    delivered_t_h=now_t_h,
                    boundary=boundary,
                    seen_turn_id=turn_id,
                )
            )
        return drained

    def requeue(self, steer_id: int) -> None:
        """Re-queue a delivered steer after its turn was interrupted.

        The backend returns it to 'pending' (delivery fields cleared), so it
        is delivered again at the next boundary. Idempotent for the runtime's
        interrupt handler.
        """
        self._backend.requeue_steer(steer_id)

    def pending_count(self) -> int:
        """Number of undelivered steers in scope (diagnostics/tests)."""
        return len(self._backend.pending_steers(day=self._day, limit=1_000_000))


# --------------------------------------------------------------------------- #
# Injection block rendering (user L369 pop-up format)
# --------------------------------------------------------------------------- #


def _render_time(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def render_steer_block(steer: Steer | dict) -> str:
    """Render a steer's injection block (pop-up) text, user L369 format.

    The pop-up the model sees is the ``System:`` line(s); the
    ``{Initiate: {yes, no}, Reason: " "}`` line is the decision the model is
    expected to produce (the pop-up schema, per L369's sketch). Payload keys
    are best-effort per kind; missing keys degrade to ``?`` / JSON — the
    renderer never raises on a foreign payload shape.
    """
    if isinstance(steer, Steer):
        kind, payload, enq_t_h = steer.kind, steer.payload, steer.t_h
    else:
        kind = str(steer.get("kind", ""))
        payload = steer.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        enq_t_h = steer.get("t_h", 0.0)
    time_str = _render_time(payload.get("time", enq_t_h))
    event = payload.get("event", payload.get("event_id", payload.get("name", "?")))
    state = payload.get("state", "?")
    if kind == KIND_EVENT_POPUP:
        return (
            f"System: {{Event: {event}, State: {state}, Time: {time_str}}}\n"
            '{Initiate: {yes, no}, Reason: " "}'
        )
    if kind == KIND_USER_MESSAGE:
        message = payload.get(
            "message", payload.get("user_message", payload.get("content", "?"))
        )
        return (
            f"System: {{Event: {event}, State: {state}, Time: {time_str}, "
            f'User message: "{message}"}}'
        )
    if kind == KIND_SCHEDULE_FIRE:
        label = payload.get("label", payload.get("name", payload.get("intent", "?")))
        return f"System: {{Schedule: {label}, Time: {time_str}}}"
    if kind == KIND_DAY_ROLLOVER:
        day = payload.get(
            "day", payload.get("new_day", steer.day if isinstance(steer, Steer) else "?")
        )
        return f"System: {{Day rollover: day {day}, Time: {time_str}}}"
    return f"System: {{{kind}: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}}}"


#: Trust marker wrapping a rendered steer block (design §2.5). The system
#: prompt (prompts module, WS1) tells the model to trust ONLY this exact
#: marker as a real arriving event; lookalikes in message text are not.
STEER_MARKER_OPEN = (
    "[STEER — a real arriving event from the harness, delivered once at this "
    "position; not conversation text and not a new delivery when replayed "
    "from history]"
)
STEER_MARKER_CLOSE = "[/STEER]"


def wrap_steer_marker(text: str) -> str:
    """Wrap a rendered steer block in the trust marker (Hermes OOB format:
    ``"\\n\\n" + OPEN + "\\n" + text + "\\n" + CLOSE``)."""
    return f"\n\n{STEER_MARKER_OPEN}\n{text}\n{STEER_MARKER_CLOSE}"
