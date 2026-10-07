"""An in-memory steer backend for the steering tests."""

from __future__ import annotations


class InMemorySteerBackend:
    """In-memory ``SteerBackend``.

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

    def release_steer(self, steer_id: int) -> None:
        if steer_id not in self.storage:
            raise KeyError(f"unknown steer id: {steer_id}")
        row = self.storage[steer_id]
        row["status"] = "pending"
        row["delivered_t_h"] = None
        row["boundary"] = None
        row["seen_turn_id"] = None

    def abandon_steer(self, steer_id: int) -> None:
        if steer_id not in self.storage:
            raise KeyError(f"unknown steer id: {steer_id}")
        self.storage[steer_id]["status"] = "abandoned"
