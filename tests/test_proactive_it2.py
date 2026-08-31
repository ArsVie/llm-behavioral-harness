"""Iteration-2 A3 tests (proactivity/runtime): ContactOpportunity separation,
opportunity-time intent resolution, EXACT intent identity end-to-end, A6
concurrency integration, rollover clock discipline (E0), and SUPPRESS-as-
normal semantics. Runs on the seam-faithful SeamStore (test_proactive) and
the real SQLiteStore where noted; the sleeper is always injected (recorded,
never real seconds) except where a bounded real sleep is the deterministic
trigger for the rollover-vs-firing race.

A5's ``session.fire_proactive(intent_id)`` has not merged in this worktree:
:class:`ExactIntentSession` is a seam double implementing the documented A5
contract (plan §5-A5 T3 — fetch the exact intent by id, carry it into the
snapshot, persist ``message.intent_id``). The runtime calls
``fire_proactive(intent.id)``; when A5's session lands the same tests pass
against the real session unchanged.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import numpy as np
import pytest

import engine.rng as rng_mod
import harness.concurrency as conc
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import ContactOpportunity, DailyAgenda, ProactiveIntent
from harness.judge import ScriptedJudge
from harness.proactive import SOURCE_AGENDA, IntentResolver, compose_hook
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import (
    OPPORTUNITY_VALIDITY_H,
    REASON_CHECK_IN,
    REASON_SCHEDULE,
    REASON_VALIDITY_H,
    ProactiveSchedule,
    adj_from_score,
    build_opportunity,
    initiative_factor,
    plan_proactive_events,
)
from harness.session import Session, TurnResult
from harness.store import SQLiteStore
from tests.helpers import (
    SeamStore,
    agenda_item,
    ground_agenda,
    make_session,
    rows,
    suppressed_codes,
)

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

FAST = TimeScale(seconds_per_virtual_hour=0.02)


def _session(*, replies=None, session_cls=None):
    """Local 3-tuple wrapper over the shared make_session (store, clock,
    session) — this file's call sites unpack all three and use the
    module's PERSONA/TIMING/VARIANT/SEED constants; some pass a
    specialized session_cls (ExactIntentSession)."""
    store = SeamStore()
    clock = VirtualClock()
    session = make_session(
        store,
        clock=clock,
        client=FakeClient(responses=replies or ["ok!"]),
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        session_cls=session_cls or Session,
    )
    return store, clock, session
SLOW = TimeScale(seconds_per_virtual_hour=0.5)


# helpers


def _run(store, session, schedule, channel, *, max_hours, scale=FAST,
         resolver=None, sleeper=None, seed=SEED):
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=seed,
        time_scale=scale, max_virtual_hours=max_hours,
        resolver=resolver if resolver is not None else IntentResolver(
            store, rng=rng_mod.stream_rng(seed)
        ),
        sleeper=sleeper if sleeper is not None else record,
    )
    asyncio.run(runtime.run())
    runtime._delays = delays
    return runtime


def _stored_intent(item, intent_id, t_h, *, reason=REASON_SCHEDULE):
    """A fully-grounded ProactiveIntent exactly as the resolver would build
    it (deterministic hook), with a caller-chosen id."""
    return ProactiveIntent(
        id=intent_id, reason=reason, source_type=SOURCE_AGENDA,
        source_id=item.id, hook=compose_hook(item, reason),
        created_t_h=t_h, valid_until_t_h=t_h + REASON_VALIDITY_H[reason],
        salience=0.8, evidence=f"agenda_item:{item.id}", opportunity_id=None,
    )


class FixedIdResolver(IntentResolver):
    """Real resolver whose resolved intent is renamed to a fixed id — the
    adversarial setup stores #87/#88 up front and the runtime must validate
    and fire the EXACT id, never a same-reason sibling."""

    def __init__(self, store, intent_id: str, *args, **kwargs):
        super().__init__(store, *args, **kwargs)
        self._intent_id = intent_id

    def resolve(self, opportunity):
        intent = super().resolve(opportunity)
        if intent is None:
            return None
        return replace(intent, id=self._intent_id)


class ExactIntentSession(Session):
    """A5-seam double (plan §5-A5 T3) over the legacy session.

    ``fire_proactive(intent_id)`` fetches the EXACT stored intent by id,
    carries it into the snapshot (never a same-reason sibling — invariant
    7), and persists ``message.intent_id`` (A7 M1, which the legacy ``_chat``
    does not yet pass). ``fire_calls`` records every id the runtime passes.
    Replaced by A5's session at its merge; the contract is identical.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._firing_id: str | None = None
        self.fire_calls: list[str] = []

    def fire_proactive(self, intent_id: str) -> TurnResult:
        intent = self.store.load_proactive_intent(intent_id)
        if intent is None:
            raise ValueError(f"unknown proactive intent: {intent_id!r}")
        self._firing_id = intent_id
        try:
            result = self._chat(None, proactive=True, intent=intent)
        finally:
            self._firing_id = None
        # The session passes intent_id to add_message; the legacy _chat does
        # not, so exact provenance is attached to the stored row.
        self.store.update_message_intent_id(
            self.store.recent_messages()[-1]["id"], intent_id
        )
        self.fire_calls.append(intent_id)
        return result

    def _resolve_intent(self, reason):
        if self._firing_id is not None:
            return self.store.load_proactive_intent(self._firing_id)
        return super()._resolve_intent(reason)


