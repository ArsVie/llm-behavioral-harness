"""Channel protocol, message shapes, and the shared FakeChannel.

Concrete channels (CLI, Telegram) live here.
"""

from harness.channels.base import (
    Channel,
    FakeChannel,
    InboundHandler,
    InboundMessage,
    OutboundMessage,
)

__all__ = [
    "Channel",
    "FakeChannel",
    "InboundHandler",
    "InboundMessage",
    "OutboundMessage",
]
