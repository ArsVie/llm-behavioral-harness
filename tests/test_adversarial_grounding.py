"""A9 adversarial wave — GROUNDING attack class (plan §9, cases G-1..G-8).

Hard invariant attacked (plan §14.13): NO grounded source -> NO proactive
message. Every fired message must carry an intent_id resolving to a live,
non-superseded source at fire time.
"""

from __future__ import annotations

from engine.rng import stream_rng
from harness.domain import (
    AgendaItem,
    DailyAgenda,
    EpisodicMemory,
    Interest,
    MemoryKind,
    ProactiveIntent,
)
from harness.gates import content_gate
from harness.proactive import IntentResolver
from harness.store import SQLiteStore

SEED = 4242
#: 12:00 local — outside the check-in windows (8-11, 19-22), so agenda /
#: callback / shared-interest candidates win the rank cleanly.
NOW_H = 300.0


def _store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


def _resolver(store, seed: int = SEED) -> IntentResolver:
    return IntentResolver(store, rng=stream_rng(seed, 77))


def _episode(ep_id: str, kind: MemoryKind, occurred: float, tags=("t",),
             importance: float = 0.9, summary: str = "shared moment",
             session: str | None = None) -> EpisodicMemory:
    return EpisodicMemory(
        ep_id, summary, kind, occurred, occurred + 1.0, importance, 0, None,
        None, session or f"day-{int(occurred // 24)}", (int(occurred),),
        (summary,), tags,
    )


def _agenda_item(item_id: str, start: float, end: float, activity: str = "pottery",
                 status: str = "planned", source_type: str = "arc") -> AgendaItem:
    return AgendaItem(item_id, start, end, activity, source_type,
                      "arc1" if source_type == "arc" else "drawing", 0.7, status)


#: items below cover NOW_H (300.0 = day 12 12:00) so the resolver's 2h margin
#: always admits them (dist == 0).
def _near_item(item_id: str, activity: str = "pottery class") -> AgendaItem:
    return _agenda_item(item_id, 299.0, 301.0, activity)


# --------------------------------------------------------------------------- #
# G-1: source deleted / expired before firing
# --------------------------------------------------------------------------- #


def test_g1_agenda_item_skipped_before_firing_suppressed(tmp_path):
    """G-1: intent created referencing an agenda item; the item is forced to
    a removed state ('skipped') before fire time. The message must be
    SUPPRESSED (source_superseded), never hallucinated, with no fallback to a
    generic 'schedule' reason; the intent row ends suppressed."""
    store = _store(tmp_path, "g1a.db")
    store.save_agenda(8, DailyAgenda(8, (
        _near_item("pottery_1"),
    )))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None and intent.source_type == "agenda_item"
    store.update_agenda_item_status("pottery_1", "skipped")
    decision = content_gate(intent, store, now_h=NOW_H)
    assert not decision.allowed
    assert decision.code == "source_superseded"
    # the runtime marks the intent suppressed and never falls back to schedule
    store.save_proactive_intent(intent)
    store.update_proactive_intent_status(intent.id, "suppressed")
    row = store.conn.execute(
        "SELECT status FROM proactive_intents WHERE id = ?", (intent.id,)
    ).fetchone()
    assert row["status"] == "suppressed"
    store.close()


def test_g1b_agenda_item_row_deleted_suppressed(tmp_path):
    """G-1 variant: the item ROW is hard-deleted from the store. The content
    gate's existence check must reject with no_source (no hallucinated
    message)."""
    store = _store(tmp_path, "g1b.db")
    store.save_agenda(8, DailyAgenda(8, (
        _near_item("pottery_2"),
    )))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None
    store.conn.execute("DELETE FROM agenda_items WHERE id = 'pottery_2'")
    store.conn.commit()
    assert store.resolve_intent_source(intent) is None
    decision = content_gate(intent, store, now_h=NOW_H)
    assert not decision.allowed and decision.code == "no_source"
    store.close()


# --------------------------------------------------------------------------- #
# G-2: shared-interest reason with no shared-interest record
# --------------------------------------------------------------------------- #


def test_g2_shared_interest_without_record_no_grounded_reason(tmp_path):
    """G-2: interests exist but NO episode carries a matching tag (and no
    other candidate exists) → resolver returns None ⇒ SUPPRESS:
    no_grounded_reason; no invented 'we both like X' text can be emitted."""
    store = _store(tmp_path, "g2.db")
    store.save_interests([Interest("metal", "exact", 0.9)])
    store.insert_episode(_episode(
        "ep_plants", MemoryKind.SHARED_EPISODE, 280.0, tags=("plants",),
        summary="quiet evening, he mentioned his plants",
    ))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is None  # no shared-interest record ⇒ no_grounded_reason
    store.close()


