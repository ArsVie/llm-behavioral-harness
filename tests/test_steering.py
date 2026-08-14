"""Steering queue tests (WS3): one-shot delivery, ordering, re-queue on
interrupt, enqueue/delivery timestamps, restart persistence, replay guard
and injection block rendering (user L369 format)."""

import pytest

from harness.steering import (
    BOUNDARY_AFTER_REPLY,
    BOUNDARY_AFTER_TOOL,
    BOUNDARY_IDLE,
    KIND_DAY_ROLLOVER,
    KIND_EVENT_POPUP,
    KIND_SCHEDULE_FIRE,
    KIND_USER_MESSAGE,
    InMemorySteerBackend,
    Steer,
    SteeringQueue,
    render_steer_block,
    wrap_steer_marker,
)


def _queue(day: int = 7, storage: dict | None = None) -> SteeringQueue:
    return SteeringQueue(InMemorySteerBackend(storage=storage), day=day)


# -- one-shot delivery ------------------------------------------------------- #


def test_drain_delivers_pending_steers_once():
    q = _queue()
    q.enqueue(KIND_EVENT_POPUP, {"event": "gym"}, day=7, t_h=1.0)
    q.enqueue(KIND_SCHEDULE_FIRE, {"label": "morning"}, day=7, t_h=2.0)

    drained = q.drain_pending(BOUNDARY_IDLE, "t1", now_t_h=3.0)

    assert len(drained) == 2
    # One-shot: nothing is ever re-delivered after the first drain.
    assert q.drain_pending(BOUNDARY_IDLE, "t2", now_t_h=4.0) == []
    assert q.drain_pending(BOUNDARY_IDLE, "t3", now_t_h=5.0) == []
    assert q.pending_count() == 0


def test_drain_empty_queue():
    assert _queue().drain_pending(BOUNDARY_IDLE, "t1", 1.0) == []


# -- ordering at one boundary (decide_reply > decide_event > schedule > rollover) -- #


def test_ordering_kind_priority_then_enqueue_time():
    q = _queue()
    # Enqueued in the OPPOSITE delivery order — kind priority must dominate.
    q.enqueue(KIND_DAY_ROLLOVER, {"day": 8}, day=7, t_h=1.0)
    q.enqueue(KIND_SCHEDULE_FIRE, {"label": "a"}, day=7, t_h=2.0)
    q.enqueue(KIND_EVENT_POPUP, {"event": "e1"}, day=7, t_h=3.0)
    q.enqueue(KIND_USER_MESSAGE, {"message": "hi"}, day=7, t_h=4.0)

    drained = q.drain_pending(BOUNDARY_IDLE, "t1", 5.0)

    assert [d.kind for d in drained] == [
        KIND_USER_MESSAGE,  # decide_reply first (user L356/L361)
        KIND_EVENT_POPUP,   # decide_event second
        KIND_SCHEDULE_FIRE,
        KIND_DAY_ROLLOVER,
    ]


def test_same_kind_ordered_by_enqueue_time():
    q = _queue()
    q.enqueue(KIND_EVENT_POPUP, {"event": "late"}, day=7, t_h=9.0)
    q.enqueue(KIND_EVENT_POPUP, {"event": "early"}, day=7, t_h=7.0)

    drained = q.drain_pending(BOUNDARY_IDLE, "t1", 10.0)

    assert [d.payload["event"] for d in drained] == ["early", "late"]


# -- enqueue + delivery timestamps (summary #23) ----------------------------- #


def test_delivery_records_timestamps_boundary_and_seen_turn():
    q = _queue()
    q.enqueue(KIND_EVENT_POPUP, {"event": "gym"}, day=7, t_h=13.5)

    (drained,) = q.drain_pending(BOUNDARY_AFTER_REPLY, "t9", now_t_h=14.75)

    assert drained.t_h == 13.5  # enqueue time
    assert drained.enqueued_t_h == 13.5  # explicit alias (summary #23)
    assert drained.delivered_t_h == 14.75  # actual delivery time
    assert drained.boundary == BOUNDARY_AFTER_REPLY
    assert drained.seen_turn_id == "t9"
    assert drained.kind == KIND_EVENT_POPUP
    assert drained.payload == {"event": "gym"}
    assert isinstance(drained.steer_id, int)


def test_delivery_persisted_in_backend():
    backend = InMemorySteerBackend()
    q = SteeringQueue(backend, day=7)
    steer_id = q.enqueue(KIND_EVENT_POPUP, {"event": "gym"}, day=7, t_h=13.5)

    q.drain_pending(BOUNDARY_AFTER_TOOL, "t1", 14.0)

    row = backend.storage[steer_id]
    assert row["status"] == "delivered"
    assert row["delivered_t_h"] == 14.0
    assert row["boundary"] == BOUNDARY_AFTER_TOOL
    assert row["seen_turn_id"] == "t1"
    assert backend.pending_steers(day=7) == []


# -- re-queue on interrupt + replay guard ------------------------------------ #


def test_requeue_after_interrupt_delivers_at_next_boundary():
    q = _queue()
    a = q.enqueue(KIND_EVENT_POPUP, {"event": "a"}, day=7, t_h=1.0)
    b = q.enqueue(KIND_EVENT_POPUP, {"event": "b"}, day=7, t_h=2.0)

    drained = q.drain_pending(BOUNDARY_AFTER_TOOL, "turn-1", 3.0)
    assert [d.steer_id for d in drained] == [a, b]

    # turn-1 interrupted: re-queue the steers it received
    q.requeue(a)
    q.requeue(b)

    drained2 = q.drain_pending(BOUNDARY_AFTER_TOOL, "turn-2", 4.0)
    assert [d.steer_id for d in drained2] == [a, b]
    assert all(d.seen_turn_id == "turn-2" for d in drained2)
    assert q.pending_count() == 0


