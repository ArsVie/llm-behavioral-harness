"""Async runtime — real-time rollover + gated proactive firing (wave 3, seam A-6; A7; it2 A3).

The deterministic engine path (Session) stays synchronous and replay-exact;
this module is the ONLY place in the harness allowed to read wall-clock, and
only to pace the virtual clock (``TimeScale``: real seconds per virtual
hour). Two loops run concurrently inside :meth:`AsyncRuntime.run`:

- ``_rollover_loop`` — sleeps until the next virtual midnight (paced),
  advances the clock, calls ``session.ensure_day`` (idempotent: finalizes the
  previous day, judges it, applies the end-of-day engine update, samples the
  new day's mood), then re-plans + persists the schedule for the CURRENT day
  with the previous day's real judge score and today's initiative
  (``day_scores`` — never ``scores=None`` in live scheduling) and refreshes
  ``self.schedule`` from the store (restart-safe, INSERT OR IGNORE never
  resurrects fired/expired rows). CLOCK DISCIPLINE (it2 A3, the E0
  confounder): the rollover NEVER jumps the virtual clock past a pending
  opportunity — when the earliest pending event lies before the next
  midnight it parks AT that event hour and waits for the firing loop, so
  every event is gated at its own time and accelerated time can never turn a
  still-valid event into a spurious 'expired' suppression.

- ``_firing_loop`` — waits for the next pending schedule event (short poll
  when none; overdue events are visible thanks to ``next_pending``'s A7
  restart fix), advances the clock to it, then GATES before generating:
  the content gate resolves the OPPORTUNITY to a GROUNDED intent
  (``IntentResolver(opportunity)`` → ``content_gate(intent, store)``) and the
  context gate (quiet hours, cooldown, daily cap). No grounded candidate ⇒
  SUPPRESS (``no_grounded_reason`` is a legitimate outcome). Suppressed
  events are consumed (marked fired — or expired when the validity window
  elapsed) and logged as ``proactive_suppressed`` with the failing code;
  allowed events fire via ``session.fire_proactive(intent.id)`` — the EXACT
  validated intent id, never a reason (invariant 6/7: two same-reason
  intents are never interchangeable) — and are sent through the channel as
  proactive OutboundMessages. During quiet hours a still-valid event whose
  validity outlives the quiet window is DEFERRED (A9 R-4b), never consumed
  as fired-without-delivery: the row stays pending until the next awake
  instant, and only events past ``valid_until`` are expired. The deferral
  itself ADVANCES the virtual clock to that awake instant (clamped to
  max_virtual_hours; R1-F1), so a parked event still terminates the run
  instead of livelocking — the firing loop re-evaluates the event at the
  awake ``now`` and fires it there.

Delivery latency (A7): after the LLM returns, the runtime waits the
requested ``response_delay_s`` (wall-clock seconds — NOT scaled by
TimeScale) through the injectable ``sleeper`` (default
``concurrency.default_sleeper``; tests inject a recorder so the suite never
waits real seconds) and only then calls ``channel.send``.

Concurrency (it2 A6): ALL ``session.*`` calls run on an OWNED
``concurrency.ExecutorOwner`` (``llh-runtime`` workers) under a single
``asyncio.Lock`` so the event loop never blocks on an LLM/judge call and
inbound (reactive) and scheduled (proactive) turns never overlap (single
user, no reentrancy); the executor is shut down explicitly in ``run()``'s
``finally`` (invariant 17: runtime tests must terminate their Python
process). Engine steps are never called directly: rollover is driven ONLY
through ``session.ensure_day`` / ``session.finalize_current``.

Anchor mode (Wave 2, W-runtime; seam S2): when an ``AsyncRuntime`` is
constructed with an ``anchor: RealTimeAnchor``, the runtime runs in REAL
time instead of the paced virtual time: target sleeps become ABSOLUTE
wall-clock sleeps (``anchor.epoch_of(target_t_h) - now()``, self-correcting
— a late wake re-sleeps the residual instead of accumulating drift), the
virtual clock resumes at the CURRENT real virtual hour
(``anchor.t_h_at(now)``) instead of the persisted day's virtual midnight,
and clock skew (a persisted store that already reached a LATER virtual hour
than the anchor maps now to — i.e. the system clock moved backwards) raises
instead of guessing. ``anchor=None`` (the default) keeps today's behavior
byte-identical — the accelerated fleet never touches the anchor path.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import engine.rng as rng_mod
from engine.circadian import envelope
from engine.types import ENVELOPE_RAMP_H, TimingParams
from harness.anchor import RealTimeAnchor
from harness.channels.base import Channel, InboundMessage, OutboundMessage

if TYPE_CHECKING:  # S3: telegram defines ControlCommand (channel side, merged)
    from harness.channels.telegram import ControlCommand
from harness.concurrency import (
    ExecutorOwner,
    ResourceRegistry,
    Sleeper,
    default_sleeper,
    ensure_thread_safe_connection,
)
from harness.gates import content_gate, context_gate
from harness.proactive import IntentResolver
from harness.scheduler import (
    REASON_SCHEDULE,
    REASON_VALIDITY_H,
    ProactiveSchedule,
    day_scores,
)
from harness.session import Session
from harness.store import SQLiteStore

#: Poll cadence when no schedule event is pending, in VIRTUAL hours. The real
#: sleep is POLL_INTERVAL_H * seconds_per_virtual_hour, so tests with a tiny
#: TimeScale stay responsive (µs polls) while a real-time run polls rarely.
POLL_INTERVAL_H = 0.05

#: Seam S1 kv keys persisting the RealTimeAnchor (seam S2). W-runtime owns
#: the keys: written on fresh start (anchor_for_fresh_start) and on a /tz
#: change; read at startup by the launcher (run_async).
ANCHOR_KV_KEYS = ("anchor.epoch0_s", "anchor.t_h0", "anchor.tz")


def load_anchor(store) -> RealTimeAnchor | None:
    """Load the persisted RealTimeAnchor via the S1 kv seam.

    Returns None when the kv seam is absent or any of the three keys is
    missing (no anchor persisted yet — the launcher treats that as a fresh
    start). Never raises on partial state: an incomplete anchor means no
    anchor.
    """
    get_kv = getattr(store, "get_kv", None)
    if get_kv is None:
        return None
    values = [get_kv(k) for k in ANCHOR_KV_KEYS]
    if any(v is None for v in values):
        return None
    return RealTimeAnchor(float(values[0]), float(values[1]), values[2])


def persist_anchor(store, anchor: RealTimeAnchor) -> None:
    """Persist the anchor under the S1 kv keys (INSERT OR REPLACE).

    No-op on stores without the kv seam (legacy fakes).
    """
    set_kv = getattr(store, "set_kv", None)
    if set_kv is None:
        return
    set_kv("anchor.epoch0_s", str(anchor.epoch0_s))
    set_kv("anchor.t_h0", str(anchor.t_h0))
    set_kv("anchor.tz", anchor.tz)


def _env_bool(name: str, default: bool = False) -> bool:
    """Env bool with the harness convention (mirrors tools._env_bool):
    unset/empty -> default; truthy = 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class TimeScale:
    """Pace the virtual clock against wall-clock (the ONLY wall-clock use).

    ``seconds_per_virtual_hour``: real seconds per virtual hour. 3600.0 means
    one real hour per virtual hour (real time); tests pass a tiny value (e.g.
    0.001) to run days in milliseconds.
    """

    seconds_per_virtual_hour: float = 3600.0


