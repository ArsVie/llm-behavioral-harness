"""Hardening for the week-long live trial (review 2026-09-02).

Four separate ways the live entry could not have survived a week, each
pinned here:

* a failed turn ending the whole run (``survive_turn_failures``),
* a stale real-time anchor manufacturing days on resume
  (``check_resume_gap``),
* the owner and the companion both being ablation-matrix fixtures
  (``owner_profile`` / ``rename_companion``),
* an fsync-per-commit default with no way to say otherwise
  (``sqlite_synchronous``).

Convención del repo: docstrings en español para experiments/, inglés aquí
(el resto de tests/ está en inglés); identificadores siempre en inglés.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from engine.types import PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor, anchor_for_fresh_start
from harness.channels.base import FakeChannel, OutboundMessage
from harness.clock import VirtualClock
from harness.domain import UserProfile
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from harness.store import SQLiteStore, sqlite_synchronous

import engine.rng as rng_mod
from experiments.live_companion import (
    MAX_RESUME_GAP_DAYS,
    build_store,
    bootstrap,
    check_resume_gap,
    owner_profile,
    rename_companion,
)
from tests.helpers import ground_agenda, make_session, no_wait

PERSONA = PersonaParams()
TIMING = TimingParams()
SEED = 12345
FAST = TimeScale(seconds_per_virtual_hour=0.002)


class BoomChannel(FakeChannel):
    """Channel whose proactive delivery always fails.

    Stands in for the real failure this guards: the LLM call inside the turn
    raising after the client exhausts its retries. Delivery is the easiest
    place to inject it and takes the same path out of ``_firing_loop``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send(self, message: OutboundMessage) -> None:
        if message.proactive:
            self.attempts += 1
            raise RuntimeError("send exploded")
        await super().send(message)


def _armed_run(tmp_path, name: str, *, survive: bool):
    """Run the runtime over one planned proactive event on a BoomChannel."""
    store = build_store(tmp_path / name)
    schedule = ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    hour = next(float(h) for h in schedule.event_hours if h < 20.0)
    ground_agenda(store, hour - 0.5, hour + 0.5)
    session = make_session(store, clock=VirtualClock())
    channel = BoomChannel()
    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=hour + 2.0,
        resolver=IntentResolver(store, rng=rng_mod.stream_rng(SEED)),
        sleeper=no_wait,
        survive_turn_failures=survive,
    )
    return store, channel, runtime


# --- turn-failure policy -------------------------------------------------


def test_failed_proactive_turn_ends_an_experiment_cell(tmp_path):
    """Default policy is fail-fast, and stays that way.

    A bounded ablation cell that kept running after a broken turn would
    report a corrupt result as a clean one, so the exception must reach the
    caller. This pins the pre-existing contract that the live policy opts
    out of, NOT a new behaviour.
    """
    store, channel, runtime = _armed_run(tmp_path, "cell.db", survive=False)
    try:
        with pytest.raises(RuntimeError, match="send exploded"):
            asyncio.run(runtime.run())
        assert channel.attempts >= 1
    finally:
        store.close()


def test_failed_proactive_turn_does_not_end_a_live_run(tmp_path):
    """With the live policy the run completes, and the failure is on record.

    Before this, one exception unwound ``_firing_loop`` through the
    ``asyncio.gather`` in ``run()`` and the process exited — a week-long
    trial ended by a single bad provider response.
    """
    store, channel, runtime = _armed_run(tmp_path, "live.db", survive=True)
    try:
        asyncio.run(runtime.run())  # completes instead of raising
        assert channel.attempts >= 1
        failures = [e for e in store.events_since(0)
                    if e["event"] == "proactive_failed"]
        assert failures, "the failure must be recorded, not swallowed"
        assert "RuntimeError" in failures[0]["detail"]
    finally:
        store.close()


