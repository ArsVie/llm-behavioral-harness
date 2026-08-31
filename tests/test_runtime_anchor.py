"""AsyncRuntime anchor-mode tests (Wave 2, worker W-runtime; seam S2/S3/S4).

Covers the anchor-mode additions to harness/runtime.py — ALL default-off:

- anchor=None parity: the accelerated path is byte-identical (paced REAL
  sleeps; the injectable sleeper stays reserved for response_delay_s, so
  latency traces are unchanged).
- absolute-sleep correctness: with an anchor, target sleeps are
  ``anchor.epoch_of(target) - now()`` — wall-clock-derived, ignoring
  TimeScale, and self-correcting (a late wake re-sleeps the residual).
- the resume fix: on startup with an anchor the clock resumes at the CURRENT
  real virtual hour (``anchor.t_h_at(now)``) — pinning "restart at real
  18:00 → local_hour ≈ 18" — and clock skew (persisted state already past
  the anchor's now) RAISES instead of guessing.
- S3 command dispatch: ControlCommand routes to harness.commands.
  handle_command under the runtime lock (lazy import — commands.py lands
  with W-commands AFTER this file), never session.on_message.
- S4 typing: generation + response_delay_s run inside the channel's
  typing_context() when the channel has one (duck-typed probe, no-op
  otherwise).
- the CommandContext narrow hooks: /tz applied at the next rollover (the
  epoch→t_h mapping never jumps), /mute defers (never consumes) pending
  events until the window ends.

Anchor-mode runs pace in REAL time, so every anchor test injects a
ManualClock wall-clock source + a sleeper that advances it — no test ever
waits real seconds.
"""

import asyncio
import sys
import types
from contextlib import asynccontextmanager

import pytest

import engine.rng as rng_mod
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor
from harness.channels.base import FakeChannel
from harness.channels.telegram import ControlCommand
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import DailyAgenda
from harness.judge import ScriptedJudge
from harness.proactive import IntentResolver
from harness.runtime import (
    ANCHOR_KV_KEYS,
    AsyncRuntime,
    TimeScale,
    load_anchor,
    persist_anchor,
)
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore
from tests.helpers import (
    AnchorManualClock,
    SeamStore,
    agenda_item,
    ground_agenda,
    make_session,
    make_store,
    rows,
    suppressed_codes,
)

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345


def _session(store=None, *, replies=None):
    """Local 3-tuple wrapper over the shared make_session (store, clock,
    session) — this file's call sites unpack all three and use the
    module's PERSONA/TIMING/VARIANT/SEED constants."""
    store = store if store is not None else SeamStore()
    clock = VirtualClock()
    session = make_session(
        store,
        clock=clock,
        client=FakeClient(responses=replies or ["ok!"]),
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
    )
    return store, clock, session

#: 1 virtual hour = 2 ms real (same as test_runtime.FAST).
FAST = TimeScale(seconds_per_virtual_hour=0.002)
#: Robust scale for gate races (same as test_runtime.SLOW).
SLOW = TimeScale(seconds_per_virtual_hour=0.5)

#: Fixed epoch the ManualClock starts at; anchors are built against it.
T0 = 1_000_000.0


class CommandAwareChannel(FakeChannel):
    """FakeChannel extended with the Wave-1 channel seams: the S3
    ``start(on_message, on_command=...)`` signature and an S4
    ``typing_context()`` that records enter/exit (mirrors the TelegramChannel
    surface the runtime probes)."""

    def __init__(self):
        super().__init__()
        self.command_handler = None
        self.typing_events: list[str] = []

    async def start(self, on_message, on_command=None):
        self.handler = on_message
        self.command_handler = on_command

    @asynccontextmanager
    async def typing_context(self):
        self.typing_events.append("enter")
        try:
            yield
        finally:
            self.typing_events.append("exit")


def _anchor_runtime(store, session, channel, *, anchor, manual, max_hours,
                    sleeper=None, scale=TimeScale(), enable_commands=False):
    """Build an anchor-mode runtime over the given store/session. The sleeper
    defaults to one that ADVANCES the ManualClock (so paced sleeps consume
    fake wall time instead of real seconds)."""
    if sleeper is None:
        sleeper = manual.sleep
    return AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=scale, max_virtual_hours=max_hours,
        resolver=IntentResolver(store, rng=rng_mod.stream_rng(SEED)),
        sleeper=sleeper, anchor=anchor, now=manual,
        enable_commands=enable_commands,
    )