# the scheduler creates contact opportunities with no semantic reason


def test_opportunity_has_no_semantic_reason():
    opp = build_opportunity(
        10.0, day=0, phase_label="follicular", timing=TIMING,
        previous_score=None, initiative=0.5,
    )
    assert isinstance(opp, ContactOpportunity)
    assert not hasattr(opp, "reason")          # no invented reason
    assert "reason" not in opp.hazard_components


def test_build_opportunity_shape_and_validity():
    opp = build_opportunity(
        10.0, day=0, phase_label="follicular", timing=TIMING,
        previous_score=0.3, initiative=0.7,
    )
    assert opp.id == "opp_10.000"
    assert opp.desired_t_h == 10.0
    assert opp.created_t_h == 10.0
    assert opp.valid_until_t_h == pytest.approx(10.0 + OPPORTUNITY_VALIDITY_H)
    assert set(opp.hazard_components) == {
        "base", "circadian", "phase", "initiative", "prior_score",
    }
    # the reported multipliers are exactly the frozen modulator factors
    assert opp.initiative_multiplier == pytest.approx(initiative_factor(0.7))
    assert opp.previous_score_multiplier == pytest.approx(adj_from_score(0.3, TIMING))
    assert opp.hazard_components["initiative"] == opp.initiative_multiplier
    assert opp.hazard_components["prior_score"] == opp.previous_score_multiplier
    assert opp.hazard_components["phase"] == TIMING.phase_multipliers["follicular"]


def test_plan_and_persist_creates_opportunities_for_every_event():
    store = SeamStore()
    schedule = ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    assert len(schedule.event_hours) > 0
    for h in schedule.event_hours:
        opp = schedule.opportunity_for(h)
        assert opp is not None, f"no opportunity for event at {h}"
        assert opp.desired_t_h == float(h)
        assert opp.valid_until_t_h > opp.desired_t_h
    # the store's optional seam received them (A7 gap: SQLiteStore has none)
    assert len(store.load_contact_opportunities()) == len(schedule.event_hours)


def test_plan_and_persist_opportunities_idempotent_across_replans():
    store = SeamStore()
    s1 = ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    s2 = ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    assert set(s1.opportunities) == set(s2.opportunities)
    for h in s1.opportunities:
        assert s1.opportunity_for(h) == s2.opportunity_for(h)
    assert len(store.load_contact_opportunities()) == len(s1.opportunities)


def test_restore_rebuilds_opportunities_from_store_seam():
    store = SeamStore()
    ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    restored = ProactiveSchedule.restore(SEED, store)
    persisted = {opp.id: opp for opp in store.load_contact_opportunities()}
    assert {opp.id for opp in restored.opportunities.values()} == set(persisted)
    for opp in restored.opportunities.values():
        assert restored.opportunity_for(opp.desired_t_h) == persisted[opp.id]


def test_restore_without_seam_carries_no_opportunities(tmp_path):
    """The real SQLiteStore has no contact_opportunities table yet (A7 gap —
    flagged in the handoff): restore must degrade cleanly to bare hours."""
    store = SQLiteStore(tmp_path / "s.db")
    try:
        store.save_schedule_events(SEED, [{"t_h": 10.0, "day": 0,
                                           "reason": REASON_SCHEDULE}])
        restored = ProactiveSchedule.restore(SEED, store)
        assert restored.opportunity_for(10.0) is None
        assert restored.next_pending(10.0) == 10.0
    finally:
        store.close()


