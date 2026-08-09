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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import engine.rng as rng_mod
from engine.circadian import envelope
from engine.types import ENVELOPE_RAMP_H, TimingParams
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
        sleeper: Sleeper | None = None,
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
        resources are never closed here)."""
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
            reply = await self._executor.run_in_thread(self.session.on_message, msg.text)
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
                pending = self.schedule.next_pending(now)
                # it3 B2: park the clock at the conversation's next close
                # instant (quiet boundary or user_left deadline) so the
                # close is recorded AT its boundary, not at the next wake.
                close_t = self.session.next_conversation_close_t_h(now)
            if close_t is not None and close_t < target:
                target = close_t
            if (
                pending is not None
                and now <= pending < target
                and not self._firing_done
            ):
                # Park at the earliest pending event; the firing loop gates
                # it at its own time. Never jump past it.
                target = pending
            if target <= now:
                # Parked at (or past) a pending event hour: yield to the
                # firing loop WITHOUT advancing the clock past the event.
                await asyncio.sleep(self._poll_sleep())
                continue
            await asyncio.sleep(
                (target - now) * self.time_scale.seconds_per_virtual_hour
            )
            now = self.session.clock.now_h()
            if self._max_reached(now):
                return
            async with self._lock:
                if now < target:
                    self.session.clock.advance_hours(target - now)
                now = self.session.clock.now_h()
                # it3 B2: the clock landed on a conversation close instant
                # — record the close at the boundary (idempotent no-op
                # otherwise).
                await self._executor.run_in_thread(
                    self.session.check_conversation_lifecycle, now
                )
                day = self.session.clock.day()
                if target >= next_midnight:
                    # A real rollover crossed midnight. At the run's end
                    # boundary (target == max_virtual_hours < next_midnight)
                    # we do NOT re-plan: that would inject a fresh plan for a
                    # run that is about to finish.
                    await self._executor.run_in_thread(self.session.ensure_day, day)
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
                await asyncio.sleep(self._poll_sleep())
                continue
            if self.max_virtual_hours is not None and nxt >= self.max_virtual_hours:
                self._firing_done = True
                return
            if nxt > now:
                await asyncio.sleep(
                    (nxt - now) * self.time_scale.seconds_per_virtual_hour
                )
            defer_until: float | None = None
            async with self._lock:
                now = self.session.clock.now_h()
                if now < nxt:
                    self.session.clock.advance_hours(nxt - now)
                now = self.session.clock.now_h()
                defer_until = self._quiet_defer_until(nxt, now)
                if defer_until is not None and now - nxt < 1e-9:
                    # R1-F1 (it2 A3): an ON-SCHEDULE deferral (event gated at
                    # its own hour — the rollover is parked AT it, so the
                    # clock can never move on its own) must ADVANCE the
                    # virtual clock to the next awake instant — sleeping
                    # wall time alone would re-defer forever and the run
                    # would livelock (invariants 3/17). _quiet_defer_until
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
                    self.session.clock.advance_hours(defer_until - now)
                    now = self.session.clock.now_h()
                    defer_until = self._quiet_defer_until(nxt, now)
                # it3 B2: the clock may have jumped past a conversation
                # close boundary (deferral to the next awake instant) —
                # record the close at the detection instant (idempotent).
                await self._executor.run_in_thread(
                    self.session.check_conversation_lifecycle, now
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
                    result = await self._fire_exact_intent(intent)
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
            if defer_until is not None:
                await asyncio.sleep(
                    (defer_until - now) * self.time_scale.seconds_per_virtual_hour
                )

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
        opportunity = self.schedule.opportunity_for(nxt)
        if opportunity is not None:
            valid_until = opportunity.valid_until_t_h
        else:
            rows = [
                r for r in self.store.schedule_events_for_seed(self.seed)
                if abs(float(r["t_h"]) - nxt) < 1e-9
            ]
            if not rows:
                return None
            valid_until = nxt + REASON_VALIDITY_H[rows[0]["reason"]]
        if now > valid_until:
            return None  # past validity: the normal path expires the row
        awake_at = self._next_awake_at(now)
        if valid_until <= awake_at:
            return None  # expires before the window ends -> can never fire
        if self.max_virtual_hours is not None:
            return min(awake_at, self.max_virtual_hours)
        return awake_at

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
