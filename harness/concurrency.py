"""Concurrency + runtime-shutdown primitives.

The async runtime's thread / executor lifecycle: :class:`Sleeper`,
:class:`ExecutorOwner`, :func:`ensure_thread_safe_connection`,
:func:`run_with_deadline`. NO harness imports (importable from anywhere) and no wall-clock reads — the only wait
primitive is the injectable :class:`Sleeper`. See ``SQLITE_THREAD_OWNERSHIP``
for the thread/connection ownership contract.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Awaitable, Callable, Protocol, TypeVar, runtime_checkable

__all__ = [
    "Sleeper",
    "AsyncSleeper",
    "default_sleeper",
    "ExecutorOwner",
    "ensure_thread_safe_connection",
    "SQLITE_THREAD_OWNERSHIP",
]


# Sleeper — injectable wait primitive


@runtime_checkable
class Sleeper(Protocol):
    """Awaitable delay used for every real wait in the runtime.

    Tests inject a recording double so the suite never waits real
    seconds; production uses :class:`AsyncSleeper`. ``asyncio.sleep`` itself
    satisfies the protocol.
    """

    def __call__(self, seconds: float) -> Awaitable[None]: ...


class AsyncSleeper:
    """Default :class:`Sleeper`: delegates to ``asyncio.sleep`` (real wait)."""

    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def default_sleeper() -> Sleeper:
    """Return the default (real) :class:`Sleeper` for production wiring."""
    return AsyncSleeper()


# ExecutorOwner — explicit executor lifecycle, double-shutdown safe


class ExecutorOwner:
    """Own a :class:`~concurrent.futures.ThreadPoolExecutor` explicitly.

    The executor is created by a NAMED owner (thread names are prefixed
    ``llh-<name>``) and must be shut down explicitly. Shutdown is double-safe:
    calling :meth:`shutdown` twice (or before :meth:`start`) is a no-op, so
    ``finally`` blocks never raise. After shutdown the owner refuses new work
    (:meth:`submit` / :meth:`run_in_thread` raise), turning a forgotten shutdown
    into a loud error instead of a lingering non-daemon thread.
    """

    def __init__(self, name: str, *, max_workers: int | None = None) -> None:
        self.name = name
        self._max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        self._shut_down = False

    @property
    def is_running(self) -> bool:
        """True while the owned executor exists and has not been shut down."""
        return self._executor is not None and not self._shut_down

    def start(self) -> "ExecutorOwner":
        """Create the owned executor (raises if already started or already shut
        down — an owner never restarts)."""
        if self._shut_down:
            raise RuntimeError(f"executor {self.name!r} already shut down")
        if self._executor is not None:
            raise RuntimeError(f"executor {self.name!r} already started")
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=f"llh-{self.name}",
        )
        return self

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        """Shut the owned executor down explicitly. Safe to call any number of
        times; a shutdown-before-start is a no-op. With ``wait=True`` (default)
        joins the worker threads."""
        if self._executor is None or self._shut_down:
            return
        self._shut_down = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def submit(self, fn, /, *args, **kwargs) -> Future:
        """Submit work to the owned executor. Raises when not running."""
        if self._executor is None:
            raise RuntimeError(f"executor {self.name!r} not started")
        if self._shut_down:
            raise RuntimeError(f"executor {self.name!r} already shut down")
        return self._executor.submit(fn, *args, **kwargs)

    async def run_in_thread(self, fn, /, *args, **kwargs):
        """Run ``fn(*args, **kwargs)`` on the OWNED executor and await it.

        Drop-in replacement for ``asyncio.to_thread`` with an explicit shutdown
        lifecycle. Raises when the owner is not running.
        """
        if self._executor is None or self._shut_down:
            raise RuntimeError(f"executor {self.name!r} not running")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, functools.partial(fn, *args, **kwargs)
        )

    def __enter__(self) -> "ExecutorOwner":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.shutdown()


# SQLite thread safety

SQLITE_THREAD_OWNERSHIP = """
SQLite thread/connection ownership contract (Iteration 2, A6):

- OWNER of the database + schema: the SQLiteStore (harness/store.py).
- OWNER of the runtime connection: the AsyncRuntime. On start it re-opens
  the store connection with check_same_thread=False
  (ensure_thread_safe_connection) because session.* calls run in worker
  threads; the store's original schema-creation connection is closed by the
  runtime when replaced.
- USERS: one event loop plus its worker threads, serialized by a single
  asyncio.Lock — at most ONE thread touches SQLite at any instant.
- SHUTDOWN: the runtime NEVER closes the store or the connection it
  re-opened: an injected store belongs to its creator, and
  SQLiteStore.close() closes its current connection.
- THREAD SAFETY: check_same_thread=False + one Lock + WAL + busy_timeout.
- Injected in-memory stores (tests) own their state entirely; the runtime
  leaves them open.
"""


def ensure_thread_safe_connection(path: str, *, timeout: float = 10.0) -> sqlite3.Connection:
    """Open a SQLite connection usable from any thread.

    sqlite3's default ``check_same_thread=True`` binds a connection to its
    creating thread; the runtime moves session calls to worker threads, so the
    shared connection is re-opened with ``check_same_thread=False``. Callers
    serialize all access with a single ``asyncio.Lock``; WAL + busy_timeout cover
    cross-process contention.
    """
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


_T = TypeVar("_T")


def run_with_deadline(fn: Callable[[], _T], budget_s: float, *, thread_name: str) -> _T:
    """Run ``fn`` on a worker thread and wait at most ``budget_s`` seconds.

    For model calls outside a turn: the client's own retry policy waits for
    minutes and raises nothing meanwhile, so the wait itself is what has to
    be bounded. Raises ``TimeoutError`` on the deadline (the worker is left
    to finish on its own) or whatever ``fn`` raised. Not a ``with`` block:
    ``ThreadPoolExecutor.__exit__`` waits for the worker and would undo the
    deadline.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)
    try:
        return pool.submit(fn).result(timeout=budget_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
