"""Tests for the Channel protocol, message shapes, FakeChannel, and
active-channel selection (Part B, seams B-1 + B-3).

The cli/telegram branches of select_channel are intentionally NOT tested:
those modules are built by later workers. Only the fake branch is exercised.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import get_type_hints

from harness.channels import (
    Channel,
    FakeChannel,
    InboundHandler,
    InboundMessage,
    OutboundMessage,
)
from harness.config import DEFAULT_CHANNEL, select_channel


def _run(coro) -> None:
    asyncio.run(coro)


def test_fake_channel_satisfies_channel_protocol() -> None:
    """FakeChannel structurally conforms to the Channel protocol."""
    # The protocol declares the `name` data attribute plus three async methods.
    hints = get_type_hints(Channel)
    assert "name" in hints
    for method in ("start", "send", "stop"):
        assert callable(getattr(Channel, method))
        assert inspect.iscoroutinefunction(getattr(Channel, method))

    fake = FakeChannel()
    assert fake.name == "fake"
    for method in ("start", "send", "stop"):
        assert callable(getattr(fake, method))
        assert inspect.iscoroutinefunction(getattr(fake, method))
    assert inspect.iscoroutinefunction(fake.feed)


def test_fake_channel_sane_defaults() -> None:
    """Construction is trivial: inbound=None -> empty list, no handler yet."""
    fake = FakeChannel()
    assert fake.inbound == []
    assert fake.sent == []
    assert fake.handler is None


def test_feed_round_trip_delivers_inbound_message() -> None:
    """feed() -> handler receives an InboundMessage with text/sender_id/t_h."""
    fake = FakeChannel()
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)

    async def scenario() -> None:
        await fake.start(handler)
        await fake.feed("hello there", t_h=37.5)

    _run(scenario())
    assert len(received) == 1
    msg = received[0]
    assert msg.text == "hello there"
    assert msg.sender_id == "fake"
    assert msg.t_h == 37.5
    assert msg.received_at is None


def test_feed_without_start_raises() -> None:
    """feed() before start() (no handler registered) raises a clear error."""
    fake = FakeChannel()

    async def scenario() -> None:
        await fake.feed("nobody listening")

    try:
        _run(scenario())
    except RuntimeError as exc:
        assert "start()" in str(exc)
    else:
        raise AssertionError("feed() without handler should have raised")


def test_send_accumulates_outbound_messages() -> None:
    """send() appends OutboundMessage (reactive and proactive) to .sent."""
    fake = FakeChannel()

    async def scenario() -> None:
        await fake.send(OutboundMessage(text="plain reply"))
        await fake.send(
            OutboundMessage(text="reaching out", proactive=True, reason="schedule")
        )

    _run(scenario())
    assert len(fake.sent) == 2
    assert fake.sent[0].text == "plain reply"
    assert fake.sent[0].proactive is False
    assert fake.sent[0].reason is None
    assert fake.sent[1].text == "reaching out"
    assert fake.sent[1].proactive is True
    assert fake.sent[1].reason == "schedule"


def test_feed_uses_preloaded_inbound_list() -> None:
    """The inbound list is stored as given, for channels that preload."""
    fake = FakeChannel(inbound=["a", "b"])
    assert fake.inbound == ["a", "b"]


def test_select_channel_fake_returns_fake_channel() -> None:
    """select_channel('fake') builds a FakeChannel carrying the inbound list."""
    channel = select_channel("fake", inbound=["hello"])
    assert isinstance(channel, FakeChannel)
    assert channel.name == "fake"
    assert channel.inbound == ["hello"]


def test_select_channel_fake_end_to_end() -> None:
    """FakeChannel built via select_channel works end-to-end: feed -> handler
    -> send -> .sent."""
    channel = select_channel("fake")
    assert isinstance(channel, FakeChannel)
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)
        await channel.send(OutboundMessage(text="echo: " + msg.text))

    async def scenario() -> None:
        await channel.start(handler)
        await channel.feed("ping")

    _run(scenario())
    assert len(received) == 1 and received[0].text == "ping"
    assert len(channel.sent) == 1 and channel.sent[0].text == "echo: ping"


def test_select_channel_unknown_name_raises_value_error() -> None:
    """An unknown channel name raises ValueError listing valid names."""
    try:
        select_channel("bogus")
    except ValueError as exc:
        message = str(exc)
        assert "bogus" in message
        for valid in ("cli", "telegram", "fake"):
            assert valid in message
    else:
        raise AssertionError("select_channel('bogus') should have raised")


def test_default_channel_is_cli() -> None:
    """The documented default active channel is 'cli'."""
    assert DEFAULT_CHANNEL == "cli"


def test_inbound_handler_type_alias() -> None:
    """InboundHandler is the callable shape the protocol expects."""
    async def handler(msg: InboundMessage) -> None:
        del msg  # unused

    # A plain async callable is assignable to the alias (runtime sanity only).
    fake = FakeChannel()
    assert fake.handler is None
    fake.handler = handler  # type: ignore[assignment]
    assert fake.handler is handler
    assert InboundHandler is not None
