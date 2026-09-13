"""Defensive branches that only fire on degenerate input."""

from __future__ import annotations

import pytest

from engine.types import TimingParams
from harness.behavior import derive_behavior
from harness.bubbles import parse_bubbles
from harness.embeddings import cosine
from harness.negotiation_state import NegotiationState


def test_bubbles_of_whitespace_only_text_is_empty():
    """Whitespace-only input yields no bubbles; [] and [one] both mean one message."""
    assert parse_bubbles("\n\n   \n") == []
    assert parse_bubbles("   ") == []


def test_bubbles_collapse_a_run_of_newlines_into_one_boundary():
    assert parse_bubbles("one\n\n\ntwo") == ["one", "two"]


def test_derive_behavior_rejects_a_non_positive_mood_scale():
    """A non-positive mood_scale is rejected instead of producing nonsense."""

    class _Record:
        M = 5
        phase_label = "follicular"
        mu = 0.0
        eta = 0.0
        g = 1.0
        m = 0.0

    for bad in (0, -1):
        with pytest.raises(ValueError, match="mood_scale"):
            derive_behavior(
                _Record(), TimingParams(), hour=12.0, mood_scale=bad
            )


@pytest.mark.parametrize("a,b", [
    ([0.0, 0.0], [1.0, 0.0]),
    ([1.0, 0.0], [0.0, 0.0]),
    ([0.0, 0.0], [0.0, 0.0]),
])
def test_cosine_of_a_zero_vector_is_zero_not_a_division_error(a, b):
    """An all-zero embedding degrades to 0.0 instead of raising."""
    assert cosine(a, b) == 0.0


def test_cosine_of_identical_vectors_is_one():
    assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_decide_index_counts_the_delays_taken():
    """decide_index counts the delays already taken (stable id across a restart)."""
    state = NegotiationState(
        item_id="g1", activity="pottery class", source_type="arc",
        start_t_h=10.0, end_t_h=11.0, salience=0.8,
    )
    assert state.decide_index == 0
    state.delay_count = 2
    assert state.decide_index == 2
