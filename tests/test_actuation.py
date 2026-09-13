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

    # max_tokens is 600 * scale, rounded.
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


def test_closing_guidance_is_disabled_by_default() -> None:
    """Closing guidance is disabled: every tendency renders the empty string."""
    assert actuation.CLOSING_GUIDANCE_ENABLED is False
    for tendency in (0.05, 0.5, 0.9):
        controls = actuation.controls_from_directive(
            _directive(closing_tendency=tendency)
        )
        assert controls.closing_guidance == ""


def test_closing_bands_still_map_when_re_enabled(monkeypatch) -> None:
    """The band prose is retained verbatim for the day the channel returns."""
    monkeypatch.setattr(actuation, "CLOSING_GUIDANCE_ENABLED", True)
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
    # The factor is exp(beta * (I - 0.5)) with beta=2.0 and I=1.0.
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


# --------------------------------------------------------------------------- #


class _LengthProportionalClient:
    """Fake client whose reply length realizes the max_tokens budget
    (word count ~= budget // 8): the observed artifact of the budget."""

    supports_json: bool = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append({"max_tokens": max_tokens, "system": system})
        budget = max_tokens if max_tokens is not None else 96
        return " ".join(["reply"] * max(1, budget // 8))

    def close(self) -> None:
        return None


def _extreme_directive(*, low_energy: bool) -> BehaviorDirective:
    """Construct the low- and high-energy extremes directly."""
    from engine.types import DayRecord, TimingParams
    from harness.behavior import derive_behavior

    def rec(mood: int, phase: str, t: int) -> DayRecord:
        return DayRecord(
            t=t, m=0.0, g=1.0, arg=0.0, p=mood / 10, M=mood, score=0.0,
            mu=0.0, eta=0.0, cycle_day=float(t), phase_label=phase, seed=5001,
        )

    if low_energy:
        return derive_behavior(
            rec(1, "menstrual", 0), TimingParams(), hour=2.0,
            previous=rec(8, "menstrual", -1),
        )
    return derive_behavior(
        rec(10, "ovulatory", 0), TimingParams(), hour=14.0,
        previous=rec(3, "ovulatory", -1),
    )


def _realized_turns(tmp_path, directive: BehaviorDirective, *, n: int = 12) -> list[dict]:
    """Run n turns through the real session + fake client; the caller pins
    derive_behavior to `directive`."""
    from engine.types import MoodVariant, PersonaParams, TimingParams
    from harness.clock import VirtualClock
    from harness.judge import ScriptedJudge
    from harness.session import Session
    from harness.store import SQLiteStore

    store = SQLiteStore(tmp_path / f"s_{id(directive)}.db")
    clock = VirtualClock()
    client = _LengthProportionalClient()
    session = Session(
        store,
        persona=PersonaParams(),
        timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=5001,
        client=client,
        clock=clock,
        judge=ScriptedJudge(0.5).judge_day,
        feedback=False,
        synthetic_score=False,
    )
    rows: list[dict] = []
    clock.advance_hours(9.0)
    for i in range(n):
        result = session.on_message(f"turn {i}")
        assert result.controls is not None
        rows.append(
            {
                "max_tokens": result.controls.max_tokens,
                "delay": result.controls.response_delay_s,
                "closing": result.controls.closing_tendency,
                "reply_words": len(result.reply.split()),
            }
        )
        clock.advance_hours(1.0)
    store.close()
    return rows


def test_ab_extreme_states_separated_by_preregistered_margin(tmp_path, monkeypatch) -> None:
    """Extreme states separate the realized controls over 12 scripted turns."""
    import harness.session as session_mod

    monkeypatch.setattr(session_mod, "derive_behavior", lambda *a, **k: _extreme_directive(low_energy=True))
    lows = _realized_turns(tmp_path, _extreme_directive(low_energy=True))
    monkeypatch.setattr(session_mod, "derive_behavior", lambda *a, **k: _extreme_directive(low_energy=False))
    highs = _realized_turns(tmp_path, _extreme_directive(low_energy=False))

    def mean(rows: list[dict], key: str) -> float:
        return sum(r[key] for r in rows) / len(rows)

    low_tokens, high_tokens = mean(lows, "max_tokens"), mean(highs, "max_tokens")
    low_words, high_words = mean(lows, "reply_words"), mean(highs, "reply_words")
    low_delay, high_delay = mean(lows, "delay"), mean(highs, "delay")
    low_closing, high_closing = mean(lows, "closing"), mean(highs, "closing")

    assert high_tokens >= 2.5 * low_tokens, f"{high_tokens=} {low_tokens=}"
    assert high_words >= 2.5 * low_words, f"{high_words=} {low_words=}"
    assert low_delay >= 3.0 * high_delay, f"{low_delay=} {high_delay=}"
    assert low_closing >= 2.0 * high_closing, f"{low_closing=} {high_closing=}"


def test_30day_realized_ranges_cover_frozen_band(tmp_path) -> None:
    """Realized controls over a 30-day scripted run cover the frozen band."""
    from engine.types import MoodVariant, PersonaParams, TimingParams
    from harness.client import FakeClient
    from harness.clock import VirtualClock
    from harness.judge import ScriptedJudge
    from harness.session import Session
    from harness.store import SQLiteStore

    store = SQLiteStore(tmp_path / "s30.db")
    clock = VirtualClock()
    client = FakeClient(responses=["ok!"])
    session = Session(
        store,
        persona=PersonaParams(),
        timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=5001,
        client=client,
        clock=clock,
        judge=ScriptedJudge(0.5).judge_day,
        feedback=False,
        synthetic_score=False,
    )
    budgets: list[int] = []
    delays: list[float] = []
    closings: list[float] = []
    guidances: list[str] = []
    for day in range(30):
        for h in (9.0, 12.0, 15.0, 18.0, 21.0):
            clock.advance_hours(day * 24.0 + h - clock.now_h())
            result = session.on_message(f"day {day} hour {h}")
            assert result.controls is not None
            controls = result.controls
            budgets.append(controls.max_tokens)
            delays.append(controls.response_delay_s)
            closings.append(controls.closing_tendency)
            guidances.append(controls.closing_guidance)
    store.close()

    assert min(budgets) <= 350 and max(budgets) >= 630, f"{min(budgets)=} {max(budgets)=}"
    assert len({b for b in budgets}) >= 40, "degenerate clustering in max_tokens"
    assert min(delays) <= 8.0 and max(delays) >= 26.0, f"{min(delays)=} {max(delays)=}"
    assert min(closings) <= 0.25 and max(closings) >= 0.80, f"{min(closings)=} {max(closings)=}"
    # Closing guidance is disabled, so every turn renders "". The
    # band-to-prose mapping has its own test.

    assert set(guidances) == {""}


def test_closing_guidance_yields_at_least_four_distinct_strings(monkeypatch) -> None:
    """A sweep over the closing range yields >= 4 distinct guidance strings."""
    monkeypatch.setattr(actuation, "CLOSING_GUIDANCE_ENABLED", True)
    strings = {
        actuation.controls_from_directive(_directive(closing_tendency=t)).closing_guidance
        for t in (0.05, 0.15, 0.30, 0.50, 0.70, 0.90)
    }
    assert len(strings) >= 4
