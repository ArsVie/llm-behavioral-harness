"""Contrato de la primera capa actuadora del arnés conductual."""

from __future__ import annotations

from engine.types import DayRecord, TimingParams
from harness.behavior import derive_behavior


def _record(
    *,
    t: int = 0,
    mood: int = 5,
    g: float = 1.0,
    mu: float = 0.0,
    eta: float = 0.0,
    phase: str = "follicular",
) -> DayRecord:
    return DayRecord(
        t=t,
        m=0.0,
        g=g,
        arg=0.0,
        p=mood / 10,
        M=mood,
        score=0.0,
        mu=mu,
        eta=eta,
        cycle_day=float(t),
        phase_label=phase,
        seed=123,
    )


def test_low_mood_remains_caring_instead_of_becoming_cold() -> None:
    directive = derive_behavior(_record(mood=1, mu=-0.8), TimingParams(), hour=14.0)

    assert directive.warmth >= 0.35
    assert directive.playfulness < directive.reflectiveness
    assert "Keep care intact" in directive.prompt_brief


def test_valence_and_circadian_energy_are_independent_channels() -> None:
    happy_but_tired = derive_behavior(_record(mood=9), TimingParams(), hour=2.0)
    low_but_energetic = derive_behavior(_record(mood=2), TimingParams(), hour=14.0)

    assert happy_but_tired.valence > low_but_energetic.valence
    assert happy_but_tired.energy < low_but_energetic.energy
    assert happy_but_tired.playfulness > low_but_energetic.playfulness
    assert happy_but_tired.response_delay_s > low_but_energetic.response_delay_s


def test_previous_day_changes_momentum_without_overwriting_current_mood() -> None:
    current = _record(t=2, mood=5)
    rising = derive_behavior(current, TimingParams(), hour=14.0, previous=_record(t=1, mood=2))
    falling = derive_behavior(current, TimingParams(), hour=14.0, previous=_record(t=1, mood=8))

    assert rising.valence == falling.valence
    assert rising.momentum > 0.0
    assert falling.momentum < 0.0
    assert rising.expressiveness > falling.expressiveness


def test_hormonal_gain_changes_reactivity_subtly_not_base_warmth() -> None:
    previous = _record(t=0, mood=4)
    low_gain = derive_behavior(
        _record(t=1, mood=6, g=0.75, phase="menstrual"),
        TimingParams(),
        hour=14.0,
        previous=previous,
    )
    high_gain = derive_behavior(
        _record(t=1, mood=6, g=1.25, phase="ovulatory"),
        TimingParams(),
        hour=14.0,
        previous=previous,
    )

    assert high_gain.reactivity > low_gain.reactivity
    assert abs(high_gain.warmth - low_gain.warmth) < 0.08
    assert high_gain.expressiveness > low_gain.expressiveness


def test_prompt_brief_shows_state_without_exposing_numbers_or_hormones() -> None:
    directive = derive_behavior(
        _record(mood=3, g=1.2, eta=-0.7, phase="luteal_late"),
        TimingParams(),
        hour=20.0,
        previous=_record(t=-1, mood=5),
    )
    brief = directive.prompt_brief.lower()

    assert "hormon" not in brief
    assert "luteal" not in brief
    assert not any(character.isdigit() for character in brief)


    assert "show it through" not in brief
    assert brief.startswith("current bearing:")  # lowercased above





def _extreme_records() -> tuple[tuple[DayRecord, DayRecord], tuple[DayRecord, DayRecord]]:
    """Fixed extremes: low-energy night vs high-energy afternoon; deterministic."""
    low = _record(t=0, mood=1, phase="menstrual")
    low_previous = _record(t=-1, mood=8, phase="menstrual")
    high = _record(t=0, mood=10, phase="ovulatory")
    high_previous = _record(t=-1, mood=3, phase="ovulatory")
    return (low, low_previous), (high, high_previous)


def test_length_scale_spans_terse_to_expansive() -> None:
    """The length channel spans terse (night) to expansive (afternoon)."""
    (low, low_prev), (high, high_prev) = _extreme_records()
    low_dir = derive_behavior(low, TimingParams(), hour=2.0, previous=low_prev)
    high_dir = derive_behavior(high, TimingParams(), hour=14.0, previous=high_prev)

    assert low_dir.response_length_scale <= 0.40
    assert high_dir.response_length_scale >= 1.10
    assert low_dir.response_length_scale < high_dir.response_length_scale


def test_delay_channel_is_perceptible_inter_turn_latency() -> None:
    """Delay maps to perceptible inter-turn latency."""
    (low, low_prev), (high, high_prev) = _extreme_records()
    low_dir = derive_behavior(low, TimingParams(), hour=2.0, previous=low_prev)
    high_dir = derive_behavior(high, TimingParams(), hour=14.0, previous=high_prev)

    assert low_dir.response_delay_s >= 30.0
    assert high_dir.response_delay_s <= 10.0


def test_closing_tendency_reaches_low_and_high_targets() -> None:
    """A low-energy day settles (high closing tendency) and a high-energy day stays open."""
    (low, low_prev), (high, high_prev) = _extreme_records()
    low_dir = derive_behavior(low, TimingParams(), hour=2.0, previous=low_prev)
    high_dir = derive_behavior(high, TimingParams(), hour=14.0, previous=high_prev)

    assert low_dir.closing_tendency >= 0.80
    assert high_dir.closing_tendency <= 0.25

