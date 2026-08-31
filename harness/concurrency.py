"""Concurrency + runtime-shutdown primitives (Iteration 2, A6).

Small, importable API for the async runtime's thread / executor / resource
lifecycle. A3 integrates these into ``harness/runtime.py`` at its merge
(plan §5-A6: A6 exposes the abstraction, A3 wires it in). This module has
NO harness imports, so it can be imported from anywhere without circular
imports, and it never reads the wall clock: the only wait primitive is the
injectable :class:`Sleeper`.

API surface
-----------
- :class:`Sleeper` (protocol), :class:`AsyncSleeper` (default),
  :class:`RecordingSleeper` (test double), :func:`default_sleeper`
- :class:`ExecutorOwner` — a named :class:`~concurrent.futures.ThreadPoolExecutor`
  with an explicit, double-shutdown-safe lifecycle
- :class:`CloseGuard` — idempotent close wrapper (double-close safe)
- :class:`ResourceRegistry` — owned vs injected resources; ``close()`` closes
  each OWNED resource exactly once and never closes injected ones
- :func:`shutdown_executor` — idempotent shutdown helper for bare executors
- :func:`ensure_thread_safe_connection` — SQLite connection usable from any
  thread (the runtime's ``check_same_thread=False`` re-open, extracted)
- ``SQLITE_THREAD_OWNERSHIP`` — the documented thread/connection ownership
  contract (invariant for the whole harness)

SQLite thread/connection ownership contract
-------------------------------------------
- **Store owns the database and schema.** ``harness/store.py``
  (``SQLiteStore``) creates the on-disk database, its schema, and its
  original connection.
- **Runtime owns the runtime connection.** When the runtime starts it
  re-opens the store's connection with ``check_same_thread=False`` via
  :func:`ensure_thread_safe_connection` (sqlite3's default binds a
  connection to the thread that created it; the runtime moves ``session.*``
  calls to worker threads). The ORIGINAL schema-creation connection is
  closed by the runtime when replaced.
- **Users: one event loop + its worker threads, serialized by a single
  ``asyncio.Lock``.** At most ONE thread touches SQLite at any instant; WAL +
  busy_timeout cover cross-process contention.
- **Shutdown: the runtime closes the connection it opened and NEVER closes
  the store itself.** An injected store belongs to its creator — register it
  with ``owned=False`` in the runtime's :class:`ResourceRegistry` so it is
  never closed twice. Injected in-memory stores (tests) own their state
  entirely and are left open.

Integration notes for A3 (runtime.py)
-------------------------------------
- Replace ``sleeper: Callable | None = None`` defaulting to ``asyncio.sleep``
  with ``sleeper: Sleeper | None = None`` defaulting to
  :func:`default_sleeper` — ``asyncio.sleep`` itself satisfies the protocol,
  so existing call sites keep working.
- Own ONE :class:`ExecutorOwner` per runtime (e.g. ``self._executor =
  ExecutorOwner("runtime").start()``) and replace ``asyncio.to_thread(fn,
  *args)`` with ``await self._executor.run_in_thread(fn, *args)``. The
  default executor is a process-global that outlives the runtime; an owned
  executor shuts down explicitly with the runtime (double-shutdown safe).
- Shut down in a ``finally``: ``self._executor.shutdown()`` then close the
  runtime's OWNED resources via its :class:`ResourceRegistry`; injected
  channel/store are registered ``owned=False`` and left to their creators.
- Re-open the store connection with :func:`ensure_thread_safe_connection`
  (replaces the inline ``_ensure_thread_safe_store`` workaround) and record
  the connection in the registry as OWNED (the runtime created it).
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Awaitable, Protocol, runtime_checkable

__all__ = [
    "Sleeper",
    "AsyncSleeper",
    "RecordingSleeper",
    "default_sleeper",
    "ExecutorOwner",
    "CloseGuard",
    "ResourceRegistry",
    "shutdown_executor",
    "ensure_thread_safe_connection",
    "SQLITE_THREAD_OWNERSHIP",
]


# Sleeper — injectable wait primitive


@runtime_checkable
class Sleeper(Protocol):
    """Awaitable delay used for every real wait in the runtime.

    Tests inject a :class:`RecordingSleeper` so the suite never waits real
    seconds; production uses :class:`AsyncSleeper`. ``asyncio.sleep`` itself
    satisfies the protocol, so call sites that pass it directly keep working.
    """

    def __call__(self, seconds: float) -> Awaitable[None]: ...


class AsyncSleeper:
    """Default :class:`Sleeper`: delegates to ``asyncio.sleep`` (real wait)."""

    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class RecordingSleeper:
    """Test double: records every requested duration, never waits.

    ``delays`` holds the requested seconds in call order. Recorded durations
    are assertions (e.g. a 0.5 s response delay) that never pay real time.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def default_sleeper() -> Sleeper:
    """Return the default (real) :class:`Sleeper` for production wiring."""
    return AsyncSleeper()


