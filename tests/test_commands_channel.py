"""End-to-end command tests: channel seam -> run_async wiring.

Commands drive the TelegramChannel seam with the shared FakeApplication.
"""

import argparse
import asyncio
from typing import Any

import pytest

telegram = pytest.importorskip("telegram")  # noqa: F401 - shared with the channel tests

from tests.helpers import FakeApplication, make_store  # noqa: E402 - shared fake, unmodified

from engine.types import PersonaParams, TimingParams  # noqa: E402
from harness.channels.base import OutboundMessage  # noqa: E402
from harness.channels.telegram import TelegramChannel  # noqa: E402
from harness.clock import VirtualClock  # noqa: E402
from harness.scheduler import ProactiveSchedule  # noqa: E402
from sim.run_async import (  # noqa: E402
    CommandBridgeChannel,
    build_command_callback,
    resolve_tz,
)


def _run(coro) -> None:
    asyncio.run(coro)


def _defaults(store, clock, **overrides):
    """Kwargs for build_command_callback with recorder hooks."""
    hooks: dict[str, list] = {"tz": [], "mute": [], "setup": []}
    kw: dict[str, Any] = dict(
        seed=5001,
        anchor=None,
        flags={"debug": False},
        commit_sha="deadbee",
        request_tz_change=lambda name: hooks["tz"].append(name),
        request_mute=lambda hours: hooks["mute"].append(hours),
        request_setup=lambda: hooks["setup"].append(True) or "persona=Test",
    )
    kw.update(overrides)
    return kw, hooks


async def _started(channel, on_command, on_message=None):
    """Start the channel with the S3 command handler and return it."""
    received = []

    async def _on_message(msg):
        received.append(msg)

    handler = on_message if on_message is not None else _on_message
    await channel.start(handler, on_command=on_command)
    return received


# happy paths through the FakeApplication


@pytest.mark.parametrize(
    "command_text,expect",
    [
        ("/ping", "pong"),
        ("/help", "/setup"),
        ("/status", "day 0"),
        ("/version", "deadbee"),
        ("/tz America/Mexico_City", "next rollover"),
    ],
)
def test_command_happy_paths_end_to_end(tmp_path, command_text, expect) -> None:
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, _ = _defaults(store, clock)
    cb = build_command_callback(store, clock, channel=channel, **kw)

    async def scenario() -> None:
        received = await _started(channel, cb)
        await app.handlers[1](app.command_update(command_text, 42))
        assert received == []  # commands NEVER become InboundMessage

    _run(scenario())
    assert len(app.bot.calls) == 1
    assert expect in app.bot.calls[0]["text"]
    assert app.bot.calls[0]["chat_id"] == "42"


def test_setup_end_to_end_initializes_the_blank_db(tmp_path) -> None:
    """/setup on a blank DB runs the hook and the refusal follows."""
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    assert store.load_persona() is None
    args = argparse.Namespace(
        seed=5001, user_name=None, user_interests=None,
    )
    from harness.bootstrap import OnboardingConfig, ensure_companion_initialized
    from sim.run_async import _restore_or_plan

    persona = PersonaParams()
    timing = TimingParams()

    def _request_setup() -> str:
        boot = ensure_companion_initialized(
            store, seed=args.seed, config=OnboardingConfig(), day=0
        )
        _restore_or_plan(store, args.seed, persona, timing, 60)
        agenda = boot.today_agenda
        return (
            f"persona={boot.persona.name} interests={len(boot.persona.interests)} "
            f"arcs={len(boot.life_arcs)} "
            f"agenda[0]={len(agenda.items) if agenda else 0}"
        )

    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, _ = _defaults(store, clock, request_setup=_request_setup)
    cb = build_command_callback(store, clock, channel=channel, **kw)

    async def scenario() -> None:
        await _started(channel, cb)
        await app.handlers[1](app.command_update("/setup", 42))
        # Second /setup must REFUSE (persona row now exists).
        await app.handlers[1](app.command_update("/setup", 42))

    _run(scenario())
    assert store.load_persona() is not None
    assert store.pending_schedule_events(5001)  # schedule horizon planned
    assert len(app.bot.calls) == 2
    assert app.bot.calls[0]["text"].startswith("setup complete")
    assert "refused" in app.bot.calls[1]["text"]