def test_requeue_clears_delivery_fields():
    backend = InMemorySteerBackend()
    q = SteeringQueue(backend, day=7)
    steer_id = q.enqueue(KIND_EVENT_POPUP, {"event": "a"}, day=7, t_h=1.0)
    q.drain_pending(BOUNDARY_IDLE, "turn-1", 2.0)

    q.requeue(steer_id)

    row = backend.storage[steer_id]
    assert row["status"] == "pending"
    assert row["delivered_t_h"] is None
    assert row["boundary"] is None
    assert row["seen_turn_id"] is None  # a pending row always means undelivered


def test_seen_marker_blocks_same_turn_reinjection():
    """Replay guard: if a pending row's seen marker names the current turn,
    the queue must not inject it again (the marker is persisted with the
    turn so replay can detect double-delivery). WS2's SQLite clears the
    marker on requeue; this guards backends that retain it."""
    backend = InMemorySteerBackend()
    q = SteeringQueue(backend, day=7)
    steer_id = q.enqueue(KIND_EVENT_POPUP, {"event": "a"}, day=7, t_h=1.0)
    q.drain_pending(BOUNDARY_IDLE, "turn-1", 2.0)
    q.requeue(steer_id)
    # Simulate a backend that retains the seen marker on requeue.
    backend.storage[steer_id]["seen_turn_id"] = "turn-1"

    # Same turn replayed: no double delivery.
    assert q.drain_pending(BOUNDARY_IDLE, "turn-1", 3.0) == []
    # A different turn gets the re-queued steer.
    drained = q.drain_pending(BOUNDARY_IDLE, "turn-2", 4.0)
    assert [d.steer_id for d in drained] == [steer_id]


# -- restart persistence (backend contract) ---------------------------------- #


def test_restart_with_fresh_backend_same_storage():
    storage: dict = {}
    q1 = _queue(storage=storage)
    q1.enqueue(KIND_SCHEDULE_FIRE, {"label": "delivered-before-restart"}, day=7, t_h=2.0)
    q1.drain_pending(BOUNDARY_IDLE, "old-turn", 3.0)  # delivered before restart
    kept = q1.enqueue(KIND_EVENT_POPUP, {"event": "kept"}, day=7, t_h=1.0)  # still pending

    # "Restart": a brand-new queue over the same storage (fresh backend
    # instance) sees only the undelivered steer.
    q2 = _queue(storage=storage)
    assert q2.pending_count() == 1
    drained = q2.drain_pending(BOUNDARY_IDLE, "new-turn", 4.0)
    assert [d.steer_id for d in drained] == [kept]
    assert drained[0].delivered_t_h == 4.0


def test_queue_scoped_to_day():
    q = _queue(day=7)
    q.enqueue(KIND_EVENT_POPUP, {"event": "today"}, day=7, t_h=1.0)
    q.enqueue(KIND_EVENT_POPUP, {"event": "tomorrow"}, day=8, t_h=1.0)

    drained = q.drain_pending(BOUNDARY_IDLE, "t1", 2.0)

    assert [d.payload["event"] for d in drained] == ["today"]


def test_enqueue_rejects_unknown_kind():
    q = _queue()
    with pytest.raises(ValueError, match="unknown steer kind"):
        q.enqueue("mystery_kind", {}, day=7, t_h=1.0)


# -- injection block rendering (user L369 pop-up format) --------------------- #


def test_render_event_popup_block():
    steer = Steer(
        steer_id=1, day=7, t_h=13.5, kind=KIND_EVENT_POPUP,
        payload={"event": "gym", "state": "start", "time": 14.0},
    )
    assert render_steer_block(steer) == (
        "System: {Event: gym, State: start, Time: 14}\n"
        '{Initiate: {yes, no}, Reason: " "}'
    )


def test_render_other_kinds():
    user = Steer(
        steer_id=2, day=7, t_h=1.0, kind=KIND_USER_MESSAGE,
        payload={"event": "gym", "state": "start", "time": 1.5, "message": "hi"},
    )
    assert 'User message: "hi"' in render_steer_block(user)

    sched = Steer(steer_id=3, day=7, t_h=2.0, kind=KIND_SCHEDULE_FIRE,
                  payload={"label": "morning check-in"})
    assert "Schedule: morning check-in" in render_steer_block(sched)

    roll = Steer(steer_id=4, day=7, t_h=3.0, kind=KIND_DAY_ROLLOVER,
                 payload={"day": 8})
    assert "Day rollover: day 8" in render_steer_block(roll)


def test_render_accepts_dict_and_never_raises_on_foreign_payload():
    block = render_steer_block({"kind": KIND_EVENT_POPUP, "payload": {"weird": True}, "t_h": 5.0})
    assert block.startswith("System: {Event: ?")
    # Unknown kind degrades to a JSON fallback, never an exception.
    assert render_steer_block({"kind": "something_new", "payload": {"x": 1}, "t_h": 5.0}).startswith(
        "System: {something_new:"
    )


def test_render_uses_enqueue_time_when_payload_has_no_time():
    steer = Steer(steer_id=5, day=7, t_h=9.25, kind=KIND_EVENT_POPUP, payload={"event": "x"})
    assert "Time: 9.25" in render_steer_block(steer)


def test_wrap_steer_marker_format():
    wrapped = wrap_steer_marker("System: {Event: gym}")
    assert wrapped.startswith("\n\n[STEER")
    assert "System: {Event: gym}" in wrapped
    assert wrapped.endswith("[/STEER]")
    # The trust marker names the one-shot rule (design §2.5).
    assert "delivered once at this position" in wrapped
