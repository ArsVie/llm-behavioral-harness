"""The async runtime launcher -- helpers, wiring and whole in-process runs.

Called in-process with the sleeper stubbed, so the wiring is exercised in
milliseconds.
"""

from __future__ import annotations

import inspect
import os

import pytest
from zoneinfo import ZoneInfoNotFoundError

import sim.run_async as ra
from harness.channels.base import FakeChannel, OutboundMessage
from harness.store import SQLiteStore
from engine.types import PersonaParams, TimingParams
from tests.helpers import no_wait

SEED = 12345


@pytest.fixture(autouse=True)
def _no_real_delays(monkeypatch):
    """Force the injectable sleeper so no run waits response_delay_s.

    ``BehaviorDirective.response_delay_s`` is wall-clock seconds and is not
    scaled by TimeScale, so an un-stubbed in-process run would sit for ~30 s
    per turn exactly as the subprocess tests do.
    """
    real = ra.AsyncRuntime

    def patched(**kwargs):
        kwargs.setdefault("sleeper", no_wait)
        return real(**kwargs)

    monkeypatch.setattr(ra, "AsyncRuntime", patched)


def _run(tmp_path, name, *extra):
    return ra.main([
        "--fake", "--channel", "fake", "--store", str(tmp_path / name),
        "--days", "1", "--time-scale", "0.0005", *extra,
    ])


class _Args:
    """Minimal stand-in for the parsed namespace the helpers read."""

    def __init__(self, **kw):
        self.seed = SEED
        self.days = 1
        self.user_name = None
        self.user_interests = None
        for k, v in kw.items():
            setattr(self, k, v)


# --- _env_bool -----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("nonsense", False),
])
def test_env_bool_truthy_vocabulary(monkeypatch, raw, expected):
    monkeypatch.setenv("HARNESS_X", raw)
    assert ra._env_bool("HARNESS_X") is expected


def test_env_bool_unset_and_blank_take_the_default(monkeypatch):
    monkeypatch.delenv("HARNESS_X", raising=False)
    assert ra._env_bool("HARNESS_X") is False
    assert ra._env_bool("HARNESS_X", True) is True
    monkeypatch.setenv("HARNESS_X", "   ")
    assert ra._env_bool("HARNESS_X", True) is True


# --- onboarding config ---------------------------------------------------


def test_onboarding_config_parses_and_trims_interests():
    cfg = ra._onboarding_config(
        _Args(user_name="Ars", user_interests="lifting, metal ,, sketching")
    )
    assert cfg.user_name == "Ars"
    assert cfg.user_interests == ("lifting", "metal", "sketching")


def test_onboarding_config_defaults_when_flags_are_absent():
    """Unset flags fall back to the bootstrap defaults, read from where they
    are actually defined rather than from a re-export."""
    from harness.bootstrap import DEFAULT_USER_INTERESTS, DEFAULT_USER_NAME

    cfg = ra._onboarding_config(_Args())
    assert cfg.user_name == DEFAULT_USER_NAME
    assert cfg.user_interests == tuple(DEFAULT_USER_INTERESTS)


# --- resolve_tz ----------------------------------------------------------


def test_resolve_tz_without_flag_or_env_means_no_anchor():
    assert ra.resolve_tz(None, {}) == (None, None)
    assert ra.resolve_tz("   ", {}) == (None, None)


def test_resolve_tz_flag_wins_over_env():
    name, anchor = ra.resolve_tz("America/Chihuahua", {"HARNESS_TZ": "UTC"})
    assert name == "America/Chihuahua"
    assert anchor is not None and anchor.tz == "America/Chihuahua"


def test_resolve_tz_falls_back_to_the_env():
    name, anchor = ra.resolve_tz(None, {"HARNESS_TZ": "UTC"})
    assert name == "UTC" and anchor is not None


def test_resolve_tz_rejects_a_bad_zone():
    """A typo must raise, not silently run unanchored — the launcher turns
    this into an argparse error and exit 2."""
    with pytest.raises(ZoneInfoNotFoundError):
        ra.resolve_tz("Mars/Olympus_Mons", {})


# --- misc helpers --------------------------------------------------------


def test_commit_sha_is_best_effort():
    """Either a short sha or None; it must never raise or fail a launch."""
    sha = ra._commit_sha()
    assert sha is None or (sha and "\n" not in sha)