def test_g2b_shared_interest_without_record_empty_store(tmp_path):
    """G-2 variant: completely empty store → None (blank slate)."""
    store = _store(tmp_path, "g2b.db")
    assert _resolver(store).resolve(NOW_H) is None
    store.close()


# --------------------------------------------------------------------------- #
# G-3: hook not attached to the source
# --------------------------------------------------------------------------- #


def test_g3_hook_not_traceable_to_source_rejected(tmp_path):
    """G-3: an intent whose hook references a detail ABSENT from the source
    record (LLM-embellished hook) must be rejected even though the source
    exists — the gate re-derives the hook deterministically."""
    store = _store(tmp_path, "g3.db")
    store.save_agenda(8, DailyAgenda(8, (
        _agenda_item("pottery_3", 295.0, 297.0, "pottery class"),
    )))
    embellished = ProactiveIntent(
        "pi_embellished", "schedule", "agenda_item", "pottery_3",
        "Agenda: pottery class (295.0-297.0h) — I'm nervous about glazing the bowl",
        NOW_H, NOW_H + 3.0, 0.6, "agenda_item:pottery_3",
    )
    decision = content_gate(embellished, store, now_h=NOW_H)
    assert not decision.allowed
    assert decision.code == "hook_mismatch"
    store.close()


# --------------------------------------------------------------------------- #
# G-4: source superseded at L4 (stale truth must not fire)
# --------------------------------------------------------------------------- #


def test_g4_superseded_l4_assertion_grounds_nothing(tmp_path):
    """G-4: the intent's source episode embodies a fact whose L4 assertion was
    SUPERSEDED ('Luna' flipped to 'no cat'). The stale claim must NOT reach a
    proactive message even though the old episode row still exists. The
    content gate must treat the superseded fact as an invalid source."""
    from harness.domain import UserModelAssertion

    store = _store(tmp_path, "g4.db")
    store.save_interests([Interest("music", "exact", 0.9)])
    store.insert_episode(_episode(
        "ep_luna", MemoryKind.SHARED_EPISODE, 280.0, tags=("music",),
        summary="we talked about my cat Luna",
    ))
    # L4: the cat assertion was superseded (M-1b flow result)
    store.upsert_assertion(UserModelAssertion(
        "user:cat", "user has a cat named Luna", 0.6, 60.0,
        ("ep_luna",), "superseded",
    ))
    store.upsert_assertion(UserModelAssertion(
        "user:luna", "user no longer has luna", 0.7, 240.0,
        ("ep_luna2",), "current",
    ))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None and intent.source_type == "shared_interest"
    decision = content_gate(intent, store, now_h=NOW_H)
    assert not decision.allowed, (
        "superseded L4 fact must not ground a proactive message "
        f"(gate allowed {intent.hook!r})"
    )
    store.close()


# --------------------------------------------------------------------------- #
# G-5: 'just finished X' with completion not persisted
# --------------------------------------------------------------------------- #


def test_g5_completion_claim_without_persisted_completion(tmp_path):
    """G-5: an intent claiming 'Finished: X' (life_event) whose agenda item is
    still 'planned' in the store (completion write lost). The message must
    never claim completion the store does not record — suppressed or re-hooked
    to a truthful source."""
    store = _store(tmp_path, "g5.db")
    store.save_agenda(8, DailyAgenda(8, (
        _agenda_item("pottery_5", 290.0, 293.0, "pottery class"),
    )))
    stale = ProactiveIntent(
        "pi_finished", "event", "life_event", "pottery_5",
        "Finished: pottery class", 296.0, 300.0, 0.6, "life_event:pottery_5",
    )
    # the item is still 'planned' — the completion write was lost
    assert store.resolve_intent_source(stale).status == "planned"
    decision = content_gate(stale, store, now_h=NOW_H)
    assert not decision.allowed, (
        "life_event claim passed while the store records the item as planned"
    )
    store.close()


# --------------------------------------------------------------------------- #
# G-6: timeliness boundary at valid_until
# --------------------------------------------------------------------------- #


