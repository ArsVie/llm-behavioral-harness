"""A3: negotiation decisions -> companion episodes (G0 contract seam).

Maps a ``NegotiationEpisode`` (harness.negotiation_contract, frozen) onto the
existing ``store.insert_episode`` seam.  memory.py is MUST-NOT-TOUCH; this
module is the only adapter A1 needs.

Determinism contract:
  * episode id is derived purely from episode fields (``neg-`` + item id +
    outcome + numeric occurred time), so replaying the same episode yields
    the same id; ``insert_episode``'s ON CONFLICT(id) upsert means a
    re-emitted episode never duplicates a row.
  * summary text is composed from episode fields (fallback only: A1's own
    ``episode.summary`` is respected when provided).
  * no wall-clock / random inputs anywhere.
"""

from __future__ import annotations

from harness.domain import EpisodicMemory, MemoryKind
from harness.negotiation_contract import (
    EMIT_FORCED,
    EMIT_GO_WITHOUT_DELAYS,
    EMIT_GO_WITH_DELAYS,
    EMIT_SKIP,
    TAG_DELAY,
    TAG_FORCED,
    TAG_GO,
    TAG_SKIP,
    NegotiationEpisode,
)
from harness.store import SQLiteStore

#: Outcomes the contract's NegotiationEpisode can carry.
_OUTCOME_GO = "GO"
_OUTCOME_SKIP = "SKIP"
_OUTCOME_FORCED = "FORCED"


def _should_emit(episode: NegotiationEpisode) -> bool:
    """Salience gate per contract: only consequential outcomes emit."""
    if episode.outcome == _OUTCOME_GO:
        if episode.delay_count >= 1:
            return EMIT_GO_WITH_DELAYS
        return EMIT_GO_WITHOUT_DELAYS
    if episode.outcome == _OUTCOME_SKIP:
        return EMIT_SKIP
    if episode.outcome == _OUTCOME_FORCED:
        return EMIT_FORCED
    return False


def _compose_summary(episode: NegotiationEpisode) -> str:
    """Deterministic natural summary from the episode's own fields."""
    if episode.outcome == _OUTCOME_GO:
        return (
            "kept choosing to stay with you instead of "
            + episode.activity
            + " ("
            + str(episode.delay_count)
            + " delays), then went"
        )
    if episode.outcome == _OUTCOME_SKIP:
        return "skipped " + episode.activity + " to stay with you"
    # FORCED (backstop): window closed, missed entirely
    return "missed " + episode.activity + " entirely — window closed"


def _episode_tags(episode: NegotiationEpisode) -> tuple[str, ...]:
    """Negotiation tags layered on top of A1's own episode tags."""
    tags = list(episode.tags)
    if episode.outcome == _OUTCOME_GO:
        tags.append(TAG_GO)
    elif episode.outcome == _OUTCOME_SKIP:
        tags.append(TAG_SKIP)
    elif episode.outcome == _OUTCOME_FORCED:
        tags.append(TAG_FORCED)
    if episode.delay_count >= 1:
        tags.append(TAG_DELAY)
        tags.append("negotiation_delay:" + str(episode.delay_count))
    return tuple(tags)


def _episode_id(episode: NegotiationEpisode) -> str:
    """Deterministic id: same episode inputs -> same id, always."""
    return (
        "neg-"
        + episode.item_id
        + "-"
        + episode.outcome
        + "-"
        + str(episode.occurred_at_t_h)
    )


def emit_negotiation_episode(
    store: SQLiteStore, episode: NegotiationEpisode
) -> str | None:
    """Emit a negotiation outcome as a companion episode; returns the id.

    Returns ``None`` when the salience gate withholds emission (a plain go
    with zero delays per the contract; unknown outcomes are also withheld).
    Replay-idempotent: emitting the same episode twice writes one row.
    """
    if not _should_emit(episode):
        return None
    mem = EpisodicMemory(
        id=_episode_id(episode),
        summary=episode.summary if episode.summary else _compose_summary(episode),
        category=MemoryKind.COMPANION_EPISODE,
        occurred_at_t_h=float(episode.occurred_at_t_h),
        created_at_t_h=float(episode.occurred_at_t_h),
        importance=float(episode.salience),
        access_count=0,
        last_accessed_t_h=None,
        affect=None,
        source_session_id=episode.source_session_id,
        source_turn_ids=(),
        verbatim_anchors=(),
        tags=_episode_tags(episode),
    )
    return store.insert_episode(mem)