def test_mute_defer_semantics_end_to_end(tmp_path) -> None:
    """/mute records the deferral; pending schedule rows are never consumed."""
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    persona = PersonaParams()
    timing = TimingParams()
    ProactiveSchedule.plan_and_persist(3, 5001, persona, timing, store)
    pending_before = len(store.pending_schedule_events(5001))
    assert pending_before > 0

    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, hooks = _defaults(store, clock)
    cb = build_command_callback(store, clock, channel=channel, **kw)

    async def scenario() -> None:
        await _started(channel, cb)
        await app.handlers[1](app.command_update("/mute 4", 42))

    _run(scenario())
    assert hooks["mute"] == [4.0]  # the defer request, hours parsed
    assert len(store.pending_schedule_events(5001)) == pending_before  # untouched
    assert app.bot.calls[-1]["text"].startswith("proactive messages muted for 4 h")


# CommandBridgeChannel — launcher wiring for Channel.start


def test_bridge_injects_the_launcher_callback_into_start(tmp_path) -> None:
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, hooks = _defaults(store, clock)
    launcher_cb = build_command_callback(store, clock, channel=channel, **kw)
    bridge = CommandBridgeChannel(channel, launcher_cb)

    async def scenario() -> None:
        await bridge.start(lambda msg: None)  # the runtime's plain call
        assert len(app.handlers) == 2  # text + command handlers registered
        await app.handlers[1](app.command_update("/ping", 42))

    _run(scenario())
    assert app.bot.calls[-1]["text"].startswith("pong")
    assert hooks["setup"] == []  # unrelated hooks untouched


def test_bridge_runtimes_own_callback_wins(tmp_path) -> None:
    """When the runtime passes its own on_command, it supersedes the launcher's."""
    store = make_store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, hooks = _defaults(store, clock)
    launcher_cb = build_command_callback(store, clock, channel=channel, **kw)
    bridge = CommandBridgeChannel(channel, launcher_cb)
    runtime_calls = []

    async def runtime_cb(cmd):
        runtime_calls.append(cmd.name)

    async def scenario() -> None:
        await bridge.start(lambda msg: None, on_command=runtime_cb)
        await app.handlers[1](app.command_update("/ping", 42))

    _run(scenario())
    assert runtime_calls == ["ping"]
    assert app.bot.calls == []  # the launcher callback never ran


def test_bridge_delegates_everything_else(tmp_path) -> None:
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    bridge = CommandBridgeChannel(channel, lambda cmd: None)
    assert bridge.name == "telegram"  # __getattr__ delegation

    async def scenario() -> None:
        await bridge.send(OutboundMessage(text="hi"))
        await bridge.stop()

    _run(scenario())
    assert app.bot.calls == [{"chat_id": "42", "text": "hi"}]


def test_bridge_skips_channels_without_the_command_seam(tmp_path) -> None:
    """CLIChannel.start(on_message) has no on_command: the bridge degrades
    to the plain signature instead of crashing (commands dropped there)."""
    from harness.channels.cli import CLIChannel

    calls = []

    class _CLI(CLIChannel):
        async def start(self, on_message):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    bridge = CommandBridgeChannel(_CLI(), lambda cmd: None)

    async def scenario() -> None:
        await bridge.start(lambda msg: None)
        await bridge.stop()

    _run(scenario())
    assert calls == ["start", "stop"]


# launcher flag resolution


def test_resolve_tz_anchor_maps_wall_clock_to_virtual_hours() -> None:
    import time

    name, anchor = resolve_tz("UTC", env={})
    now = time.time()
    # Round-trip through the anchor.
    assert abs(anchor.t_h_at(anchor.epoch_of(now)) - now) < 1e-6
