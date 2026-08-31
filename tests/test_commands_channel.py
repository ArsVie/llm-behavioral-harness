"""End-to-end command tests (Wave 2, W-commands): channel seam -> run_async wiring.

Drives real command updates through the TelegramChannel's S3 command seam
using the SHARED FakeApplication from tests/test_channel_telegram.py
(consumed UNMODIFIED — its ``command_update()`` injection builds the stub
update the registered command handler receives) and the launcher-side
callback factory from sim/run_async.py. Covers:

- every command's happy path end-to-end (update -> ControlCommand ->
  handle_command -> reply -> channel.send -> bot.calls);
- /setup refusal after a persona row exists;
- /mute defer semantics (hook records, schedule rows untouched);
- command-flag-off parity: on_command=None means commands are dropped,
  matching today's behavior;
- the CommandBridgeChannel launcher wiring (injects on_command into
  Channel.start; a runtime's own callback wins).

The unit-level semantics tests live in tests/test_commands_core.py.
"""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

telegram = pytest.importorskip("telegram")  # noqa: F401 - shared with the channel tests

from tests.helpers import FakeApplication  # noqa: E402 - shared fake, unmodified

from engine.types import PersonaParams, TimingParams  # noqa: E402
from harness.channels.base import OutboundMessage  # noqa: E402
from harness.channels.telegram import TelegramChannel  # noqa: E402
from harness.clock import VirtualClock  # noqa: E402
from harness.scheduler import ProactiveSchedule  # noqa: E402
from harness.store import SQLiteStore  # noqa: E402
from sim.run_async import (  # noqa: E402
    CommandBridgeChannel,
    build_command_callback,
    resolve_tz,
)
from zoneinfo import ZoneInfoNotFoundError  # noqa: E402


def _run(coro) -> None:
    asyncio.run(coro)


def _store(tmp_path, name="e2e.db"):
    return SQLiteStore(tmp_path / name)


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
        ("/mute 4", "deferred, never consumed"),
    ],
)
def test_command_happy_paths_end_to_end(tmp_path, command_text, expect) -> None:
    store = _store(tmp_path)
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
    store = _store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    assert store.load_persona() is None
    args = argparse.Namespace(
        seed=5001, user_name=None, user_interests=None,
    )
    from harness.bootstrap import OnboardingConfig, ensure_companion_initialized
    from sim.run_async import _make_request_setup, _restore_or_plan

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
    store = _store(tmp_path)
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


