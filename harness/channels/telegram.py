"""Telegram channel (Part B, seam B-4) — python-telegram-bot transport.

Env contract:
  TELEGRAM_BOT_TOKEN  required at runtime; a clear error is raised when
                      missing (mirrors OpenAICompatibleClient's missing-key
                      message). Credentials come from the environment, never
                      from the repo, tests, or logs.
  TELEGRAM_CHAT_ID    optional; the single owner's chat. Used as the default
                      ``send`` target and to filter inbound to the owner only.
  HARNESS_DEBOUNCE    optional, default OFF. When enabled, rapid inbound text
                      is merged: messages are buffered AFTER the owner filter
                      and delivered as ONE InboundMessage joined with ``\\n``,
                      trailing-edge 2 s after the last arrival, hard-capped at
                      8 s after the FIRST buffered message. A /command
                      arrival flushes the buffer immediately. OFF (default) =
                      today's behavior (each message delivered immediately).
  HARNESS_TYPING      optional, default OFF. When enabled, ``typing_context()``
                      refreshes the Telegram "typing" chat action every 4.5 s
                      (the indicator expires after ~5 s) while inside the
                      context. OFF (default) = no chat actions are ever sent.

python-telegram-bot is an OPTIONAL dependency: importing this module never
requires it. The guarded top-level import only sets a capability flag; every
method that needs the library checks the flag (and imports the needed
classes lazily), so ``harness.config.select_channel`` can import this module
without the extra installed and ``from_env()`` can raise its missing-token
error without it.

Wave-1 seams (orchestration plan 2026-08-15, worker W-channel):
  S3 (command seam): :class:`ControlCommand` + the optional ``on_command``
  argument of :meth:`TelegramChannel.start` — the command handler is
  registered ONLY when the callback is given (default None -> commands are
  dropped, matching the ``filters.TEXT & ~filters.COMMAND`` registration).
  Commands NEVER become InboundMessage; a command arrival flushes the
  debounce buffer first.
  S4 (typing capability): :meth:`TelegramChannel.typing_context` — duck-typed:
  the runtime probes ``getattr(channel, 'typing_context', None)``; channels
  that lack the method (CLI, fakes) are a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from harness.channels.base import InboundMessage, OutboundMessage
from harness.concurrency import Sleeper, default_sleeper

try:  # optional dependency
    import telegram  # noqa: F401  (capability probe)
    from telegram.ext import Application

    _PTB_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install
    _PTB_AVAILABLE = False

#: Trailing-edge debounce window: flush this many seconds after the last
#: buffered message arrives. Configurable via HARNESS_DEBOUNCE_TRAILING_S.
DEFAULT_DEBOUNCE_TRAILING_S = 4.5
#: Debounce hard cap: flush at most this long after the first buffered
#: message. Configurable via HARNESS_DEBOUNCE_MAX_WAIT_S.
DEFAULT_DEBOUNCE_MAX_WAIT_S = 12.0
#: Typing refresh cadence; the typing indicator expires after ~5 s.
_TYPING_INTERVAL_S = 4.5

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

    Unset/empty -> default. Invalid values FAIL LOUDLY (ValueError at
    channel construction): a misconfigured HARNESS_DEBOUNCE_* must never
    silently change the merge window.
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


def _env_bool(name: str, default: bool = False) -> bool:
    """Env bool with the harness convention (mirrors tools._env_bool):
    unset/empty -> default; truthy = 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ControlCommand:
    """Parsed slash-command delivered to ``start(on_command=...)`` (seam S3).

    Commands NEVER become InboundMessage: they are routed to the command
    callback only, and only when ``start()`` was given one (default None ->
    commands are dropped, exactly like the ``filters.TEXT & ~filters.COMMAND``
    registration). ``name`` carries no leading slash.
    """

    name: str  # "tz", "status", ... (no slash)
    args: str  # raw remainder
    sender_id: int


