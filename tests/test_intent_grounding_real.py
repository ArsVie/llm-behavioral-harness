"""MAJOR-1 gate-fix integration tests (orchestrator, 2026-08-08).

Every source_type harness.proactive emits must resolve against the REAL
SQLiteStore and pass the content gate — previously only `agenda_item`
resolved in production (A2 mapped 3 types, A7 emits 5), so all
life-event/callback/shared-interest/check-in hooks suppressed with
`no_source`. These tests drive the REAL store end to end through the public
IntentResolver API: each scenario isolates exactly one candidate kind.
"""

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
NOW_H = 200.0  # day 8, 08:00 local — inside the check-in window (8-11)


def _mk_store(tmp_path, name: str) -> SQLiteStore:
    store = SQLiteStore(tmp_path / name)
    # episodes' source sessions must exist (g8b provenance check)
    for sid, start in (("day-6", 144.0), ("day-7", 168.0)):
        store.open_session(sid, start)
        store.close_session(sid, start + 24.0)
    return store


def _episode(ep_id: str, kind: MemoryKind, occurred: float, tags=("t",),
             importance: float = 0.9, summary: str = "shared moment") -> EpisodicMemory:
    return EpisodicMemory(
        ep_id, summary, kind, occurred, occurred + 1.0, importance, 0, None,
        None, f"day-{int(occurred // 24)}", (int(occurred),),
        ("verbatim anchor",), tags,
    )


def _resolve_once(store: SQLiteStore) -> ProactiveIntent | None:
    resolver = IntentResolver(store, rng=stream_rng(SEED, 77))
    return resolver.resolve(NOW_H)


def test_schedule_intent_resolves_and_passes_gate(tmp_path):
    store = _mk_store(tmp_path, "sched.db")
    store.save_agenda(8, DailyAgenda(8, (
        AgendaItem("i_plan", 199.5, 201.0, "pottery class", "arc", "arc1",
                   0.7, "planned"),
    )))
    intent = _resolve_once(store)
    assert intent is not None and intent.source_type == "agenda_item"
    assert store.resolve_intent_source(intent) is not None
    assert content_gate(intent, store, now_h=NOW_H).allowed
    store.close()


def test_life_event_intent_resolves_and_passes_gate(tmp_path):
    store = _mk_store(tmp_path, "event.db")
    store.save_agenda(8, DailyAgenda(8, (
        AgendaItem("i_done", 190.0, 195.0, "finished a drawing", "interest",
                   "drawing", 0.6, "completed"),
    )))
    intent = _resolve_once(store)
    assert intent is not None and intent.source_type == "life_event"
    assert store.resolve_intent_source(intent) is not None
    assert content_gate(intent, store, now_h=NOW_H).allowed
    store.close()


def test_callback_intent_resolves_and_passes_gate(tmp_path):
    store = _mk_store(tmp_path, "cb.db")
    store.insert_episode(_episode(
        "ep_cb", MemoryKind.CALLBACK, 160.0, tags=("callback",),
        summary="user asked me to remind him about the vet"))
    # 12:00 local — outside the check-in windows so callback wins the rank
    resolver = IntentResolver(store, rng=stream_rng(SEED, 77))
    intent = resolver.resolve(180.0)
    assert intent is not None and intent.source_type == "callback"
    assert store.resolve_intent_source(intent) is not None
    assert content_gate(intent, store, now_h=180.0).allowed
    store.close()


def test_shared_interest_intent_resolves_and_passes_gate(tmp_path):
    store = _mk_store(tmp_path, "si.db")
    store.save_interests([Interest("metal", "exact", 0.9)])
    store.insert_episode(_episode(
        "ep_metal", MemoryKind.SHARED_EPISODE, 150.0, tags=("metal",),
        summary="we talked about going to a metal show"))
    # 12:00 local — outside the check-in windows so shared-interest wins
    resolver = IntentResolver(store, rng=stream_rng(SEED, 77))
    intent = resolver.resolve(180.0)
    assert intent is not None and intent.source_type == "shared_interest"
    assert store.resolve_intent_source(intent) is not None
    assert content_gate(intent, store, now_h=180.0).allowed
    store.close()


def test_check_in_intent_resolves_and_passes_gate(tmp_path):
    store = _mk_store(tmp_path, "ci.db")
    # only an anchor episode: no callbacks, no interest tags, no agenda
    store.insert_episode(_episode(
        "ep_anchor", MemoryKind.SHARED_EPISODE, 180.0, tags=("plants",),
        importance=0.4, summary="quiet evening, he mentioned his plants"))
    intent = _resolve_once(store)
    assert intent is not None and intent.source_type == "check_in"
    assert store.resolve_intent_source(intent) is not None
    assert content_gate(intent, store, now_h=NOW_H).allowed
    store.close()


def test_resolver_emits_nothing_when_store_is_empty(tmp_path):
    store = _mk_store(tmp_path, "empty.db")
    assert _resolve_once(store) is None  # no grounded reason -> SUPPRESS
    store.close()


def test_stale_intent_fails_the_content_gate(tmp_path):
    store = _mk_store(tmp_path, "stale.db")
    store.save_agenda(8, DailyAgenda(8, (
        AgendaItem("i_done", 190.0, 195.0, "finished a drawing", "interest",
                   "drawing", 0.6, "completed"),
    )))

    # a persisted intent whose agenda source was skipped must be rejected
    stale = ProactiveIntent("p_stale", "life_event", "life_event", "i_done",
                            "Finished: finished a drawing", 190.0, 210.0, 0.6,
                            "agenda_item:i_done")
    store.update_agenda_item_status("i_done", "skipped")
    decision = content_gate(stale, store, now_h=NOW_H)
    assert not decision.allowed
    assert decision.code == "source_superseded"

    # an intent pointing at a source that never existed -> no_source
    ghost = ProactiveIntent("p_ghost", "callback", "callback", "ep_ghost",
                            "Callback: x", 190.0, 210.0, 0.6, "episode:ep_ghost")
    decision = content_gate(ghost, store, now_h=NOW_H)
    assert not decision.allowed
    assert decision.code == "no_source"
    store.close()