def test_g6_valid_until_boundary_inclusive(tmp_path):
    """G-6: fire attempted at V−ε, at V (inclusive — pinned by test), and at
    V+ε. Only V+ε may expire, so restart timing can never flip the verdict."""
    store = _store(tmp_path, "g6.db")
    store.save_agenda(8, DailyAgenda(8, (
        _near_item("pottery_6"),
    )))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None
    V = intent.valid_until_t_h
    assert content_gate(intent, store, now_h=V - 0.01).allowed
    assert content_gate(intent, store, now_h=V).allowed, (
        "valid_until must be inclusive (at V the intent is still timely)"
    )
    decision = content_gate(intent, store, now_h=V + 0.01)
    assert not decision.allowed and decision.code == "expired"
    store.close()


# --------------------------------------------------------------------------- #
# G-7: no re-fire / no re-creation of intents after restart
# --------------------------------------------------------------------------- #


def test_g7_fired_intent_never_refired_and_expired_not_resurrected(tmp_path):
    """G-7: a fired intent row keeps its status (idempotent lifecycle — a
    later opportunity at the same moment re-derives the SAME id but the upsert
    must not resurrect the fired row), expired intents are never re-used, and
    every fireable intent resolves to a live source."""
    store = _store(tmp_path, "g7.db")
    store.save_agenda(8, DailyAgenda(8, (
        _near_item("pottery_7"),
    )))
    resolver = _resolver(store)
    intent = resolver.resolve(NOW_H)
    assert intent is not None
    store.save_proactive_intent(intent)
    store.update_proactive_intent_status(intent.id, "fired")

    # 'restart': a fresh resolver over the same store, same opportunity
    resolver2 = _resolver(store)
    again = resolver2.resolve(NOW_H)
    assert again is not None
    store.save_proactive_intent(again)  # upsert path the runtime uses
    row = store.conn.execute(
        "SELECT status FROM proactive_intents WHERE id = ?", (again.id,)
    ).fetchone()
    assert row["status"] == "fired", "fired intent resurrected by a later resolve"
    # expired intents stay expired
    store.update_proactive_intent_status(intent.id, "expired")
    store.save_proactive_intent(intent)
    row = store.conn.execute(
        "SELECT status FROM proactive_intents WHERE id = ?", (intent.id,)
    ).fetchone()
    assert row["status"] == "expired"
    # every fired message's intent_id resolves to a real source
    for i in store.list_proactive_intents():
        assert store.resolve_intent_source(i) is not None
    store.close()


# --------------------------------------------------------------------------- #
# G-8: callback memory deleted / provenance broken
# --------------------------------------------------------------------------- #


def test_g8_callback_memory_deleted_before_fire_suppressed(tmp_path):
    """G-8: a CALLBACK memory ('promise to send the playlist') is the intent's
    source; the memory is deleted before fire time → suppressed; no 'you said
    you'd…' message without a record."""
    store = _store(tmp_path, "g8a.db")
    store.insert_episode(_episode(
        "ep_cb", MemoryKind.CALLBACK, 290.0, tags=("callback",),
        summary="promised to send the playlist",
    ))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None and intent.source_type == "callback"
    store.conn.execute("DELETE FROM memory_episodes WHERE id = 'ep_cb'")
    store.conn.commit()
    decision = content_gate(intent, store, now_h=NOW_H)
    assert not decision.allowed and decision.code == "no_source"
    store.close()


def test_g8b_callback_provenance_session_gone_suppressed(tmp_path):
    """G-8 variant: the callback episode exists but its source session no
    longer exists in the store (provenance chain broken) → the message must
    still be suppressed: no record of the promise ⇒ no claim."""
    store = _store(tmp_path, "g8b.db")
    store.insert_episode(_episode(
        "ep_cb2", MemoryKind.CALLBACK, 290.0, tags=("callback",),
        summary="promised to send the playlist", session="day-99",
    ))
    # the source session was never registered / was deleted
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM memory_sessions WHERE session_id='day-99'"
    ).fetchone()["n"] == 0
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None and intent.source_type == "callback"
    decision = content_gate(intent, store, now_h=NOW_H)
    assert not decision.allowed, (
        "callback with a broken provenance chain must not fire"
    )
    store.close()


# --------------------------------------------------------------------------- #
# V-1: gate-level provenance completeness (plan §5-A9 V1, invariant 4) —
# content_gate allows an intent ONLY when its source is live; every source
# family flips to a suppression code the moment its record dies.
# --------------------------------------------------------------------------- #