class TelegramChannel:
    """Channel that delivers messages through a Telegram bot.

    Build via ``TelegramChannel.from_env()`` (production) or by injecting a
    fake application and owner chat id (tests). No network access happens
    unless the real library and token are present.

    Inbound policy: when TELEGRAM_CHAT_ID is set, only that chat's text
    messages are forwarded (others are dropped). When it is UNSET, inbound
    is fail-open — any chat that finds the bot can talk to the companion
    (documented trade-off of the single-user POC; set the env var to lock
    it down).

    Wave-1 additions (debounce, typing, commands) are ALL flag-gated OFF by
    default (HARNESS_DEBOUNCE / HARNESS_TYPING; ``on_command`` defaults to
    None) so the live bot stays byte-identical until the owner flips flags.
    The debounce sleeper and the monotonic clock are injectable (runtime's
    Sleeper pattern) so tests never wait real seconds.
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
        #: Injectable sleep and clock: production uses real sleep and
        #: time.monotonic; tests inject fakes.
        self._sleeper: Sleeper = sleeper if sleeper is not None else default_sleeper()
        self._monotonic: Callable[[], float] = (
            monotonic if monotonic is not None else time.monotonic
        )
        #: Flag-controlled behavior, both off by default.
        self.debounce_enabled: bool = _env_bool("HARNESS_DEBOUNCE")
        self.typing_enabled: bool = _env_bool("HARNESS_TYPING")
        #: Debounce windows, env-configurable; defaults are trailing 4.5 s
        #: and cap 12 s, resolved at construction.
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

        Raises a clear RuntimeError when the token is missing. The token
        check does not require python-telegram-bot to be installed.
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
        """Validate the bot token via getMe — sends NOTHING.

        Gate-style verification for the stolen-Hermes-token path: proves
        the token is live without delivering a single message. Works
        without python-telegram-bot (raw HTTP)."""
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
                    # No owner chat set: warn that owner-only inbound filtering is off.
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

        With a real ptb Application this initializes and starts polling;
        with an injected fake application (tests) it just registers the raw
        callback the fake can drive directly — the pure seam, no network.

        ``on_command`` is the seam-S3 command callback: when given, a
        command handler is registered too and slash-commands are routed to
        it (never to ``on_message``). When None (default), commands are
        dropped — matching today's ``filters.TEXT & ~filters.COMMAND``
        registration, so the live bot is unchanged.
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
                # Route every slash-command to _on_command_update;
                # filters.COMMAND also handles unknown command names.
                app.add_handler(
                    MessageHandler(filters.COMMAND, self._on_command_update)
                )
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
        else:
            # Injected fake application (tests): record the raw callbacks.
            app.add_handler(self._on_update)
            if on_command is not None:
                app.add_handler(self._on_command_update)
        if on_command is not None:
            # Register the user-facing command menu when commands are enabled.
            await self._register_commands()

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

    @asynccontextmanager
    async def typing_context(self):
        """S4 typing capability: keep the Telegram "typing" indicator alive
        while inside the context.

        Sends ``send_chat_action('typing')`` immediately on entry, then every
        ~4.5 s (the indicator expires after ~5 s). Flag-gated by
        HARNESS_TYPING (default OFF -> the context is a no-op, exactly like
        channels that do not expose the method at all — the runtime probes
        it with ``getattr(channel, 'typing_context', None)``). The runtime
        wraps generation + response_delay_s in this context (Wave 2).
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
        """Send a typing action, then refresh it every _TYPING_INTERVAL_S.

        A failed send stops the loop quietly: the indicator is cosmetic and
        the real delivery path (send()) reports errors loudly."""
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

        Deterministic debounce shutdown: any pending flush task is cancelled
        and buffered messages are DROPPED (never delivered). Shutdown must
        not invoke the session handler — the runtime finalizes the session
        before stopping the channel — and the owner can simply re-send after
        restart.
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
            # stop() first, then shutdown(): shutdown tears down the bot,
            # updater, and processors, and raises if the app is running.
            await app.stop()
            await app.shutdown()

    async def _on_update(self, update, context=None) -> None:
        """ptb update callback: wrap the update and forward it to the handler.

        Declared async so ptb (which supports coroutine callbacks) and
        injected fakes can both drive it directly. The ``context`` argument
        is optional: real ptb MessageHandler callbacks receive
        (update, context) — the fake applications used in tests call with a
        single argument.

        Commands NEVER become InboundMessage: they are dropped here (matching
        the ``filters.TEXT & ~filters.COMMAND`` registration) and routed only
        via the command handler registered by ``start(on_command=...)``. With
        HARNESS_DEBOUNCE enabled, owner text is buffered (AFTER the owner
        filter) and flushed as ONE merged message.
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
        """ptb command callback (seam S3): parse a :class:`ControlCommand`
        and hand it to the ``start(on_command=...)`` callback.

        Mirrors ``_on_update``'s signature so real ptb and injected fakes can
        drive it the same way. The owner filter applies to commands too. The
        debounce buffer is flushed FIRST (contract S3) so a command is never
        delayed by, or merged with, buffered chat text. Commands NEVER become
        InboundMessage.
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

        Lazy ptb import (optional dependency — the guarded-import pattern).
        ``/state`` is never included: mood internals in the user's view
        contaminate the perceptual read (standing decision); the handler
        stays dispatchable but is not user-visible.
        """
        from telegram import BotCommand  # lazy ptb (optional dep)

        return [
            BotCommand(command=name, description=desc)
            for name, desc in USER_COMMANDS
        ]

    async def _register_commands(self) -> None:
        """Register ``USER_COMMANDS`` via Telegram ``setMyCommands``.

        Runs at channel start ONLY when commands are enabled (``start()``
        was given ``on_command`` — the seam-S3 gate). Best-effort: a
        network failure logs a warning and the channel still starts — the
        menu is client UI; the command dispatch itself is unaffected.
        Fakes (tests) record the call on their bot.
        """
        if self._command_callback is None:
            return  # commands not enabled: nothing to register
        bot = getattr(self.application, "bot", None)
        setter = getattr(bot, "set_my_commands", None)
        if setter is None:
            return  # stub without a setter: nothing to register
        try:
            await setter(self._bot_commands())
        except Exception as exc:  # noqa: BLE001 - cosmetic; dispatch unaffected
            print(
                f"[telegram] WARNING: setMyCommands failed (the command menu "
                f"will not appear in the client): {exc}",
                flush=True,
            )

    def _wrap_update(self, update) -> InboundMessage | None:
        """Map a raw update to an InboundMessage, or None when it is not a
        text message from the owner (photos, stickers, strangers). Command
        text is wrapped too and dropped by _on_update — commands never
        become InboundMessage."""
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

    # --- Debounce machinery (HARNESS_DEBOUNCE, default off) ---

    def _buffer_text(self, text: str, sender_id: str | None) -> None:
        """Buffer one owner text message. Creates the single flush task on
        the first buffered message; the task re-arms itself for later
        arrivals, so at most ONE flush task exists at any time."""
        if self._buffer_first_at is None:
            self._buffer_first_at = self._monotonic()
        self._last_arrival_at = self._monotonic()
        self._buffer.append((text, sender_id))
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._debounce_loop())

    async def _debounce_loop(self) -> None:
        """Trailing-edge debounce with a hard max-wait cap.

        Waits min(trailing-edge remaining, max-wait remaining), then
        re-checks: a new arrival during the wait extends the trailing edge,
        while the cap is measured from the FIRST buffered message. Flushes
        when either window is exhausted, then loops if a message arrived
        during the flush. Never sleeps past the cap: ``wait`` is always
        <= max-wait remaining, so the cap deadline is a wake point.
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

        Contract S3: a command arrival flushes the buffer first. Cancels the
        pending flush task (if any), then delivers the buffer synchronously.
        """
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self._flush()