def test_failed_proactive_turn_consumes_its_schedule_row(tmp_path):
    """A failed event is consumed, never left pending.

    An overdue pending row is re-evaluated on the next pass, so leaving it
    would spin the firing loop against a provider that is already failing.
    One proactive message is lost; the run and the provider are not.
    """
    store, _channel, runtime = _armed_run(tmp_path, "consume.db", survive=True)
    try:
        asyncio.run(runtime.run())
        assert store.pending_schedule_events(SEED) == []
    finally:
        store.close()


def test_failed_reactive_turn_does_not_end_a_live_run(tmp_path):
    """An inbound turn that raises is logged and the runtime stays up.

    python-telegram-bot swallows handler exceptions, so before this the user
    got silence and the operator got nothing at all.
    """
    store = build_store(tmp_path / "reactive.db")

    class Boom:
        supports_json = True
        supports_tools = False
        model = "boom"

        def chat(self, *a, **k):
            raise RuntimeError("provider down")

        def chat_with_meta(self, *a, **k):
            raise RuntimeError("provider down")

    session = make_session(store, clock=VirtualClock(), client=Boom())
    channel = FakeChannel()
    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=1.0,
        sleeper=no_wait, survive_turn_failures=True,
    )

    async def driver():
        feed = asyncio.create_task(channel.feed("hi", t_h=0.5))
        try:
            await runtime.run()
        finally:
            if not feed.done():
                feed.cancel()

    try:
        asyncio.run(driver())  # completes instead of raising
        failed = [e for e in store.events_since(0) if e["event"] == "reply_failed"]
        assert failed and "RuntimeError" in failed[0]["detail"]
    finally:
        store.close()


# --- stale-resume guard --------------------------------------------------


def test_fresh_anchor_resume_is_allowed(tmp_path):
    """An anchor drawn now maps to the day the store is already on."""
    store = build_store(tmp_path / "fresh.db")
    try:
        bootstrap(store, SEED)
        now = 1_788_000_000.0
        anchor = anchor_for_fresh_start(now, "America/Chihuahua")
        assert check_resume_gap(store, anchor, now) is None
    finally:
        store.close()


def test_stale_anchor_resume_is_refused(tmp_path):
    """An anchor parked for weeks is refused, naming both days.

    ``Session.ensure_day`` rolls every missing day forward at the next
    midnight, finalising each through the judge — so resuming here would
    start the trial on top of days that never happened.
    """
    store = build_store(tmp_path / "stale.db")
    try:
        bootstrap(store, SEED)
        now = 1_788_000_000.0
        stale = RealTimeAnchor(
            epoch0_s=now - 18 * 86400.0, t_h0=1.0, tz="America/Chihuahua"
        )
        problem = check_resume_gap(store, stale, now)
        assert problem is not None
        assert "day 18" in problem or "18.0" in problem
        assert "--accept-resume-gap" in problem
    finally:
        store.close()


def test_resume_gap_threshold_is_the_documented_one(tmp_path):
    """Just inside the allowance passes; just outside it does not."""
    store = build_store(tmp_path / "edge.db")
    try:
        bootstrap(store, SEED)
        now = 1_788_000_000.0
        inside = RealTimeAnchor(now - (MAX_RESUME_GAP_DAYS * 0.5) * 86400.0,
                                0.0, "UTC")
        outside = RealTimeAnchor(now - (MAX_RESUME_GAP_DAYS + 1.0) * 86400.0,
                                 0.0, "UTC")
        assert check_resume_gap(store, inside, now) is None
        assert check_resume_gap(store, outside, now) is not None
    finally:
        store.close()


# --- owner and companion identity ---------------------------------------


def test_owner_profile_reads_the_environment(monkeypatch):
    """The owner's real name and interests replace the matrix fixture."""
    monkeypatch.setenv("LILY_OWNER_NAME", "Ars")
    monkeypatch.setenv("LILY_OWNER_INTERESTS", "lifting, sketching , metal,")
    profile = owner_profile()
    assert profile == UserProfile(
        name="Ars", interests=("lifting", "sketching", "metal")
    )


