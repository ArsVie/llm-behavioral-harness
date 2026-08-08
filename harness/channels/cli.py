"""Async stdin/stdout CLI channel (Part B, worker B2).

``CLIChannel`` implements the Channel protocol from
``harness/channels/base.py`` for direct terminal interaction. A background
asyncio task reads lines from stdin — via ``asyncio.to_thread`` so the event
loop is never blocked and the reader stays cancellable — and forwards each
non-empty line to the inbound handler as an ``InboundMessage``. Outbound
messages are printed to stdout, with a ``[proactive] `` prefix when flagged
proactive.

A blank line or EOF is the stop signal: it ends the reader loop (the channel
then silently delivers nothing more until ``stop()`` or process exit).
"""

from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from harness.channels.base import InboundHandler, InboundMessage, OutboundMessage


class CLIChannel:
    """Terminal channel: stdin -> InboundMessage, OutboundMessage -> stdout.

    ``start()`` schedules a cancellable background reader task and returns
    immediately (it never blocks on input). ``send()`` prints the message
    text with flush. ``stop()`` cancels the reader task and is idempotent.
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
            # Off the event loop; cancellable because to_thread returns a
            # cancellable future even while the OS read blocks in the thread.
            line = await asyncio.to_thread(self._stdin.readline)
            if line == "":
                break  # EOF: no more input
            text = line.strip()
            if text == "":
                break  # blank line: stop signal per spec
            await self._on_message(InboundMessage(text=text, sender_id="cli"))

    async def send(self, message: OutboundMessage) -> None:
        """Print the outbound message, prefixed with ``[proactive] `` when
        proactive; flushed immediately."""
        prefix = "[proactive] " if message.proactive else ""
        print(prefix + message.text, file=self._stdout, flush=True)

    async def stop(self) -> None:
        """Cancel the reader task. Idempotent: repeated calls are no-ops."""
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
