"""Defensive branches that only fire on degenerate input.

Each of these is a single guard that the ordinary paths never reach — the
kind of line that stays uncovered until something odd happens in
production, which is exactly when you want it to have been tested.
"""

from __future__ import annotations

import pytest

from engine.types import TimingParams
from harness.behavior import derive_behavior
from harness.bubbles import parse_bubbles
from harness.embeddings import cosine
from harness.negotiation_state import NegotiationState


def test_bubbles_of_whitespace_only_text_is_empty():
    """Whitespace-only input yields no bubbles at all.

    The session only treats a reply as bubbled when it gets >= 2 parts, so
    [] and [one] both mean "send it as a single message" — there is no path
    where an empty list loses a message.
    """
    assert parse_bubbles("\n\n   \n") == []
    assert parse_bubbles("   ") == []


def test_bubbles_collapse_a_run_of_newlines_into_one_boundary():
    assert parse_bubbles("one\n\n\ntwo") == ["one", "two"]


def test_derive_behavior_rejects_a_non_positive_mood_scale():
    """mood_scale divides the mood into valence; zero or negative would
    silently produce nonsense (or a ZeroDivisionError) instead of failing."""

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
    """An all-zero embedding is possible (an empty or unknown text); the
    similarity must degrade to 0.0 rather than raise."""
    assert cosine(a, b) == 0.0


def test_cosine_of_identical_vectors_is_one():
    assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_decide_index_counts_the_delays_taken():
    """The decide-leg index is what makes a decision id stable across a
    restart: neg-<item>-decide-<index>, index = delays already taken."""
    state = NegotiationState(
        item_id="g1", activity="pottery class", source_type="arc",
        start_t_h=10.0, end_t_h=11.0, salience=0.8,
    )
    assert state.decide_index == 0
    state.delay_count = 2
    assert state.decide_index == 2
