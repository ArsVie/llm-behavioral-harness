"""Telegram channel — python-telegram-bot transport.

Env: ``TELEGRAM_BOT_TOKEN`` (required at runtime), ``TELEGRAM_CHAT_ID`` (the
owner chat), ``HARNESS_DEBOUNCE`` / ``HARNESS_TYPING`` (opt-in, off by
default). ptb itself is optional — importing this module never requires it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from harness.channels.base import InboundMessage, OutboundMessage
from harness.concurrency import Sleeper, default_sleeper
from harness.env import env_bool as _env_bool

#: Errors raised inside a ptb handler are reported here instead of being
#: swallowed by the library's default.
_logger = logging.getLogger(__name__)

try:  # optional dependency
    import telegram  # noqa: F401  (capability probe)
    from telegram.ext import Application

    _PTB_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install
    _PTB_AVAILABLE = False

#: Trailing-edge debounce window (HARNESS_DEBOUNCE_TRAILING_S): flush this
#: many seconds after the last buffered message.
DEFAULT_DEBOUNCE_TRAILING_S = 4.5
#: Debounce hard cap (HARNESS_DEBOUNCE_MAX_WAIT_S): flush at most this long
#: after the first buffered message.
DEFAULT_DEBOUNCE_MAX_WAIT_S = 12.0
#: Typing refresh cadence; the typing indicator expires after ~5 s.
_TYPING_INTERVAL_S = 4.5

#: Telegram hard limit per message (set just under, to leave room for entities).
_TELEGRAM_MAX_LEN = 4000

#: MarkdownV2 metacharacters escaped outside entity spans.
_MDV2_ESCAPE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')

#: RP emphasis span: *...* (no newlines, no nesting; paragraphs are split
#: first, so a span never crosses a send).
_ITALIC_SPAN = re.compile(r'\*([^\n*]+)\*')


def _escape_mdv2(text: str) -> str:
    """Escape MarkdownV2 metacharacters in a literal run."""
    return _MDV2_ESCAPE.sub(r'\\\1', text)


def render_markdown(text: str) -> str:
    """Render one paragraph as MarkdownV2: RP ``*span*`` becomes italics.

    Everything else is escaped, so stray asterisks or underscores can
    never break parsing. An unmatched ``*`` stays literal (escaped).
    """
    parts: list[str] = []
    pos = 0
    for match in _ITALIC_SPAN.finditer(text):
        parts.append(_escape_mdv2(text[pos:match.start()]))
        parts.append('_' + _escape_mdv2(match.group(1)) + '_')
        pos = match.end()
    parts.append(_escape_mdv2(text[pos:]))
    return ''.join(parts)


def split_sends(text: str) -> list[str]:
    """Split a reply into one send per paragraph (blank-line runs).

    Single paragraph in -> single send out. Empties dropped: no blank
    message ever reaches the wire.
    """
    return [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]


def chunk_send(text: str, limit: int = _TELEGRAM_MAX_LEN) -> list[str]:
    """Hard-split an oversized paragraph under the Telegram limit.

    Prefer a newline, else a space, else a hard cut. Replies are far
    shorter in practice — this is the guardrail, not the path.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind('\n', 0, limit)
        if cut < 0:
            cut = rest.rfind(' ', 0, limit)
        if cut < 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def acquire_poller_lock(token: str):
    """Claim the single-poller lock for one bot token (multi-profile guard).

    Telegram sends each update to one getUpdates consumer only, so a second
    poller Conflict-loops both. An OS flock that dies with the process; the
    caller must keep the returned file object open for the process lifetime.
    """
    import fcntl  # lazy: posix-only, and this module stays importable without it

    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    path = os.path.join(tempfile.gettempdir(), f"lily-poller-{digest}.lock")
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(
            "another live poller already holds this bot token — refusing "
            "a second getUpdates consumer (one telegram profile per token)"
        )
    return fh

#: User-facing command menu registered via setMyCommands when commands
#: are enabled; /state is not included.
USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("help", "list of commands and usage"),
    ("ping", "alive check"),
    ("setup", "initialize a fresh database (pre-bootstrap only)"),
    ("tz", "change timezone (IANA name), applied at the next rollover"),
    ("status", "day, local hour, pending proactives, last-exchange age"),
    ("mute", "pause proactive messages for N hours"),
    ("version", "commit, seed, active flags"),
)


