"""Active-channel selection for the harness (Part B, seam B-3).

One active channel per process. ``select_channel`` is the factory the async
runtime (``sim/run_async.py``) calls with its ``--channel`` flag (default
DEFAULT_CHANNEL). The env var ``HARNESS_CHANNEL`` may override that flag — it
is read by run_async, NOT here: ``select_channel`` takes the name as a
parameter. Concrete CLI/Telegram channels live in harness/channels; their imports
are lazy so importing this module never requires optional dependencies.
"""

from __future__ import annotations

from harness.channels.base import Channel

DEFAULT_CHANNEL = "cli"


def select_channel(name: str, *, inbound: list[str] | None = None) -> Channel:
    """Factory for the single active channel (one per process).
      name=='cli'      -> harness.channels.cli.CLIChannel()
      name=='telegram' -> harness.channels.telegram.TelegramChannel.from_env()
      name=='fake'     -> harness.channels.base.FakeChannel(inbound=inbound)
    Import the telegram module LAZILY inside the branch so importing config
    never requires python-telegram-bot to be installed. Unknown names raise
    ValueError listing the valid names."""
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