def _run_accelerated(store, session, channel, *, max_hours, scale=FAST):
    """Run WITHOUT an anchor (today's path): real paced sleeps, the
    injectable sleeper reserved for response delays. Returns the recorded
    response delays."""
    delays: list[float] = []

    async def record(delay):
        delays.append(delay)

    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=scale, max_virtual_hours=max_hours,
        resolver=IntentResolver(store, rng=rng_mod.stream_rng(SEED)),
        sleeper=record,
    )
    asyncio.run(runtime.run())
    return delays


# --- anchor=None parity (accelerated path byte-identical) ---


def test_anchor_none_paced_sleeps_are_real_and_never_touch_wall_clock():
    """anchor=None: target sleeps stay REAL asyncio sleeps — the injectable
    sleeper is NOT called for pacing (latency traces unchanged) and the
    injectable wall clock is never consulted."""
    store, clock, session = _session(SeamStore())

    def boom():
        raise AssertionError("wall clock consulted without an anchor")

    recorded = []

    async def record(delay):
        recorded.append(delay)

    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), FakeChannel(),
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=2.0,
        sleeper=record, now=boom,
    )
    asyncio.run(runtime._sleep_until_t_h(2.0, 0.5))
    runtime._executor.shutdown()
# The paced sleep completed without the sleeper and without the wall clock.
    assert recorded == []
    assert clock.t_h == 0.0  # the caller advances the clock, not the sleep


def test_anchor_none_end_to_end_parity():
    """anchor=None end-to-end: the exact scenario of
    test_quiet_hours_and_cooldown_events_suppressed produces the same sends,
    suppression codes and row consumption (the anchor path is invisible)."""
    store, clock, session = _session(SeamStore())
    ground_agenda(store, 0.5, 3.5, item_id="night")
    ground_agenda(store, 9.5, 10.6, item_id="morning")
    store.save_schedule_events(SEED, [
        {"t_h": 2.0, "day": 0, "reason": REASON_SCHEDULE},    # quiet hours
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},   # awake -> fires
        {"t_h": 10.1, "day": 0, "reason": REASON_SCHEDULE},   # cooldown
    ])
    channel = FakeChannel()
    _run_accelerated(store, session, channel, max_hours=12.0, scale=SLOW)

    assert len(channel.sent) == 1
    msg = channel.sent[0]
    assert msg.proactive is True and msg.reason == REASON_SCHEDULE
    assert store.proactive_count(0) == 1
    assert suppressed_codes(store) == {"quiet_hours", "cooldown"}
    schedule_rows = rows(store, SEED)
    for t_h in (2.0, 10.0, 10.1):
        assert schedule_rows[t_h]["status"] == "fired", f"row {t_h} not consumed"
    assert schedule_rows[10.0]["fired_t_h"] == 10.0


# --- absolute sleeps: epoch-derived, TimeScale-independent, self-correcting ---


def test_anchor_absolute_sleep_is_epoch_derived_and_self_correcting():
    """With an anchor, _sleep_until_t_h computes ``epoch_of(target) - now()``:
    the first request is the full 7200 s (NOT the 0.02-scaled relative
    delta), and a late wake (drift 0.9) re-sleeps the residual until the
    deadline is hit exactly — cumulative drift is killed."""
    store, clock, session = _session(SeamStore())
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=0.0, tz="UTC")
    manual = AnchorManualClock(t0=T0, drift=0.9)
    requested: list[float] = []

    async def sleeper(delay):
        requested.append(delay)
        await manual.sleep(delay)

    runtime = _anchor_runtime(
        store, session, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=2.0, sleeper=sleeper, scale=FAST,
    )
    asyncio.run(runtime._sleep_until_t_h(2.0, 0.0))
    runtime._executor.shutdown()

    deadline = anchor.epoch_of(2.0)  # T0 + 7200
    assert requested[0] == pytest.approx(7200.0)  # absolute, not scale-relative
    assert len(requested) > 1  # the residual was re-slept (self-correction)
    assert manual.t == pytest.approx(deadline, abs=0.01)  # no cumulative drift
    assert clock.t_h == 0.0  # the caller advances the clock afterwards


def test_anchor_absolute_sleep_no_drift_single_request():
    """A compliant sleeper (drift 1.0) needs exactly one request — the loop
    exits as soon as the deadline is reached."""
    store, clock, session = _session(SeamStore())
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=0.0, tz="UTC")
    manual = AnchorManualClock(t0=T0)
    requested: list[float] = []

    async def sleeper(delay):
        requested.append(delay)
        await manual.sleep(delay)

    runtime = _anchor_runtime(
        store, session, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=2.0, sleeper=sleeper,
    )
    asyncio.run(runtime._sleep_until_t_h(2.0, 0.0))
    runtime._executor.shutdown()

    assert requested == [7200.0]
    assert manual.t == pytest.approx(anchor.epoch_of(2.0))


