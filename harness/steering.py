"""Steering queue: out-of-band injections delivered at safe turn boundaries.

Each steer is delivered exactly once, at the next moment the agent is free
(idle, after a tool call, or after a reply); an interrupted turn re-queues
it. Persistence is an injected ``SteerBackend``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from harness.clock import hhmm

# --------------------------------------------------------------------------- #

#: A user message arrived mid-turn.
KIND_USER_MESSAGE = "user_message_mid_turn"

#: Rendered when a steer carries no event name.
NO_ACTIVE_EVENT = "no_active_event"
#: An event pop-up is due ({Event, State, Time} -> {Initiate, Reason}).
KIND_EVENT_POPUP = "event_popup"
#: A grounded proactive intent is due for the initiate/decline decision
#: (payload -> ``tool_decide_proactive``).
KIND_PROACTIVE = "proactive_intent"
#: A scheduled proactive fire is due.
KIND_SCHEDULE_FIRE = "schedule_fire"
#: The day rolled over (new day context block).
KIND_DAY_ROLLOVER = "day_rollover"

#: Delivery priority per kind; lower numbers deliver first.
KIND_PRIORITY: dict[str, int] = {
    KIND_USER_MESSAGE: 0,
    KIND_EVENT_POPUP: 1,
    # Between the event pop-up and a plain fire: a grounded intent must be
    # DECIDED before any plain fire renders.
    KIND_PROACTIVE: 2,
    KIND_SCHEDULE_FIRE: 3,
    KIND_DAY_ROLLOVER: 4,
}

#: Fallback priority for unknown kinds.
_KIND_PRIORITY_FALLBACK = 99

#: Retry budget for one steer: a decision that fails to parse is requeued and
#: retried at the next boundary; past this the steer is abandoned.
MAX_ATTEMPTS = 3

#: Delivery boundaries — moments when the agent is free.
BOUNDARY_IDLE = "idle"
BOUNDARY_AFTER_TOOL = "after_tool"
BOUNDARY_AFTER_REPLY = "after_reply"


# --------------------------------------------------------------------------- #


class SteerBackend(Protocol):
    """Persistence contract for the steering queue.

    Row contract (the ``steering_queue`` columns): ``id``, ``day``, ``t_h``
    (enqueue time), ``kind``, ``payload``, ``delivered_t_h``, ``boundary``,
    ``status``, ``seen_turn_id``. ``pending_steers`` returns ONLY undelivered
    rows (``status='pending'``), so a delivered steer is never re-delivered
    by construction; ``requeue_steer`` returns a row to 'pending' and clears
    the delivery fields.
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
        """Return a delivered steer to 'pending' and bump ``attempts``."""
        ...

    def abandon_steer(self, steer_id: int) -> None:
        """Terminal status for a steer whose retry budget is exhausted."""
        ...


