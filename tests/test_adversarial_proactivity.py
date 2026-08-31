"""A9 Iteration-2 adversarial wave — PROACTIVITY attack class (plan §5-A9 P1).

Attacks on the contact-opportunity → grounded-intent seam (plan §16
invariants 3-7): ContactOpportunity has NO semantic reason field; every
ProactiveIntent always resolves to a real persisted source (intent ⇒ source);
an opportunity without a grounded intent produces NO message (suppression is
normal, never an error); expired/unknown intent ids raise ValueError with no
message; and two same-reason intents are never interchangeable (exact-id
isolation at the store and session level).

Every test is deterministic: fixed seeds, injected clock/sleeper, no LLM.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import (
    ContactOpportunity,
    DailyAgenda,
    EpisodicMemory,
    MemoryKind,
    ProactiveIntent,
    UserProfile,
)
from harness.gates import content_gate
from harness.interests import build_catalog
from harness.judge import ScriptedJudge
from harness.proactive import IntentResolver, compose_hook
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import (
    REASON_CALLBACK,
    REASON_CHECK_IN,
    REASON_EVENT,
    REASON_SCHEDULE,
    REASON_SHARED_INTEREST,
    REASON_VALIDITY_H,
    ProactiveSchedule,
    build_opportunity,
)
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

FAST = TimeScale(seconds_per_virtual_hour=0.02)


def _store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


def _session(store, clock: VirtualClock | None = None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=["ok!"]),
        clock=clock or VirtualClock(),
        judge=ScriptedJudge(score=0.5).judge_day,
    )


def _ground_agenda(store, start_t_h, end_t_h, *, item_id="g1", salience=0.8,
                   activity="pottery class"):
    from harness.domain import AgendaItem

    item = AgendaItem(item_id, start_t_h, end_t_h, activity, "arc", "arc1",
                      salience, "planned")
    store.save_agenda(0, DailyAgenda(0, (item,)))
    return item


def _stored_intent(item, intent_id: str, t_h: float, *,
                   reason: str = REASON_SCHEDULE) -> ProactiveIntent:
    """A fully-grounded ProactiveIntent exactly as the resolver would build
    it, with a caller-chosen id (the #87/#88 adversarial setup)."""
    return ProactiveIntent(
        id=intent_id, reason=reason, source_type="agenda_item",
        source_id=item.id, hook=compose_hook(item, reason),
        created_t_h=t_h, valid_until_t_h=t_h + REASON_VALIDITY_H[reason],
        salience=0.8, evidence=f"agenda_item:{item.id}", opportunity_id=None,
    )


def _run(store, session, schedule, channel, *, max_hours, resolver=None):
    async def record(delay: float) -> None:
        pass

    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=FAST, max_virtual_hours=max_hours,
        resolver=resolver if resolver is not None else IntentResolver(store),
        sleeper=record,
    )
    asyncio.run(runtime.run())
    return runtime


# ContactOpportunity has no semantic reason field


def test_p1_contact_opportunity_has_no_semantic_reason_field():
    """The dataclass itself carries no semantic reason: no field named
    reason/justification/motivation/source, and a real opportunity's
    dataclass fields stay within the timing vocabulary."""
    forbidden = {"reason", "justification", "motivation", "source", "source_id"}
    fields = {f.name for f in dataclasses.fields(ContactOpportunity)}
    assert forbidden.isdisjoint(fields), (
        f"ContactOpportunity carries a semantic reason field: {forbidden & fields}"
    )
    opp = build_opportunity(
        10.0, day=0, phase_label="follicular", timing=TIMING,
        previous_score=None, initiative=0.5,
    )
    assert not hasattr(opp, "reason")
    # The hazard vocabulary is timing mechanics only.
    assert set(opp.hazard_components) <= {
        "base", "circadian", "phase", "initiative", "prior_score",
    }


# Intent implies a real source for every reason type


