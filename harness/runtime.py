"""Async runtime — real-time rollover + gated proactive firing (wave 3, seam A-6; A7).

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
  resurrects fired/expired rows).

- ``_firing_loop`` — waits for the next pending schedule event (short poll
  when none; overdue events are visible thanks to ``next_pending``'s A7
  restart fix), advances the clock to it, then GATES before generating:
  the content gate resolves the event to a GROUNDED intent
  (``IntentResolver`` → ``content_gate(intent, store)``) and the context gate
  (quiet hours, cooldown, daily cap). No grounded candidate ⇒ SUPPRESS
  (``no_grounded_reason`` is a legitimate outcome). Suppressed events are
  consumed (marked fired — or expired when the validity window elapsed) and
  logged as ``proactive_suppressed`` with the failing code; allowed events
  fire via ``session.fire_proactive`` and are sent through the channel as
  proactive OutboundMessages.

Delivery latency (A7): after the LLM returns, the runtime waits the
requested ``response_delay_s`` (wall-clock seconds — NOT scaled by
TimeScale) through the injectable ``sleeper`` (default ``asyncio.sleep``;
tests inject a recorder so the suite never waits real seconds) and only
then calls ``channel.send``.

All ``session.*`` calls run in worker threads under a single ``asyncio.Lock``
so the event loop never blocks on an LLM/judge call and inbound (reactive)
and scheduled (proactive) turns never overlap (single user, no reentrancy).
Engine steps are never called directly: rollover is driven ONLY through
``session.ensure_day`` / ``session.finalize_current``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import engine.rng as rng_mod
from harness.channels.base import Channel, InboundMessage, OutboundMessage
from harness.gates import content_gate, context_gate
from harness.proactive import IntentResolver
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule, day_scores
from harness.session import Session
from harness.store import SQLiteStore
from engine.types import TimingParams

#: Poll cadence when no schedule event is pending, in VIRTUAL hours. The real
#: sleep is POLL_INTERVAL_H * seconds_per_virtual_hour, so tests with a tiny
#: TimeScale stay responsive (µs polls) while a real-time run polls rarely.
POLL_INTERVAL_H = 0.05


@dataclass
class TimeScale:
    """Pace the virtual clock against wall-clock (the ONLY wall-clock use).

    ``seconds_per_virtual_hour``: real seconds per virtual hour. 3600.0 means
    one real hour per virtual hour (real time); tests pass a tiny value (e.g.
    0.001) to run days in milliseconds.
    """

    seconds_per_virtual_hour: float = 3600.0


class AsyncRuntime:
    """Orchestrate a Session, a persisted ProactiveSchedule and a Channel."""

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
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.session = session
        self.schedule = schedule
        self.channel = channel
        self.store = store
        self.timing = timing
        self.seed = seed
        self.time_scale = time_scale
        self.max_virtual_hours = max_virtual_hours
        #: Grounded-intent resolver (store-backed). Default uses a seeded
        #: engine.rng stream distinct from the event stream so resolver
        #: tie-breaks never perturb the planned event times.
        self.resolver = resolver if resolver is not None else IntentResolver(
            store, rng=rng_mod.stream_rng(seed, rng_mod.EXPERIMENT_STREAM)
        )
        #: Injectable delay function (default asyncio.sleep) so tests record
        #: the requested response_delay_s without waiting real seconds.
        self.sleeper = sleeper if sleeper is not None else asyncio.sleep
        self._lock = asyncio.Lock()
        self._ensure_thread_safe_store()

    def _ensure_thread_safe_store(self) -> None:
        """Re-open the store connection for cross-thread use.

        The merged SQLiteStore binds its connection to the creating thread
        (sqlite3 default ``check_same_thread=True``), but this runtime moves
        ``session.*`` calls to worker threads (plan requirement: never block
        the event loop). Re-opening the connection with
        ``check_same_thread=False`` keeps that contract without touching the
        frozen store: the asyncio.Lock serializes all session calls, WAL +
        busy_timeout cover the remaining store access, and the schema is
        already on disk. (Workaround contained here; report-only issue.)
        Seam-faithful in-memory stores (tests; the real A2 SQLiteStore has
        not landed yet) expose no sqlite connection and are skipped."""
        if not (hasattr(self.store, "path") and hasattr(self.store, "conn")):
            return
        import sqlite3

        old_conn = self.store.conn
        conn = sqlite3.connect(
            self.store.path, timeout=10.0, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        if old_conn is not None:
            old_conn.close()  # schema-creation conn from SQLiteStore.__init__
        self.store.conn = conn

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Start the channel, run rollover + firing until max_virtual_hours
        (or cancelled), then finalize the current day and stop the channel."""
        await self.channel.start(self._on_inbound)
        try:
            await asyncio.gather(self._rollover_loop(), self._firing_loop())
        finally:
            try:
                await self._session_call(self.session.finalize_current)
            finally:
                await self.channel.stop()

    async def _session_call(self, fn, *args):
        """Run one synchronous session call in a worker thread, locked."""
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def _max_reached(self, now_h: float) -> bool:
        return self.max_virtual_hours is not None and now_h >= self.max_virtual_hours

    def _poll_sleep(self) -> float:
        return POLL_INTERVAL_H * self.time_scale.seconds_per_virtual_hour

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
        then send the reply as a non-proactive OutboundMessage."""
        async with self._lock:
            if msg.t_h is not None and msg.t_h > self.session.clock.now_h():
                self.session.clock.advance_hours(
                    msg.t_h - self.session.clock.now_h()
                )
            reply = await asyncio.to_thread(self.session.on_message, msg.text)
            await self.sleeper(self._response_delay(reply))
            await self.channel.send(
                OutboundMessage(text=reply.reply, proactive=False)
            )

    # ------------------------------------------------------------------ #
    # day rollover
    # ------------------------------------------------------------------ #

    async def _rollover_loop(self) -> None:
        """Sleep until the next virtual midnight (paced), roll the session
        over, then — only on a REAL midnight (not the max_virtual_hours end
        boundary) — re-plan + persist the CURRENT day's schedule."""
        while True:
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            next_midnight = (self.session.clock.day() + 1) * 24.0
            if self.max_virtual_hours is not None:
                target = min(next_midnight, self.max_virtual_hours)
            else:
                target = next_midnight
            await asyncio.sleep(
                (target - now) * self.time_scale.seconds_per_virtual_hour
            )
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            async with self._lock:
                if now < target:
                    self.session.clock.advance_hours(target - now)
                day = self.session.clock.day()
                await asyncio.to_thread(self.session.ensure_day, day)
            if target >= next_midnight:
                # A real rollover crossed midnight. At the run's end boundary
                # (target == max_virtual_hours < next_midnight) we do NOT
                # re-plan: that would inject a fresh plan for a run that is
                # about to finish.
                self._replan()

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
        intent, gate it (content + context), then fire or consume+log the
        suppression. Overdue events are evaluated on recovery: still valid ⇒
        fire; past the validity window ⇒ expire."""
        while True:
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            async with self._lock:
                nxt = self.schedule.next_pending(now)
            if nxt is None:
                await asyncio.sleep(self._poll_sleep())
                continue
            if self.max_virtual_hours is not None and nxt >= self.max_virtual_hours:
                return
            if nxt > now:
                await asyncio.sleep(
                    (nxt - now) * self.time_scale.seconds_per_virtual_hour
                )
            async with self._lock:
                now = self.session.clock.now_h()
                if now < nxt:
                    self.session.clock.advance_hours(nxt - now)
                now = self.session.clock.now_h()
                day = self.session.clock.day()
                # Contact opportunity -> contact reason: a grounded intent
                # anchored at the OPPORTUNITY time (nxt), so an overdue event
                # is evaluated against its own validity window on recovery.
                # None ⇒ SUPPRESS: no_grounded_reason is a legitimate outcome.
                intent = self.resolver.resolve(nxt)
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
                result = await asyncio.to_thread(
                    self.session.fire_proactive, intent.reason
                )
                await self.sleeper(self._response_delay(result))
                await self.channel.send(
                    OutboundMessage(
                        text=result.reply, proactive=True, reason=intent.reason
                    )
                )
                self.store.update_proactive_intent_status(intent.id, "fired")
                self.schedule.mark_fired_persisted(
                    nxt, now, self.seed, self.store
                )