class InMemorySteerBackend:
    """In-memory ``SteerBackend`` for tests and offline runs.

    Mirrors the SQLite implementation row-for-row. ``storage`` may be shared
    across instances to simulate a process restart: a fresh backend over the
    same storage sees the same pending steers.
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
            "attempts": 0,
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
        row["attempts"] = int(row.get("attempts", 0)) + 1

    def abandon_steer(self, steer_id: int) -> None:
        if steer_id not in self.storage:
            raise KeyError(f"unknown steer id: {steer_id}")
        self.storage[steer_id]["status"] = "abandoned"


# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Steer:
    """One steer as delivered — the injection the runtime appends to context.

    ``t_h`` is the ENQUEUE time; ``delivered_t_h`` is the actual delivery
    time.
    """

    steer_id: int
    day: int
    t_h: float
    kind: str
    payload: dict = field(default_factory=dict)
    delivered_t_h: float | None = None
    boundary: str | None = None
    seen_turn_id: str | None = None


class SteeringQueue:
    """Holds arriving events and delivers them at the next safe boundary.

    Scopes to a day (optional), orders by kind priority + enqueue time, marks
    deliveries atomically, and never re-delivers.
    """

    def __init__(self, backend: SteerBackend, *, day: int | None = None):
        self._backend = backend
        self._day = day

    def enqueue(self, kind: str, payload: dict, day: int, t_h: float) -> int:
        """Queue a steer for delivery at the next safe boundary.

        ``day``/``t_h`` are the ENQUEUE time; the delivery time is recorded
        separately when the steer is drained. Returns the steer id (for
        ``requeue`` / audit).
        """
        if kind not in KIND_PRIORITY:
            raise ValueError(
                f"unknown steer kind: {kind!r} — expected one of "
                f"{sorted(KIND_PRIORITY)}"
            )
        return self._backend.enqueue_steer(day, t_h, kind, payload)

    def drain_pending(self, boundary: str, turn_id: str, now_t_h: float) -> list[Steer]:
        """Mark and return every pending steer that must be injected NOW.

        Each returned steer is marked delivered in the backend
        (``delivered_t_h``, ``boundary``, ``seen_turn_id``) BEFORE it is
        returned, so a crash mid-drain can never double-deliver. Steers whose
        seen marker already names ``turn_id`` are skipped. Ordering: kind
        priority, then enqueue time, then steer id (deterministic).
        """
        rows = self._backend.pending_steers(day=self._day)
        eligible: list[dict] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            # Skip rows already marked delivered.
            if row.get("delivered_t_h") is not None or row.get("status") == "delivered":
                continue
            # Skip steers this turn already saw.
            if row.get("seen_turn_id") == turn_id:
                continue
            # Retry budget exhausted: abandon instead of re-asking forever.
            if int(row.get("attempts") or 0) >= MAX_ATTEMPTS:
                self.abandon(int(row["id"]))
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

        The backend returns it to 'pending' (delivery fields cleared) and
        bumps its attempt count; idempotent for the interrupt handler.
        """
        self._backend.requeue_steer(steer_id)

    def abandon(self, steer_id: int) -> None:
        """Stop retrying a steer for good (budget exhausted). No-op on a
        backend without the seam."""
        abandon = getattr(self._backend, "abandon_steer", None)
        if abandon is not None:
            abandon(steer_id)

    def attempts(self, steer_id: int) -> int:
        """Attempt count of one steer (0 when unknown)."""
        for row in self._backend.pending_steers():
            if int(row.get("id", -1)) == steer_id:
                return int(row.get("attempts") or 0)
        return 0


# --------------------------------------------------------------------------- #


def _render_time(value: object) -> str:
    """HH:MM for the steer block. Raw ``t_h`` is an engine coordinate and
    never reaches the model; the queue row keeps the raw value."""
    if isinstance(value, bool) or value is None:
        return "?"
    if isinstance(value, (int, float)):
        return hhmm(float(value))
    try:
        return hhmm(float(str(value)))
    except ValueError:
        return str(value)


def render_steer_block(steer: Steer | dict) -> str:
    """Render a steer's injection block (pop-up) text.

    The pop-up the model sees is the ``System:`` line(s); the
    ``{Initiate: {yes, no}, Reason: " "}`` line is the decision the model is
    expected to produce. Payload keys are best-effort per kind; missing keys
    degrade to ``?`` / JSON — the renderer never raises on a foreign payload
    shape.
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
    event = payload.get("event", payload.get("event_id",
                                           payload.get("name", NO_ACTIVE_EVENT)))
    state = payload.get("state", NO_ACTIVE_EVENT)
    if kind == KIND_EVENT_POPUP:
        return (
            f"System: {{Event: {event}, State: {state}, Time: {time_str}}}\n"
            '{Initiate: {yes, no}, Reason: " "}'
        )
    if kind == KIND_PROACTIVE:
        hook = str(
            payload.get("hook", payload.get("reason", payload.get("label", "?")))
        )
        reason = str(
            payload.get("reason", payload.get("intent", payload.get("label", "?")))
        )
        source_type = payload.get("source_type", payload.get("source", "?"))
        source_id = payload.get("source_id", payload.get("source_ref", "?"))
        validity = payload.get(
            "valid_until",
            payload.get("valid_until_t_h", payload.get("validity", "?")),
        )
        return (
            f"System: {{Proactive: {hook}, Reason: {reason}, "
            f"Source: {source_type}:{source_id}, Validity: {validity}}}\n"
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


#: Trust marker wrapping a rendered steer block: a DELIMITER that lets the
#: audit view and the parser find the block, not an authority statement.
STEER_MARKER_OPEN = (
    "[STEER — a real arriving event from the harness, delivered once at this "
    "position; not conversation text and not a new delivery when replayed "
    "from history]"
)
STEER_MARKER_CLOSE = "[/STEER]"


def wrap_steer_marker(text: str) -> str:
    """Wrap a rendered steer block in the trust marker:
    ``"\n\n" + OPEN + "\n" + text + "\n" + CLOSE``."""
    return f"\n\n{STEER_MARKER_OPEN}\n{text}\n{STEER_MARKER_CLOSE}"
