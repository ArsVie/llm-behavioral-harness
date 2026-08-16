"""Command seam S3 for the Telegram channel — Wave 1, W-channel.

Commands NEVER become InboundMessage: they reach the start(on_command=...)
callback as parsed ControlCommand values — and only when that callback was
given. Default None -> commands are dropped, matching the TEXT & ~COMMAND
registration (the live bot is unchanged).
"""

import asyncio

import pytest

from harness.channels.telegram import (
    USER_COMMANDS,
    ControlCommand,
    TelegramChannel,
)
from test_channel_telegram import FakeApplication


def test_command_routes_to_on_command_callback() -> None:
    """With on_command set, the fake application gets TWO handlers; driving
    the command handler delivers a parsed ControlCommand — and NEVER an
    InboundMessage to on_message."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []
    commands = []

    async def handler(msg):
        received.append(msg)

    async def on_command(cmd):
        commands.append(cmd)

    async def scenario() -> None:
        await channel.start(handler, on_command=on_command)
        assert len(app.handlers) == 2
        await app.handlers[1](app.command_update("/ping extra args", 42))

    asyncio.run(scenario())
    assert received == []
    assert commands == [
        ControlCommand(name="ping", args="extra args", sender_id=42)
    ]


def test_command_handler_not_registered_without_callback() -> None:
    """on_command=None (default): only the text handler is registered and a
    command update is dropped — commands never become InboundMessage."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []

    async def handler(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(handler)  # no on_command
        assert len(app.handlers) == 1
        await app.handlers[0](app.command_update("/ping", 42))

    asyncio.run(scenario())
    assert received == []


def test_command_name_strips_bot_suffix() -> None:
    """/tz@Lily_Vie_bot parses to name 'tz' (Telegram appends the bot
    username to commands)."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    commands = []

    async def on_command(cmd):
        commands.append(cmd)

    async def scenario() -> None:
        await channel.start(lambda msg: None, on_command=on_command)
        await app.handlers[1](
            app.command_update("/tz@Lily_Vie_bot America/New_York", 42)
        )

    asyncio.run(scenario())
    assert commands == [
        ControlCommand(name="tz", args="America/New_York", sender_id=42)
    ]


def test_command_from_non_owner_is_dropped() -> None:
    """The owner filter applies to commands too: a stranger's /command is
    never routed."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    commands = []

    async def on_command(cmd):
        commands.append(cmd)

    async def scenario() -> None:
        await channel.start(lambda msg: None, on_command=on_command)
        await app.handlers[1](app.command_update("/ping", 999))

    asyncio.run(scenario())
    assert commands == []


def test_control_command_is_frozen_with_slashless_name() -> None:
    """ControlCommand is a frozen dataclass and its name carries no leading
    slash (seam S3 shape)."""
    cmd = ControlCommand(name="status", args="", sender_id=42)
    assert cmd.name == "status"
    assert cmd.args == ""
    assert cmd.sender_id == 42
    with pytest.raises(AttributeError):
        setattr(cmd, "name", "muted")


# --------------------------------------------------------------------------- #
# setMyCommands (WS-A): the user-facing command menu registers only when
# commands are enabled; /state is never registered.
# --------------------------------------------------------------------------- #


def test_set_my_commands_registered_when_commands_enabled() -> None:
    """start(on_message, on_command=...) registers the user-facing menu via
    setMyCommands on the bot (fake records the ptb BotCommand list)."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.start(lambda msg: None, on_command=lambda cmd: None)

    asyncio.run(scenario())
    assert app.bot.registered_commands is not None
    names = [c.command for c in app.bot.registered_commands]
    assert names == ["help", "ping", "setup", "tz", "status", "mute", "version"]


def test_set_my_commands_never_registers_state() -> None:
    """The standing decision: /state is NOT user-visible — it is absent from
    both the registered menu and the USER_COMMANDS contract itself."""
    names = [c for c, _ in USER_COMMANDS]
    assert "state" not in names
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.start(lambda msg: None, on_command=lambda cmd: None)

    asyncio.run(scenario())
    registered = [c.command for c in app.bot.registered_commands]
    assert "state" not in registered
    # ... while the dispatch table still handles it (debug-gated handler).
    from harness.commands import _COMMANDS  # noqa: PLC2701 - contract check

    assert "state" in _COMMANDS


def test_set_my_commands_not_registered_when_commands_disabled() -> None:
    """start(on_message) without on_command (default): NO registration —
    matching today's behavior, the live bot unchanged until the flag flips."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")

    async def scenario() -> None:
        await channel.start(lambda msg: None)

    asyncio.run(scenario())
    assert app.bot.registered_commands is None


def test_user_commands_are_documented_with_descriptions() -> None:
    """Every registered command has a non-empty description (the client menu
    renders it) and the set is exactly the plan's list."""
    assert [c for c, _ in USER_COMMANDS] == [
        "help", "ping", "setup", "tz", "status", "mute", "version",
    ]
    assert all(desc.strip() for _, desc in USER_COMMANDS)
