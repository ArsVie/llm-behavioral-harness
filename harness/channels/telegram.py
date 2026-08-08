"""Telegram channel (Part B, seam B-4) — python-telegram-bot transport.

Env contract:
  TELEGRAM_BOT_TOKEN  required at runtime; a clear error is raised when
                      missing (mirrors OpenAICompatibleClient's missing-key
                      message). Credentials come from the environment, never
                      from the repo, tests, or logs.
  TELEGRAM_CHAT_ID    optional; the single owner's chat. Used as the default
                      ``send`` target and to filter inbound to the owner only.

python-telegram-bot is an OPTIONAL dependency: importing this module never
requires it. The guarded top-level import only sets a capability flag; every
method that needs the library checks the flag (and imports the needed
classes lazily), so ``harness.config.select_channel`` can import this module
without the extra installed and ``from_env()`` can raise its missing-token
error without it.
"""

from __future__ import annotations

import os
import time

from harness.channels.base import InboundMessage, OutboundMessage

try:  # optional dependency — see module docstring
    import telegram  # noqa: F401  (capability probe)
    from telegram.ext import Application

    _PTB_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install
    _PTB_AVAILABLE = False


class TelegramChannel:
    """Channel that delivers messages through a Telegram bot.

    Build via ``TelegramChannel.from_env()`` (production) or by injecting a
    fake application and owner chat id (tests). No network access happens
    unless the real library and token are present.
    """

    name = "telegram"

    def __init__(self, application=None, owner_chat_id=None):
        self.application = application  # real Application (from_env) or fake (tests)
        self.owner_chat_id = owner_chat_id
        self._handler = None
        self._stopped = False

    @classmethod
    def from_env(cls) -> "TelegramChannel":
        """Build from the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars.

        Raises a clear RuntimeError when the token is missing. The token
        check does not require python-telegram-bot to be installed.
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set — the harness never stores "
                "credentials. Export it before running live."
            )
        owner_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        application = cls._build_application(token)
        return cls(application=application, owner_chat_id=owner_chat_id)

    @staticmethod
    def _build_application(token):
        if not _PTB_AVAILABLE:
            raise ImportError(
                "python-telegram-bot is not installed — install the optional "
                "'channels' dependency group of this project to use the "
                "Telegram channel."
            )
        return Application.builder().token(token).build()

    async def start(self, on_message) -> None:
        """Register the inbound handler and begin delivering updates.

        With a real ptb Application this initializes and starts polling;
        with an injected fake application (tests) it just registers the raw
        callback the fake can drive directly — the pure seam, no network.
        """
        self._handler = on_message
        app = self.application
        if app is None:
            raise RuntimeError(
                "TelegramChannel has no application — build it via "
                "from_env() or inject one in the constructor."
            )
        if _PTB_AVAILABLE and isinstance(app, Application):
            from telegram.ext import MessageHandler, filters

            app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_update)
            )
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
        else:
            # Injected fake application (tests): record the raw callback.
            app.add_handler(self._on_update)

    async def send(self, message: OutboundMessage) -> None:
        """Post an outbound (reactive or proactive) message to the owner chat."""
        if self.owner_chat_id is None:
            raise RuntimeError(
                "TELEGRAM_CHAT_ID is not set — cannot determine the owner "
                "chat to send to. Export it before running live."
            )
        app = self.application
        if app is None:
            raise RuntimeError(
                "TelegramChannel has no application — build it via "
                "from_env() or inject one in the constructor."
            )
        await app.bot.send_message(chat_id=self.owner_chat_id, text=message.text)

    async def stop(self) -> None:
        """Stop listeners and release resources. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        app = self.application
        if _PTB_AVAILABLE and isinstance(app, Application):
            await app.stop()

    async def _on_update(self, update) -> None:
        """ptb update callback: wrap the update and forward it to the handler.

        Declared async so ptb (which supports coroutine callbacks) and
        injected fakes can both drive it directly.
        """
        message = self._wrap_update(update)
        if message is not None and self._handler is not None:
            await self._handler(message)

    def _wrap_update(self, update) -> InboundMessage | None:
        """Map a raw update to an InboundMessage, or None when it is not a
        text message from the owner (photos, stickers, commands, strangers)."""
        message = getattr(update, "message", None)
        text = getattr(message, "text", None) if message is not None else None
        if not text:
            return None
        chat_id = self._chat_id_of(update)
        if chat_id is None:
            return None
        if self.owner_chat_id is not None and str(chat_id) != self.owner_chat_id:
            return None  # filter inbound to the owner only
        return InboundMessage(
            text=text, sender_id=str(chat_id), received_at=time.time()
        )

    @staticmethod
    def _chat_id_of(update):
        """Chat id of an update: effective_chat (ptb idiom) with a fallback
        to message.chat (hand-built stubs)."""
        effective_chat = getattr(update, "effective_chat", None)
        if effective_chat is not None:
            return getattr(effective_chat, "id", None)
        message = getattr(update, "message", None)
        chat = getattr(message, "chat", None) if message is not None else None
        if chat is not None:
            return getattr(chat, "id", None)
        return None