def _debounce_window(name: str, default: float) -> float:
    """Resolve one debounce window from its env var (float seconds).

    Unset or empty -> default; invalid values raise ValueError.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a number of seconds, got {raw!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a positive number of seconds, got {raw!r}"
        )
    return value




@dataclass(frozen=True)
class ControlCommand:
    """Parsed slash-command delivered to ``start(on_command=...)``.

    ``name`` carries no leading slash. Commands never become InboundMessage.
    """

    name: str  # "tz", "status", ... (no slash)
    args: str  # raw remainder
    sender_id: int


class TelegramChannel:
    """Channel that delivers messages through a Telegram bot.

    Build via ``TelegramChannel.from_env()`` or by injecting a fake application
    and owner chat id (tests); no network access happens without the real
    library and token.

    Inbound policy: with ``TELEGRAM_CHAT_ID`` set, only that chat's text is
    forwarded; UNSET means fail-open — any chat that finds the bot can talk to
    the companion (set the env var to lock it down).
    """

    name = "telegram"

    def __init__(
        self,
        application: Any = None,
        owner_chat_id=None,
        *,
        sleeper: Sleeper | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.application = application  # real Application (from_env) or fake (tests)
        # Normalize owner_chat_id to str for the owner filter comparison.
        self.owner_chat_id = str(owner_chat_id) if owner_chat_id is not None else None
        self._handler = None
        self._command_callback = None
        self._stopped = False
        #: Injectable sleep and clock (tests inject fakes).
        self._sleeper: Sleeper = sleeper if sleeper is not None else default_sleeper()
        self._monotonic: Callable[[], float] = (
            monotonic if monotonic is not None else time.monotonic
        )
        #: Flag-controlled behavior, both off by default.
        self.debounce_enabled: bool = _env_bool("HARNESS_DEBOUNCE")
        self.typing_enabled: bool = _env_bool("HARNESS_TYPING")
        #: Debounce windows, env-configurable, resolved at construction.
        self.debounce_trailing_s: float = _debounce_window(
            "HARNESS_DEBOUNCE_TRAILING_S", DEFAULT_DEBOUNCE_TRAILING_S
        )
        self.debounce_max_wait_s: float = _debounce_window(
            "HARNESS_DEBOUNCE_MAX_WAIT_S", DEFAULT_DEBOUNCE_MAX_WAIT_S
        )
        #: Debounce state: buffered (text, sender_id) pairs, monotonic times
        #: of the first/last arrival, and the single flush task.
        self._buffer: list[tuple[str, str | None]] = []
        self._buffer_first_at: float | None = None
        self._last_arrival_at: float | None = None
        self._flush_task: asyncio.Task | None = None

    @classmethod
    def from_env(cls) -> "TelegramChannel":
        """Build from the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars.

        Raises RuntimeError when the token is missing (no ptb required).
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set — the harness never stores "
                "credentials. Export it before running live."
            )
        # Hermes stores the owner chat as TELEGRAM_HOME_CHANNEL; accept both names.
        owner_chat_id = (os.environ.get("TELEGRAM_CHAT_ID")
                         or os.environ.get("TELEGRAM_HOME_CHANNEL"))
        application = cls._build_application(token)
        return cls(application=application, owner_chat_id=owner_chat_id)

    async def check_token(self) -> bool:
        """Validate the bot token via getMe — sends NOTHING. Raw HTTP, no ptb."""
        import httpx

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getMe"
                )
                if resp.status_code != 200:
                    return False
                data = resp.json()
                ok = bool(data.get("ok"))
                if ok and self.owner_chat_id is None and self.application is not None:
                    print(
                        "[telegram] WARNING: TELEGRAM_CHAT_ID not set — "
                        "outbound works, owner-only inbound filtering is off",
                        flush=True,
                    )
                return ok
        except Exception:
            return False

    @staticmethod
    def _build_application(token):
        if not _PTB_AVAILABLE:
            raise ImportError(
                "python-telegram-bot is not installed — install the optional "
                "'channels' dependency group of this project to use the "
                "Telegram channel."
            )
        return Application.builder().token(token).build()

    async def start(self, on_message, on_command=None) -> None:
        """Register the inbound handler and begin delivering updates.

        A real ptb Application initializes and starts polling; an injected fake
        (tests) only records the raw callbacks — no network.

        ``on_command`` (default None) registers the command handler and routes
        slash-commands to it, never to ``on_message``; with None, commands are
        dropped.
        """
        self._handler = on_message
        self._command_callback = on_command
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
            if on_command is not None:
                # filters.COMMAND also matches unknown command names.
                app.add_handler(
                    MessageHandler(filters.COMMAND, self._on_command_update)
                )
            app.add_error_handler(self._on_handler_error)
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
        else:
            # Injected fake application (tests): record the raw callbacks.
            app.add_handler(self._on_update)
            if on_command is not None:
                app.add_handler(self._on_command_update)
        if on_command is not None:
            await self._register_commands()

    async def _on_handler_error(self, update: object, context: object) -> None:
        """Log an exception raised inside a ptb handler; polling continues."""
        error = getattr(context, "error", None)
        _logger.exception(
            "telegram handler error: %s", error, exc_info=error
        )

    async def send(self, message: OutboundMessage) -> None:
        """Post an outbound (reactive or proactive) message to the owner chat.

        One send per paragraph, RP ``*span*`` rendered as MarkdownV2 italics. A
        failed Markdown send retries once as plain text; if that fails too the
        error propagates.
        """
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
        for paragraph in split_sends(message.text):
            for chunk in chunk_send(paragraph):
                try:
                    await app.bot.send_message(
                        chat_id=self.owner_chat_id,
                        text=render_markdown(chunk),
                        parse_mode='MarkdownV2',
                    )
                except Exception:
                    _logger.warning('markdown send failed, retrying plain')
                    await app.bot.send_message(
                        chat_id=self.owner_chat_id, text=chunk)

    @asynccontextmanager
    async def typing_context(self):
        """Keep the Telegram "typing" indicator alive while inside the context.

        Sends ``send_chat_action('typing')`` on entry, then refreshes every
        ~4.5 s (the indicator expires after ~5 s). Gated by HARNESS_TYPING
        (default OFF -> no-op).
        """
        if not self.typing_enabled:
            yield
            return
        task = asyncio.create_task(self._typing_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _typing_loop(self) -> None:
        """Send a typing action, then refresh it every _TYPING_INTERVAL_S; a
        failed send stops the loop quietly (the indicator is cosmetic)."""
        while True:
            try:
                await self._send_typing()
            except Exception:
                return
            await self._sleeper(_TYPING_INTERVAL_S)

    async def _send_typing(self) -> None:
        if self.owner_chat_id is None:
            raise RuntimeError(
                "TELEGRAM_CHAT_ID is not set — cannot send the typing "
                "indicator to the owner chat."
            )
        app = self.application
        if app is None:
            raise RuntimeError(
                "TelegramChannel has no application — build it via "
                "from_env() or inject one in the constructor."
            )
        await app.bot.send_chat_action(chat_id=self.owner_chat_id, action="typing")

    async def stop(self) -> None:
        """Stop listeners and release resources. Idempotent.

        Buffered debounce text is dropped, never delivered: shutdown must not
        invoke the session handler.
        """
        if self._stopped:
            return
        self._stopped = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            # Suppress the cancel's CancelledError and any stale flush-task exception.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._flush_task
            self._flush_task = None
        self._buffer = []
        self._buffer_first_at = None
        self._last_arrival_at = None
        app = self.application
        if _PTB_AVAILABLE and isinstance(app, Application):
            # stop() first: shutdown tears the app down and raises if running.
            await app.stop()
            await app.shutdown()

    async def _on_update(self, update, context=None) -> None:
        """ptb update callback: wrap the update and forward it to the handler.

        Declared async so ptb and injected fakes can both drive it directly;
        ``context`` is optional (fakes call with a single argument). Commands
        are for the command handler only, never an InboundMessage. With
        HARNESS_DEBOUNCE on, owner text is buffered and flushed as ONE message.
        """
        if self._stopped:
            return
        message = self._wrap_update(update)
        if message is None:
            return
        if message.text.startswith("/"):
            return  # commands route via _on_command_update, not here
        if self.debounce_enabled:
            self._buffer_text(message.text, message.sender_id)
            return
        if self._handler is not None:
            await self._handler(message)

    async def _on_command_update(self, update, context=None) -> None:
        """ptb command callback: parse a :class:`ControlCommand` and hand it to
        the ``start(on_command=...)`` callback.

        Same signature as ``_on_update``; the owner filter applies. The debounce
        buffer is flushed FIRST, so a command is never merged with buffered chat
        text. Commands never become InboundMessage.
        """
        message = getattr(update, "message", None)
        text = getattr(message, "text", None) if message is not None else None
        if not text or not text.startswith("/"):
            return
        chat_id = self._chat_id_of(update)
        if chat_id is None:
            return
        if self.owner_chat_id is not None and str(chat_id) != self.owner_chat_id:
            return  # the owner filter applies to commands too
        body = text[1:].strip()
        name, _, args = body.partition(" ")
        name = name.split("@", 1)[0]  # strip bot suffix: /tz@Lily_Vie_bot -> tz
        await self._flush_debounce()
        if self._command_callback is not None:
            await self._command_callback(
                ControlCommand(name=name, args=args.strip(), sender_id=int(chat_id))
            )

    # --- User-facing command menu (setMyCommands) ---

    def _bot_commands(self) -> list:
        """The user-facing command list as ptb ``BotCommand`` values.

        Lazy ptb import; ``/state`` is never in the menu (dispatchable, not
        user-visible).
        """
        from telegram import BotCommand  # lazy ptb (optional dep)

        return [
            BotCommand(command=name, description=desc)
            for name, desc in USER_COMMANDS
        ]

    async def _register_commands(self) -> None:
        """Register ``USER_COMMANDS`` via Telegram ``setMyCommands``.

        Only when commands are enabled; best-effort — a failure logs a warning
        and the channel still starts (the menu is client UI).
        """
        if self._command_callback is None:
            return
        bot = getattr(self.application, "bot", None)
        setter = getattr(bot, "set_my_commands", None)
        if setter is None:
            return
        try:
            await setter(self._bot_commands())
        except Exception as exc:  # noqa: BLE001 - cosmetic; dispatch unaffected
            print(
                f"[telegram] WARNING: setMyCommands failed (the command menu "
                f"will not appear in the client): {exc}",
                flush=True,
            )

    def _wrap_update(self, update) -> InboundMessage | None:
        """Map a raw update to an InboundMessage, or None when it is not a text
        message from the owner (photos, stickers, strangers)."""
        message = getattr(update, "message", None)
        text = getattr(message, "text", None) if message is not None else None
        if not text:
            return None
        chat_id = self._chat_id_of(update)
        if chat_id is None:
            return None
        if self.owner_chat_id is not None and str(chat_id) != self.owner_chat_id:
            return None
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

    # --- Debounce machinery (HARNESS_DEBOUNCE, default off) ---

    def _buffer_text(self, text: str, sender_id: str | None) -> None:
        """Buffer one owner text message; the single flush task is created on
        the first buffered message and re-arms itself for later arrivals."""
        if self._buffer_first_at is None:
            self._buffer_first_at = self._monotonic()
        self._last_arrival_at = self._monotonic()
        self._buffer.append((text, sender_id))
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._debounce_loop())

    async def _debounce_loop(self) -> None:
        """Trailing-edge debounce with a hard max-wait cap.

        Waits min(trailing-edge remaining, max-wait remaining), then re-checks:
        a new arrival extends the trailing edge, while the cap is measured from
        the FIRST buffered message. Flushes when either window is exhausted,
        then loops if a message arrived during the flush.
        """
        while self._buffer:
            # A non-empty buffer always has both timestamps set.
            assert self._buffer_first_at is not None
            assert self._last_arrival_at is not None
            now = self._monotonic()
            since_last = now - self._last_arrival_at
            since_first = now - self._buffer_first_at
            wait = min(
                self.debounce_trailing_s - since_last,
                self.debounce_max_wait_s - since_first,
            )
            if wait > 0:
                await self._sleeper(wait)
                continue
            await self._flush()
        # _flush_task is not cleared here; _buffer_text checks task.done().

    async def _flush(self) -> None:
        """Deliver the buffered texts as ONE InboundMessage joined with \\n."""
        if not self._buffer:
            return
        texts = "\n".join(text for text, _ in self._buffer)
        sender_id = self._buffer[0][1]
        self._buffer = []
        self._buffer_first_at = None
        self._last_arrival_at = None
        if self._handler is not None:
            await self._handler(
                InboundMessage(text=texts, sender_id=sender_id, received_at=time.time())
            )

    async def _flush_debounce(self) -> None:
        """Flush any pending debounced texts IMMEDIATELY.

        Cancels the pending flush task (if any), then delivers the buffer.
        """
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self._flush()
