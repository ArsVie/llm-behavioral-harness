"""Clocks + sleeper + drain (ex test_telegram_helpers / test_runtime_anchor).

``ManualClock``/``GateSleeper``/``drain`` move VERBATIM from
test_telegram_helpers (the injectable monotonic-clock seam + gated sleeper
the debounce/typing tests park on). ``AnchorManualClock`` is the anchor-mode
wall clock from test_runtime_anchor (a separate shape: it advances by
``delay * drift`` inside ``sleep``), kept distinct because the two clocks
have genuinely different contracts.
"""

from __future__ import annotations

import asyncio


class ManualClock:
    """Injectable monotonic clock: callable (time.monotonic stand-in) with an
    explicit ``advance()`` the test controls."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class GateSleeper:
    """Sleeper that records the requested wait, then parks the caller until
    ``release()`` — by which time the test has advanced the clock to model
    the wait elapsing. Deterministic: the channel task only proceeds when the
    test lets it."""

    def __init__(self, clock: ManualClock):
        self.clock = clock
        self.delays: list[float] = []
        self._gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await self._gate.wait()
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()


async def drain(turns: int = 20) -> None:
    """Yield control repeatedly so background tasks (debounce flush, typing
    refresh) get to run and park on the gate."""
    for _ in range(turns):
        await asyncio.sleep(0)


class AnchorManualClock:
    """Injectable wall clock for anchor mode (ex test_runtime_anchor): a fake
    ``time.time()`` that the sleeper advances by the requested delay.
    ``drift < 1.0`` simulates a slow/late wake — the absolute sleep must
    re-sleep the residual."""

    def __init__(self, t0: float = 1_000_000.0, drift: float = 1.0):
        self.t = t0
        self.drift = drift

    def __call__(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.t += delay * self.drift
