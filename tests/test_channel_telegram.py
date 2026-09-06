import pytest

telegram = pytest.importorskip("telegram")  # noqa: F401 - optional dep; whole module skips without it
# Telegram channel tests: env contract, update -> InboundMessage mapping,
# send target, start/stop wiring; fake Bot injected, no network, no token.

import asyncio

from harness.channels.base import OutboundMessage
from harness.channels.telegram import (
    TelegramChannel,
    chunk_send,
    render_markdown,
    split_sends,
)
from tests.helpers import FakeApplication, FakeBot, StubUpdate
from tests.helpers.channel_fakes import StubChat, StubMessage


def _run(coro) -> None:
    asyncio.run(coro)


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


# --- paragraph sends + markdown ---


def test_send_splits_paragraphs_and_italicises() -> None:
    """\\n\\n becomes separate sends; RP *span* arrives as MarkdownV2 italics."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.send(OutboundMessage(
            text="*rolls eyes*\n\nFine, boring version."))

    _run(scenario())
    assert [c["text"] for c in app.bot.calls] == [
        "_rolls eyes_", "Fine, boring version\\."]


def test_send_escapes_stray_markdown() -> None:
    """Underscores and unmatched asterisks are escaped, never parsed."""
    assert split_sends("a_b\n\n*ok?") == ["a_b", "*ok?"]
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.send(OutboundMessage(text="a_b *c* 5 * 3"))

    _run(scenario())
    assert [c["text"] for c in app.bot.calls] == ["a\\_b _c_ 5 \\* 3"]


def test_send_falls_back_to_plain() -> None:
    """A rejected Markdown send retries once as plain text (logged)."""

    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if kwargs.get("parse_mode"):
                raise RuntimeError("bad parse")
            await super().send_message(chat_id, text, **kwargs)

    app = FakeApplication()
    app.bot = FlakyBot()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.send(OutboundMessage(text="*hi* there"))

    _run(scenario())
    assert [c["text"] for c in app.bot.calls] == ["*hi* there"]


def test_chunk_send_guardrail() -> None:
    """Oversized paragraphs split under the limit; short ones pass through."""
    assert chunk_send("abc") == ["abc"]
    long = "word " * 1000
    parts = chunk_send(long, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert " ".join(p.strip() for p in parts).split() == long.split()
    assert render_markdown("plain") == "plain"


# --- single-poller guard ---


def test_poller_lock_single_holder_per_token(tmp_path, monkeypatch) -> None:
    """Two pollers on one token refuse loudly; different tokens coexist;
    closing the holder releases."""
    fcntl = pytest.importorskip("fcntl")  # noqa: F841 - documents posix-only guard
    import tempfile

    from harness.channels.telegram import acquire_poller_lock

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    first = acquire_poller_lock("TOKEN-abc")
    with pytest.raises(RuntimeError, match="already holds"):
        acquire_poller_lock("TOKEN-abc")
    other = acquire_poller_lock("TOKEN-other")
    first.close()
    again = acquire_poller_lock("TOKEN-abc")
    other.close()
    again.close()
