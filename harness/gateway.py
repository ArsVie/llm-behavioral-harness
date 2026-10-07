"""The run's gateway session, and when to move it to another backend.

The gateway pins a session id to one backend. Live 2026-10-01 the backend
behind the run's id went bad and stayed bad for the rest of the run: warm
requests that extended the previous one hit ~30% cache (the system prompt
and tool menu) instead of 97-99%, decisions stopped parsing, and replies
came back empty. Nothing moved the run off it.

``GatewayWatch`` counts strikes against the run's session, and rotates it to a
fresh id after ``STRIKES`` in a row (any healthy call resets the count):

- a warm request that missed the cache: the run's previous call went out
  under ``WARM_S`` ago, it was at least ``MIN_PROMPT`` tokens, and this one
  hit under ``MIN_HIT`` of its own prompt from cache;
- a decision that did not parse;
- a reply re-asked because it came back empty or malformed (each re-ask).

A rotation costs the cache once (a new backend starts cold), so there is at
most one per ``COOLDOWN_S``. Calls sent on a forked id (``fresh_session``)
are another backend and do not count. ``HARNESS_SESSION_ROTATION=0`` turns
rotation off.

The new id goes into ``LLM_SESSION_ID``, which every lane client that was not
given an explicit id reads per request. The session drains the record of each
rotation (:meth:`GatewayWatch.drain`) and saves the id under
``KV_SESSION_ID``, so a restart stays on the new backend.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass

#: kv key holding the run's provider session id (``x-opencode-session``).
KV_SESSION_ID = "wire.session_id"

STRIKES = 3
WARM_S = 3600.0
MIN_PROMPT = 4096
MIN_HIT = 0.5
COOLDOWN_S = 3600.0


def new_session_id() -> str:
    return f"harness-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Rotation:
    old: str | None
    new: str
    reason: str


class GatewayWatch:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._strikes: list[str] = []
        self._last_call_s: float | None = None
        self._last_prompt = 0
        self._rotated_s: float | None = None
        self._pending: list[Rotation] = []

    # -- signals -------------------------------------------------------- #

    def note_call(self, prompt_tokens: int | None, cached_tokens: int | None,
                  *, re_asks: int = 0) -> None:
        """One completed request on the run's session."""
        now = self._clock()
        with self._lock:
            for _ in range(re_asks):
                self._strike("empty reply")
            warm = (self._last_call_s is not None
                    and now - self._last_call_s < WARM_S
                    and self._last_prompt >= MIN_PROMPT)
            if prompt_tokens:
                if warm and cached_tokens is not None:
                    if cached_tokens < MIN_HIT * prompt_tokens:
                        self._strike(
                            f"cache miss on a warm request "
                            f"({cached_tokens}/{prompt_tokens})")
                    elif not re_asks:
                        self._strikes.clear()
                self._last_call_s, self._last_prompt = now, prompt_tokens

    def note_decision(self, parsed: bool) -> None:
        with self._lock:
            if parsed:
                self._strikes.clear()
            else:
                self._strike("decision did not parse")

    # -- rotation -------------------------------------------------------- #

    def _strike(self, reason: str) -> None:
        self._strikes.append(reason)
        if len(self._strikes) < STRIKES:
            return
        now = self._clock()
        if self._rotated_s is not None and now - self._rotated_s < COOLDOWN_S:
            return
        if os.environ.get("HARNESS_SESSION_ROTATION", "1") == "0":
            return
        old = os.environ.get("LLM_SESSION_ID")
        new = new_session_id()
        os.environ["LLM_SESSION_ID"] = new
        self._pending.append(Rotation(old, new, "; ".join(self._strikes)))
        self._strikes.clear()
        self._rotated_s = now
        # the new backend starts cold: its first request is not "warm"
        self._last_call_s = None

    def drain(self) -> list[Rotation]:
        """Rotations since the last drain, oldest first."""
        with self._lock:
            out, self._pending = self._pending, []
        return out


#: The process's watch: every lane client shares the run's session.
WATCH = GatewayWatch()