def test_v1a_gate_allows_only_intents_with_live_sources(tmp_path):
    """For agenda and episodic (callback/shared-interest) sources: an intent
    whose source exists, is not superseded, and whose hook re-derives from
    the source passes; deleting the record → no_source; flipping the item to
    skipped → source_superseded. A proactive message can never be grounded
    on a dead record (g8b semantics keep passing)."""
    # agenda source: planned → ok; skipped → source_superseded; deleted → no_source
    store = _store(tmp_path, "v1a.db")
    store.save_agenda(0, DailyAgenda(0, (_agenda_item("g1", 299.0, 301.0),)))
    intent = _resolver(store).resolve(NOW_H)
    assert intent is not None and intent.source_type == "agenda_item"
    assert content_gate(intent, store, now_h=NOW_H).allowed
    store.update_agenda_item_status("g1", "skipped")
    assert content_gate(intent, store, now_h=NOW_H).code == "source_superseded"
    store.conn.execute("DELETE FROM agenda_items WHERE id = 'g1'")
    store.conn.commit()
    assert content_gate(intent, store, now_h=NOW_H).code == "no_source"

    # callback source: live → ok; deleted → no_source (G-8)
    # (g8b: the episode's source session must exist — register it)
    store.open_session("day-12", 288.0)
    store.close_session("day-12", 312.0)
    store.insert_episode(_episode(
        "ep_cb", MemoryKind.CALLBACK, 290.0, tags=("callback",),
        summary="promised to send the playlist",
    ))
    intent2 = _resolver(store).resolve(NOW_H)
    assert intent2 is not None and intent2.source_type == "callback"
    assert content_gate(intent2, store, now_h=NOW_H).allowed
    store.conn.execute("DELETE FROM memory_episodes WHERE id = 'ep_cb'")
    store.conn.commit()
    assert content_gate(intent2, store, now_h=NOW_H).code == "no_source"

    # shared-interest source: live → ok; deleted → no_source
    store.insert_episode(_episode(
        "ep_si", MemoryKind.SHARED_EPISODE, 290.0, tags=("pottery",),
        importance=0.9, summary="user talked about pottery class",
    ))
    from harness.domain import Interest, PersonaProfile

    store.save_persona(PersonaProfile(
        name="Nova", core="You are Nova.", interests=(Interest("pottery", "exact", 0.8),),
        routines=(),
    ))
    intent3 = _resolver(store).resolve(NOW_H)
    assert intent3 is not None and intent3.source_type == "shared_interest"
    assert content_gate(intent3, store, now_h=NOW_H).allowed
    store.conn.execute("DELETE FROM memory_episodes WHERE id = 'ep_si'")
    store.conn.commit()
    assert content_gate(intent3, store, now_h=NOW_H).code == "no_source"
    store.close()


def test_v1b_suppressed_intents_never_carry_a_message_row(tmp_path):
    """V-1 (message-level): after a runtime run whose only proactive event is
    suppressed (source deleted), NO message row exists at all — a suppressed
    intent is never attached to a message, and no ghost row carries its id."""
    store = _store(tmp_path, "v1b.db")
    from harness.channels.base import FakeChannel
    from harness.client import FakeClient
    from harness.clock import VirtualClock
    from harness.judge import ScriptedJudge
    from harness.runtime import AsyncRuntime, TimeScale
    from harness.scheduler import ProactiveSchedule
    from harness.session import Session
    from engine.types import MoodVariant, PersonaParams, TimingParams

    persona = PersonaParams()
    timing = TimingParams()
    store.save_schedule_events(SEED, [{"t_h": NOW_H, "day": 12, "reason": "schedule"}])
    store.save_agenda(0, DailyAgenda(0, (_agenda_item("g1", 299.0, 301.0),)))
    # kill the source before the run: the event is grounded at resolve time
    # against the agenda — resolve happens AT fire time, so deleting the item
    # before the run means the resolver finds nothing → no_grounded_reason
    store.conn.execute("DELETE FROM agenda_items WHERE id = 'g1'")
    store.conn.commit()
    session = Session(
        store, persona=persona, timing=timing,
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=SEED,
        client=FakeClient(responses=["ok!"]), clock=VirtualClock(t_h=NOW_H),
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    channel = FakeChannel()

    async def record(_delay: float) -> None:
        return None

    runtime = AsyncRuntime(
        session, ProactiveSchedule.restore(SEED, store), channel,
        store=store, timing=timing, seed=SEED,
        time_scale=TimeScale(seconds_per_virtual_hour=0.02),
        max_virtual_hours=NOW_H + 1.0, resolver=IntentResolver(store),
        sleeper=record,
    )
    import asyncio

    asyncio.run(runtime.run())
    assert channel.sent == [], "suppressed event produced a message"
    assert store.recent_messages(limit=100) == [], (
        "suppressed intent left a message row behind"
    )
    assert store.list_proactive_intents() == [], (
        "unresolvable event persisted an intent"
    )
    store.close()