def test_plan_proactive_events_unchanged():
    a = plan_proactive_events(10, SEED, PERSONA, TIMING)
    b = plan_proactive_events(10, SEED, PERSONA, TIMING)
    assert np.array_equal(a, b)


# IntentResolver resolves at opportunity time, linking opportunity_id


def test_resolve_opportunity_carries_opportunity_id():
    store = SeamStore()
    item = ground_agenda(store, 9.5, 10.5)
    opp = build_opportunity(10.0, day=0, phase_label="follicular", timing=TIMING,
                            previous_score=None, initiative=0.5)
    intent = IntentResolver(store).resolve(opp)
    assert intent is not None
    assert intent.opportunity_id == opp.id
    assert intent.source_id == item.id
    assert intent.created_t_h == opp.desired_t_h


def test_resolve_float_legacy_has_no_opportunity():
    store = SeamStore()
    ground_agenda(store, 9.5, 10.5)
    intent = IntentResolver(store).resolve(10.0)
    assert intent is not None
    assert intent.opportunity_id is None


def test_opportunity_validity_bounds_intent_validity():
    store = SeamStore()
    ground_agenda(store, 9.5, 10.5)
    opp = build_opportunity(10.0, day=0, phase_label="follicular", timing=TIMING,
                            previous_score=None, initiative=0.5)
    intent = IntentResolver(store).resolve(opp)
    assert intent is not None
    # The opportunity window is 3h, equal to the schedule reason window;
    # a manually-shortened opportunity gives a tighter check.
    assert intent.valid_until_t_h == pytest.approx(
        min(10.0 + REASON_VALIDITY_H[REASON_SCHEDULE], opp.valid_until_t_h)
    )
    short = replace(opp, valid_until_t_h=opp.desired_t_h + 1.0)
    tight = IntentResolver(store).resolve(short)
    assert tight is not None
    assert tight.valid_until_t_h == pytest.approx(11.0)  # bounded by the opportunity


def test_resolve_opportunity_none_still_means_suppress():
    store = SeamStore()
    opp = build_opportunity(10.0, day=0, phase_label="follicular", timing=TIMING,
                            previous_score=None, initiative=0.5)
    assert IntentResolver(store).resolve(opp) is None  # SUPPRESS: no_grounded_reason


# SUPPRESS no_grounded_reason is normal on the opportunity path


def test_planned_opportunities_without_grounded_source_all_suppressed():
    store = SeamStore()
    schedule = ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    assert len(schedule.event_hours) > 0
    channel = FakeChannel()
    _run(store, _session()[2], schedule, channel, max_hours=24.5)
    assert channel.sent == []                       # nothing hallucinated
    assert "no_grounded_reason" in suppressed_codes(store)
    # All day-0 opportunities consumed, not stranded; only future
    # day-1 rows remain pending (the run ended before midnight+1)
    pending = store.pending_schedule_events(SEED)
    assert pending and all(r["day"] >= 1 for r in pending)
    assert store.list_proactive_intents() == []     # no intents persisted


# exact intent identity end-to-end


def test_exact_intent_identity_two_same_reason_intents():
    """Adversarial (plan §5-A3, invariant 7): TWO simultaneous intents with
    the SAME reason (#87 pottery, #88 gym). The runtime validates #87 and the
    generated message MUST use #87 — the exact id reaches
    ``session.fire_proactive(intent_id)`` and ``message.intent_id == #87``.
    Reason equality is never enough: #88 (stored later, same reason) is
    never used."""
    store, clock, session = _session(replies=["pottery ping!"],
                                         session_cls=ExactIntentSession)
    pottery = ground_agenda(store, 9.5, 10.5, item_id="pottery",
                             salience=0.9, activity="pottery class")
    gym = ground_agenda(store, 9.5, 10.5, item_id="gym",
                         salience=0.7, activity="gym session")
    # both intents stored up front, same reason "schedule"; #88 created after #87
    store.save_proactive_intent(_stored_intent(pottery, "87", t_h=9.9))
    store.save_proactive_intent(_stored_intent(gym, "88", t_h=9.95))
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=11.0,
         resolver=FixedIdResolver(store, "87", rng=rng_mod.stream_rng(SEED)))

    assert len(channel.sent) == 1 and channel.sent[0].proactive
    # the runtime passed the exact validated id, not a reason
    assert session.fire_calls == ["87"]
    # the persisted message carries exact intent provenance
    last = store.recent_messages()[-1]
    assert last["intent_id"] == "87"
    # The snapshot renders #87's intent hook, not #88's (the agenda
    # lane lists both items; the intent hook is #87's)
    system = session.client.calls[-1]["system"]
    assert "reaching out first" in system
    assert "Agenda: pottery class" in system
    assert "Agenda: gym session" not in system
    # lifecycle: #87 fired, #88 untouched
    assert {i.id for i in store.list_proactive_intents(status="fired")} == {"87"}
    assert "88" not in {
        i.id for i in store.list_proactive_intents(status="fired")
    }
    assert rows(store, SEED)[10.0]["status"] == "fired"