def test_p1b_every_reason_intent_resolves_to_a_real_source(tmp_path):
    """For every reason the resolver can produce (schedule/event/callback/
    shared_interest/check_in), the built intent's source_type/source_id/
    evidence/hook are consistent and resolve_intent_source(intent) is NOT
    None — a ProactiveIntent always implies a real persisted source.

    Each reason gets its OWN store so the candidates cannot crowd each other
    out (shared-interest outranks callback, check-in needs a 12h silence
    gap); the resolver must return exactly the expected reason and the
    intent must be resolvable back to a real row.
    """
    from harness.bootstrap import ensure_companion_initialized

    # Schedule: a planned agenda item around now.
    s1 = _store(tmp_path, "p1b_sched.db")
    try:
        item = _ground_agenda(s1, 9.5, 10.5, item_id="slot_sched",
                              activity="pottery class")
        intent = IntentResolver(s1).resolve(10.0)
        assert intent is not None and intent.reason == REASON_SCHEDULE
        assert s1.resolve_intent_source(intent) is not None
        assert compose_hook(item, REASON_SCHEDULE) == intent.hook
    finally:
        s1.close()

    # Event: a completed agenda item within 48h.
    s2 = _store(tmp_path, "p1b_event.db")
    try:
        done = _ground_agenda(s2, 4.0, 5.0, item_id="slot_done", salience=0.9,
                              activity="finished run")
        s2.update_agenda_item_status("slot_done", "completed")
        intent = IntentResolver(s2).resolve(5.5)
        assert intent is not None and intent.reason == REASON_EVENT
        assert s2.resolve_intent_source(intent) is not None
        assert compose_hook(done, REASON_EVENT) == intent.hook
    finally:
        s2.close()

    # Callback: a CALLBACK episode, resolved outside check-in windows.
    s3 = _store(tmp_path, "p1b_cb.db")
    try:
        s3.insert_episode(EpisodicMemory(
            "ep_cb", "user asked to be reminded to water the plants",
            MemoryKind.CALLBACK, 9.0, 9.1, 0.8, 0, None, None,
            "day-0", (1,), ("remind me to water the plants",), ("plants",),
        ))
        intent = IntentResolver(s3).resolve(14.0)  # 14:00, no check-in window
        assert intent is not None and intent.reason == REASON_CALLBACK
        assert s3.resolve_intent_source(intent) is not None
    finally:
        s3.close()

    # Shared interest: an episode tagged with a persona interest.
    s4 = _store(tmp_path, "p1b_si.db")
    try:
        ensure_companion_initialized(
            s4, seed=SEED, user=UserProfile(name="u", interests=("pottery",))
        )
        s4.insert_episode(EpisodicMemory(
            "ep_si", "user talked about pottery class", MemoryKind.SHARED_EPISODE,
            9.0, 9.1, 0.7, 0, None, None,
            "day-0", (1,), ("pottery class is fun",), ("pottery",),
        ))
        intent = IntentResolver(s4).resolve(9.5)
        assert intent is not None and intent.reason == REASON_SHARED_INTEREST
        assert s4.resolve_intent_source(intent) is not None
    finally:
        s4.close()

    # Check-in: episodes plus a >12h silence gap, inside the 08-11 window.
    s5 = _store(tmp_path, "p1b_ci.db")
    try:
        s5.insert_episode(EpisodicMemory(
            "ep_anchor", "we talked about the trip", MemoryKind.SHARED_EPISODE,
            9.0, 9.1, 0.6, 0, None, None,
            "day-0", (1,), ("long talk about the trip",), ("trip",),
        ))
        s5.add_message("user", "hello", 8.0, 0, proactive=False, session_id="day-0")
        intent = IntentResolver(s5).resolve(21.0)  # 21:00, evening window
        assert intent is not None and intent.reason == REASON_CHECK_IN
        assert intent.source_type == "check_in"
        assert s5.resolve_intent_source(intent) is not None
        assert "gap_h" in intent.evidence  # evidence is the silence gap
    finally:
        s5.close()


# Unknown and expired intent ids raise ValueError


def test_p1c_unknown_intent_id_raises_value_error_no_message(tmp_path):
    """fire_proactive('does-not-exist') raises ValueError and produces NO
    message row and NO client call — an unknown id can never become a
    message."""
    store = _store(tmp_path, "p1c.db")
    store.save_daily_state(0, {"day": 0, "M": 6, "m": 0.0, "g": 0.7, "p": 0.5,
                               "arg": 0.0, "mu": 0.0, "eta": 0.0,
                               "cycle_day": 0.0, "phase_label": "phase_a",
                               "seed": SEED, "score": None})
    session = _session(store, clock=VirtualClock(t_h=10.0))
    try:
        before = len(store.recent_messages())
        with pytest.raises(ValueError):
            session.fire_proactive("does-not-exist")
        assert len(store.recent_messages()) == before, (
            "unknown intent id produced a message row"
        )
        assert session.client.calls == [], "unknown intent id reached the LLM"
    finally:
        store.close()


def test_p1d_expired_intent_id_raises_value_error_no_message(tmp_path):
    """fire_proactive on a STORED but EXPIRED intent raises ValueError (the
    intent's validity window has closed) and produces no message — an
    expired intent is never deliverable."""
    store = _store(tmp_path, "p1d.db")
    store.save_daily_state(0, {"day": 0, "M": 6, "m": 0.0, "g": 0.7, "p": 0.5,
                               "arg": 0.0, "mu": 0.0, "eta": 0.0,
                               "cycle_day": 0.0, "phase_label": "phase_a",
                               "seed": SEED, "score": None})
    item = _ground_agenda(store, 9.5, 10.5, item_id="expired_slot")
    intent = _stored_intent(item, "expired-1", t_h=9.9)
    store.save_proactive_intent(intent)
    # Fire at 20:00, 10h after creation; validity is 3h.
    session = _session(store, clock=VirtualClock(t_h=20.0))
    try:
        assert content_gate(intent, store, now_h=20.0).code == "expired"
        before = len(store.recent_messages())
        with pytest.raises(ValueError):
            session.fire_proactive("expired-1")
        assert len(store.recent_messages()) == before
        assert session.client.calls == []
        # The intent row is untouched.
        assert store.load_proactive_intent("expired-1") is not None
    finally:
        store.close()