def test_tz_records_the_pending_change_end_to_end(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, hooks = _defaults(store, clock)
    cb = build_command_callback(store, clock, channel=channel, **kw)

    async def scenario() -> None:
        await _started(channel, cb)
        await app.handlers[1](app.command_update("/tz UTC", 42))

    _run(scenario())
    assert hooks["tz"] == ["UTC"]
    assert "next rollover" in app.bot.calls[-1]["text"]


def test_state_is_gated_even_end_to_end(tmp_path) -> None:
    store = _store(tmp_path)
    clock = VirtualClock(t_h=8.0)
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    kw, _ = _defaults(store, clock)  # flags={"debug": False}
    cb = build_command_callback(store, clock, channel=channel, **kw)

    async def scenario() -> None:
        await _started(channel, cb)
        await app.handlers[1](app.command_update("/state", 42))

    _run(scenario())
    assert "disabled" in app.bot.calls[-1]["text"]


# command-flag-off parity (on_command=None -> commands dropped, matching today)


def test_flag_off_parity_commands_are_dropped(tmp_path) -> None:
    """start(on_message=...) without on_command: no command handler is
    registered and command updates reach nobody — today's behavior."""
    store = _store(tmp_path)
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []

    async def _on_message(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(_on_message)  # on_command defaults to None
        assert len(app.handlers) == 1  # only the text handler registered
        # A command update routed anywhere produces nothing:
        await app.handlers[0](app.command_update("/ping", 42))
        await app.handlers[0](app.command_update("/setup", 42))

    _run(scenario())
    assert app.bot.calls == []  # no replies were ever sent
    assert received == []  # and commands never became InboundMessage


def test_flag_off_parity_plain_text_still_flows(tmp_path) -> None:
    """Without on_command the text path is untouched (parity with today)."""
    app = FakeApplication()
    channel = TelegramChannel(application=app, owner_chat_id="42")
    received = []

    async def _on_message(msg):
        received.append(msg)

    async def scenario() -> None:
        await channel.start(_on_message)
        await app.handlers[0](app.command_update("hello", 42))

    _run(scenario())
    assert len(received) == 1
    assert received[0].text == "hello"


# CommandBridgeChannel — launcher wiring for Channel.start


def test_bridge_injects_the_launcher_callback_into_start(tmp_path) -> None:
    store = _store(tmp_path)
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
    """When the runtime passes its own on_command, it supersedes the
    launcher's — no double wiring after W-runtime merges."""
    store = _store(tmp_path)
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


def test_resolve_tz_defaults_to_no_anchor() -> None:
    assert resolve_tz(None, env={}) == (None, None)
    assert resolve_tz("", env={"HARNESS_TZ": ""}) == (None, None)


def test_resolve_tz_flag_wins_over_env() -> None:
    name, anchor = resolve_tz("UTC", env={"HARNESS_TZ": "America/Mexico_City"})
    assert name == "UTC"
    assert anchor is not None and anchor.tz == "UTC"


def test_resolve_tz_env_fallback() -> None:
    name, anchor = resolve_tz(None, env={"HARNESS_TZ": "Europe/Madrid"})
    assert name == "Europe/Madrid"
    assert anchor is not None and anchor.tz == "Europe/Madrid"


def test_resolve_tz_bad_name_raises() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        resolve_tz("Not/AZone", env={})


def test_resolve_tz_anchor_maps_wall_clock_to_virtual_hours() -> None:
    import time

    name, anchor = resolve_tz("UTC", env={})
    now = time.time()
    # Round-trip through the anchor (S2 math).
    assert abs(anchor.t_h_at(anchor.epoch_of(now)) - now) < 1e-6


# CLI-level --defer-bootstrap behavior (real subprocess runs, accelerated)


def _run_async_cli(tmp_path, db, *extra_args, timeout=120):
    venv_python = Path(sys.executable)
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            str(venv_python), "-m", "sim.run_async",
            "--fake", "--store", str(db), "--days", "1",
            "--time-scale", "0.001", "--channel", "fake",
            *extra_args,
        ],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc


def test_defer_bootstrap_keeps_blank_db_uninitialized(tmp_path) -> None:
    """--defer-bootstrap on a blank DB: the run completes and the DB stays
    blank (only /setup initializes)."""
    db = tmp_path / "defer.db"
    proc = _run_async_cli(tmp_path, db, "--defer-bootstrap")
    assert "defer-bootstrap" in proc.stdout
    store = SQLiteStore(db)
    assert store.load_persona() is None  # untouched by the run itself
    store.close()


def test_default_bootstrap_is_unconditional(tmp_path) -> None:
    """Without --defer-bootstrap a blank DB is initialized at startup
    (CLI unchanged for existing invocations)."""
    db = tmp_path / "default.db"
    _run_async_cli(tmp_path, db)
    store = SQLiteStore(db)
    assert store.load_persona() is not None
    store.close()


def test_defer_bootstrap_on_initialized_db_is_a_noop(tmp_path) -> None:
    """--defer-bootstrap with a persona already present behaves like the
    normal idempotent bootstrap (identity never regenerates)."""
    from harness.bootstrap import OnboardingConfig, ensure_companion_initialized

    db = tmp_path / "init.db"
    store = SQLiteStore(db)
    boot = ensure_companion_initialized(store, seed=5001, config=OnboardingConfig(), day=0)
    first_name = boot.persona.name
    store.close()
    proc = _run_async_cli(tmp_path, db, "--defer-bootstrap")
    assert "defer-bootstrap" not in proc.stdout  # normal bootstrap ran
    store = SQLiteStore(db)
    assert store.load_persona().name == first_name
    store.close()


def test_cli_accepts_the_new_flags_without_commands(tmp_path) -> None:
    """--enable-commands on the fake channel: launcher warning, no
    crash, default-inert behavior preserved for the plain path.

    NOTE: --tz is intentionally NOT passed here — after the W-runtime merge
    --tz builds a real-time anchor, and anchored mode is 1:1 real time (it
    must not be combined with --time-scale acceleration). --tz acceptance is
    covered by test_tz_flag_resolves_anchor (resolution layer) and by
    argparse parsing itself (the subprocess below proves flags parse).
    """
    db = tmp_path / "flags.db"
    proc = _run_async_cli(
        tmp_path, db, "--enable-commands",
    )
    # The fake channel has no command seam -> warning, commands dropped.
    assert "no command seam" in proc.stdout
    store = SQLiteStore(db)
    assert store.load_persona() is not None  # bootstrap unchanged
    store.close()


def test_tz_flag_resolves_anchor(tmp_path) -> None:
    """--tz acceptance at the resolution layer: explicit flag wins, absent
    everywhere -> (None, None) (today's behavior), bad IANA name raises.
    Anchored mode is 1:1 real time by design, so no accelerated subprocess
    may combine --tz with --time-scale (that combination times out)."""
    from sim.run_async import resolve_tz

    tz, anchor = resolve_tz("UTC", env={})
    assert tz == "UTC"
    assert anchor is not None
    assert anchor.tz == "UTC"

    tz, anchor = resolve_tz(None, env={})
    assert tz is None and anchor is None

    tz, anchor = resolve_tz("", env={"HARNESS_TZ": "America/Mexico_City"})
    assert tz == "America/Mexico_City"
    assert anchor is not None

    import zoneinfo

    try:
        resolve_tz("Not/AZone", env={})
    except zoneinfo.ZoneInfoNotFoundError:
        pass
    else:
        raise AssertionError("bad IANA name must raise ZoneInfoNotFoundError")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
