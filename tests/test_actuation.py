"""Actuation tests (vertical slice A3): directive -> mechanical controls."""

from __future__ import annotations

import inspect

import pytest

from harness import actuation, domain
from harness.behavior import BehaviorDirective, BehaviorTrace


def _directive(
    *,
    valence: float = 0.0,
    energy: float = 0.5,
    momentum: float = 0.0,
    reactivity: float = 0.5,
    warmth: float = 0.6,
    expressiveness: float = 0.5,
    playfulness: float = 0.4,
    reflectiveness: float = 0.5,
    initiative: float = 0.5,
    response_length_scale: float = 1.0,
    response_delay_s: float = 3.0,
    closing_tendency: float = 0.4,
) -> BehaviorDirective:
    return BehaviorDirective(
        valence=valence,
        energy=energy,
        momentum=momentum,
        reactivity=reactivity,
        warmth=warmth,
        expressiveness=expressiveness,
        playfulness=playfulness,
        reflectiveness=reflectiveness,
        initiative=initiative,
        response_length_scale=response_length_scale,
        response_delay_s=response_delay_s,
        closing_tendency=closing_tendency,
        prompt_brief="brief",
        trace=BehaviorTrace(
            phase_label="x",
            hormonal_gain=1.0,
            event_memory=0.0,
            endogenous_tone=0.0,
            mood_delta=0.0,
        ),
    )


def test_low_directive_smaller_budget_than_high_directive() -> None:
    low = _directive(response_length_scale=0.68)
    high = _directive(response_length_scale=1.18)

    low_controls = actuation.controls_from_directive(low)
    high_controls = actuation.controls_from_directive(high)

    # Deterministic: 600 * scale, rounded.
    assert low_controls.max_tokens == 408
    assert high_controls.max_tokens == 708
    assert low_controls.max_tokens < high_controls.max_tokens


def test_max_tokens_budget_is_clamped_to_bounds() -> None:
    tiny = actuation.controls_from_directive(
        _directive(response_length_scale=0.01), min_tokens=96, max_tokens=1500
    )
    huge = actuation.controls_from_directive(
        _directive(response_length_scale=5.0), min_tokens=96, max_tokens=1500
    )

    assert tiny.max_tokens == 96
    assert huge.max_tokens == 1500


def test_to_brief_carries_all_channels_including_control_fields() -> None:
    directive = _directive(
        valence=-0.3,
        energy=0.8,
        reactivity=0.9,
        warmth=0.4,
        expressiveness=0.7,
        playfulness=0.2,
        reflectiveness=0.6,
        initiative=0.9,
        response_length_scale=1.1,
        response_delay_s=2.0,
        closing_tendency=0.2,
    )

    brief = actuation.to_brief(directive)

    assert isinstance(brief, domain.BehaviorBrief)
    assert brief.valence == directive.valence
    assert brief.energy == directive.energy
    assert brief.reactivity == directive.reactivity
    assert brief.warmth == directive.warmth
    assert brief.expressiveness == directive.expressiveness
    assert brief.playfulness == directive.playfulness
    assert brief.reflectiveness == directive.reflectiveness
    assert brief.initiative == directive.initiative
    assert brief.response_length_scale == directive.response_length_scale
    assert brief.response_delay_s == directive.response_delay_s
    assert brief.closing_tendency == directive.closing_tendency


def test_response_delay_is_clamped_and_closing_tendency_passes_through() -> None:
    slow = actuation.controls_from_directive(_directive(response_delay_s=120.0, closing_tendency=0.77))
    instant = actuation.controls_from_directive(_directive(response_delay_s=-5.0))

    assert slow.response_delay_s == 60.0
    assert instant.response_delay_s == 0.0
    assert slow.closing_tendency == 0.77


def test_closing_guidance_differs_by_closing_tendency() -> None:
    low = actuation.controls_from_directive(_directive(closing_tendency=0.1))
    mid = actuation.controls_from_directive(_directive(closing_tendency=0.5))
    high = actuation.controls_from_directive(_directive(closing_tendency=0.9))

    assert low.closing_guidance != mid.closing_guidance != high.closing_guidance
    assert "invite continuation" in low.closing_guidance.lower()
    assert "do not force" in high.closing_guidance.lower()


def test_initiative_factor_bounds_and_monotonicity() -> None:
    factors = [
        actuation.controls_from_directive(_directive(initiative=i)).initiative_factor
        for i in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]

    assert all(0.2 <= factor <= 5.0 for factor in factors)
    assert factors == sorted(factors)
    assert factors[0] < 1.0 < factors[-1]
    # exp(beta * (I - 0.5)) at beta=2.0, I=1.0 -> e^1 ~ 2.718.
    assert factors[-1] == pytest.approx(2.718281828, abs=1e-6)


def test_initiative_factor_clamps_at_extreme_beta() -> None:
    zero = actuation.controls_from_directive(_directive(initiative=0.0), beta=10.0)
    one = actuation.controls_from_directive(_directive(initiative=1.0), beta=10.0)

    assert zero.initiative_factor == 0.2
    assert one.initiative_factor == 5.0


def test_module_never_blocks_on_latency() -> None:
    source = inspect.getsource(actuation)

    assert "time.sleep" not in source
    assert "import time" not in source
    assert "asyncio.sleep" not in source
