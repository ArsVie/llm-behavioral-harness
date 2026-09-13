"""Active-channel selection for the harness: one channel per process.

``select_channel`` is the factory (the caller passes the name; the
``HARNESS_CHANNEL`` env var is read by the caller, not here). Concrete
channels live in harness/channels and are imported lazily so this module
never requires optional dependencies.
"""

from __future__ import annotations

from harness.channels.base import Channel

DEFAULT_CHANNEL = "cli"


def select_channel(name: str, *, inbound: list[str] | None = None) -> Channel:
    """Factory for the single active channel.

      name=='cli'      -> harness.channels.cli.CLIChannel()
      name=='telegram' -> harness.channels.telegram.TelegramChannel.from_env()
      name=='fake'     -> harness.channels.base.FakeChannel(inbound=inbound)

    Channel modules are imported lazily inside the branch; unknown names
    raise ValueError listing the valid names."""
    if name == "cli":
        from harness.channels.cli import CLIChannel

        return CLIChannel()
    if name == "telegram":
        from harness.channels.telegram import TelegramChannel

        return TelegramChannel.from_env()
    if name == "fake":
        from harness.channels.base import FakeChannel

        return FakeChannel(inbound=inbound)
    raise ValueError(
        f"Unknown channel name {name!r}; valid names: 'cli', 'telegram', 'fake'"
    )
