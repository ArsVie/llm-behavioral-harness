"""A3 tests: decisions-to-episodes emission hook (G0 contract).

Verifies the salience gate (plain go is not emitted; go-with-delays, skip
and forced-skip are), the deterministic episode id, replay idempotency via
the insert_episode ON CONFLICT upsert, the composed summary text, the
negotiation tags, and retrieval through store.list_episodes filtered on
MemoryKind.COMPANION_EPISODE.
"""

import pytest

from harness.domain import MemoryKind
from harness.negotiation_contract import (
    TAG_DELAY,
    TAG_FORCED,
    TAG_GO,
    TAG_SKIP,
    NegotiationEpisode,
)
from harness.negotiation_episodes import emit_negotiation_episode
from harness.store import SQLiteStore


def _episode(
    item_id: str = "item-gym",
    activity: str = "the gym",
    outcome: str = "GO",
    delay_count: int = 2,
    salience: float = 0.8,
    occurred_at_t_h: float = 100.5,
    summary: str = "",
    source_session_id: str = "sess-neg-1",
    tags: tuple[str, ...] = ("negotiation",),
) -> NegotiationEpisode:
    return NegotiationEpisode(
        item_id=item_id,
        activity=activity,
        outcome=outcome,
        delay_count=delay_count,
        salience=salience,
        occurred_at_t_h=occurred_at_t_h,
        summary=summary,
        source_session_id=source_session_id,
        tags=tags,
    )


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(tmp_path / "neg_episodes.db", audit_mode=True)
    yield s
    s.close()


def _row_count(store: SQLiteStore) -> int:
    return store.conn.execute(
        "SELECT COUNT(*) FROM memory_episodes"
    ).fetchone()[0]


# -- emission -----------------------------------------------------------------


def test_go_with_delays_emits(store):
    ep = _episode(outcome="GO", delay_count=2)
    ep_id = emit_negotiation_episode(store, ep)

    assert ep_id == "neg-item-gym-GO-100.5"
    mem = store.get_episode(ep_id)
    assert mem is not None
    assert mem.category == MemoryKind.COMPANION_EPISODE
    assert mem.summary == (
        "kept choosing to stay with you instead of the gym (2 delays), then went"
    )
    assert mem.importance == 0.8
    assert mem.source_session_id == "sess-neg-1"
    assert mem.access_count == 0
    assert mem.last_accessed_t_h is None
    assert mem.affect is None
    assert mem.source_turn_ids == ()
    assert mem.verbatim_anchors == ()
    assert mem.tags == (
        "negotiation", TAG_GO, TAG_DELAY, "negotiation_delay:2",
    )


def test_skip_emits(store):
    ep = _episode(outcome="SKIP", delay_count=0)
    ep_id = emit_negotiation_episode(store, ep)

    assert ep_id == "neg-item-gym-SKIP-100.5"
    mem = store.get_episode(ep_id)
    assert mem is not None
    assert mem.summary == "skipped the gym to stay with you"
    assert TAG_SKIP in mem.tags
    assert TAG_DELAY not in mem.tags
    assert not any(t.startswith("negotiation_delay:") for t in mem.tags)


def test_forced_emits(store):
    ep = _episode(outcome="FORCED", delay_count=1)
    ep_id = emit_negotiation_episode(store, ep)

    assert ep_id == "neg-item-gym-FORCED-100.5"
    mem = store.get_episode(ep_id)
    assert mem is not None
    assert mem.summary == "missed the gym entirely — window closed"
    assert TAG_FORCED in mem.tags
    assert TAG_DELAY in mem.tags
    assert "negotiation_delay:1" in mem.tags


# -- salience gate -------------------------------------------------------------


def test_plain_go_with_zero_delays_is_not_emitted(store):
    ep = _episode(outcome="GO", delay_count=0)
    ep_id = emit_negotiation_episode(store, ep)

    assert ep_id is None
    assert _row_count(store) == 0
    assert store.list_episodes(category=MemoryKind.COMPANION_EPISODE) == []


# -- determinism / idempotency -------------------------------------------------


def test_replay_is_idempotent(store):
    ep = _episode(outcome="GO", delay_count=3)
    first = emit_negotiation_episode(store, ep)
    second = emit_negotiation_episode(store, ep)

    assert first == second == "neg-item-gym-GO-100.5"
    assert _row_count(store) == 1
    mems = store.list_episodes(category=MemoryKind.COMPANION_EPISODE)
    assert len(mems) == 1
    assert mems[0].id == first


def test_episode_id_is_deterministic_across_outcomes(store):
    for outcome in ("GO", "SKIP", "FORCED"):
        ep = _episode(outcome=outcome, delay_count=1)
        emit_negotiation_episode(store, ep)
    assert _row_count(store) == 3
    ids = {m.id for m in store.list_episodes(
        category=MemoryKind.COMPANION_EPISODE
    )}
    assert ids == {
        "neg-item-gym-GO-100.5",
        "neg-item-gym-SKIP-100.5",
        "neg-item-gym-FORCED-100.5",
    }


# -- retrieval -----------------------------------------------------------------


def test_retrievable_via_list_episodes_category(store):
    emit_negotiation_episode(store, _episode(outcome="SKIP", delay_count=0))

    mems = store.list_episodes(category=MemoryKind.COMPANION_EPISODE)
    assert len(mems) == 1
    assert mems[0].id == "neg-item-gym-SKIP-100.5"
    assert mems[0].category == MemoryKind.COMPANION_EPISODE
    assert mems[0].summary == "skipped the gym to stay with you"


def test_a1_provided_summary_is_respected(store):
    ep = _episode(
        outcome="GO", delay_count=1,
        summary="kept choosing to stay with you instead of the gym (1 delays), then went",
    )
    emit_negotiation_episode(store, ep)

    mem = store.get_episode("neg-item-gym-GO-100.5")
    assert mem is not None
    assert mem.summary == (
        "kept choosing to stay with you instead of the gym (1 delays), then went"
    )
