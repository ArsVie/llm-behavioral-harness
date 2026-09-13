"""Async runtime — real-time rollover + gated proactive firing.

The ONLY harness code that reads wall-clock, and only to pace the virtual
clock (``TimeScale``: real seconds per virtual hour). ``session.*`` calls
run on an owned executor under one lock; anchor mode runs in real time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import cast
from zoneinfo import ZoneInfo

import engine.rng as rng_mod
from engine.circadian import envelope
from engine.types import ENVELOPE_RAMP_H, TimingParams
from harness.anchor import RealTimeAnchor
from harness.channels.base import Channel, InboundMessage, OutboundMessage

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
from harness.env import env_bool as _env_bool

#: Poll cadence with no pending schedule event, in virtual hours.
POLL_INTERVAL_H = 0.05

#: Longest single sleep the rollover PARK takes before re-checking whether a
#: nearer wake was armed (real seconds; anchor mode only).
RETARGET_SLICE_S = 60.0

#: Store kv keys that persist the RealTimeAnchor.
ANCHOR_KV_KEYS = ("anchor.epoch0_s", "anchor.t_h0", "anchor.tz")


#: Turn failures are reported here rather than killing the run.
_logger = logging.getLogger(__name__)


def load_anchor(store) -> RealTimeAnchor | None:
    """Load the persisted RealTimeAnchor from the store's kv seam.

    Returns None when the seam is absent or any key is missing; never raises
    on partial state.
    """
    get_kv = getattr(store, "get_kv", None)
    if get_kv is None:
        return None
    values = [get_kv(k) for k in ANCHOR_KV_KEYS]
    if any(v is None for v in values):
        return None
    return RealTimeAnchor(float(values[0]), float(values[1]), values[2])


def persist_anchor(store, anchor: RealTimeAnchor) -> None:
    """Persist the anchor under the kv keys (overwrite); no-op without the
    kv seam."""
    set_kv = getattr(store, "set_kv", None)
    if set_kv is None:
        return
    set_kv("anchor.epoch0_s", str(anchor.epoch0_s))
    set_kv("anchor.t_h0", str(anchor.t_h0))
    set_kv("anchor.tz", anchor.tz)




@dataclass
class TimeScale:
    """Pace the virtual clock against wall-clock.

    ``seconds_per_virtual_hour``: real seconds per virtual hour; 3600.0 is
    real time, tiny values (e.g. 0.001) run days in milliseconds.
    """

    seconds_per_virtual_hour: float = 3600.0


class AsyncRuntime:
    """Orchestrate a Session, a persisted ProactiveSchedule and a Channel.

    With ``anchor: RealTimeAnchor`` (default OFF) the clock runs in REAL time
    and resumes at the current real virtual hour; clock skew raises.
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
        survive_turn_failures: bool = False,
    ):
        self.session = session
        self.schedule = schedule
        self.channel = channel
        self.store = store
        self.timing = timing
        self.seed = seed
        self.time_scale = time_scale
        self.max_virtual_hours = max_virtual_hours
        #: Real-time anchor; None = paced virtual time.
        self.anchor: RealTimeAnchor | None = anchor
        #: Injectable wall clock for absolute sleeps and resume; default time.time.
        self._now = now if now is not None else time.time
        #: When True, run() registers _on_command with channel.start().
        self.enable_commands = enable_commands
        #: Turn-failure policy. False (default) = fail fast; True = report the
        #: failure and stay up.
        self.survive_turn_failures = bool(survive_turn_failures)
        #: Virtual hour the anchor resume set the clock to (None before start).
        self._t_h_start: float | None = None
        #: Queued /tz change, applied at the next rollover.
        self._pending_tz: str | None = None
        #: Mute window end (virtual hours); pending events defer until then.
        self._mute_until_t_h: float | None = None
        #: Grounded-intent resolver, store-backed with a seeded rng stream.
        self.resolver = resolver if resolver is not None else IntentResolver(
            store, rng=rng_mod.stream_rng(seed, rng_mod.EXPERIMENT_STREAM)
        )
        #: Injectable delay function; default concurrency.default_sleeper.
        self.sleeper: Sleeper = sleeper if sleeper is not None else default_sleeper()
        #: Owned executor and resource registry for this runtime.
        self._executor = ExecutorOwner("runtime").start()
        self._registry = ResourceRegistry("runtime")
        self._registry.register(store, owned=False)   # injected: creator owns it
        self._registry.register(channel, owned=False)  # injected: creator owns it
        self._lock = asyncio.Lock()
        #: Set when the firing loop exits; the rollover stops parking events.
        self._firing_done = False
        #: Raised by :meth:`request_retarget` when a turn may have armed a
        #: nearer wake instant; cuts the rollover park short so it re-surveys.
        self._retarget = asyncio.Event()
        self._ensure_thread_safe_store()

    def _ensure_thread_safe_store(self) -> None:
        """Re-open the store connection for cross-thread use.

        ``session.*`` calls run on worker threads, so the thread-bound sqlite
        connection is re-opened with ``check_same_thread=False`` and
        registered owned=False — ``SQLiteStore.close()`` owns it. Stores with
        no sqlite connection (test fakes) are skipped."""
        if not (hasattr(self.store, "path") and hasattr(self.store, "conn")):
            return
        old_conn = self.store.conn
        conn = ensure_thread_safe_connection(self.store.path)
        if old_conn is not None:
            old_conn.close()  # schema-creation conn from SQLiteStore.__init__
        self.store.conn = conn
        self._registry.register(conn, owned=False)

    # lifecycle

    async def run(self) -> None:
        """Start the channel, run rollover + firing until max_virtual_hours
        (or cancelled), then finalize the current day, stop the channel and
        shut down the owned executor (injected resources are never closed
        here).

        Anchor mode: the virtual clock is positioned at the CURRENT real
        virtual hour first. With ``enable_commands`` the channel is started
        with the ``on_command`` callback.
        """
        if self.anchor is not None:
            self._apply_anchor_resume()
        # Her clock starts before the channel does: the pending backlog
        # resolves as initialization, independent of any user message.
        await self._session_call(self.session.settle_pending, "startup")
        if self.enable_commands:
            await self.channel.start(self._on_inbound, on_command=self._on_command)
        else:
            await self.channel.start(self._on_inbound)
        try:
            await asyncio.gather(
                self._rollover_loop(), self._firing_loop(), self._life_loop()
            )
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
        """Wait one polling cadence. Unanchored: a real asyncio sleep; anchor
        mode: the injectable sleeper, so a fake wall clock consumes the poll
        time."""
        if self.anchor is None:
            await asyncio.sleep(self._poll_sleep())
        else:
            await self.sleeper(self._poll_sleep())

    def request_retarget(self) -> None:
        """Announce that the set of future wake instants may have changed.

        Cuts the rollover park short so it re-surveys and parks at whichever
        instant is now earliest. A hint only: nothing fires because of it.
        """
        self._retarget.set()

    async def _sleep_until_t_h(self, target_t_h: float, now_h: float,
                               *, interruptible: bool = False) -> bool:
        """Sleep until the virtual clock reaches ``target_t_h``.

        Returns True when reached; False when an ``interruptible`` sleep was
        cut short by :meth:`request_retarget` (the caller must re-survey
        instead of landing on the stale target). Only the rollover PARK is
        interruptible.

        Anchor mode sleeps the REMAINING real seconds to the target's epoch,
        re-checking after every wake; the CALLER advances the virtual clock
        after this returns. Unanchored: a paced asyncio sleep.
        """
        if self.anchor is None:
            # Accelerated runs are never interrupted.
            await asyncio.sleep(
                (target_t_h - now_h) * self.time_scale.seconds_per_virtual_hour
            )
            return True
        deadline = self.anchor.epoch_of(target_t_h)
        while True:
            remaining = deadline - self._now()
            if remaining <= 0:
                return True
            if not interruptible:
                await self.sleeper(remaining)
                continue
            if self._retarget.is_set():
                self._retarget.clear()
                return False
            # Sliced, not raced: the sleeper is the only thing that moves
            # time, and a slice still notices a retarget.
            await self.sleeper(min(remaining, RETARGET_SLICE_S))

    # anchor resume

    def _apply_anchor_resume(self) -> None:
        """Position the virtual clock at the CURRENT real virtual hour
        (``anchor.t_h_at(now)``) on startup, instead of the persisted day's
        virtual midnight.

        Raises on clock skew: the persisted state already reached a later
        virtual hour than the anchor maps the wall clock to.
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
        session clock, latest day's rollover, the open conversation's last
        turn, and the event log. Schedule rows are EXCLUDED (planned future
        times)."""
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
        """Plan ONLY the current day: days 0..current_day, so the schedule
        covers through today. Re-planning regenerates identical rows —
        INSERT OR IGNORE never drifts."""
        return self.session.clock.day() + 1

    def _replan(self) -> None:
        """Plan the CURRENT day with real timing feedback, persist it, and
        refresh the schedule from the store (already-fired/expired rows are
        never re-selected).

        Runs on the event loop directly — plan_and_persist holds no session
        state. ``scores`` must never be None in live scheduling.
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

    # reactive path

    async def _on_inbound(self, msg: InboundMessage) -> None:
        """Reactive path: advance the clock to the message's arrival hour if
        supplied (never backwards), reply via ``Session.on_message``, wait
        the requested ``response_delay_s``, then send the reply as a
        non-proactive OutboundMessage.

        The user message is queued as a ``user_message_mid_turn`` steer
        before the turn runs. Generation and the delay run inside the
        channel's ``typing_context()`` when it exposes one (no-op otherwise).

        Anchor mode advances the clock to ``anchor.t_h_at(msg.received_at)``
        (the REAL arrival time); otherwise ``msg.t_h`` applies, and no t_h
        means no advance.
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

            try:
                result = await self._generate_with_typing(_gen)
                await self._send_turn_outputs(result, proactive=False)
            except asyncio.CancelledError:
                raise  # shutdown, not a turn failure
            except Exception as exc:  # noqa: BLE001 - the run must outlive one turn
                # Without this the exception escapes into the channel's
                # handler and is swallowed: log it, record it, stay up.
                _logger.exception("reactive turn failed")
                self.store.log_event(
                    self.session.clock.day(), self.session.clock.now_h(),
                    "reply_failed", f"error={type(exc).__name__}: {exc}",
                )
                if not self.survive_turn_failures:
                    raise
        # A turn can arm a nearer wake than the rollover is parked at: announce
        # it OUTSIDE the lock, after the reply went out, so the loop re-surveys.
        self.request_retarget()

    async def _generate_with_typing(self, generation):
        """Run ``generation()`` (the LLM call) and the following
        ``response_delay_s`` sleep inside the channel's ``typing_context()``
        when it exposes one (no-op otherwise); the send happens AFTER the
        context exits. Returns the generation result."""
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

        Channel outputs first — ``proactive_out`` (initiate verdicts) as
        proactive messages, ``notices`` (no-reply verdicts) as plain
        messages — then the ordinary reply, when there is one (a suppressed
        reply is ``""`` and sends nothing).

        With bubbles the reply is fanned out as a paced multi-send; the
        persisted reply stays the single joined text.
        """
        # A turn can arm a NEARER wake than the rollover is parked at: announce
        # it before the sends so the bubbling early-return cannot skip it.
        self.request_retarget()
        for out_reason, text in getattr(result, "proactive_out", ()):
            await self.channel.send(
                OutboundMessage(text=text, proactive=True, reason=out_reason)
            )
        for notice in getattr(result, "notices", ()):
            await self.channel.send(OutboundMessage(text=notice, proactive=False))
        bubbles = getattr(result, "bubbles", None)
        if bubbles:
            for i, part in enumerate(bubbles):
                if i > 0:
                    gap = 1.0 + 0.5 * len(part) / 80.0
                    gap = max(0.6, min(gap, 2.5))
                    await self.sleeper(gap)
                await self.channel.send(
                    OutboundMessage(text=part, proactive=proactive, reason=reason)
                )
            return
        if (result.reply or "").strip():
            await self.channel.send(
                OutboundMessage(
                    text=result.reply, proactive=proactive, reason=reason
                )
            )

    # day rollover

    async def _life_loop(self) -> None:
        """Her day on her own clock: wake at each agenda instant and resolve
        it — decide rounds, records, no user required, nothing sent.

        Mirrors the firing loop's shape: survey the next wake, sleep to it
        in real time (anchor mode), advance the virtual clock under the
        lock, then settle. With nothing ahead it polls, so a rollover
        replan is picked up on the cadence.
        """
        if not self.session.life_instants_enabled():
            return
        while True:
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            nxt = await self._session_call(self.session.next_event_instant, now)
            if nxt is None:
                await self._poll_wait()
                continue
            if self.max_virtual_hours is not None and nxt >= self.max_virtual_hours:
                return
            if nxt > now:
                await self._sleep_until_t_h(nxt, now)
            async with self._lock:
                at = self.session.clock.now_h()
                if at < nxt:
                    self.session.clock.advance_hours(nxt - at)
            await self._session_call(self.session.settle_pending, "event-instant")

    async def _rollover_loop(self) -> None:
        """Sleep until the next virtual midnight (paced), roll the session
        over, then — only on a REAL midnight, not the max_virtual_hours end
        boundary — re-plan + persist the CURRENT day's schedule.

        CLOCK DISCIPLINE: the rollover never advances the clock PAST a
        pending event — it parks AT the event hour and yields until the
        firing loop gates it. Overdue events go to the firing loop's
        recovery evaluation.
        """
        while True:
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            next_midnight = (self.session.clock.day() + 1) * 24.0
            target, now, neg_outs = await self._survey_park_target(next_midnight)
            await self._send_neg_outs(neg_outs)
            if target <= now:
                # Yield to the firing loop without advancing past the parked event hour.
                await self._poll_wait()
                continue
            if not await self._sleep_until_t_h(target, now,
                                               interruptible=True):
                # A turn armed a nearer wake mid-sleep: re-survey rather
                # than land on a target that is no longer the earliest.
                continue
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            await self._land_on_target(target, next_midnight)

    async def _survey_park_target(
        self, next_midnight: float
    ) -> tuple[float, float, list]:
        """Where the rollover may pace the clock to.

        Returns ``(target, now, neg_outs)``. The target starts at the next
        midnight (or the run end) and is pulled EARLIER by whichever of the
        conversation's next close, the next negotiation wake, and the
        earliest pending event lands first. ``neg_outs`` is returned unsent —
        the caller delivers it after the lock releases.
        """
        if self.max_virtual_hours is not None:
            target = min(next_midnight, self.max_virtual_hours)
        else:
            target = next_midnight
        async with self._lock:
            # Re-read the clock under the lock; the firing loop may have advanced it.
            now = self.session.clock.now_h()
            neg_outs = await self._due_hook_outputs(now)
            pending = self.schedule.next_pending(now)
            if pending is not None and pending < now - 1e-9:
                # Overdue row: park only if the firing loop will consume it (verdict None).
                overdue_park = self._defer_verdict(pending, now) is None
            else:
                overdue_park = True
            # Park at the conversation's next close instant (quiet boundary or deadline).
            close_t = self.session.next_conversation_close_t_h(now)
            # Park at the next availability-negotiation wake (AFK bomb or backstop).
            neg_t = self.session.next_negotiation_trigger_t_h(now)
        if close_t is not None and close_t < target:
            target = close_t
        if neg_t is not None and neg_t < target:
            target = neg_t
        if (
            pending is not None
            # Include overdue rows: next_pending returns overdue-first.
            and pending < target
            and not self._firing_done
            # Park strictly overdue rows only when the firing loop will consume them.
            and (pending > now or overdue_park)
        ):
            # Park at the earliest pending event; the firing loop gates it there.
            target = pending
        return target, now, neg_outs

    async def _land_on_target(self, target: float,
                              next_midnight: float) -> None:
        """Advance the clock onto ``target`` and run what landing there owes.

        Only a target at-or-past the next midnight is a REAL rollover: the
        run-end boundary reaches the same code path but must not re-plan the
        day or apply a queued timezone change.
        """
        async with self._lock:
            # Re-read the clock inside the lock so the advance cannot overshoot the target.
            now = self.session.clock.now_h()
            if now < target:
                self.session.clock.advance_hours(target - now)
            now = self.session.clock.now_h()
            neg_outs = await self._due_hook_outputs(now)
            day = self.session.clock.day()
            if target >= next_midnight:
                # A real rollover crossed midnight; the run-end boundary does not re-plan.
                await self._executor.run_in_thread(self.session.ensure_day, day)
                self._replan()
                # Apply a queued /tz change at the next rollover.
                self._apply_pending_tz()
        # Send negotiation decide-leg outputs after the lock releases.
        await self._send_neg_outs(neg_outs)

    # proactive firing

    @staticmethod
    def _response_delay(result) -> float:
        """Wall-clock seconds to wait between LLM completion and
        ``channel.send`` (``result.controls.response_delay_s``)."""
        return float(result.controls.response_delay_s)

    async def _firing_loop(self) -> None:
        """Wait for the next pending event, advance the clock to it, resolve
        a grounded intent AT the opportunity time, gate it (content +
        context), then fire with the EXACT intent id or consume + log the
        suppression.

        Quiet hours: firing is blocked by the context gate, but a still-valid
        event is DEFERRED (row stays pending) until the next awake instant —
        never consumed as fired-without-delivery; only events past
        ``valid_until`` expire. The deferral advances the clock to that awake
        instant so a parked event cannot livelock the run.
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
            defer_until, now = await self._service_event(nxt)
            if defer_until is not None:
                await self._sleep_until_t_h(defer_until, now)

    async def _service_event(self, nxt: float) -> tuple[float | None, float]:
        """Do everything one pending event needs, under the runtime lock.

        Returns ``(defer_until, now)``. Non-None ``defer_until`` means the
        caller sleeps to that instant and the row stays pending; None means
        the event was consumed (fired, suppressed, expired or failed).
        """
        async with self._lock:
            now = self.session.clock.now_h()
            if now < nxt:
                self.session.clock.advance_hours(nxt - now)
            now = self.session.clock.now_h()
            defer_until, now = await self._settle_deferral(nxt, now)
            await self._run_due_hooks(now)
            if defer_until is not None:
                return defer_until, now
            day = self.session.clock.day()
            intent = self._resolve_grounded_intent(nxt, now, day)
            if intent is None:
                return None, now
            if not self._gate_or_consume(intent, nxt, now, day):
                return None, now
            await self._fire_or_record_failure(intent, nxt, now, day)
            return None, now

    async def _settle_deferral(self, nxt: float,
                               now: float) -> tuple[float | None, float]:
        """Resolve the quiet-hours deferral verdict, advancing if it is due.

        An ON-SCHEDULE deferral advances the virtual clock to the next awake
        instant and re-asks; an OVERDUE recovery does not advance (it is
        already late).
        """
        defer_until = self._defer_verdict(nxt, now)
        if defer_until is None or now - nxt >= 1e-9:
            return defer_until, now
        if self.anchor is not None:
            # Anchor mode sleeps in real time before the deferral advance.
            await self._sleep_until_t_h(defer_until, now)
        self.session.clock.advance_hours(defer_until - now)
        now = self.session.clock.now_h()
        return self._defer_verdict(nxt, now), now

    async def _due_hook_outputs(self, now: float) -> list:
        """Run the session hooks the clock's arrival at ``now`` makes due and
        return the negotiation output UNSENT.

        Delivery is the caller's business on purpose: the firing loop sends
        while the lock is held, the rollover sends after releasing it.
        """
        await self._executor.run_in_thread(
            self.session.check_conversation_lifecycle, now
        )
        return await self._executor.run_in_thread(
            self.session.check_negotiation, now
        )

    async def _send_neg_outs(self, neg_outs) -> None:
        """Deliver negotiation decide-leg output as proactive messages."""
        for out_reason, text in neg_outs:
            await self.channel.send(
                OutboundMessage(text=text, proactive=True, reason=out_reason)
            )

    async def _run_due_hooks(self, now: float) -> None:
        """Run the due session hooks and send their output (firing-loop
        order: delivery happens while the lock is still held)."""
        await self._send_neg_outs(await self._due_hook_outputs(now))

    def _resolve_grounded_intent(self, nxt: float, now: float, day: int):
        """Resolve the contact opportunity into a grounded ProactiveIntent.

        The resolver supplies the reason (the opportunity carries none). None
        means nothing grounded — the row is consumed as a suppression.
        """
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
            self.schedule.mark_fired_persisted(nxt, now, self.seed, self.store)
            return None
        self.store.save_proactive_intent(intent)
        return intent

    def _gate_or_consume(self, intent, nxt: float, now: float,
                         day: int) -> bool:
        """True when both gates allow the intent; otherwise consume it.

        An EXPIRED intent marks the schedule row expired; every other
        suppression consumes the row normally. Both paths record the
        rejecting gate code.
        """
        cg = content_gate(intent, self.store, now_h=now)
        xg = context_gate(
            now,
            day,
            store=self.store,
            timing=self.timing,
            last_fired_t_h=self.store.last_proactive_t_h(self.seed),
        )
        if cg.allowed and xg.allowed:
            return True
        code = cg.code if cg.code != "ok" else xg.code
        self.store.log_event(day, now, "proactive_suppressed", code)
        self.store.update_proactive_intent_status(intent.id, "suppressed")
        if cg.code == "expired":
            self.store.mark_schedule_expired(self.seed, nxt)
            self.schedule.mark_fired(nxt)
        else:
            self.schedule.mark_fired_persisted(nxt, now, self.seed, self.store)
        return False

    async def _fire_or_record_failure(self, intent, nxt: float, now: float,
                                      day: int) -> None:
        """Generate and deliver one proactive turn; never let it end the run.

        A failure consumes the schedule row and marks the intent ``failed``;
        the ``proactive_failed`` event names the exception.
        """
        try:
            async def _gen():
                return await self._fire_exact_intent(intent)

            result = await self._generate_with_typing(_gen)
            await self._send_turn_outputs(
                result, proactive=True, reason=intent.reason
            )
        except asyncio.CancelledError:
            raise  # shutdown, not a turn failure
        except Exception as exc:  # noqa: BLE001 - the run must outlive one turn
            _logger.exception("proactive turn failed (intent %s)", intent.id)
            self.store.log_event(
                day, now, "proactive_failed",
                f"id={intent.id} error={type(exc).__name__}: {exc}",
            )
            self.store.update_proactive_intent_status(intent.id, "failed")
            if not self.survive_turn_failures:
                self.schedule.mark_fired_persisted(
                    nxt, now, self.seed, self.store
                )
                raise
        else:
            self.store.update_proactive_intent_status(intent.id, "fired")
        self.schedule.mark_fired_persisted(nxt, now, self.seed, self.store)

    async def _fire_exact_intent(self, intent):
        """Fire ``session.fire_proactive(intent.id)`` — the EXACT validated
        intent id, never a reason type."""
        return await self._executor.run_in_thread(
            self.session.fire_proactive, intent.id
        )

    # quiet-hours deferral

    def _quiet_defer_until(self, nxt: float, now: float) -> float | None:
        """Quiet-hours deferral verdict for an overdue event recovered at
        ``now`` (event hour ``nxt``).

        Quiet hours block firing but must NOT consume a still-valid event as
        fired-without-delivery. Returns the virtual hour to sleep until (next
        awake instant, capped at max_virtual_hours) or None when the event is
        evaluated normally: not quiet; past ``valid_until``; or expiring
        before the window ends (such an event can never be delivered).
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
            return None  # expires before the window ends
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
        evaluated normally."""
        q = self._quiet_defer_until(nxt, now)
        m = self._mute_defer_until(nxt, now)
        if q is None:
            return m
        if m is None:
            return q
        return max(q, m)

    def _mute_defer_until(self, nxt: float, now: float) -> float | None:
        """Mute-window deferral verdict: while a mute request is active, a
        still-valid event is DEFERRED (row stays pending) to the mute end; an
        event expiring BEFORE the window ends is consumed normally. Returns
        the virtual hour to sleep until (mute end, capped at
        max_virtual_hours) or None."""
        if self._mute_until_t_h is None or now >= self._mute_until_t_h:
            return None
        valid_until = self._valid_until_of(nxt)
        if valid_until is None:
            return None
        if now > valid_until:
            return None  # past validity: the normal path expires the row
        if valid_until <= self._mute_until_t_h:
            return None  # expires inside the mute window
        if self.max_virtual_hours is not None:
            return min(self._mute_until_t_h, self.max_virtual_hours)
        return self._mute_until_t_h

    # command dispatch (ControlCommand -> harness.commands)

    async def _on_command(self, cmd) -> None:
        """Route a :class:`ControlCommand` (the channel's ``on_command``
        seam) to ``harness.commands.handle_command`` UNDER the runtime lock —
        never ``session.on_message``. The handler runs on the owned executor;
        its reply is sent as a plain non-proactive message."""
        from harness.commands import CommandContext, handle_command

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
                request_setup=self._request_setup,
            )
            reply = await self._executor.run_in_thread(handle_command, cmd, ctx)
        if reply:
            await self.channel.send(OutboundMessage(text=reply, proactive=False))

    def _request_setup(self) -> str:
        """The /setup hook: initialize identity on a blank database.

        ``handle_command`` only calls this pre-bootstrap, so it never
        regenerates an existing identity. The onboarding LLM call goes
        through the session's own client, falling back to the offline
        heuristic when there is none.
        """
        from harness.bootstrap import ensure_companion_initialized

        lines: list[str] = []
        boot = ensure_companion_initialized(
            self.store,
            seed=self.seed,
            day=self.session.clock.day(),
            client=getattr(self.session, "client", None),
            logger=lines.append,
        )
        for line in lines:
            _logger.info("setup: %s", line)
        agenda = boot.today_agenda
        detail = (
            f"{boot.persona.name} is ready for {boot.user_profile.name} — "
            f"{len(boot.persona.interests)} interests, "
            f"{len(boot.life_arcs)} life arcs, "
            f"{len(agenda.items) if agenda else 0} things on today."
        )
        if lines:
            detail += " (" + "; ".join(lines) + ")"
        return detail

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
        """CommandContext hook (/tz): queue a timezone change, applied at the
        NEXT rollover (only the anchor's tz metadata moves — the virtual
        clock cannot go backwards). Raises ValueError on an unknown IANA
        name."""
        try:
            ZoneInfo(tz)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {tz!r}") from exc
        self._pending_tz = tz

    def _apply_pending_tz(self) -> None:
        """Apply a queued /tz change at rollover: re-persist the anchor's tz
        metadata and update the in-memory anchor. Without an anchor the
        request is dropped."""
        if self._pending_tz is None:
            return
        if self.anchor is not None:
            self.anchor = replace(self.anchor, tz=self._pending_tz)
            persist_anchor(self.store, self.anchor)
        self._pending_tz = None

    def _request_mute(self, hours: float) -> None:
        """CommandContext hook (/mute): defer proactives for ``hours`` —
        still-valid pending events are deferred (never consumed) until the
        window ends; events expiring inside the window are consumed normally.
        Pure runtime pacing: no session state, no memory writes."""
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
