"""Telegram-channel fakes (ex test_channel_telegram) — the most-imported
fake seam in the suite. Moved VERBATIM; consumed unmodified by the
command/debounce/typing tests.
"""

from __future__ import annotations

import asyncio


class StubChat:
    def __init__(self, chat_id):
        self.id = chat_id


class StubMessage:
    def __init__(self, text, chat_id):
        self.text = text
        self.chat = StubChat(chat_id)


class StubUpdate:
    """Hand-built update: only .message.text and .message.chat.id, like the
    minimal stub the mapping contract reads."""

    def __init__(self, text, chat_id):
        self.message = StubMessage(text, chat_id)


class FakeBot:
    def __init__(self):
        self.calls = []
        self.chat_actions = []
        self.registered_commands = None  # setMyCommands recording (WS-A)

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text})

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.chat_actions.append({"chat_id": chat_id, "action": action})

    async def set_my_commands(self, commands, **kwargs):
        self.registered_commands = list(commands)


class FakeApplication:
    """Records registered handlers; never polls, never touches the network.

    Wave-1 growth (shared seam; Wave 2 consumes it unmodified):
      - ``bot.send_chat_action`` recording (typing-indicator tests)
      - ``command_update()`` — build a stub update carrying a /command
        (command-routing tests drive the registered command handler with it)
    """

    def __init__(self):
        self.bot = FakeBot()
        self.handlers = []

    def add_handler(self, handler):
        self.handlers.append(handler)

    def command_update(self, text, chat_id):
        """Stub update carrying a slash-command (command-update injection)."""
        return StubUpdate(text, chat_id)


async def feed_when_started(channel, text: str, *, t_h: float | None = None) -> None:
    """Feed one inbound once the runtime has opened the channel.

    The startup settle runs BEFORE the channel opens, so a feed created
    ahead of ``runtime.run()`` must wait for the handler instead of
    tripping FakeChannel's before-start guard.
    """
    while getattr(channel, "handler", None) is None:
        await asyncio.sleep(0)
    await channel.feed(text, t_h=t_h)