def test_exact_identity_not_downgraded_to_reason_when_same_reason_pending():
    """If the exact id were ever downgraded to its reason, the legacy
    reason-lookup would pick the MOST RECENT same-reason intent (#88). The
    runtime never does that: fire_proactive(intent_id) resolves by id only,
    and the reason-only lookup demonstrably returns the wrong sibling."""
    store, _, session = _session(session_cls=ExactIntentSession)
    pottery = ground_agenda(store, 9.5, 10.5, item_id="pottery", salience=0.9)
    gym = ground_agenda(store, 9.5, 10.5, item_id="gym", salience=0.7)
    store.save_proactive_intent(_stored_intent(pottery, "87", t_h=9.9))
    store.save_proactive_intent(_stored_intent(gym, "88", t_h=9.95))
    # The legacy reason-lookup returns #88 (most recent 'schedule'),
    # which is why identity is not downgraded to a reason
    assert session._resolve_intent(REASON_SCHEDULE).id == "88"
    session.fire_calls = []
    result = session.fire_proactive("87")
    assert session.fire_calls == ["87"]
    assert result.reply == "ok!"
    assert store.recent_messages()[-1]["intent_id"] == "87"


# rollover clock: events near midnight fire, not expire


def test_near_midnight_events_fire_not_expire_under_accelerated_time():
    """E0 regression: under FAST accelerated time the rollover loop must not
    jump the virtual clock past a pending event. Event A (20.5) holds the
    firing loop in its response-delay sleeper long enough for the rollover's
    midnight sleep to complete; without the park discipline the rollover
    advances the clock to 24:00 and event B (21.0, 3h validity → expires at
    exactly 24:00) is spuriously gated 'expired'. With the discipline the
    rollover parks at 21.0 and B fires at its own time."""
    store, clock, session = _session(replies=["a!", "b!"])
    ground_agenda(store, 20.0, 21.0, item_id="slot_a", activity="evening a")
    ground_agenda(store, 21.0, 22.0, item_id="slot_b", activity="evening b")
    store.save_schedule_events(SEED, [
        {"t_h": 20.5, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 21.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    channel = FakeChannel()

    async def blocking_sleeper(delay: float) -> None:
        # bounded real wait: deterministic trigger for the rollover-vs-firing
        # race (the rollover's 24:00 sleep completes during event A's delay)
        await asyncio.sleep(0.3)

    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=25.0, scale=FAST, sleeper=blocking_sleeper)

    schedule_rows = rows(store, SEED)
    assert schedule_rows[20.5]["status"] == "fired"
    # The near-midnight event fired at its own time, not expired early
    assert schedule_rows[21.0]["status"] == "fired"
    assert schedule_rows[21.0]["fired_t_h"] == pytest.approx(21.0)
    assert "expired" not in suppressed_codes(store)
    assert len([m for m in channel.sent if m.proactive]) == 2


def test_rollover_parks_at_pending_event_before_midnight():
    """The rollover advances the clock only UP TO a pending event (park), so
    the firing loop gates it at its own time (fired_t_h == the event hour),
    then the rollover crosses midnight afterwards."""
    store, clock, session = _session(replies=["late evening hi"])
    ground_agenda(store, 21.5, 22.5, item_id="late", activity="late pottery")
    store.save_schedule_events(SEED, [
        {"t_h": 22.0, "day": 0, "reason": REASON_SCHEDULE},  # awake, 2h before midnight
    ])
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=25.5, scale=FAST)
    assert len(channel.sent) == 1 and channel.sent[0].proactive
    row = rows(store, SEED)[22.0]
    assert row["status"] == "fired"
    assert row["fired_t_h"] == pytest.approx(22.0)  # gated at its own time
    assert clock.now_h() >= 24.0                    # midnight still crossed


def test_overdue_pending_event_does_not_hang_rollover():
    """An OVERDUE pending event (recovered during quiet hours, deferred past
    max_virtual_hours) must not park the rollover forever: overdue events are
    left to the firing loop's recovery evaluation and the run terminates."""
    store = SeamStore()
    clock = VirtualClock(t_h=27.0)  # 03:00 next day — quiet hours
    session = Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=FakeClient(responses=["ok!"]), clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    store.save_schedule_events(SEED, [
        {"t_h": 23.5, "day": 0, "reason": REASON_CHECK_IN},  # valid until 11:30 d+1
    ])
    channel = FakeChannel()
    _run(store, session, ProactiveSchedule.restore(SEED, store), channel,
         max_hours=27.5, scale=FAST)  # completes → no hang
    assert channel.sent == []
    assert rows(store, SEED)[23.5]["status"] in ("pending", "expired")


# concurrency integration


def test_runtime_shuts_down_owned_executor():
    store, clock, session = _session()
    channel = FakeChannel()
    runtime = _run(store, session, ProactiveSchedule.restore(SEED, store),
                   channel, max_hours=0.5, scale=FAST)
    assert not runtime._executor.is_running          # explicit shutdown
    names = [t.name for t in threading.enumerate()]
    assert not any(n.startswith("llh-runtime") for n in names)  # threads joined


def test_runtime_uses_concurrency_sleeper_default():
    store, clock, session = _session()
    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), FakeChannel(),
        store=store, timing=TIMING, seed=SEED,
    )
    try:
        assert isinstance(runtime.sleeper, conc.Sleeper)
    finally:
        runtime._executor.shutdown()


