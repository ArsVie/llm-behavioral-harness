"""Channel protocol, message shapes, and the shared FakeChannel (Part B).

`from harness.channels import ...` exposes the base types; concrete channels
(CLI, Telegram) land in later waves.
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
