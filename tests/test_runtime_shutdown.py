"""Unit tests for harness/concurrency.py (Iteration 2, A6) -- shutdown semantics.

Covers the injectable sleeper, explicit executor lifecycle (double-shutdown
safe) and SQLite worker/thread ownership.
"""

import asyncio
import sqlite3
import threading

from harness.concurrency import (
    AsyncSleeper,
    ExecutorOwner,
    ensure_thread_safe_connection,
)


# --- Sleeper — injectable wait primitive ---


def test_async_sleeper_delegates_to_asyncio_sleep(monkeypatch):
    """The default sleeper really is asyncio.sleep (delegation, not a copy)."""
    recorded: list[float] = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(AsyncSleeper()(2.5))
    assert recorded == [2.5]


# --- ExecutorOwner — explicit lifecycle, double-shutdown safe ---


def _llh_threads(name: str) -> list[str]:
    return sorted(t.name for t in threading.enumerate() if t.name.startswith(f"llh-{name}"))


def test_owner_lifecycle_start_submit_shutdown():
    owner = ExecutorOwner("life")
    assert not owner.is_running
    owner.start()
    assert owner.is_running
    future = owner.submit(lambda: 6 * 7)
    assert future.result(timeout=5) == 42
    owner.shutdown()
    assert not owner.is_running
    assert _llh_threads("life") == []  # worker threads joined, none linger


def test_double_shutdown_is_safe_and_shutdown_before_start_is_noop():
    owner = ExecutorOwner("twice")
    owner.shutdown()  # not started: no-op
    owner.start()
    owner.shutdown()
    owner.shutdown()  # double shutdown: no-op, no raise
    owner.shutdown(wait=True)
    assert not owner.is_running


def test_submit_after_shutdown_raises():
    owner = ExecutorOwner("dead")
    owner.start()
    owner.shutdown()
    try:
        owner.submit(lambda: 1)
    except RuntimeError as exc:
        assert "shut down" in str(exc)
    else:
        raise AssertionError("submit after shutdown must raise")


def test_run_in_thread_executes_on_a_worker_thread():
    """run_in_thread is the asyncio.to_thread replacement: work runs on a
    DIFFERENT thread and the result comes back through the owned executor."""
    caller_tid = threading.get_ident()
    seen: dict = {}

    def work():
        seen["tid"] = threading.get_ident()
        seen["name"] = threading.current_thread().name
        return 21 * 2

    owner = ExecutorOwner("worker")
    owner.start()
    try:
        result = asyncio.run(owner.run_in_thread(work))
    finally:
        owner.shutdown()
    assert result == 42
    assert seen["tid"] != caller_tid
    assert seen["name"].startswith("llh-worker")


def test_run_in_thread_before_start_raises():
    owner = ExecutorOwner("never")

    async def drive():
        await owner.run_in_thread(lambda: 1)

    try:
        asyncio.run(drive())
    except RuntimeError as exc:
        assert "not running" in str(exc)
    else:
        raise AssertionError("run_in_thread before start must raise")


def test_run_in_thread_after_shutdown_raises():
    owner = ExecutorOwner("stopped")
    owner.start()
    owner.shutdown()

    async def drive():
        await owner.run_in_thread(lambda: 1)

    try:
        asyncio.run(drive())
    except RuntimeError as exc:
        assert "not running" in str(exc)
    else:
        raise AssertionError("run_in_thread after shutdown must raise")


def test_owner_context_manager_shuts_down_on_exit():
    with ExecutorOwner("ctx") as owner:
        assert owner.is_running
        assert owner.submit(lambda: 1).result(timeout=5) == 1
    assert not owner.is_running
    assert _llh_threads("ctx") == []
    try:
        owner.submit(lambda: 1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit after context exit must raise")


def test_owner_refuses_restart_after_shutdown():
    owner = ExecutorOwner("once")
    owner.start()
    owner.shutdown()
    try:
        owner.start()
    except RuntimeError as exc:
        assert "already shut down" in str(exc)
    else:
        raise AssertionError("restart after shutdown must raise")


# --- SQLite thread ownership ---


def test_ensure_thread_safe_connection_usable_from_worker_thread(tmp_path):
    """A connection opened via the helper is usable from a worker thread —
    the exact failure mode (sqlite3.ProgrammingError) the runtime hit."""
    db = tmp_path / "rt.db"
    creator_conn = sqlite3.connect(str(db))
    creator_conn.execute("CREATE TABLE t (v INTEGER)")
    creator_conn.execute("INSERT INTO t VALUES (7)")
    creator_conn.commit()

    worker_conn = ensure_thread_safe_connection(str(db))
    assert worker_conn.execute("SELECT v FROM t").fetchone()[0] == 7

    owner = ExecutorOwner("sqlite")
    owner.start()
    try:
        read = asyncio.run(owner.run_in_thread(
            lambda: worker_conn.execute("SELECT v FROM t").fetchone()[0]
        ))
    finally:
        owner.shutdown()
    assert read == 7
    creator_conn.close()
    worker_conn.close()
