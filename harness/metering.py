"""Ledger metering for auxiliary one-shot model calls.

The day planner, the interest extension, the routine setup and the judge build
their provider calls outside the session's generation paths; ``MeteredClient``
wraps a client so each call is recorded under the role that made it. Metering
only RECORDS:

- the result, the exception and the timing are the wrapped client's, untouched;
- a method the wrapped client does not have is NOT defined on the proxy, so
  capability gates (``hasattr(client, "chat_with_meta")``) keep answering
  truthfully;
- everything else (``supports_json``, ``model``, ``lane``) falls through;
- a ledger that cannot take usage kwargs gets the row without them, and a
  store with no ``log_llm_call`` is a no-op rather than a crash.

It is not a retry, cache, routing or prompt-building layer.
"""

from __future__ import annotations

import inspect
import sqlite3
import threading

__all__ = ["MeteredClient", "render_prompt"]

#: Per-thread sibling stores, keyed by (store class, path): a store's own
#: connection belongs to the thread that opened it.
_THREAD_STORES = threading.local()

#: Methods a metered client forwards AND records (``chat_stream`` excluded:
#: no auxiliary caller streams).
_METERED_METHODS = ("chat_with_meta", "chat")


def render_prompt(messages, system: str | None = None) -> str:
    """The human-readable prompt a ledger row keeps for an aux call.

    The exact payload survives only in ``repro`` (audit mode).
    """
    lines = []
    if system:
        lines.append(f"system: {system}")
    for message in messages or ():
        if isinstance(message, dict):
            lines.append(f"{message.get('role', '?')}: {message.get('content', '')}")
        else:
            lines.append(str(message))
    return "\n".join(lines)


def _sibling_log(store):
    """``log_llm_call`` on a handle usable from THIS thread, or None.

    ``sqlite3`` binds a connection to the thread that opened it, so when the
    store's own connection belongs elsewhere the row goes through a cached
    same-class handle on the same file (WAL + busy_timeout cover contention).
    """
    path = getattr(store, "path", None)
    if not path or not callable(getattr(store, "log_llm_call", None)):
        return None
    cache = getattr(_THREAD_STORES, "by_key", None)
    if cache is None:
        cache = _THREAD_STORES.by_key = {}
    key = (type(store), str(path))
    handle = cache.get(key)
    if handle is None:
        try:
            handle = type(store)(
                path, audit_mode=bool(getattr(store, "audit_mode", False))
            )
        except Exception:  # noqa: BLE001 - an unusable sibling just means no row
            return None
        cache[key] = handle
    return handle.log_llm_call


class MeteredClient:
    """A client wrapper that records each call it forwards.

    ``role`` names the caller in the ledger; the lane comes from the wrapped
    client unless given explicitly.
    """

    def __init__(self, client, store, clock, role: str, *, lane: str | None = None,
                 logger=None):
        self._client = client
        self._store = store
        self._clock = clock
        self._role = role
        #: Optional ``callable(str)``: a ledger that cannot be written is
        #: REPORTED, but never breaks the call it was recording.
        self._logger = logger
        self._lane = lane if lane is not None else getattr(client, "lane", None)
        log = getattr(store, "log_llm_call", None)
        self._log = log if callable(log) else None
        self._log_takes_usage = self._log is not None and (
            "usage" in inspect.signature(self._log).parameters
        )
        for name in _METERED_METHODS:
            inner = getattr(client, name, None)
            if callable(inner):
                setattr(self, name, self._metered(inner))

    def __getattr__(self, name):
        # Only reached when the attribute is not on the proxy; private names
        # are refused so a half-built proxy cannot recurse through itself.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_client"), name)

    def _metered(self, call):
        def wrapper(messages=None, *args, **kwargs):
            result = call(messages, *args, **kwargs)
            self._record(messages, kwargs, result)
            return result

        return wrapper

    def _record(self, messages, kwargs, result) -> None:
        if self._log is None:
            return
        extra = (
            {
                "usage": getattr(result, "usage", None),
                "lane": self._lane,
                "raw_cost": getattr(result, "raw_cost", None),
            }
            if self._log_takes_usage
            else {}
        )
        now = self._clock.now_h() if self._clock is not None else 0.0
        arguments = (
            int(now // 24.0),
            now,
            self._role,
            render_prompt(messages, kwargs.get("system")),
            getattr(result, "content", None)
            or (result if isinstance(result, str) else ""),
            getattr(self._client, "model", None),
            {"aux": True, "role": self._role},
        )
        write_kwargs = {"repro": {"messages": messages, "kwargs": dict(kwargs)}, **extra}
        try:
            self._log(*arguments, **write_kwargs)
            return
        except sqlite3.ProgrammingError:
            pass          # connection bound to another thread: retry as a sibling
        except Exception as exc:  # noqa: BLE001 - a ledger write is never fatal
            self._report(exc)
            return

        sibling = _sibling_log(self._store)
        if sibling is None:
            self._report(
                RuntimeError("ledger connection belongs to another thread")
            )
            return
        try:
            sibling(*arguments, **write_kwargs)
        except Exception as exc:  # noqa: BLE001 - same rule as above
            self._report(exc)

    def _report(self, exc: Exception) -> None:
        """Say the row was lost, then carry on: the CALL is what matters, so a
        failed ledger write never raises to the caller."""
        if self._logger is not None:
            self._logger(f"metering: ledger write failed ({exc}) - call continued")