# --- resume fix: t_h_start = anchor.t_h_at(now), loud clock-skew failure ---


def test_anchor_resume_at_real_1800_pins_local_hour(tmp_path):
    """THE resume pin: restart with an anchor at real 18:00 on day 1 (t_h
    42.0) after the store already persisted day 1 — the clock must resume at
    42.0 (local_hour ≈ 18), NOT at the persisted day's virtual midnight
    (24.0 → local_hour 0, the pre-fix land-at-midnight behavior)."""
    store = SQLiteStore(tmp_path / "resume.db")
    _, _, session = _session(store)
    ground_agenda(store, 20.0, 30.0, item_id="g1")
    _run_accelerated(store, session, FakeChannel(), max_hours=26.0)
    assert store.latest_daily_state()["day"] == 1  # day-1 state persisted

# Restart: real 18:00 on day 1 -> anchor.t_h_at(now) = 42.0
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=42.0, tz="America/Mexico_City")
    manual = AnchorManualClock(t0=T0)
    store2, clock2, session2 = _session(store)
    runtime = _anchor_runtime(
        store2, session2, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=42.0,  # run ends immediately: t_h_start == max
    )
    asyncio.run(runtime.run())

    assert runtime._t_h_start == pytest.approx(42.0)
    assert clock2.t_h == pytest.approx(42.0)
    assert clock2.day() == 1
    assert clock2.local_hour() == pytest.approx(18.0)  # ~18:00, NOT 00:00


def test_anchor_resume_clock_skew_backwards_raises(tmp_path):
    """Clock skew: the persisted store reached day 1 (t_h 24) but the anchor
    maps the current wall clock to day 0 10:00 (t_h 10) — the system clock
    moved backwards. The runtime must RAISE, never guess."""
    store = SQLiteStore(tmp_path / "skew.db")
    _, _, session = _session(store)
    ground_agenda(store, 20.0, 30.0, item_id="g1")
    _run_accelerated(store, session, FakeChannel(), max_hours=26.0)

    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=10.0, tz="UTC")  # now -> t_h 10
    manual = AnchorManualClock(t0=T0)
    _, _, session2 = _session(store)
    runtime = _anchor_runtime(
        store, session2, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=42.0,
    )
    with pytest.raises(RuntimeError, match="clock skew"):
        asyncio.run(runtime.run())


def test_anchor_resume_skew_detected_from_event_log(tmp_path):
    """The skew reference includes the EVENT LOG: even with no persisted day,
    an event already logged at t_h 20.5 makes a now-mapped t_h of 10.0 a
    backward jump -> raise."""
    store = SQLiteStore(tmp_path / "skew-log.db")
    store.log_event(0, 20.5, "contact_opportunity", "id=x")
    _, _, session = _session(store)
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=10.0, tz="UTC")
    manual = AnchorManualClock(t0=T0)
    runtime = _anchor_runtime(
        store, session, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=42.0,
    )
    with pytest.raises(RuntimeError, match="clock skew"):
        asyncio.run(runtime.run())


def test_anchor_resume_no_skew_when_now_maps_forward(tmp_path):
    """No skew when the anchor maps now PAST everything recorded (machine
    off for a while): the clock advances to the real virtual hour and the
    run proceeds."""
    store = SQLiteStore(tmp_path / "forward.db")
    _, _, session = _session(store)
    ground_agenda(store, 20.0, 30.0, item_id="g1")
    _run_accelerated(store, session, FakeChannel(), max_hours=26.0)

    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=50.0, tz="UTC")  # now -> day 2 02:00
    manual = AnchorManualClock(t0=T0)
    _, clock2, session2 = _session(store)
    runtime = _anchor_runtime(
        store, session2, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=50.0,  # run ends immediately: t_h_start == max
    )
    asyncio.run(runtime.run())
    assert clock2.t_h == pytest.approx(50.0)


# --- command dispatch (ControlCommand -> harness.commands, lazy import) ---


def _fake_commands_module(monkeypatch, calls, sent):
    """Install a stub ``harness.commands`` in sys.modules (commands.py lands
    with W-commands; the runtime imports it lazily)."""
    mod = types.ModuleType("harness.commands")

    class CommandContext:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def handle_command(cmd, ctx):
        calls.append((cmd, ctx))
        return f"reply:{cmd.name}"

    mod.CommandContext = CommandContext
    mod.handle_command = handle_command
    monkeypatch.setitem(sys.modules, "harness.commands", mod)
    return mod


