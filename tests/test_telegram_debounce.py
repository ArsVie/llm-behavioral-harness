"""Debounce (HARNESS_DEBOUNCE) for the Telegram channel — Wave 1, W-channel.

All timing is virtual (see test_telegram_helpers): the channel's injectable
sleeper parks on a GateSleeper and time is a ManualClock the test advances
explicitly, so no test sleeps for real and every flush instant is
deterministic. Test discipline: buffer -> drain (background task parks on
the gate) -> advance the clock -> release -> drain (flush delivers).
"""

import asyncio

from harness.channels.telegram import TelegramChannel
from test_channel_telegram import FakeApplication, StubUpdate
from test_telegram_helpers import GateSleeper, ManualClock, drain


def make_channel(monkeypatch, *, owner="42", debounce=True):
    """Channel with the debounce flag wired via env and virtual time."""
    monkeypatch.setenv("HARNESS_DEBOUNCE", "1" if debounce else "0")
    app = FakeApplication()
    clock = ManualClock()
    sleeper = GateSleeper(clock)
    channel = TelegramChannel(
        application=app, owner_chat_id=owner, sleeper=sleeper, monotonic=clock
    )
    return app, channel, clock, sleeper


def test_debounce_merges_burst_into_one_message(monkeypatch) -> None:
    """Two rapid messages become ONE InboundMessage with the texts joined by
    \\n; the single wait is the trailing edge (2 s)."""
    app, channel, clock, sleeper = make_channel(monkeypatch)
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate("first", 42))
        await app.handlers[0](StubUpdate("second", 42))  # same instant
        await drain()  # flush task parks (wait 2.0)
        clock.advance(2.0)  # trailing edge elapses
        sleeper.release()
        await drain()  # flush delivers the merged message

    asyncio.run(scenario())
    assert len(received) == 1
    assert received[0].text == "first\nsecond"
    assert received[0].sender_id == "42"
    assert isinstance(received[0].received_at, float)
    assert sleeper.delays == [2.0]


def test_debounce_max_wait_caps_at_8s_since_first(monkeypatch) -> None:
    """A steady stream (one arrival per second) keeps the trailing edge fresh
    but the buffer flushes at 8 s after the FIRST message — the hard cap."""
    app, channel, clock, sleeper = make_channel(monkeypatch)
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate("m1", 42))  # t=1000
        await drain()  # task parks with the full trailing window
        for i in range(2, 9):  # arrivals every 1 s: t=1001..1007
            clock.advance(1.0)
            await app.handlers[0](StubUpdate(f"m{i}", 42))
            sleeper.release()
            await drain()
        clock.advance(1.0)  # t=1008 — the cap deadline
        sleeper.release()
        await drain()

    asyncio.run(scenario())
    assert len(received) == 1
    assert received[0].text == "\n".join(f"m{i}" for i in range(1, 9))
    assert clock.now == 1008.0  # flushed by the cap, not the trailing edge
    assert sleeper.delays == [2.0] * 7 + [1.0]


def test_debounce_off_delivers_each_message_immediately(monkeypatch) -> None:
    """Flag OFF (default): today's behavior — every message is delivered
    immediately, no merging, no waits (live-bot byte-parity)."""
    app, channel, clock, sleeper = make_channel(monkeypatch, debounce=False)
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate("first", 42))
        await app.handlers[0](StubUpdate("second", 42))

    asyncio.run(scenario())
    assert [msg.text for msg in received] == ["first", "second"]
    assert sleeper.delays == []


def test_debounce_flag_wiring(monkeypatch) -> None:
    """HARNESS_DEBOUNCE=1 -> on; unset -> off (default)."""
    monkeypatch.setenv("HARNESS_DEBOUNCE", "1")
    assert (
        TelegramChannel(application=FakeApplication(), owner_chat_id="42")
        .debounce_enabled
        is True
    )
    monkeypatch.delenv("HARNESS_DEBOUNCE", raising=False)
    assert (
        TelegramChannel(application=FakeApplication(), owner_chat_id="42")
        .debounce_enabled
        is False
    )


def test_debounce_buffers_only_owner_messages(monkeypatch) -> None:
    """The buffer sits AFTER the owner filter: a stranger's message is never
    buffered and never merged in."""
    app, channel, clock, sleeper = make_channel(monkeypatch)
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate("mine", 42))
        await app.handlers[0](StubUpdate("theirs", 999))  # dropped pre-buffer
        await drain()
        clock.advance(2.0)
        sleeper.release()
        await drain()

    asyncio.run(scenario())
    assert len(received) == 1
    assert received[0].text == "mine"


def test_command_arrival_flushes_debounce_buffer_first(monkeypatch) -> None:
    """Seam S3: a /command arrival flushes the buffered text to on_message
    BEFORE the command reaches on_command."""
    app, channel, clock, sleeper = make_channel(monkeypatch)
    events = []

    async def handler(msg):
        events.append(f"msg:{msg.text}")

    async def on_command(cmd):
        events.append(f"cmd:{cmd.name}")

    async def scenario() -> None:
        await channel.start(handler, on_command=on_command)
        await app.handlers[0](StubUpdate("hello", 42))
        await drain()  # flush task parks with "hello"
        await app.handlers[1](StubUpdate("/ping now", 42))
        await drain()

    asyncio.run(scenario())
    assert events == ["msg:hello", "cmd:ping"]


def test_stop_drops_pending_buffer_deterministically(monkeypatch) -> None:
    """stop() with a pending buffer: the flush task is cancelled and the
    buffer is dropped (never delivered); inbound after stop is ignored."""
    app, channel, clock, sleeper = make_channel(monkeypatch)
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate("pending", 42))
        await drain()  # flush task parks with "pending"
        await channel.stop()
        await app.handlers[0](StubUpdate("after", 42))  # stopped: ignored
        await drain()

    asyncio.run(scenario())
    assert received == []
    assert channel._buffer == []
