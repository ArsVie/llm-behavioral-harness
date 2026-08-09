"""Unit tests for harness/concurrency.py (Iteration 2, A6) — shutdown
semantics of the runtime's concurrency abstraction.

Covers the plan §5-A6 design requirements:

- sleeper injectable (protocol/ABC; tests never wait real seconds);
- executor lifecycle explicit: created by a named owner, shutdown explicit,
  double-shutdown safe;
- SQLite worker/thread ownership explicit (documented contract + helper);
- closing a runtime closes OWNED resources; injected resources are NOT
  accidentally closed twice (ownership-flag pattern);
- deterministic: no real-clock reads, no multi-second sleeps (worker
  threads are joined via ``shutdown(wait=True)``, never polled).

Async code follows the repo pattern (pitfall 12): sync ``def test_``
functions driving coroutines via ``asyncio.run(...)``.
"""

import asyncio
import sqlite3
import threading

import harness.concurrency as conc
from harness.concurrency import (
    AsyncSleeper,
    CloseGuard,
    ExecutorOwner,
    RecordingSleeper,
    ResourceRegistry,
    default_sleeper,
    ensure_thread_safe_connection,
    shutdown_executor,
)

MODULE_SOURCE = open(conc.__file__, encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# Sleeper — injectable wait primitive
# --------------------------------------------------------------------------- #


def test_recording_sleeper_records_without_waiting():
    """Recorded durations, zero real waits (sub-ms even on a loaded box)."""
    import time

    sleeper = RecordingSleeper()

    async def drive():
        await sleeper(0.5)
        await sleeper(1.25)

    start = time.monotonic()
    asyncio.run(drive())
    elapsed = time.monotonic() - start

    assert sleeper.delays == [0.5, 1.25]
    assert elapsed < 0.05


def test_async_sleeper_delegates_to_asyncio_sleep(monkeypatch):
    """The default sleeper really is asyncio.sleep (delegation, not a copy)."""
    recorded: list[float] = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(AsyncSleeper()(2.5))
    assert recorded == [2.5]


def test_default_sleeper_factory_returns_async_sleeper():
    assert isinstance(default_sleeper(), AsyncSleeper)
    assert isinstance(default_sleeper(), conc.Sleeper)


def test_asyncio_sleep_satisfies_sleeper_protocol():
    """A3 compat: runtime.py today passes `asyncio.sleep` directly — the
    protocol must accept it unchanged."""
    assert isinstance(asyncio.sleep, conc.Sleeper)
    assert isinstance(RecordingSleeper(), conc.Sleeper)
    assert isinstance(AsyncSleeper(), conc.Sleeper)


# --------------------------------------------------------------------------- #
# ExecutorOwner — explicit lifecycle, double-shutdown safe
# --------------------------------------------------------------------------- #


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
    owner.shutdown()  # never started: no-op
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


# --------------------------------------------------------------------------- #
# Resource ownership — owned vs injected, close exactly once
# --------------------------------------------------------------------------- #


class _CountingCloser:
    def __init__(self):
        self.closes = 0

    def close(self):
        self.closes += 1


class _ExplodingCloser:
    def __init__(self):
        self.closes = 0

    def close(self):
        self.closes += 1
        raise RuntimeError("boom")


def test_close_guard_closes_exactly_once():
    resource = _CountingCloser()
    guard = CloseGuard(resource)
    guard.close()
    guard.close()
    guard.close()
    assert resource.closes == 1
    assert guard.closed


def test_close_guard_context_manager():
    resource = _CountingCloser()
    with CloseGuard(resource) as guard:
        assert not guard.closed
    assert resource.closes == 1
    assert guard.closed
    guard.close()  # still idempotent after the context exited
    assert resource.closes == 1


def test_registry_closes_owned_resources_exactly_once():
    registry = ResourceRegistry("rt")
    owned = _CountingCloser()
    registry.register(owned, owned=True)
    registry.close()
    registry.close()  # idempotent
    assert owned.closes == 1
    assert registry.closed


def test_registry_never_closes_injected_resources():
    registry = ResourceRegistry("rt")
    injected = _CountingCloser()
    registry.register(injected, owned=False)
    registry.close()
    assert injected.closes == 0  # injected: creator owns it, no double close
    assert registry.injected_count == 1
    assert registry.owned_count == 0


def test_registry_mixes_owned_and_injected():
    registry = ResourceRegistry("rt")
    owned_a = _CountingCloser()
    owned_b = _CountingCloser()
    injected = _CountingCloser()
    registry.register(owned_a, owned=True)
    registry.register(injected, owned=False)
    registry.register(owned_b, owned=True)
    registry.close()
    assert owned_a.closes == 1
    assert owned_b.closes == 1
    assert injected.closes == 0


def test_registry_register_after_close_raises():
    registry = ResourceRegistry("rt")
    registry.close()
    try:
        registry.register(_CountingCloser(), owned=True)
    except RuntimeError as exc:
        assert "already closed" in str(exc)
    else:
        raise AssertionError("register after close must raise")


def test_registry_close_continues_after_resource_error():
    registry = ResourceRegistry("rt")
    registry.register(_ExplodingCloser(), owned=True)
    tail = _CountingCloser()
    registry.register(tail, owned=True)
    try:
        registry.close()
    except RuntimeError as exc:
        assert "failed to close" in str(exc)
    else:
        raise AssertionError("close must re-raise the resource error")
    assert tail.closes == 1  # remaining owned resources still closed
    assert registry.closed


def test_shutdown_executor_helper_is_idempotent():
    from concurrent.futures import ThreadPoolExecutor

    shutdown_executor(None)  # no-op
    executor = ThreadPoolExecutor(max_workers=1)
    shutdown_executor(executor)
    shutdown_executor(executor)  # double shutdown: no raise
    try:
        executor.submit(lambda: 1)
    except RuntimeError:
        pass  # ThreadPoolExecutor refuses new work after shutdown
    else:
        raise AssertionError("submit after shutdown must raise")


# --------------------------------------------------------------------------- #
# SQLite thread ownership
# --------------------------------------------------------------------------- #


def test_sqlite_thread_ownership_contract_documented():
    """The ownership contract is explicit and covers the four invariants:
    who owns the store, who owns the runtime connection, who may use it,
    and who closes what (no double close of injected stores)."""
    contract = conc.SQLITE_THREAD_OWNERSHIP
    assert isinstance(contract, str) and len(contract) > 200
    for token in ("check_same_thread", "asyncio.Lock", "store", "runtime",
                  "owned=False"):
        assert token in contract, f"ownership contract must mention {token!r}"


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


def test_ensure_thread_safe_connection_sets_pragmas_and_row_factory(tmp_path):
    db = tmp_path / "rt2.db"
    conn = ensure_thread_safe_connection(str(db))
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Module hygiene — importable, clock-free, random-free
# --------------------------------------------------------------------------- #


def test_module_has_no_harness_imports():
    """No harness imports → no circular import for A3's runtime integration."""
    assert "from harness" not in MODULE_SOURCE
    assert "import harness" not in MODULE_SOURCE


def test_module_uses_no_clock_or_random():
    """Determinism guard (repo pattern, pitfall 24): the abstraction never
    reads the wall clock and never draws randomness; asyncio.sleep is the
    only wait primitive (and it is injectable via Sleeper)."""
    for forbidden in ("import random", "import time", "time.monotonic",
                      "time.time", "time.sleep", "time.perf_counter",
                      "datetime", "np.random", "default_rng("):
        assert forbidden not in MODULE_SOURCE, (
            f"harness/concurrency.py must not use {forbidden!r}"
        )