def test_command_dispatch_routes_to_handle_command(monkeypatch):
    """A ControlCommand dispatches to harness.commands.handle_command under
    the runtime lock with the full S3 CommandContext; the reply goes out as a
    plain non-proactive message; the session is NEVER touched (no turns, no
    closing draws, no memory writes)."""
    store, clock, session = _session(SeamStore())
    channel = FakeChannel()
    calls = []
    _fake_commands_module(monkeypatch, calls, channel.sent)
    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=1.0,
        sleeper=lambda d: _noop_sleeper(d),
        enable_commands=True,
    )
    asyncio.run(runtime._on_command(
        ControlCommand(name="status", args="", sender_id=42)
    ))
    runtime._executor.shutdown()

    assert len(calls) == 1
    cmd, ctx = calls[0]
    assert cmd.name == "status" and cmd.sender_id == 42
    ctx_kw = ctx.kwargs
    assert ctx_kw["store"] is store
    assert ctx_kw["clock"] is clock
    assert ctx_kw["anchor"] is None
    assert ctx_kw["persona_exists"] is False
    assert ctx_kw["pending_proactive_count"] == 0
    assert ctx_kw["request_tz_change"] == runtime._request_tz_change
    assert ctx_kw["request_mute"] == runtime._request_mute
    assert channel.sent[-1].text == "reply:status"
    assert channel.sent[-1].proactive is False
# session untouched: no messages, no client calls
    assert store.recent_messages(5) == []
    assert session.client.calls == []


async def _noop_sleeper(delay):
    return None


def test_command_dispatch_lazy_import_before_commands_lands():
    """runtime.py must NOT import harness.commands at module scope (the
    import is function-level, so dispatch works even before W-commands
    merges). Verified in a fresh interpreter — in-session sys.modules is
    polluted by other tests, so the property is pinned process-isolated.
    (With commands.py now on main, dispatch itself is covered by
    test_run_wires_command_callback_only_when_enabled and the status
    dispatch test above.)"""
    import subprocess
    import sys

    code = (
        "import harness.runtime, sys; "
        "assert 'harness.commands' not in sys.modules, "
        "'runtime.py must not import harness.commands at module level'; "
        "print('LAZY_OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "LAZY_OK" in proc.stdout


def test_run_wires_command_callback_only_when_enabled():
    """enable_commands=True registers on_command with the channel's S3 seam;
    the default (False) keeps today's single-callback start (commands stay
    dropped)."""
    store, clock, session = _session(SeamStore())
    channel = CommandAwareChannel()
    runtime = _anchor_runtime(
        store, session, channel, anchor=None, manual=AnchorManualClock(),
        max_hours=1.0, scale=FAST, enable_commands=True,
    )
    asyncio.run(runtime.run())
    assert channel.command_handler == runtime._on_command
    assert channel.handler == runtime._on_inbound

    store2, _, session2 = _session(SeamStore())
    channel2 = CommandAwareChannel()
    runtime2 = AsyncRuntime(
        session2, ProactiveSchedule.restore(SEED, store2), channel2,
        store=store2, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=1.0,
        sleeper=_noop_sleeper,
    )
    asyncio.run(runtime2.run())
    assert channel2.command_handler is None


# --- typing wrap (generation + response_delay_s inside typing_context) ---


def test_typing_context_wraps_inbound_generation_and_delay():
    """The reactive path runs generation + response_delay_s inside the
    channel's typing_context(): enter/exit once per turn, and the response
    delay is waited while the context is ENTERED (not yet exited)."""
    store, clock, session = _session(SeamStore(), replies=["hi!"])
    channel = CommandAwareChannel()
    observed = []

    async def record(delay):
        observed.append(("delay", len(channel.typing_events)))

    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=2.0,
        sleeper=record,
    )

    async def driver():
        feed = asyncio.create_task(channel.feed("hello", t_h=0.5))
        try:
            await runtime.run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    assert channel.typing_events == ["enter", "exit"]
    assert observed == [("delay", 1)]  # sleeper ran inside the context


