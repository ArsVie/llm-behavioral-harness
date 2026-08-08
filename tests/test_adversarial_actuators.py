"""A9 adversarial wave — ACTUATOR attack class (plan §9, cases A-1..A-6).

Attacks plan §9's claim: mechanical controls still work when the client
completely ignores prompt wording. A FakeClient that only records request
mechanics must observe different max_tokens / delay / continuation policy /
initiative-derived plans for different directives.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re

from engine.types import ADJ_SLOPE, MoodVariant, PersonaParams, TimingParams
from harness import actuation, domain
from harness.behavior import BehaviorDirective, BehaviorTrace
from harness.channels.base import FakeChannel, InboundMessage
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import (
    CompanionSnapshot,
    GenerationControls,
    MemoryContext,
    PersonaProfile,
)
from harness.interests import build_catalog
from harness.judge import ScriptedJudge
from harness.persona import build_persona
from harness.runtime import AsyncRuntime
from harness.scheduler import initiative_factor, plan_proactive_events
from harness.session import Session, TurnResult
from harness.store import SQLiteStore
from harness.assembler import assemble_snapshot

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345


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
        valence=valence, energy=energy, momentum=momentum,
        reactivity=reactivity, warmth=warmth, expressiveness=expressiveness,
        playfulness=playfulness, reflectiveness=reflectiveness,
        initiative=initiative, response_length_scale=response_length_scale,
        response_delay_s=response_delay_s, closing_tendency=closing_tendency,
        prompt_brief="brief",
        trace=BehaviorTrace("x", 1.0, 0.0, 0.0, 0.0),
    )


def _controls(directive) -> GenerationControls:
    return actuation.controls_from_directive(directive)


def _snapshot(profile: PersonaProfile, brief=None) -> CompanionSnapshot:
    return CompanionSnapshot(
        persona=profile,
        current_behavior=brief,
        current_activity=None,
        agenda=(),
        life_arcs=(),
        memory_context=MemoryContext((), (), (), None, ()),
        recent_conversation=(),
        proactive_intent=None,
    )


# --------------------------------------------------------------------------- #
# A-1: length actuator reaches the generation budget
# --------------------------------------------------------------------------- #


def test_a1_length_actuator_reaches_max_tokens_budget():
    """A-1: identical prompt wording, low (0.3) vs high (1.8)
    response_length_scale → FakeClient records measurably different
    max_tokens, both inside [96, 1500]."""
    low = _controls(_directive(response_length_scale=0.3))
    high = _controls(_directive(response_length_scale=1.8))
    assert low.max_tokens == 180 and high.max_tokens == 1080
    assert 96 <= low.max_tokens < high.max_tokens <= 1500

    client = FakeClient()
    prompt = "the exact same system prompt for both calls"
    client.chat([], system=prompt, max_tokens=low.max_tokens)
    client.chat([], system=prompt, max_tokens=high.max_tokens)
    recorded = [c["max_tokens"] for c in client.calls]
    assert recorded == [180, 1080], "budget did not reach the client"
    assert client.calls[0]["system"] == client.calls[1]["system"]


# --------------------------------------------------------------------------- #
# A-2: delay actuator reaches runtime delivery
# --------------------------------------------------------------------------- #


def test_a2_delay_reaches_runtime_before_send(tmp_path):
    """A-2: response_delay_s=7.0 flows through the runtime's delivery path —
    the injectable sleeper is called with ~7.0s BEFORE channel.send, and the
    runtime module contains no literal time.sleep."""
    import harness.runtime as rt_mod

    store = SQLiteStore(tmp_path / "a2.db")
    clock = VirtualClock()
    session = Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=FakeClient(responses=["ok!"]), clock=clock,
        judge=ScriptedJudge(0.5).judge_day,
    )
    directive = _directive(response_delay_s=7.0)
    result = TurnResult(
        reply="hello", directive=directive, day=0, hour=12.0,
        controls=GenerationControls(600, 7.0, 0.4, 1.0, "neutral"),
    )
    session.on_message = lambda text: result  # ignore prompt wording entirely

    trace: list[str] = []
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)
        trace.append("sleep")

    class TracingChannel(FakeChannel):
        async def send(self, message):
            trace.append("send")
            await super().send(message)

    channel = TracingChannel()
    runtime = AsyncRuntime(
        session, None, channel, store=store, timing=TIMING, seed=SEED, sleeper=sleeper,
    )
    asyncio.run(runtime._on_inbound(InboundMessage(text="hi")))
    assert sleeps == [7.0], f"sleeper got {sleeps}, expected [7.0]"
    assert trace == ["sleep", "send"], "delay must happen BEFORE delivery"
    assert channel.sent and channel.sent[0].text == "hello"
    # no literal time.sleep anywhere in the runtime module (the delay is data)
    src = inspect.getsource(rt_mod)
    assert "time.sleep" not in src
    store.close()


# --------------------------------------------------------------------------- #
# A-3: closing tendency is observable (not a dead number)
# --------------------------------------------------------------------------- #


def test_a3_closing_tendency_changes_observable_signal():
    """A-3: closing_tendency 0.1 vs 0.9 must change an observable mechanical
    signal — the continuation-policy guidance in the request — so the two
    directives produce measurably different requests."""
    low = _controls(_directive(closing_tendency=0.1))
    high = _controls(_directive(closing_tendency=0.9))
    assert low.closing_guidance != high.closing_guidance
    assert "invite continuation" in low.closing_guidance.lower() or "open" in low.closing_guidance.lower()
    assert "Do not force a follow-up question" in high.closing_guidance

    profile = build_persona(SEED, graph=build_catalog())
    p_low = assemble_snapshot(_snapshot(profile), controls=low)
    p_high = assemble_snapshot(_snapshot(profile), controls=high)
    assert p_low != p_high, "closing tendency changed nothing in the request"
    client = FakeClient()
    client.chat([], system=p_low, max_tokens=600)
    client.chat([], system=p_high, max_tokens=600)
    assert client.calls[0]["system"] != client.calls[1]["system"]


# --------------------------------------------------------------------------- #
# A-4: initiative reaches the scheduler
# --------------------------------------------------------------------------- #


def test_a4_initiative_reaches_scheduler_hazard():
    """A-4: same seed/persona/timing, initiative I=1.0 vs 0.0 (r_I =
    exp(beta(I-0.5))) — the planned-event count is measurably higher for high
    initiative, bounded, deterministic per seed; I=0.5 is neutral."""
    import numpy as np

    assert initiative_factor(0.5) == 1.0
    assert initiative_factor(1.0) > 1.0 > initiative_factor(0.0)

    def counts(initiative: float) -> int:
        scores = np.full(30, (initiative_factor(initiative) - 1.0) / ADJ_SLOPE)
        return len(plan_proactive_events(30, SEED, PERSONA, TIMING, scores=scores))

    high, mid, low = counts(1.0), counts(0.5), counts(0.0)
    assert high > mid > low, f"initiative direction broken: {high} {mid} {low}"
    # deterministic per seed
    assert counts(1.0) == high
    assert counts(0.0) == low


# --------------------------------------------------------------------------- #
# A-5: extreme directive values clamp safely
# --------------------------------------------------------------------------- #


def test_a5_extreme_values_clamp_safely():
    """A-5: response_length_scale 0.001/100, response_delay_s −1/1e6, NaN
    initiative: max_tokens clamped to [96, 1500], delay clamped to [0, 60],
    no negative/zero/NaN values ever reach the client or the sleeper."""
    tiny = _controls(_directive(response_length_scale=0.001))
    huge = _controls(_directive(response_length_scale=100.0))
    assert tiny.max_tokens == 96 and huge.max_tokens == 1500

    neg = _controls(_directive(response_delay_s=-1.0))
    far = _controls(_directive(response_delay_s=1e6))
    assert neg.response_delay_s == 0.0
    assert far.response_delay_s == 60.0

    nan_dir = _directive(initiative=float("nan"))
    nan_controls = _controls(nan_dir)
    assert math.isfinite(nan_controls.initiative_factor)
    assert 0.2 <= nan_controls.initiative_factor <= 5.0
    assert nan_controls.max_tokens > 0 and nan_controls.response_delay_s >= 0.0

    client = FakeClient()
    client.chat([], system="s", max_tokens=nan_controls.max_tokens)
    assert isinstance(client.calls[0]["max_tokens"], int)
    assert client.calls[0]["max_tokens"] > 0


# --------------------------------------------------------------------------- #
# A-6: actuators-off ablation is neutral and text-clean
# --------------------------------------------------------------------------- #


def test_a6_actuators_off_neutral_and_text_clean():
    """A-6: NO_ACTUATORS condition — neutral controls (scale=1.0, delay=0,
    closing neutral, initiative=0.5) stay valid (max_tokens in bounds);
    behavior-channel values never appear as literal strings in the prompt and
    raw engine/cycle state never reaches conversational context."""
    neutral = _directive(
        response_length_scale=1.0, response_delay_s=0.0,
        closing_tendency=0.5, initiative=0.5,
        energy=0.9, warmth=0.9, playfulness=0.9,  # channel values ≠ neutral
    )
    controls = _controls(neutral)
    assert controls.max_tokens == 600
    assert controls.response_delay_s == 0.0
    assert controls.initiative_factor == 1.0
    assert 96 <= controls.max_tokens <= 1500

    profile = build_persona(SEED, graph=build_catalog())
    brief = actuation.to_brief(neutral)
    prompt = assemble_snapshot(_snapshot(profile, brief=brief), controls=controls)
    forbidden = (
        "menstrual", "follicular", "ovulatory", "luteal", "phase_label",
        "cycle_day", "hormone", " mu", " eta", "phase:",
    )
    low = prompt.lower()
    for token in forbidden:
        assert token not in low, f"raw state leaked into prompt: {token!r}"
    assert not re.search(r"\b\d+\.\d+\b", prompt), (
        "numeric channel/state values leaked into the prompt"
    )
    # channel values only affect mechanics: same prompt content, same controls
    client = FakeClient()
    client.chat([], system=prompt, max_tokens=controls.max_tokens)
    assert client.calls[0]["max_tokens"] == 600
