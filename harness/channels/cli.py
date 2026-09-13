"""Async stdin/stdout CLI channel.

A background task reads stdin via ``asyncio.to_thread`` (never blocks the event
loop) and forwards each non-empty line to the inbound handler; outbound
messages print to stdout.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from harness.channels.base import InboundHandler, InboundMessage, OutboundMessage


class CLIChannel:
    """Terminal channel: stdin -> InboundMessage, OutboundMessage -> stdout.

    ``start()`` schedules a background reader task and returns immediately (it
    never blocks on input); EOF or a blank line ends that reader. ``stop()``
    cancels it and is idempotent.
    """

    name = "cli"

    def __init__(self, stdin: TextIO = sys.stdin, stdout: TextIO | None = None):
        """Create the channel. ``stdin``/``stdout`` are injectable for tests;
        defaults keep the production path on the real terminal streams."""
        self._stdin = stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._on_message: InboundHandler | None = None
        self._reader: asyncio.Task | None = None

    async def start(self, on_message: InboundHandler) -> None:
        """Begin reading stdin in the background; return once the task is
        scheduled (does not block on input)."""
        self._on_message = on_message
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Read lines until EOF or a blank line, forwarding each to the
        handler as InboundMessage(text=..., sender_id="cli")."""
        assert self._on_message is not None
        while True:
            # Off the event loop (to_thread stays cancellable while the read blocks).
            line = await asyncio.to_thread(self._stdin.readline)
            if line == "":
                break
            text = line.strip()
            if text == "":
                break
            await self._on_message(InboundMessage(text=text, sender_id="cli"))

    async def send(self, message: OutboundMessage) -> None:
        """Print the outbound message, prefixed with ``[proactive] `` when
        proactive; flushed immediately."""
        prefix = "[proactive] " if message.proactive else ""
        print(prefix + message.text, file=self._stdout, flush=True)

    async def stop(self) -> None:
        """Cancel the reader task and release stdin. Idempotent.

        Closing stdin unblocks the executor thread parked in ``readline()``,
        which would otherwise hang process exit.
        """
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        try:
            self._stdin.close()
        except Exception:
            pass