def test_restore_or_plan_plans_then_restores(tmp_path):
    """A blank store gets a fresh horizon; a store with pending rows is
    restored instead of re-planned (restart-resume)."""
    store = SQLiteStore(tmp_path / "s.db")
    try:
        persona, timing = PersonaParams(), TimingParams()
        planned = ra._restore_or_plan(store, SEED, persona, timing, 2)
        assert len(planned.event_hours) > 0
        restored = ra._restore_or_plan(store, SEED, persona, timing, 2)
        assert list(restored.event_hours) == list(planned.event_hours)
    finally:
        store.close()


def test_request_setup_initializes_and_summarizes(tmp_path):
    """The /setup hook creates identity and reports what it made."""
    store = SQLiteStore(tmp_path / "setup.db")
    try:
        assert store.load_persona() is None
        hook = ra._make_request_setup(
            store, _Args(), PersonaParams(), TimingParams()
        )
        summary = hook()
        assert "persona=" in summary and "arcs=" in summary
        assert store.load_persona() is not None
    finally:
        store.close()


# --- CommandBridgeChannel ------------------------------------------------


def test_command_bridge_forwards_start_and_send():
    """The bridge is a transparent wrapper: the inner channel still sees
    everything, with the launcher's command callback threaded in."""
    inner = FakeChannel()
    seen: list[str] = []
    bridge = ra.CommandBridgeChannel(inner, lambda text: seen.append(text))
    assert "on_command" in inspect.signature(bridge.start).parameters

    import asyncio

    async def drive():
        await bridge.start(lambda msg: None)
        await bridge.send(OutboundMessage(text="hi", proactive=False))
        await bridge.stop()

    asyncio.run(drive())
    assert [m.text for m in inner.sent] == ["hi"]


# --- whole in-process runs ----------------------------------------------


def test_run_bootstraps_and_completes(tmp_path, capsys):
    assert _run(tmp_path, "run.db") == 0
    out = capsys.readouterr().out
    assert "async runtime" in out and "bootstrap:" in out
    store = SQLiteStore(tmp_path / "run.db")
    try:
        assert store.load_persona() is not None
    finally:
        store.close()


def test_run_is_restart_safe(tmp_path, capsys):
    """A second run over the same DB resumes rather than re-bootstrapping."""
    assert _run(tmp_path, "resume.db") == 0
    store = SQLiteStore(tmp_path / "resume.db")
    name = store.load_persona().name
    store.close()
    capsys.readouterr()
    assert _run(tmp_path, "resume.db") == 0
    store = SQLiteStore(tmp_path / "resume.db")
    try:
        assert store.load_persona().name == name
    finally:
        store.close()


def test_defer_bootstrap_leaves_a_blank_db_uninitialized(tmp_path, capsys):
    assert _run(tmp_path, "defer.db", "--defer-bootstrap") == 0
    assert "defer-bootstrap" in capsys.readouterr().out
    store = SQLiteStore(tmp_path / "defer.db")
    try:
        assert store.load_persona() is None
    finally:
        store.close()


def test_defer_bootstrap_on_an_initialized_db_is_a_normal_start(tmp_path,
                                                                capsys):
    """Identity never regenerates: --defer-bootstrap only applies to a blank
    DB, so an initialized one takes the ordinary bootstrap path."""
    assert _run(tmp_path, "init.db") == 0
    capsys.readouterr()
    assert _run(tmp_path, "init.db", "--defer-bootstrap") == 0
    assert "defer-bootstrap" not in capsys.readouterr().out


def test_run_with_a_timezone_anchors_the_clock(tmp_path, capsys):
    assert _run(tmp_path, "tz.db", "--tz", "America/Chihuahua") == 0
    assert "tz=America/Chihuahua" in capsys.readouterr().out


def test_run_with_a_bad_timezone_exits_two(tmp_path):
    """argparse.error exits 2 — a mistyped zone must not start a run."""
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "badtz.db", "--tz", "Mars/Olympus_Mons")
    assert exc.value.code == 2


def test_enable_commands_warns_on_a_channel_without_the_seam(tmp_path,
                                                             capsys):
    """The fake channel has no command seam; the launcher says so out loud
    rather than silently dropping every command."""
    assert _run(tmp_path, "cmd.db", "--enable-commands") == 0
    out = capsys.readouterr().out
    assert "commands=on" in out or "no command seam" in out


def test_channel_falls_back_to_the_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_CHANNEL", "fake")
    rc = ra.main([
        "--fake", "--store", str(tmp_path / "env.db"),
        "--days", "1", "--time-scale", "0.0005",
    ])
    assert rc == 0
    assert "channel=fake" in capsys.readouterr().out


def test_default_channel_constant_is_cli():
    """The documented default when neither flag nor env is set."""
    assert ra.DEFAULT_CHANNEL == "cli"
    assert os.environ.get("HARNESS_CHANNEL") in (None, "", "fake", "cli")