def test_owner_profile_falls_back_without_the_environment(monkeypatch):
    """Unset env keeps today's fixture rather than inventing an identity."""
    monkeypatch.delenv("LILY_OWNER_NAME", raising=False)
    monkeypatch.delenv("LILY_OWNER_INTERESTS", raising=False)
    profile = owner_profile()
    assert profile.name == "User"
    assert profile.interests  # the matrix fixture, not empty


def test_bootstrap_names_the_companion_and_rewrites_the_core(tmp_path,
                                                             monkeypatch):
    """The persona name reaches the prose core, not just the row.

    ``build_persona`` hard-codes "Nova" and writes it into the core ("You
    are Nova, ..."), so renaming only the column would leave her
    introducing herself by the old name on turn one.
    """
    monkeypatch.setenv("LILY_COMPANION_NAME", "Lily")
    monkeypatch.setenv("LILY_OWNER_NAME", "Ars")
    monkeypatch.setenv("LILY_OWNER_INTERESTS", "lifting,sketching")
    store = build_store(tmp_path / "identity.db")
    try:
        bootstrap(store, SEED)
        persona = store.load_persona()
        assert persona.name == "Lily"
        assert "You are Lily" in persona.core
        assert "Nova" not in persona.core
    finally:
        store.close()


def test_rename_companion_is_a_no_op_when_nothing_changes(tmp_path,
                                                          monkeypatch):
    """Renaming to the current name (or to nothing) touches no state."""
    monkeypatch.delenv("LILY_COMPANION_NAME", raising=False)
    store = build_store(tmp_path / "noop.db")
    try:
        bootstrap(store, SEED)
        current = store.load_persona().name
        assert rename_companion(store, current) is False
        assert rename_companion(store, "") is False
        assert store.load_persona().name == current
    finally:
        store.close()


# --- sqlite durability knob ---------------------------------------------


def test_sqlite_synchronous_defaults_to_full(monkeypatch):
    """Production durability is the SQLite default unless asked otherwise."""
    monkeypatch.delenv("HARNESS_SQLITE_SYNCHRONOUS", raising=False)
    assert sqlite_synchronous() == "FULL"


def test_sqlite_synchronous_reads_the_environment(monkeypatch):
    """The level is honoured case-insensitively and applied to the store."""
    monkeypatch.setenv("HARNESS_SQLITE_SYNCHRONOUS", "off")
    assert sqlite_synchronous() == "OFF"


def test_sqlite_synchronous_rejects_a_typo(monkeypatch):
    """A misspelt level raises rather than silently downgrading durability."""
    monkeypatch.setenv("HARNESS_SQLITE_SYNCHRONOUS", "NORMALL")
    with pytest.raises(ValueError, match="HARNESS_SQLITE_SYNCHRONOUS"):
        sqlite_synchronous()


def test_store_applies_the_configured_level(tmp_path, monkeypatch):
    """The pragma reaches the live connection, not just the helper."""
    monkeypatch.setenv("HARNESS_SQLITE_SYNCHRONOUS", "OFF")
    store = SQLiteStore(tmp_path / "pragma.db")
    try:
        level = store.conn.execute("PRAGMA synchronous").fetchone()[0]
        assert level == 0  # 0 = OFF, 2 = FULL
    finally:
        store.close()


# --- telegram handler errors --------------------------------------------


def test_telegram_handler_error_is_reported(caplog):
    """A handler exception is logged instead of vanishing.

    python-telegram-bot discards handler exceptions when no error handler is
    registered, which is why a failing turn used to look exactly like a user
    who simply got no reply.
    """
    from harness.channels.telegram import TelegramChannel

    channel = TelegramChannel(application=object(), owner_chat_id=1)

    class Ctx:
        error = RuntimeError("handler blew up")

    with caplog.at_level(logging.ERROR, logger="harness.channels.telegram"):
        asyncio.run(channel._on_handler_error(object(), Ctx()))
    assert "handler blew up" in caplog.text
