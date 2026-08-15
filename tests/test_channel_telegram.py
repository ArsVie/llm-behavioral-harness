import pytest

telegram = pytest.importorskip("telegram")  # noqa: F401 - optional dep; whole module skips without it
# Tests for the Telegram channel (Part B, seam B-4): env contract, update ->
# InboundMessage mapping, send target, start/stop wiring. NO network, NO token:
# a fake Application/Bot is injected through the constructor seam and the
# polling loop is never exercised.

import asyncio

from harness.channels.base import OutboundMessage
from harness.channels.telegram import TelegramChannel


def _run(coro) -> None:
    asyncio.run(coro)


# --- fakes (pure seams; no python-telegram-bot runtime objects) ---


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

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text})

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.chat_actions.append({"chat_id": chat_id, "action": action})


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


# --- env contract ---


def test_from_env_missing_token_raises(monkeypatch) -> None:
    """from_env() without TELEGRAM_BOT_TOKEN raises a clear error."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        TelegramChannel.from_env()


def test_from_env_reads_token_and_owner_chat(monkeypatch) -> None:
    """from_env() with both vars set builds the channel (no network: ptb
    Application.builder().build() only constructs)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    channel = TelegramChannel.from_env()
    assert channel.name == "telegram"
    assert channel.owner_chat_id == "42"
    assert channel.application is not None


# --- update -> InboundMessage mapping ---


def test_update_mapping_forwards_inbound_message() -> None:
    """A text update from the owner reaches the handler as InboundMessage
    with text, sender_id=str(chat_id), and a float received_at."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        assert len(app.handlers) == 1
        await app.handlers[0](StubUpdate("hello", 42))

    _run(scenario())
    assert len(received) == 1
    msg = received[0]
    assert msg.text == "hello"
    assert msg.sender_id == "42"
    assert isinstance(msg.received_at, float)


def test_non_owner_inbound_is_filtered() -> None:
    """Messages from chats other than the owner are dropped before the handler."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate("hello", 999))
        await app.handlers[0](StubUpdate("hello", 42))

    _run(scenario())
    assert len(received) == 1
    assert received[0].sender_id == "42"


def test_non_text_updates_are_ignored() -> None:
    """Updates without text (photos, stickers) produce no InboundMessage."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)
        await app.handlers[0](StubUpdate(None, 42))

    _run(scenario())
    assert received == []


# --- send ---


def test_send_posts_to_owner_chat() -> None:
    """send() delivers the text to the owner chat id via the bot."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.send(
            OutboundMessage(text="reaching out", proactive=True, reason="schedule")
        )

    _run(scenario())
    assert app.bot.calls == [{"chat_id": "42", "text": "reaching out"}]


def test_send_without_owner_chat_raises() -> None:
    """Without TELEGRAM_CHAT_ID there is no default target — clear error."""
    channel = TelegramChannel(application=FakeApplication(), owner_chat_id=None)

    async def scenario() -> None:
        await channel.send(OutboundMessage(text="hi"))

    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        _run(scenario())


# --- start/stop wiring ---


def test_start_without_application_raises() -> None:
    """A channel with no application (neither from_env nor injected) fails
    fast with a clear error instead of touching the network."""
    channel = TelegramChannel(application=None, owner_chat_id="42")

    async def scenario() -> None:
        await channel.start(lambda msg: None)

    with pytest.raises(RuntimeError, match="no application"):
        _run(scenario())


def test_stop_is_idempotent() -> None:
    """stop() can be called repeatedly without error; start() registered the
    handler on the (fake) application."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.start(lambda msg: None)
        await channel.stop()
        await channel.stop()

    _run(scenario())
    assert len(app.handlers) == 1