# ExecutorOwner — explicit executor lifecycle, double-shutdown safe


class ExecutorOwner:
    """Own a :class:`~concurrent.futures.ThreadPoolExecutor` explicitly.

    The executor is created by a NAMED owner (diagnostics; thread names are
    prefixed ``llh-<name>``) and must be shut down explicitly. Shutdown is
    double-safe: calling :meth:`shutdown` twice (or before :meth:`start`)
    is a no-op, so ``finally`` blocks never raise. After shutdown the owner
    refuses new work (:meth:`submit` / :meth:`run_in_thread` raise), which
    turns a forgotten shutdown into a loud error instead of a lingering
    non-daemon thread that keeps the Python process alive.

    The asyncio default executor is a process-global that outlives any
    runtime; using an owned executor is what makes runtime shutdown
    deterministic (plan §5-A6, invariant 17: runtime tests must terminate
    their Python process).
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
        """Create the owned executor (idempotent guard: raises if already
        started or already shut down — an owner never restarts)."""
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
        """Shut the owned executor down explicitly. Safe to call any number
        of times; a shutdown-before-start is a no-op. With ``wait=True``
        (default) joins the worker threads, so a clean shutdown leaves no
        threads behind."""
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

        Drop-in replacement for ``asyncio.to_thread`` with an explicit
        shutdown lifecycle. Raises when the owner is not running.
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


def shutdown_executor(executor: ThreadPoolExecutor | None, *, wait: bool = True) -> None:
    """Idempotent shutdown for a bare executor (None-safe).

    ``ThreadPoolExecutor.shutdown`` is itself safe to call repeatedly; this
    helper makes the intent explicit and tolerates ``None`` (no executor
    created yet)."""
    if executor is None:
        return
    executor.shutdown(wait=wait)


# Resource ownership — owned vs injected, close exactly once


class CloseGuard:
    """Idempotent close wrapper: the wrapped resource's ``close()`` runs
    exactly once no matter how many times :meth:`close` is called.

    A runtime that closes its owned resources through guards can never
    double-close, and a resource closed early by someone else is skipped.
    """

    def __init__(self, resource) -> None:
        self._resource = resource
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._resource.close()

    def __enter__(self) -> "CloseGuard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ResourceRegistry:
    """Track resources with an explicit OWNED vs INJECTED flag.

    - ``register(resource, owned=True)``: the registry closes this resource
      (exactly once) in :meth:`close`.
    - ``register(resource, owned=False)``: injected from outside — tracked
      but NEVER closed here; its creator owns it (no double close).

    :meth:`close` is idempotent; registering after close raises. If a
    resource's ``close()`` raises, the remaining owned resources are still
    closed and the first error is re-raised.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._owned: list = []
        self._injected: list = []
        self._closed = False

    @property
    def owned_count(self) -> int:
        return len(self._owned)

    @property
    def injected_count(self) -> int:
        return len(self._injected)

    @property
    def closed(self) -> bool:
        return self._closed

    def register(self, resource, *, owned: bool = True) -> None:
        if self._closed:
            raise RuntimeError(f"registry {self.name!r} already closed")
        (self._owned if owned else self._injected).append(resource)

    def close(self) -> None:
        """Close each OWNED resource exactly once; never touch injected."""
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for resource in self._owned:
            try:
                CloseGuard(resource).close()
            except Exception as exc:  # keep closing the rest, then re-raise
                errors.append(exc)
        self._owned.clear()
        if errors:
            raise RuntimeError(
                f"{len(errors)} owned resource(s) failed to close "
                f"in registry {self.name!r}"
            ) from errors[0]


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
- SHUTDOWN: the runtime closes the connection it opened and NEVER closes
  the store itself (an injected store belongs to its creator; register it
  owned=False so it is never closed twice).
- THREAD SAFETY: check_same_thread=False + one Lock + WAL + busy_timeout.
- Injected in-memory stores (tests) own their state entirely; the runtime
  leaves them open.
"""


def ensure_thread_safe_connection(path: str, *, timeout: float = 10.0) -> sqlite3.Connection:
    """Open a SQLite connection usable from any thread.

    sqlite3's default ``check_same_thread=True`` binds a connection to the
    thread that created it; the runtime moves session calls to worker
    threads, so the shared connection must be re-opened with
    ``check_same_thread=False``. Callers serialize all access with a single
    ``asyncio.Lock``; WAL + busy_timeout cover cross-process contention.
    """
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
