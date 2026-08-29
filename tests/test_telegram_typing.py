"""Typing capability (S4, HARNESS_TYPING) for the Telegram channel — Wave 1,
W-channel.

TelegramChannel.typing_context() sends periodic send_chat_action('typing')
while inside the context; the capability is duck-typed (the runtime probes
``getattr(channel, 'typing_context', None)``), so channels that lack the
method — CLI, FakeChannel — are a no-op. All waits are virtual (GateSleeper).
"""

import asyncio

from harness.channels.base import FakeChannel
from harness.channels.telegram import TelegramChannel
from tests.helpers import FakeApplication, GateSleeper, ManualClock, drain

TYPING_ACTION = {"chat_id": "42", "action": "typing"}


def make_channel(monkeypatch, *, typing=True):
    """Channel with the typing flag wired via env and virtual time."""
    monkeypatch.setenv("HARNESS_TYPING", "1" if typing else "0")
    app = FakeApplication()
    clock = ManualClock()
    sleeper = GateSleeper(clock)
    channel = TelegramChannel(
        application=app, owner_chat_id="42", sleeper=sleeper, monotonic=clock
    )
    return app, channel, sleeper


def test_typing_context_sends_periodic_chat_actions(monkeypatch) -> None:
    """Inside typing_context(), a 'typing' action is sent immediately on
    entry and refreshed every 4.5 s (the recorded waits)."""
    app, channel, sleeper = make_channel(monkeypatch)

    async def scenario() -> None:
        async with channel.typing_context():
            await drain()  # refresh task sends once, then parks
            assert app.bot.chat_actions == [TYPING_ACTION]
            sleeper.release()  # 4.5 s elapse -> refresh
            await drain()
            assert app.bot.chat_actions == [TYPING_ACTION, TYPING_ACTION]
        await drain()  # exit cancels the loop cleanly

    asyncio.run(scenario())
    assert sleeper.delays == [4.5, 4.5]


def test_typing_flag_off_is_noop(monkeypatch) -> None:
    """HARNESS_TYPING off (default): typing_context() sends nothing and waits
    nothing."""
    app, channel, sleeper = make_channel(monkeypatch, typing=False)

    async def scenario() -> None:
        async with channel.typing_context():
            await drain()

    asyncio.run(scenario())
    assert app.bot.chat_actions == []
    assert sleeper.delays == []


def test_typing_context_cleans_up_on_exit(monkeypatch) -> None:
    """Exiting the context cancels the refresh loop: no further actions are
    sent afterwards."""
    app, channel, sleeper = make_channel(monkeypatch)

    async def scenario() -> None:
        async with channel.typing_context():
            await drain()
            assert len(app.bot.chat_actions) == 1
        await drain()

    asyncio.run(scenario())
    assert len(app.bot.chat_actions) == 1
    assert sleeper.delays == [4.5]


def test_typing_duck_typing_contract(monkeypatch) -> None:
    """S4 probe: TelegramChannel exposes a callable typing_context; channels
    that lack it (Fake/CLI) yield None -> the runtime's getattr is a no-op."""
    app, channel, sleeper = make_channel(monkeypatch)
    assert callable(getattr(channel, "typing_context", None))
    assert getattr(FakeChannel(), "typing_context", None) is None


def test_typing_flag_wiring(monkeypatch) -> None:
    """HARNESS_TYPING=1 -> on; unset -> off (default)."""
    monkeypatch.setenv("HARNESS_TYPING", "1")
    assert (
        TelegramChannel(application=FakeApplication(), owner_chat_id="42")
        .typing_enabled
        is True
    )
    monkeypatch.delenv("HARNESS_TYPING", raising=False)
    assert (
        TelegramChannel(application=FakeApplication(), owner_chat_id="42")
        .typing_enabled
        is False
    )