class AsyncRuntime:
    """Orchestrate a Session, a persisted ProactiveSchedule and a Channel.

    Anchor mode (seam S2, default OFF): with ``anchor: RealTimeAnchor`` the
    runtime paces the virtual clock in REAL time (absolute, self-correcting
    sleeps), resumes at the current real virtual hour instead of the
    persisted day's virtual midnight, and raises on clock skew. With
    ``anchor=None`` (the default) every path is byte-identical to the
    pre-anchor runtime — the accelerated fleet is untouched.
    """

    def __init__(
        self,
        session: Session,
        schedule: ProactiveSchedule,
        channel: Channel,
        *,
        store: SQLiteStore,
        timing: TimingParams,
        seed: int,
        time_scale: TimeScale = TimeScale(),
        max_virtual_hours: float | None = None,
        resolver: IntentResolver | None = None,
        sleeper: Sleeper | None = None,
        anchor: RealTimeAnchor | None = None,
        now=None,
        enable_commands: bool = False,
    ):
        self.session = session
        self.schedule = schedule
        self.channel = channel
        self.store = store
        self.timing = timing
        self.seed = seed
        self.time_scale = time_scale
        self.max_virtual_hours = max_virtual_hours
        #: Real-time anchor (seam S2); None = today's paced behavior.
        self.anchor: RealTimeAnchor | None = anchor
        #: Injectable wall clock (anchor mode only): the source for absolute
        #: sleeps and the resume computation. Default time.time; tests inject
        #: a ManualClock-style source so anchor runs never wait real seconds.
        #: Never consulted when anchor is None (byte parity).
        self._now = now if now is not None else time.time
        #: S3 command dispatch flag (default OFF): when True, run() registers
        #: ``_on_command`` with the channel's ``start(on_message,
        #: on_command=...)`` seam; when False (default) the channel is started
        #: exactly as before — commands stay dropped.
        self.enable_commands = enable_commands
        #: Virtual hour the anchor resume positioned the clock at (None until
        #: an anchor-mode run starts).
        self._t_h_start: float | None = None
        #: Queued /tz change (S3 CommandContext hook): applied at the next
        #: rollover; the epoch->t_h mapping never jumps.
        self._pending_tz: str | None = None
        #: Mute window end (virtual hours) for /mute (S3 CommandContext hook):
        #: pending events are deferred (never consumed) until this instant.
        self._mute_until_t_h: float | None = None
        #: Grounded-intent resolver (store-backed). Default uses a seeded
        #: engine.rng stream distinct from the event stream so resolver
        #: tie-breaks never perturb the planned event times.
        self.resolver = resolver if resolver is not None else IntentResolver(
            store, rng=rng_mod.stream_rng(seed, rng_mod.EXPERIMENT_STREAM)
        )
        #: Injectable delay function (default concurrency.default_sleeper —
        #: asyncio.sleep; tests inject a recorder) so tests record the
        #: requested response_delay_s without waiting real seconds.
        self.sleeper: Sleeper = sleeper if sleeper is not None else default_sleeper()
        #: A6: ONE owned executor per runtime (explicit lifecycle; shutdown in
        #: run()'s finally) + a resource registry (owned vs injected).
        self._executor = ExecutorOwner("runtime").start()
        self._registry = ResourceRegistry("runtime")
        self._registry.register(store, owned=False)   # injected: creator owns it
        self._registry.register(channel, owned=False)  # injected: creator owns it
        self._lock = asyncio.Lock()
        #: Set when the firing loop exits for good (run end): the rollover
        #: then stops parking at pending events — nothing will gate them.
        self._firing_done = False
        self._ensure_thread_safe_store()

    def _ensure_thread_safe_store(self) -> None:
        """Re-open the store connection for cross-thread use (A6 helper).

        The merged SQLiteStore binds its connection to the creating thread
        (sqlite3 default ``check_same_thread=True``), but this runtime moves
        ``session.*`` calls to worker threads (plan requirement: never block
        the event loop). ``concurrency.ensure_thread_safe_connection``
        re-opens the connection with ``check_same_thread=False``, keeping
        that contract without touching the frozen store: the asyncio.Lock
        serializes all session calls, WAL + busy_timeout cover the remaining
        store access, and the schema is already on disk.

        Ownership note (deviation from A6's integration note, justified):
        A6 suggested registering the re-opened connection as OWNED, but the
        connection becomes the STORE's connection (``store.conn`` now
        references it and ``SQLiteStore.close()`` closes it) — closing it in
        the runtime would leave the injected store dead. It is registered
        owned=False: the store's lifecycle owns it, the runtime never closes
        it twice. Seam-faithful in-memory stores (tests) expose no sqlite
        connection and are skipped entirely."""
        if not (hasattr(self.store, "path") and hasattr(self.store, "conn")):
            return
        old_conn = self.store.conn
        conn = ensure_thread_safe_connection(self.store.path)
        if old_conn is not None:
            old_conn.close()  # schema-creation conn from SQLiteStore.__init__
        self.store.conn = conn
        self._registry.register(conn, owned=False)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Start the channel, run rollover + firing until max_virtual_hours
        (or cancelled), then finalize the current day, stop the channel, and
        shut down the OWNED executor (A6: explicit lifecycle; double-shutdown
        safe — the registry's owned set is empty by design, injected
        resources are never closed here).

        Anchor mode: the resume fix runs FIRST — the virtual clock is
        positioned at the CURRENT real virtual hour (``anchor.t_h_at(now)``)
        and clock skew raises before anything starts. Commands (S3): when
        ``enable_commands`` is set, the channel is started with the
        ``on_command`` callback; default OFF keeps today's single-callback
        start.
        """
        if self.anchor is not None:
            self._apply_anchor_resume()
        if self.enable_commands:
            await self.channel.start(self._on_inbound, on_command=self._on_command)
        else:
            await self.channel.start(self._on_inbound)
        try:
            await asyncio.gather(self._rollover_loop(), self._firing_loop())
        finally:
            try:
                await self._session_call(self.session.finalize_current)
            finally:
                await self.channel.stop()
                self._executor.shutdown()
                self._registry.close()

    async def _session_call(self, fn, *args):
        """Run one synchronous session call on the OWNED executor, locked."""
        async with self._lock:
            return await self._executor.run_in_thread(fn, *args)

    def _max_reached(self, now_h: float) -> bool:
        return self.max_virtual_hours is not None and now_h >= self.max_virtual_hours

    def _poll_sleep(self) -> float:
        return POLL_INTERVAL_H * self.time_scale.seconds_per_virtual_hour

    async def _poll_wait(self) -> None:
        """Wait one polling cadence (no schedule event pending / parked
        yield). None mode: a REAL asyncio sleep — byte-identical, never
        routed through the injectable sleeper. Anchor mode: the injectable
        sleeper, so a fake wall clock (tests) consumes the poll time instead
        of real seconds; production's default sleeper is asyncio.sleep, so
        the live cadence is unchanged."""
        if self.anchor is None:
            await asyncio.sleep(self._poll_sleep())
        else:
            await self.sleeper(self._poll_sleep())

    async def _sleep_until_t_h(self, target_t_h: float, now_h: float) -> None:
        """Sleep until the virtual clock reaches ``target_t_h``.

        Anchor mode (S2): ABSOLUTE wall-clock sleep — the target's epoch is
        computed once (``anchor.epoch_of``) and the sleeper waits for the
        REMAINING real seconds, re-checking after every wake, so a late wake
        (host sleep, GC pause, load) re-sleeps the residual instead of
        accumulating drift. The virtual clock is advanced by the CALLER after
        this returns, so the mapping stays exact.

        None mode (default): today's paced behavior — a REAL asyncio sleep of
        ``(target - now) * seconds_per_virtual_hour``. The injectable sleeper
        stays reserved for ``response_delay_s`` (latency tests assert the
        sleeper trace contains ONLY response delays), so the accelerated path
        is byte-identical.
        """
        if self.anchor is None:
            await asyncio.sleep(
                (target_t_h - now_h) * self.time_scale.seconds_per_virtual_hour
            )
            return
        deadline = self.anchor.epoch_of(target_t_h)
        while True:
            remaining = deadline - self._now()
            if remaining <= 0:
                return
            await self.sleeper(remaining)

    # ------------------------------------------------------------------ #
    # anchor resume (S2): real-time startup, loud clock-skew failure
    # ------------------------------------------------------------------ #

    def _apply_anchor_resume(self) -> None:
        """Anchor-mode startup — the resume fix: position the virtual clock
        at the CURRENT real virtual hour (``anchor.t_h_at(now)``) instead of
        landing at the persisted day's virtual midnight (Session._resume_from
        parks the clock at ``day*24``).

        FAILS LOUDLY on clock skew: when the persisted state demonstrably
        REACHED a later virtual hour than the anchor maps the current wall
        clock to, the system clock moved backwards (or the anchor changed) —
        raise, never guess. The reference is the latest virtual instant the
        simulation actually reached (session clock position after resume,
        latest persisted day's rollover, the open conversation's last turn,
        the event log); planned schedule rows are excluded (future times).
        """
        assert self.anchor is not None  # anchor mode only
        t_h_start = self.anchor.t_h_at(self._now())
        latest = self._latest_recorded_t_h()
        if t_h_start < latest - 1e-9:
            raise RuntimeError(
                "clock skew on anchor resume: the persisted anchor maps the "
                f"current wall clock to t_h={t_h_start:.3f}, but the store has "
                f"already reached t_h={latest:.3f} — the system clock moved "
                "backwards (or the anchor was changed). Refusing to guess; "
                "fix the clock or re-anchor."
            )
        if t_h_start > self.session.clock.now_h():
            self.session.clock.advance_hours(
                t_h_start - self.session.clock.now_h()
            )
        self._t_h_start = t_h_start

    def _latest_recorded_t_h(self) -> float:
        """Highest virtual hour the persisted state demonstrably REACHED:
        the session clock's current position (Session._resume_from lands it
        at the latest persisted day's midnight), the latest persisted day's
        rollover instant (``day*24``), the open conversation's last turn, and
        the event log. Schedule rows are EXCLUDED — they hold PLANNED future
        times. Defensive against legacy store stubs (seam-less fakes)."""
        assert self.anchor is not None  # anchor mode only
        t = self.session.clock.now_h()
        latest_fn = getattr(self.store, "latest_daily_state", None)
        if callable(latest_fn):
            latest = cast("dict | None", latest_fn())
            if latest is not None:
                t = max(t, float(latest["day"]) * 24.0)
        conv_fn = getattr(self.store, "load_open_conversation", None)
        if callable(conv_fn):
            conv = conv_fn()
            if conv is not None:
                turns = getattr(conv, "turns", None) or ()
                if turns:
                    t = max(t, float(getattr(turns[-1], "t_h", 0.0)))
        events_fn = getattr(self.store, "events_since", None)
        if callable(events_fn):
            for e in cast("list[dict]", events_fn(0)):
                t = max(t, float(e.get("t_h", 0.0)))
        return t

    def _horizon_days(self) -> int:
        """Plan ONLY the current day (A7): days 0..current_day, so the
        schedule covers through the end of today. Re-planning the same
        horizon with the same (stable) day_scores regenerates identical rows
        — INSERT OR IGNORE never drifts or duplicates."""
        return self.session.clock.day() + 1

    def _replan(self) -> None:
        """Plan the CURRENT day with real timing feedback, persist it, and
        refresh the schedule from the store.

        plan_and_persist is pure numpy + store upserts (no session state), so
        it runs on the event loop directly; self.schedule is rebuilt from the
        store so already-fired/expired rows are never re-selected. The scores
        array comes from ``day_scores`` (previous day's real judge score ×
        today's initiative) — NEVER ``scores=None`` in live scheduling.
        """
        day = self.session.clock.day()
        scores = day_scores(self.store, day, self.timing)
        ProactiveSchedule.plan_and_persist(
            self._horizon_days(),
            self.seed,
            self.session.persona,
            self.timing,
            self.store,
            reason=REASON_SCHEDULE,
            scores=scores,
        )
        self.schedule = ProactiveSchedule.restore(self.seed, self.store)

    # ------------------------------------------------------------------ #
    # reactive path
    # ------------------------------------------------------------------ #

    async def _on_inbound(self, msg: InboundMessage) -> None:
        """Reactive path: advance the clock to msg.t_h if the channel
        supplied one (never backwards), reply via Session.on_message, wait
        the requested response_delay_s (injectable sleeper, wall-clock),
        then send the reply as a non-proactive OutboundMessage.

        WS4 (runtime redesign): before the turn runs, the user message is
        queued as a ``user_message_mid_turn`` steer (when the decision layer
        is enabled). The session's idle boundary then turns it into a
        ``tool_decide_reply`` pop-up when an event is in progress (user
        L356). The asyncio lock serializes turns, so a message can never
        arrive WHILE a turn is generating — the steer still records the
        arrival for the next safe boundary, which is the same contract the
        design specifies for the mid-turn case.

        S4 (typing): generation + response_delay_s run inside the channel's
        ``typing_context()`` when the channel exposes one (duck-typed probe;
        channels without it — CLI, fakes — are a no-op).

        S1 clock-advance (WS-A): in anchor mode the REAL arrival time is
        authoritative. The virtual clock is advanced to
        ``anchor.t_h_at(msg.received_at)`` (never backwards), so the store
        resolves ``sent_at = real_at(t_h)`` — the anchor's exact inverse —
        to the TRUE arrival instant and the clock keeps flowing
        mid-conversation (previously frozen at the last loop wake, which
        stamped every trial message with one shared sent_at). Unanchored
        (anchor=None): the ``msg.t_h`` path is unchanged — no t_h means no
        advance and sent_at stays NULL, byte-identical replay.
        """
        async with self._lock:
            if self.anchor is not None and msg.received_at is not None:
                arrival_t_h = self.anchor.t_h_at(msg.received_at)
                if arrival_t_h > self.session.clock.now_h():
                    self.session.clock.advance_hours(
                        arrival_t_h - self.session.clock.now_h()
                    )
            elif msg.t_h is not None and msg.t_h > self.session.clock.now_h():
                self.session.clock.advance_hours(
                    msg.t_h - self.session.clock.now_h()
                )
            enqueue = getattr(self.session, "enqueue_user_message_steer", None)
            if enqueue is not None:
                enqueue(msg.text, self.session.clock.now_h())

            async def _gen():
                return await self._executor.run_in_thread(
                    self.session.on_message, msg.text
                )

            result = await self._generate_with_typing(_gen)
            await self._send_turn_outputs(result, proactive=False)

    async def _generate_with_typing(self, generation):
        """S4 typing wrap: run ``generation()`` (the LLM call) and the
        following ``response_delay_s`` sleep inside the channel's
        ``typing_context()`` when the channel exposes one (duck-typed
        ``getattr(channel, 'typing_context', None)`` probe; channels without
        it are a no-op). The indicator stays up while the reply is composed
        and the delay elapses; the send itself happens AFTER the context
        exits. Returns the generation result."""
        typing_ctx = getattr(self.channel, "typing_context", None)

        async def _gen():
            result = await generation()
            await self.sleeper(self._response_delay(result))
            return result

        if typing_ctx is None:
            return await _gen()
        async with typing_ctx():
            return await _gen()

    async def _send_turn_outputs(
        self, result, *, proactive: bool, reason: str | None = None
    ) -> None:
        """Send everything one turn produced through the channel.

        WS4 order (single reply-path invariant): decision-layer channel
        outputs first — ``proactive_out`` (initiate verdicts) as proactive
        messages, ``notices`` (no-reply verdicts) as plain messages — then
        the ordinary reply, when there is one (a suppressed reply is
        ``""`` and sends nothing).
        """
        for out_reason, text in getattr(result, "proactive_out", ()):
            await self.channel.send(
                OutboundMessage(text=text, proactive=True, reason=out_reason)
            )
        for notice in getattr(result, "notices", ()):
            await self.channel.send(OutboundMessage(text=notice, proactive=False))
        if (result.reply or "").strip():
            await self.channel.send(
                OutboundMessage(
                    text=result.reply, proactive=proactive, reason=reason
                )
            )

    # ------------------------------------------------------------------ #
    # day rollover
    # ------------------------------------------------------------------ #

    async def _rollover_loop(self) -> None:
        """Sleep until the next virtual midnight (paced), roll the session
        over, then — only on a REAL midnight (not the max_virtual_hours end
        boundary) — re-plan + persist the CURRENT day's schedule.

        CLOCK DISCIPLINE (it2 A3, the E0 confounder): the rollover never
        advances the virtual clock PAST a pending event. When the earliest
        pending event lies at-or-after now and before the target (midnight
        or max_virtual_hours) the rollover parks AT that event hour: it
        paces the clock there and yields (short poll) until the firing loop
        gates the event AT ITS OWN TIME. Overdue events (pending < now) are
        left to the firing loop's recovery evaluation. This makes
        accelerated time safe: a still-valid event can never be spuriously
        'expired' by a midnight jump (the old race), because the clock never
        passes the event while it is still pending. Once the firing loop has
        exited (run end) parking stops — nothing will gate the event, and
        the rollover winds down to its target."""
        while True:
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            next_midnight = (self.session.clock.day() + 1) * 24.0
            if self.max_virtual_hours is not None:
                target = min(next_midnight, self.max_virtual_hours)
            else:
                target = next_midnight
            async with self._lock:
                # Re-read the clock under the lock: the firing loop may have
                # advanced it (A9 deferral jumps) since the loop-top read.
                now = self.session.clock.now_h()
                # it3 B2: close the open conversation the instant its
                # boundary close is due (quiet-hours boundary crossed after
                # a restart, user silence deadline passed) — never lazily
                # at the next turn. Idempotent + cheap at every wake.
                await self._executor.run_in_thread(
                    self.session.check_conversation_lifecycle, now
                )
                # G0 A1: availability-negotiation wakes — lazy event-
                # boundary detection plus every due decide leg (AFK bomb
                # fired / window-close backstop). A go verdict closes the
                # conversation inside the session and returns her natural
                # close as a proactive outbound, sent below.
                neg_outs = await self._executor.run_in_thread(
                    self.session.check_negotiation, now
                )
                pending = self.schedule.next_pending(now)
                if pending is not None and pending < now - 1e-9:
                    # STRICTLY overdue row: ask the firing loop's OWN
                    # deferral verdict whether it will consume the row (None)
                    # or defer it (non-None) — same pure function, same lock
                    # discipline. (An ON-SCHEDULE row, pending ~= now, is
                    # never deferred-without-advance: the firing loop's
                    # R1-F1 leg advances the clock to the awake instant, so
                    # the rollover parks for it exactly as before.)
                    overdue_park = self._defer_verdict(pending, now) is None
                else:
                    overdue_park = True
                # it3 B2: park the clock at the conversation's next close
                # instant (quiet boundary or user_left deadline) so the
                # close is recorded AT its boundary, not at the next wake.
                # (close_t is a SEPARATE timer from the schedule-row park
                # below: the quiet-deferral verdict above governs schedule
                # rows, close_t governs the conversation boundary. When both
                # exist, the later park assignment keeps the EARLIER
                # instant — close_t wins if it precedes the pending row.)
                close_t = self.session.next_conversation_close_t_h(now)
                # G0 A1: park at the next availability-negotiation wake —
                # the AFK-bomb decide instant or the window-close backstop
                # instant — exactly like the conversation-close park above
                # (strictly-future instants only; a past deadline fires at
                # the next wake of any kind). The earlier of close_t and
                # neg_t wins via the min-style assignment below.
                neg_t = self.session.next_negotiation_trigger_t_h(now)
            for out_reason, text in neg_outs:
                await self.channel.send(
                    OutboundMessage(text=text, proactive=True, reason=out_reason)
                )
            if close_t is not None and close_t < target:
                target = close_t
            if neg_t is not None and neg_t < target:
                target = neg_t
            if (
                pending is not None
                # Overdue rows INCLUDED (no now <= pending bound): next_pending
                # returns overdue-first (A7), and an overdue row must route the
                # rollover into the target <= now yield below, letting the
                # firing loop clear it, instead of sleeping to midnight and
                # jumping the clock PAST the next future event — which then
                # expires instead of firing (the it2/FEED race, B8 Finding 4).
                and pending < target
                and not self._firing_done
                # ... but a STRICTLY OVERDUE row (pending < now) is parked
                # only when the firing loop will actually CONSUME it (verdict
                # None: it fires, suppresses or expires the row — even during
                # quiet hours, a past-validity straggler is expired by
                # policy, so it must be parked for that recovery). When the
                # verdict is a quiet-hours DEFERRAL (R-4b: still-valid row,
                # never consumed as fired-without-delivery; R-10: it stays
                # pending for a later awake run) the firing loop does NOT
                # advance the clock — parking would freeze the clock below
                # max_virtual_hours forever, the rollover waiting for the
                # firing loop while it waits for the rollover to wind down
                # (test_r4b hang, FEED regression). On-schedule rows
                # (pending ~= now) always park: their deferral is the R1-F1
                # leg that DOES advance the clock. Pre-feed the rollover
                # excluded overdue rows entirely (now <= pending bound); the
                # feed driver needs the overdue park to win the
                # fired/expired race (B8 F4), so keep it for every row the
                # firing loop will clear.
                and (pending > now or overdue_park)
            ):
                # Park at the earliest pending event; the firing loop gates
                # it at its own time. Never jump past it.
                target = pending
            if target <= now:
                # Parked at (or past) a pending event hour: yield to the
                # firing loop WITHOUT advancing the clock past the event.
                await self._poll_wait()
                continue
            await self._sleep_until_t_h(target, now)
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            async with self._lock:
                # FRESH read inside the lock: the pre-lock read above can be
                # stale — inbound messages that advanced the clock while we
                # waited for the lock make ``advance_hours(target - now)``
                # OVERSHOOT the target (e.g. the clock lands at 48.5 instead
                # of exactly 48.0). The feed driver's midnight guard relies
                # on the clock sitting EXACTLY at the boundary; an overshoot
                # hides the replan-in-progress lock and lets the driver
                # launch day-D feeds before the day-D schedule rows exist —
                # its feed then jumps the clock past a still-pending
                # opportunity, which the firing loop (starved behind the
                # feed) evaluates only after validity expired (the FEED
                # day-2 fired/expired race). Re-reading inside the lock
                # makes the advance exact: never past the park/midnight
                # target.
                now = self.session.clock.now_h()
                if now < target:
                    self.session.clock.advance_hours(target - now)
                now = self.session.clock.now_h()
                # it3 B2: the clock landed on a conversation close instant
                # — record the close at the boundary (idempotent no-op
                # otherwise).
                await self._executor.run_in_thread(
                    self.session.check_conversation_lifecycle, now
                )
                # G0 A1: the clock landed on (or crossed) a negotiation
                # park instant — run the due decide legs (AFK bomb /
                # backstop) at the boundary and send any natural close.
                neg_outs = await self._executor.run_in_thread(
                    self.session.check_negotiation, now
                )
                day = self.session.clock.day()
                if target >= next_midnight:
                    # A real rollover crossed midnight. At the run's end
                    # boundary (target == max_virtual_hours < next_midnight)
                    # we do NOT re-plan: that would inject a fresh plan for a
                    # run that is about to finish.
                    await self._executor.run_in_thread(self.session.ensure_day, day)
                    self._replan()
                    # S3: a /tz change queued via request_tz_change is applied
                    # at the next rollover — the epoch->t_h mapping never
                    # jumps (only the anchor's tz metadata moves).
                    self._apply_pending_tz()
            # G0 A1: negotiation decide-leg outputs (AFK bomb / backstop /
            # natural close) are sent AFTER the lock releases — channel I/O
            # never runs under the runtime lock (mirrors proactive sends).
            for out_reason, text in neg_outs:
                await self.channel.send(
                    OutboundMessage(text=text, proactive=True, reason=out_reason)
                )

    # ------------------------------------------------------------------ #
    # proactive firing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _response_delay(result) -> float:
        """Wall-clock seconds the runtime waits between LLM completion and
        channel.send. A1 wave 2 wires TurnResult.controls (GenerationControls);
        today's TurnResult carries the BehaviorDirective — both expose
        response_delay_s."""
        controls = getattr(result, "controls", None)
        if controls is not None:
            return float(controls.response_delay_s)
        return float(result.directive.response_delay_s)

    async def _firing_loop(self) -> None:
        """Wait for the next pending event (overdue events are visible after
        the A7 next_pending fix), advance the clock to it, resolve a GROUNDED
        intent AT the opportunity time (it2 A3: the scheduler's
        ContactOpportunity — no semantic reason on it — is resolved here into
        a ProactiveIntent carrying opportunity_id), gate it (content +
        context), then fire with the EXACT intent id or consume+log the
        suppression. Overdue events are evaluated on recovery: still valid ⇒
        fire; past the validity window ⇒ expire.

        Quiet hours (A9 R-4b): firing is blocked by the context gate, but a
        still-valid event whose validity outlives the quiet window must NOT
        be consumed as fired-without-delivery — it is deferred (row stays
        pending) until the next awake instant. Only events past valid_until
        are expired, and events that expire before the window ends are
        consumed (they can never be delivered). The deferral advances the
        virtual clock to the next awake instant (never past
        max_virtual_hours) so a still-pending event parked by the rollover
        cannot livelock the run (R1-F1); the event is re-evaluated — and
        fires — at that awake instant.
        """
        while True:
            now = self.session.clock.now_h()
            if self._max_reached(now):
                self._firing_done = True
                return
            async with self._lock:
                nxt = self.schedule.next_pending(now)
            if nxt is None:
                await self._poll_wait()
                continue
            if self.max_virtual_hours is not None and nxt >= self.max_virtual_hours:
                self._firing_done = True
                return
            if nxt > now:
                await self._sleep_until_t_h(nxt, now)
            defer_until: float | None = None
            async with self._lock:
                now = self.session.clock.now_h()
                if now < nxt:
                    self.session.clock.advance_hours(nxt - now)
                now = self.session.clock.now_h()
                defer_until = self._defer_verdict(nxt, now)
                if defer_until is not None and now - nxt < 1e-9:
                    # R1-F1 (it2 A3): an ON-SCHEDULE deferral (event gated at
                    # its own hour — the rollover is parked AT it, so the
                    # clock can never move on its own) must ADVANCE the
                    # virtual clock to the next awake instant — sleeping
                    # wall time alone would re-defer forever and the run
                    # would livelock (invariants 3/17). _defer_verdict
                    # already clamps to max_virtual_hours, so the advance
                    # never passes the run's end. Re-evaluate AT the awake
                    # instant inside the same lock: the still-valid event
                    # then falls through to the normal path below
                    # (resolve/gate at the opportunity hour ``nxt``, envelope
                    # at the awake ``now`` — pitfall 49 semantics preserved)
                    # and fires exactly once. An OVERDUE recovery inside
                    # quiet hours (now > nxt, e.g. a restart at 03:00) is
                    # NOT advanced: the rollover is not parked there, so the
                    # run winds down to max_virtual_hours on its own and the
                    # still-valid event stays pending for a later awake run
                    # (A9 R-10 restart semantics).
                    if self.anchor is not None:
                        # Anchor mode: pace the deferral in REAL time BEFORE
                        # the advance — the virtual clock must never run
                        # ahead of the wall clock, so a mute/quiet-deferred
                        # event fires only once the deferral instant actually
                        # arrives (the absolute sleep self-corrects). None
                        # mode keeps the instant jump (byte-identical).
                        await self._sleep_until_t_h(defer_until, now)
                    self.session.clock.advance_hours(defer_until - now)
                    now = self.session.clock.now_h()
                    defer_until = self._defer_verdict(nxt, now)
                # it3 B2: the clock may have jumped past a conversation
                # close boundary (deferral to the next awake instant) —
                # record the close at the detection instant (idempotent).
                await self._executor.run_in_thread(
                    self.session.check_conversation_lifecycle, now
                )
                # G0 A1: the clock advance may also have crossed an
                # availability-negotiation park instant (AFK bomb /
                # backstop) — run the due decide legs here and send any
                # natural close through the channel.
                neg_outs = await self._executor.run_in_thread(
                    self.session.check_negotiation, now
                )
                for out_reason, text in neg_outs:
                    await self.channel.send(
                        OutboundMessage(text=text, proactive=True, reason=out_reason)
                    )
                if defer_until is None:
                    day = self.session.clock.day()
                    # Contact opportunity -> contact reason: the scheduler's
                    # ContactOpportunity (NO semantic reason) is resolved at
                    # OPPORTUNITY time (nxt) into a grounded intent, so an
                    # overdue event is evaluated against its own window on
                    # recovery. None ⇒ SUPPRESS: no_grounded_reason is a
                    # legitimate outcome, never an error.
                    opportunity = self.schedule.opportunity_for(nxt)
                    if opportunity is not None:
                        self.store.log_event(
                            day, nxt, "contact_opportunity",
                            f"id={opportunity.id} "
                            f"desired={opportunity.desired_t_h:.3f} "
                            f"valid_until={opportunity.valid_until_t_h:.3f} "
                            f"hazard={opportunity.hazard_components}",
                        )
                    intent = self.resolver.resolve(
                        opportunity if opportunity is not None else nxt
                    )
                    if intent is None:
                        self.store.log_event(
                            day, now, "proactive_suppressed", "no_grounded_reason"
                        )
                        self.schedule.mark_fired_persisted(
                            nxt, now, self.seed, self.store
                        )
                        continue
                    self.store.save_proactive_intent(intent)
                    cg = content_gate(intent, self.store, now_h=now)
                    xg = context_gate(
                        now,
                        day,
                        store=self.store,
                        timing=self.timing,
                        last_fired_t_h=self.store.last_proactive_t_h(self.seed),
                    )
                    if not (cg.allowed and xg.allowed):
                        code = cg.code if cg.code != "ok" else xg.code
                        self.store.log_event(
                            day, now, "proactive_suppressed", code
                        )
                        self.store.update_proactive_intent_status(
                            intent.id, "suppressed"
                        )
                        if cg.code == "expired":
                            self.store.mark_schedule_expired(self.seed, nxt)
                            self.schedule.mark_fired(nxt)
                        else:
                            self.schedule.mark_fired_persisted(
                                nxt, now, self.seed, self.store
                            )
                        continue
                    async def _gen():
                        return await self._fire_exact_intent(intent)

                    result = await self._generate_with_typing(_gen)
                    await self._send_turn_outputs(
                        result, proactive=True, reason=intent.reason
                    )
                    self.store.update_proactive_intent_status(intent.id, "fired")
                    self.schedule.mark_fired_persisted(
                        nxt, now, self.seed, self.store
                    )
            if defer_until is not None:
                await self._sleep_until_t_h(defer_until, now)

    async def _fire_exact_intent(self, intent):
        """Fire ``session.fire_proactive(intent.id)`` — the EXACT validated
        intent id (A5 seam; invariants 6/7: two same-reason intents are
        never interchangeable; the runtime never downgrades identity to
        reason type).

        Transitional leg: while A5's ``fire_proactive(intent_id)`` session
        has not merged, the legacy session accepts a REASON and raises
        ``ValueError("unknown proactive reason: ...")`` for an id. That
        exact legacy message is caught and retried with the intent's reason
        so pre-A5 callers keep working; A5's merge retires this leg (its
        session fetches by id, and its own ValueErrors — e.g. unknown
        intent — propagate)."""
        try:
            return await self._executor.run_in_thread(
                self.session.fire_proactive, intent.id
            )
        except ValueError as exc:
            if "unknown proactive reason" not in str(exc):
                raise
            return await self._executor.run_in_thread(
                self.session.fire_proactive, intent.reason
            )

    # ------------------------------------------------------------------ #
    # quiet-hours deferral (A9 R-4b)
    # ------------------------------------------------------------------ #

    def _quiet_defer_until(self, nxt: float, now: float) -> float | None:
        """Quiet-hours deferral verdict for an overdue event recovered at
        ``now`` (event hour ``nxt``).

        Quiet hours block firing (context gate) but must NOT consume a still-
        valid event as fired-without-delivery — that loses a message the
        store still grounds (A9 R-4b). Returns the virtual hour to sleep
        until (the next awake instant, capped at max_virtual_hours) when the
        event should be deferred; None when the event is evaluated normally:

        - not quiet hours;
        - already past valid_until (the normal path expires the row);
        - expiring before the quiet window ends — such an event can never be
          delivered, so consuming it loses nothing (A9 R-6 pins this leg).
        """
        if envelope(now % 24.0, self.timing) >= 1e-9:
            return None
        valid_until = self._valid_until_of(nxt)
        if valid_until is None:
            return None
        if now > valid_until:
            return None  # past validity: the normal path expires the row
        awake_at = self._next_awake_at(now)
        if valid_until <= awake_at:
            return None  # expires before the window ends -> can never fire
        if self.max_virtual_hours is not None:
            return min(awake_at, self.max_virtual_hours)
        return awake_at

    def _valid_until_of(self, nxt: float) -> float | None:
        """Validity deadline of the schedule row at ``nxt``: the opportunity's
        ``valid_until_t_h`` when the row is an opportunity, else the reason's
        validity window past the event hour. None when the row is unknown."""
        opportunity = self.schedule.opportunity_for(nxt)
        if opportunity is not None:
            return opportunity.valid_until_t_h
        rows = [
            r for r in self.store.schedule_events_for_seed(self.seed)
            if abs(float(r["t_h"]) - nxt) < 1e-9
        ]
        if not rows:
            return None
        return nxt + REASON_VALIDITY_H[rows[0]["reason"]]

    def _defer_verdict(self, nxt: float, now: float) -> float | None:
        """Combined deferral verdict (quiet hours + mute window): the latest
        virtual hour both constraints allow firing, or None when the event is
        evaluated normally. With no mute active this is exactly the
        quiet-hours verdict (byte parity)."""
        q = self._quiet_defer_until(nxt, now)
        m = self._mute_defer_until(nxt, now)
        if q is None:
            return m
        if m is None:
            return q
        return max(q, m)

    def _mute_defer_until(self, nxt: float, now: float) -> float | None:
        """Mute-window deferral verdict (S3 /mute hook): while a mute request
        is active, a still-valid event is DEFERRED (never consumed as
        fired-without-delivery — the row stays pending, mirroring the
        quiet-hours R-4b semantics) to the mute end; an event that expires
        BEFORE the window ends can never be delivered and is consumed
        normally. Returns the virtual hour to sleep until (the mute end,
        capped at max_virtual_hours), or None when the event is evaluated
        normally."""
        if self._mute_until_t_h is None or now >= self._mute_until_t_h:
            return None
        valid_until = self._valid_until_of(nxt)
        if valid_until is None:
            return None
        if now > valid_until:
            return None  # past validity: the normal path expires the row
        if valid_until <= self._mute_until_t_h:
            return None  # expires inside the mute window -> can never fire
        if self.max_virtual_hours is not None:
            return min(self._mute_until_t_h, self.max_virtual_hours)
        return self._mute_until_t_h

    # ------------------------------------------------------------------ #
    # S3 command dispatch (ControlCommand -> harness.commands)
    # ------------------------------------------------------------------ #

    async def _on_command(self, cmd) -> None:
        """S3 command dispatch: route a :class:`ControlCommand` (delivered by
        the channel's ``start(on_message, on_command=...)`` seam) to
        ``harness.commands.handle_command`` UNDER the runtime lock — NEVER
        ``session.on_message`` (no turn, no closing draws, no memory writes;
        the context is read-only session facts + narrow hooks).

        ``harness/commands.py`` does not exist yet in this worktree
        (W-commands creates it, merged AFTER W-runtime) — the import is
        FUNCTION-LEVEL so runtime.py imports cleanly before that file lands.
        The handler runs on the owned executor (never blocking the event
        loop); its reply is sent as a plain non-proactive OutboundMessage.
        """
        from harness.commands import CommandContext, handle_command  # W-commands

        async with self._lock:
            ctx = CommandContext(
                store=self.store,
                clock=self.session.clock,
                anchor=self.anchor,
                persona_exists=self._persona_exists(),
                pending_proactive_count=self._pending_proactive_count(),
                flags=self._command_flags(),
                request_tz_change=self._request_tz_change,
                request_mute=self._request_mute,
            )
            reply = await self._executor.run_in_thread(handle_command, cmd, ctx)
        if reply:
            await self.channel.send(OutboundMessage(text=reply, proactive=False))

    def _persona_exists(self) -> bool:
        """CommandContext fact: whether a persona row is persisted (the
        /setup guard). Defensive against seam-less store stubs."""
        load = getattr(self.store, "load_persona", None)
        return bool(load() if callable(load) else False)

    def _pending_proactive_count(self) -> int:
        """CommandContext fact: pending (not yet fired/expired) schedule rows
        for this seed — the /status backlog."""
        rows_fn = getattr(self.store, "schedule_events_for_seed", None)
        if rows_fn is None:
            return 0
        return sum(1 for r in rows_fn(self.seed) if r.get("status") == "pending")

    #: Documented boolean HARNESS_* feature flags surfaced in CommandContext.
    _COMMAND_FLAG_ENV = (
        "HARNESS_DEBOUNCE",
        "HARNESS_TWO_PHASE_CLOSE",
        "HARNESS_TYPING",
        "HARNESS_VERBOSE",
    )

    def _command_flags(self) -> dict:
        """CommandContext fact: the boolean harness feature flags (env,
        default OFF) — /status reads them."""
        return {name: _env_bool(name) for name in self._COMMAND_FLAG_ENV}

    def _request_tz_change(self, tz: str) -> None:
        """CommandContext hook (S3 /tz): queue a timezone change, applied at
        the NEXT rollover. The epoch->t_h mapping never jumps — only the
        anchor's tz metadata moves — so the virtual clock cannot go
        backwards. Raises ValueError on an unknown IANA name."""
        try:
            ZoneInfo(tz)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {tz!r}") from exc
        self._pending_tz = tz

    def _apply_pending_tz(self) -> None:
        """Apply a queued /tz change at rollover: re-persist the anchor's tz
        metadata (seam S1 kv keys) and update the in-memory anchor. Without
        an anchor there is nothing to apply to — the request is dropped."""
        if self._pending_tz is None:
            return
        if self.anchor is not None:
            self.anchor = replace(self.anchor, tz=self._pending_tz)
            persist_anchor(self.store, self.anchor)
        self._pending_tz = None

    def _request_mute(self, hours: float) -> None:
        """CommandContext hook (S3 /mute): defer proactives for ``hours`` —
        still-valid pending events are deferred (never consumed) until the
        window ends; events expiring inside the window are consumed normally.
        The mute is pure runtime pacing: no session state, no memory writes."""
        self._mute_until_t_h = self.session.clock.now_h() + float(hours)

    def _next_awake_at(self, now: float) -> float:
        """First virtual hour after ``now`` at which the circadian envelope
        is fully awake (quiet_fin + ramp) — the moment a deferred event can
        actually pass the context gate."""
        _quiet_ini, quiet_fin = self.timing.quiet_hours
        day = int(now // 24.0)
        boundary = (
            day * 24.0 + quiet_fin
            if now % 24.0 < quiet_fin
            else (day + 1) * 24.0 + quiet_fin
        )
        return boundary + ENVELOPE_RAMP_H