# Exact-id isolation between same-reason intents


def test_p1e_exact_id_isolation_between_same_reason_siblings(tmp_path):
    """Store-level isolation (invariant 7): two intents with the SAME reason
    and the SAME hook are distinct rows; loading by exact id returns the
    exact one; lifecycle updates of one never touch the sibling."""
    store = _store(tmp_path, "p1e.db")
    try:
        pottery = _ground_agenda(store, 9.5, 10.5, item_id="pottery",
                                 activity="pottery class")
        gym = _ground_agenda(store, 9.5, 10.5, item_id="gym",
                             activity="gym session")
        i87 = _stored_intent(pottery, "87", t_h=9.9)
        i88 = _stored_intent(gym, "88", t_h=9.95)
        store.save_proactive_intent(i87)
        store.save_proactive_intent(i88)
        assert store.load_proactive_intent("87") == i87
        assert store.load_proactive_intent("88") == i88
        assert i87.reason == i88.reason == REASON_SCHEDULE
        # Firing #87 updates only #87.
        store.update_proactive_intent_status("87", "fired")
        assert {i.id for i in store.list_proactive_intents(status="fired")} == {"87"}
        assert store.load_proactive_intent("88") == i88, "sibling row mutated"
        # Superseding #88 leaves #87's lifecycle alone.
        store.update_proactive_intent_status("88", "suppressed")
        assert {i.id for i in store.list_proactive_intents(status="fired")} == {"87"}
        assert {i.id for i in store.list_proactive_intents(status="suppressed")} == {"88"}
    finally:
        store.close()


def test_p1g_session_fires_exact_id_not_reason_sibling(tmp_path):
    """Session-level (A5 seam, invariant 6/7): with #87 and #88 stored, the
    outgoing message persists EXACTLY the id passed to fire_proactive — the
    sibling is never substituted, even though a reason-only lookup would
    return the most recent sibling."""
    store = _store(tmp_path, "p1g.db")
    store.save_daily_state(0, {"day": 0, "M": 6, "m": 0.0, "g": 0.7, "p": 0.5,
                               "arg": 0.0, "mu": 0.0, "eta": 0.0,
                               "cycle_day": 0.0, "phase_label": "phase_a",
                               "seed": SEED, "score": None})
    pottery = _ground_agenda(store, 9.5, 10.5, item_id="pottery", salience=0.9,
                             activity="pottery class")
    gym = _ground_agenda(store, 9.5, 10.5, item_id="gym", salience=0.7,
                         activity="gym session")
    store.save_proactive_intent(_stored_intent(pottery, "87", t_h=9.9))
    store.save_proactive_intent(_stored_intent(gym, "88", t_h=9.95))
    session = _session(store, clock=VirtualClock(t_h=10.0))
    try:
        result = session.fire_proactive("87")
        assert result.reply == "ok!"
        last = store.recent_messages()[-1]
        assert last["intent_id"] == "87", (
            f"message carries {last['intent_id']!r}, expected the exact id '87'"
        )
        assert last["proactive"] == 1
        # The sibling's lifecycle is untouched.
        assert "88" not in {i.id for i in store.list_proactive_intents(status="fired")}
        # The prompt renders #87's hook.
        system = session.client.calls[-1]["system"]
        assert "Agenda: pottery class" in system
        assert "Agenda: gym session" not in system
    finally:
        store.close()


# An opportunity without an intent produces no message


def test_p1f_opportunity_without_intent_no_message(tmp_path):
    """A planned ContactOpportunity with NO grounded source anywhere in the
    store must flow through the runtime as a SUPPRESSION: no message, no
    persisted intent, suppression logged as no_grounded_reason (a normal
    outcome, never an error) and the event row consumed, not stranded."""
    store = _store(tmp_path, "p1f.db")
    schedule = ProactiveSchedule.plan_and_persist(1, SEED, PERSONA, TIMING, store)
    assert schedule.event_hours, "precondition: at least one opportunity today"
    h = float(schedule.event_hours[0])
    opp = schedule.opportunity_for(h)
    assert opp is not None and not hasattr(opp, "reason")
    session = _session(store)
    channel = FakeChannel()
    _run(store, session, schedule, channel, max_hours=h + 2.0)
    assert channel.sent == [], "opportunity without intent produced a message"
    assert store.list_proactive_intents() == [], (
        "ungrounded intent was persisted"
    )
    suppressed = {
        e["detail"] for e in store.events_since(0)
        if e["event"] == "proactive_suppressed"
    }
    assert "no_grounded_reason" in suppressed
    rows = {abs(float(r["t_h"])): r for r in store.schedule_events_for_seed(SEED)}
    assert rows[h]["status"] == "fired", "suppressed opportunity row stranded"