def test_typing_context_wraps_proactive_generation_not_send():
    """The proactive path: generation + delay inside typing_context, but the
    SEND happens AFTER the context exits (S4: indicator during composition,
    message arrives after)."""
    store, clock, session = _session(SeamStore())
    ground_agenda(store, 9.0, 11.0, item_id="typing")  # awake hours
    store.save_schedule_events(SEED, [
        {"t_h": 9.5, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = CommandAwareChannel()
    trace = []

    async def record(delay):
        trace.append(("delay", len(channel.typing_events)))

    orig_send = channel.send

    async def send(message):
        trace.append(("send", len(channel.typing_events)))
        await orig_send(message)

    channel.send = send
    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=11.0,
        resolver=IntentResolver(store, rng=rng_mod.stream_rng(SEED)),
        sleeper=record,
    )
    asyncio.run(runtime.run())
# typing entered before the response delay and exited before the send
    assert trace == [("delay", 1), ("send", 2)]
    assert channel.sent and channel.sent[0].proactive is True


# --- CommandContext narrow hooks: /tz at rollover, /mute defers ---


def test_request_tz_change_applied_at_next_rollover(tmp_path):
    """/tz (request_tz_change) is queued and applied at the NEXT rollover:
    the anchor's tz metadata is re-persisted under the S1 kv keys and the
    in-memory anchor updates — the epoch->t_h mapping never jumps."""
    store = SQLiteStore(tmp_path / "tz.db")
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=23.5, tz="America/Mexico_City")
    manual = AnchorManualClock(t0=T0)
    persist_anchor(store, anchor)
    _, _, session = _session(store)
    runtime = _anchor_runtime(
        store, session, FakeChannel(), anchor=anchor, manual=manual,
        max_hours=25.0,  # crosses the 24.0 midnight (23.5 -> 24.0 -> 25.0)
    )
    runtime._request_tz_change("America/New_York")
    asyncio.run(runtime.run())

    assert runtime.anchor.tz == "America/New_York"
    assert load_anchor(store).tz == "America/New_York"
    assert store.get_kv("anchor.tz") == "America/New_York"
# the mapping is untouched: t_h0/epoch0_s survive the tz change
    assert float(store.get_kv("anchor.t_h0")) == pytest.approx(23.5)


def test_request_tz_change_rejects_unknown_zone():
    store, _, session = _session(SeamStore())
    runtime = _anchor_runtime(
        store, session, FakeChannel(), anchor=None, manual=AnchorManualClock(),
        max_hours=1.0,
    )
    with pytest.raises(ValueError, match="unknown timezone"):
        runtime._request_tz_change("Not/AZone")
    runtime._executor.shutdown()


def test_request_mute_defers_pending_event_never_consumes():
    """/mute (request_mute): a still-valid pending event inside the mute
    window is DEFERRED (never consumed as fired-without-delivery) and fires
    at the mute end — fired_t_h lands at the mute end, and the message goes
    out only after the window."""
    store, clock, session = _session(SeamStore())
    ground_agenda(store, 10.0, 14.0, item_id="mute")
    store.save_schedule_events(SEED, [
        {"t_h": 10.5, "day": 0, "reason": REASON_SCHEDULE},
    ])
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=10.0, tz="UTC")  # now = 10:00
    manual = AnchorManualClock(t0=T0)
    channel = FakeChannel()
    sent_at: list[float] = []
    orig_send = channel.send

    async def send(message):
        sent_at.append(manual.t)
        await orig_send(message)

    channel.send = send
    runtime = _anchor_runtime(
        store, session, channel, anchor=anchor, manual=manual,
        max_hours=13.0,
    )
# Position the clock at the anchor-resumed hour (10:00) before the mute
# request; the hook measures from the clock's current hour.
    clock.advance_hours(10.0)
    runtime._request_mute(2.0)  # mute until t_h 12.0
    asyncio.run(runtime.run())

    schedule_rows = rows(store, SEED)
    assert schedule_rows[10.5]["status"] == "fired"
    assert schedule_rows[10.5]["fired_t_h"] == pytest.approx(12.0)  # deferred to mute end
    assert len(channel.sent) == 1
    assert channel.sent[0].proactive is True
    assert sent_at[0] >= anchor.epoch_of(12.0)  # nothing sent inside the window


# --- anchor persistence (kv keys) ---


def test_load_persist_anchor_kv_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "kv.db")
    anchor = RealTimeAnchor(epoch0_s=T0, t_h0=42.5, tz="America/Mexico_City")
    persist_anchor(store, anchor)
    assert load_anchor(store) == anchor
# partial state -> no anchor, no raise
    partial = SQLiteStore(tmp_path / "kv-partial.db")
    partial.set_kv(ANCHOR_KV_KEYS[0], "1.0")
    partial.set_kv(ANCHOR_KV_KEYS[1], "2.0")
    assert load_anchor(partial) is None
# seam-less store -> no anchor, persist is a no-op
    seam = SeamStore()
    assert load_anchor(seam) is None
    persist_anchor(seam, anchor)  # no raise
