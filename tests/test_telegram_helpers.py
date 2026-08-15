"""Virtual-time helpers for the Wave-1 Telegram channel tests (W-channel).

The channel's debounce/typing machinery sleeps through an injectable Sleeper
and reads time through an injectable monotonic clock. These helpers make
every wait deterministic: a ManualClock stands in for time.monotonic and a
GateSleeper parks the channel's background task until the test releases it
(after advancing the clock), so no test ever waits real seconds and every
flush/typing instant is exactly controlled.
"""

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
