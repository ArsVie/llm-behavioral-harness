"""Schedule persistence tests (wave-1 A1): store round-trip + ProactiveSchedule bridge."""

from engine.types import PersonaParams, TimingParams
from harness.scheduler import (
    REASON_CHECK_IN,
    REASON_SCHEDULE,
    REASON_VALIDITY_H,
    VALID_REASONS,
    ProactiveSchedule,
)
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
SEED = 4242


def _events():
    return [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 26.5, "day": 1, "reason": REASON_SCHEDULE},
        {"t_h": 50.0, "day": 2, "reason": REASON_CHECK_IN},
    ]


def test_schedule_event_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.save_schedule_events(SEED, _events())
    pending = store.pending_schedule_events(SEED)
    assert [e["t_h"] for e in pending] == [10.0, 26.5, 50.0]  # ascending by t_h
    for e in pending:
        assert set(e) == {"id", "seed", "t_h", "day", "reason", "status", "fired_t_h"}
        assert e["status"] == "pending"
        assert e["fired_t_h"] is None
    assert store.last_proactive_t_h(SEED) is None
    store.close()


def test_mark_fired_and_expired(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.save_schedule_events(SEED, _events())
    store.mark_schedule_fired(SEED, 10.0, fired_t_h=10.4)
    store.mark_schedule_expired(SEED, 26.5)
    pending = store.pending_schedule_events(SEED)
    assert [e["t_h"] for e in pending] == [50.0]
    assert store.last_proactive_t_h(SEED) == 10.4
    # last_proactive_t_h only looks at fired rows, and takes the max
    store.mark_schedule_fired(SEED, 50.0, fired_t_h=50.2)
    assert store.last_proactive_t_h(SEED) == 50.2
    store.close()


def test_replan_is_idempotent_and_never_resurrects(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.save_schedule_events(SEED, _events())
    store.mark_schedule_fired(SEED, 10.0, fired_t_h=10.4)
    store.mark_schedule_expired(SEED, 26.5)
    # re-planning the same horizon: INSERT OR IGNORE must keep statuses intact
    store.save_schedule_events(SEED, _events())
    rows = store.conn.execute(
        "SELECT t_h, status FROM schedule_events WHERE seed = ? ORDER BY t_h",
        (SEED,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (10.0, "fired"),
        (26.5, "expired"),
        (50.0, "pending"),
    ]
    assert store.last_proactive_t_h(SEED) == 10.4
    store.close()


def test_seeds_are_isolated(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.save_schedule_events(SEED, _events())
    store.save_schedule_events(
        SEED + 1, [{"t_h": 7.0, "day": 0, "reason": REASON_SCHEDULE}]
    )
    assert len(store.pending_schedule_events(SEED)) == 3
    assert len(store.pending_schedule_events(SEED + 1)) == 1
    store.close()


def test_taxonomy_extended():
    assert VALID_REASONS == (
        "schedule", "callback", "event", "shared_interest", "check_in",
    )
    assert set(REASON_VALIDITY_H) == set(VALID_REASONS)
    assert REASON_VALIDITY_H[REASON_SCHEDULE] == 3.0
    assert REASON_VALIDITY_H[REASON_CHECK_IN] == 12.0


def test_plan_and_persist_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    schedule = ProactiveSchedule.plan_and_persist(5, SEED, PERSONA, TIMING, store)
    rows = store.conn.execute(
        "SELECT t_h, day, reason FROM schedule_events WHERE seed = ? ORDER BY t_h",
        (SEED,),
    ).fetchall()
    assert [r["t_h"] for r in rows] == [float(h) for h in schedule.event_hours]
    for r in rows:
        assert r["day"] == int(r["t_h"] // 24.0)
        assert r["reason"] == REASON_SCHEDULE
    assert schedule._fired == set()  # freshly planned: nothing fired yet
    assert schedule.next_pending(0.0) is not None
    store.close()


def test_replan_after_firing_does_not_refire(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    schedule = ProactiveSchedule.plan_and_persist(5, SEED, PERSONA, TIMING, store)
    first = schedule.next_pending(0.0)
    assert first is not None
    schedule.mark_fired_persisted(first, fired_t_h=first + 0.1, seed=SEED, store=store)
    # re-plan the same horizon: the fired row must stay fired in the new schedule
    schedule2 = ProactiveSchedule.plan_and_persist(5, SEED, PERSONA, TIMING, store)
    assert first in schedule2._fired
    assert schedule2.next_pending(first) != first
    # and the store row itself is untouched (still 'fired')
    row = store.conn.execute(
        "SELECT status FROM schedule_events WHERE seed = ? AND t_h = ?",
        (SEED, first),
    ).fetchone()
    assert row["status"] == "fired"
    store.close()


def test_restore_resumes_without_replanning(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    schedule = ProactiveSchedule.plan_and_persist(5, SEED, PERSONA, TIMING, store)
    first = schedule.next_pending(0.0)
    assert first is not None
    schedule.mark_fired_persisted(first, fired_t_h=first + 0.1, seed=SEED, store=store)
    restored = ProactiveSchedule.restore(SEED, store)
    assert sorted(restored.event_hours) == sorted(schedule.event_hours)
    assert first in restored._fired
    assert restored.next_pending(first) == schedule.next_pending(first)
    store.close()