def test_runtime_leaves_sqlite_store_usable_after_run(tmp_path):
    """The runtime re-opens the store connection thread-safe (A6 helper) but
    must NOT close it: the connection is now the store's (store.conn) and the
    store's own close() owns its lifecycle."""
    store = SQLiteStore(tmp_path / "s.db")
    try:
        store.save_daily_state(0, {
            "day": 0, "M": 6, "m": 0.0, "g": 0.7, "p": 0.5, "arg": 0.0,
            "mu": 0.0, "eta": 0.0, "cycle_day": 0.0, "phase_label": "phase_a",
            "seed": SEED, "score": None,
        })
        session = Session(
            store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
            client=FakeClient(responses=["ok!"]), clock=VirtualClock(),
            judge=ScriptedJudge(score=0.5).judge_day,
        )
        _run(store, session, ProactiveSchedule.restore(SEED, store),
             FakeChannel(), max_hours=0.5, scale=FAST)
        # connection still live and usable by the store after the run
        assert store.conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
        assert store.pending_schedule_events(SEED) == []
    finally:
        store.close()


# end-to-end: a planned contact opportunity flows through the runtime


def test_planned_opportunity_resolves_and_fires_end_to_end():
    """A planned ContactOpportunity flows through the runtime: the scheduler
    creates it (no semantic reason), the firing loop resolves it at its own
    time into a grounded intent carrying opportunity_id, the intent fires,
    and the opportunity is logged to the audit trail."""
    store, clock, session = _session(replies=["pottery time!"])
    schedule = ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    h = float(schedule.event_hours[0])  # the planner does not place events in quiet hours
    opp = schedule.opportunity_for(h)
    assert opp is not None and opp.desired_t_h == h
    ground_agenda(store, h - 0.5, h + 0.5, item_id="slot", activity="pottery class")
    channel = FakeChannel()
    _run(store, session, schedule, channel, max_hours=h + 2.0, scale=SLOW)

    assert len(channel.sent) == 1 and channel.sent[0].proactive
    fired = store.list_proactive_intents(status="fired")
    assert len(fired) == 1
    # The fired intent carries its opportunity's exact id
    assert fired[0].opportunity_id == opp.id
    # the opportunity itself was logged to the audit trail
    opp_events = [e for e in store.events_since(0)
                  if e["event"] == "contact_opportunity"]
    assert any(f"id={opp.id}" in e["detail"] for e in opp_events)
    assert rows(store, SEED)[h]["status"] == "fired"
