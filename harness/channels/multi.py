"""Several channels behind one: answers go back where the message came from.

What it does: the runtime codes against one channel. ``MultiChannel`` starts
every channel it wraps with the runtime's handlers and remembers, per asyncio
task, which channel an inbound message or command arrived on. A send made
while handling that message (her reply, a command's answer, a missed-call
text) goes back to that channel; a send with no origin (a proactive message
from the life loop, a heads-up from a settle) goes to the primary channel.

The origin rides a ``contextvars.ContextVar``: asyncio copies the context
into every task, so it follows the reply through the runtime's awaits and
into tasks spawned from the handler, and never leaks into the life loop.

Interface:
    MultiChannel(primary, *others)
        .start(on_message, on_command=None)   starts every channel
        .send(message)                        to the origin, else primary
        .typing_context() / .typing_pause(s)  the origin's, when it has them
        .stop()
    origin() -> channel | None                the current task's origin

Dependencies: the Channel protocol (``harness.channels.base``) only.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect

_ORIGIN: contextvars.ContextVar = contextvars.ContextVar("channel_origin", default=None)


def origin():
    """The channel the current task's message came from, or None."""
    return _ORIGIN.get()


class MultiChannel:
    """The runtime's one channel, over ``primary`` and ``others``."""

    def __init__(self, primary, *others):
        self.primary = primary
        self.channels = (primary, *others)
        self.name = "+".join(getattr(c, "name", "?") for c in self.channels)

    def _target(self):
        found = _ORIGIN.get()
        return found if found in self.channels else self.primary

    @staticmethod
    def _tagged(channel, handler):
        async def run(item):
            token = _ORIGIN.set(channel)
            try:
                return await handler(item)
            finally:
                _ORIGIN.reset(token)
        return run

    async def start(self, on_message, on_command=None) -> None:
        for ch in self.channels:
            takes_commands = "on_command" in inspect.signature(ch.start).parameters
            if on_command is not None and takes_commands:
                await ch.start(self._tagged(ch, on_message),
                               on_command=self._tagged(ch, on_command))
            else:
                await ch.start(self._tagged(ch, on_message))

    async def send(self, message) -> None:
        await self._target().send(message)

    @contextlib.asynccontextmanager
    async def typing_context(self):
        ctx = getattr(self._target(), "typing_context", None)
        if ctx is None:
            yield
            return
        async with ctx():
            yield

    async def typing_pause(self, seconds: float) -> None:
        pause = getattr(self._target(), "typing_pause", None)
        if pause is not None:
            await pause(seconds)
            return
        import asyncio
        await asyncio.sleep(seconds)

    async def stop(self) -> None:
        for ch in self.channels:
            with contextlib.suppress(Exception):
                await ch.stop()
