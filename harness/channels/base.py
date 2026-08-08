"""Channel protocol + message shapes + shared FakeChannel (Part B, seam B-1).

This is the contract every channel (CLI, Telegram, fakes) implements and every
consumer (the async runtime) codes against. Channels are dumb transports:
they never touch Session — inbound messages arrive as InboundMessage, outbound
messages leave as OutboundMessage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass
class InboundMessage:
    text: str
    sender_id: str | None = None      # channel-native id (chat id, "cli", ...)
    t_h: float | None = None          # virtual hour if the channel drives one; else None
    received_at: float | None = None  # real epoch seconds (optional, telemetry only)


@dataclass
class OutboundMessage:
    text: str
    proactive: bool = False
    reason: str | None = None         # taxonomy tag when proactive


InboundHandler = Callable[[InboundMessage], Awaitable[None]]


class Channel(Protocol):
    name: str

    async def start(self, on_message: InboundHandler) -> None:
        """Begin delivering inbound messages to on_message. Returns once the
        channel is ready (long-lived listeners run as background tasks the
        channel owns)."""

    async def send(self, message: OutboundMessage) -> None:
        """Deliver an outbound (reactive or proactive) message to the user."""

    async def stop(self) -> None:
        """Stop listeners and release resources. Idempotent."""


class FakeChannel:
    """In-memory Channel for tests: records outbound messages in ``.sent`` and
    lets tests push inbound messages through the registered handler."""

    name = "fake"

    def __init__(self, inbound: list[str] | None = None):
        self.inbound: list[str] = inbound if inbound is not None else []
        self.sent: list[OutboundMessage] = []  # everything send() received
        self.handler: InboundHandler | None = None

    async def start(self, on_message: InboundHandler) -> None:
        self.handler = on_message

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def stop(self) -> None:
        pass

    async def feed(self, text: str, *, t_h: float | None = None) -> None:
        """Test helper: deliver one inbound message through the handler."""
        if self.handler is None:
            raise RuntimeError("FakeChannel.feed() called before start()")
        await self.handler(InboundMessage(text=text, sender_id="fake", t_h=t_h))
