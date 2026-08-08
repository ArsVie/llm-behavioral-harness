"""Tests for the CLI channel (Part B, worker B2).

Plain synchronous pytest functions wrapping ``asyncio.run()`` — pytest-asyncio
is not installed, so no extra dependency. No real terminal is used: stdin and
stdout are injected StringIO-like objects.
"""

import asyncio
import io
import queue
import time

from harness.channels.base import OutboundMessage
from harness.channels.cli import CLIChannel


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll until predicate() is truthy or the deadline passes."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


class QueueStdin:
    """Blocking fake stdin: readline() waits for the next fed line or EOF.

    Used to keep the CLIChannel reader task alive while exercising stop().
    """

    def __init__(self) -> None:
        self._lines: "queue.Queue[str]" = queue.Queue()

    def readline(self) -> str:
        return self._lines.get()

    def feed(self, line: str) -> None:
        self._lines.put(line)

    def close(self) -> None:
        self._lines.put("")  # EOF


def test_name_and_send_print_plain_and_proactive_prefix():
    out = io.StringIO()
    channel = CLIChannel(stdin=io.StringIO(), stdout=out)
    assert channel.name == "cli"

    async def run():
        await channel.send(OutboundMessage(text="hi"))
        await channel.send(OutboundMessage(text="hi", proactive=True))

    asyncio.run(run())
    assert out.getvalue() == "hi\n[proactive] hi\n"


def test_start_forwards_lines_as_inbound_messages_and_stops_on_eof():
    stdin = io.StringIO("hello there\nsecond line\n")
    channel = CLIChannel(stdin=stdin, stdout=io.StringIO())
    got = []

    async def run():
        async def handler(m):
            got.append(m)

        await channel.start(handler)
        reader = channel._reader
        assert reader is not None
        await reader  # EOF ends the loop; the task completes

    asyncio.run(run())
    assert [m.text for m in got] == ["hello there", "second line"]
    assert all(m.sender_id == "cli" for m in got)


def test_blank_line_stops_reader():
    stdin = io.StringIO("first\n\nsecond\n")
    channel = CLIChannel(stdin=stdin, stdout=io.StringIO())
    got = []

    async def run():
        async def handler(m):
            got.append(m)

        await channel.start(handler)
        reader = channel._reader
        assert reader is not None
        await reader  # blank line ends the loop

    asyncio.run(run())
    assert [m.text for m in got] == ["first"]


def test_stop_is_idempotent_and_cancels_reader():
    stdin = QueueStdin()
    channel = CLIChannel(stdin=stdin, stdout=io.StringIO())
    got = []

    async def run():
        async def handler(m):
            got.append(m)

        await channel.start(handler)
        stdin.feed("before stop\n")
        await _wait_for(lambda: len(got) == 1)
        reader = channel._reader
        assert reader is not None
        await channel.stop()
        await channel.stop()  # second call must not raise
        assert reader.done()
        stdin.feed("after stop\n")  # queued, but the reader is gone
        stdin.close()  # release the executor thread blocked on readline
        await asyncio.sleep(0.05)
        assert [m.text for m in got] == ["before stop"]

    asyncio.run(run())


def test_select_channel_wires_the_cli_branch():
    from harness.config import select_channel

    channel = select_channel("cli")
    assert channel.name == "cli"
