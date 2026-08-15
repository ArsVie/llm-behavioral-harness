"""Command seam S3 for the Telegram channel — Wave 1, W-channel.

Commands NEVER become InboundMessage: they reach the start(on_command=...)
callback as parsed ControlCommand values — and only when that callback was
given. Default None -> commands are dropped, matching the TEXT & ~COMMAND
registration (the live bot is unchanged).
"""

import asyncio

import pytest

from harness.channels.telegram import ControlCommand, TelegramChannel
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
