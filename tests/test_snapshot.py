"""Forbidden-token battery (Wave 2, A1): raw engine state never reaches the
assembled prompt.

For a battery of (seed, mood variant, day, local hour) combinations the
ACTUAL system prompt the client received is scanned for the forbidden
tokens — internal phase labels, hormone variables, mood parameters and
cycle-day indices must never appear in conversation-visible strings:

    phase_label, mu, eta, g, cycle_day, menstrual, follicular,
    ovulatory, luteal

The battery also verifies the prompt carries the persona core, a current
activity, active life arcs and today's agenda, and stays within the
assembler's character budget.

Note on `g`: checked as a standalone word (\\bg\\b) — the letter appears
inside ordinary words (dog, running, ...) which is user content, not state
leakage. All battery fixtures deliberately avoid words containing the other
tokens (e.g. "mu" inside "music") so the substring check is exact.
"""

import re

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import MAX_PROMPT_CHARS
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import Interest, PersonaProfile, Routine
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()

#: Substring check (lower-cased prompt). `g` is word-boundary only.
FORBIDDEN_SUBSTRINGS = (
    "phase_label",
    "cycle_day",
    "menstrual",
    "follicular",
    "ovulatory",
    "luteal",
    "mu",
    "eta",
)
FORBIDDEN_G_RE = re.compile(r"\bg\b")

#: Vocabulary used by the battery is deliberately free of the tokens
#: ("music", "must", "much", "beta", ... would trip the substring check).
BATTERY_MESSAGES = (
    "hello there",
    "My dog's name is Bruno.",
    "I like hiking on weekends.",
    "thanks for that",
    "see you later",
)

BATTERY_VARIANTS = (
    MoodVariant.DECOUPLED_OFFSETS,
    MoodVariant.DECOUPLED,
    MoodVariant.ORIGINAL,
)


def _profile() -> PersonaProfile:
    return PersonaProfile(
        name="Nova",
        core="You are Nova, a warm companion with an off-screen life of your own.",
        interests=(
            Interest("pottery", "exact", 0.9),
            Interest("photography", "exact", 0.7),
            Interest("running", "adjacent", 0.6),
            Interest("chess", "independent", 0.4),
        ),
        routines=(Routine("morning walk", 0.38, 0.5, 0.8, 0.3),),
    )


def _battery_session(tmp_path, seed: int, variant: MoodVariant):
    store = SQLiteStore(tmp_path / f"s{seed}.db")
    store.save_persona(_profile())
    clock = VirtualClock(t_h=0.0)
    client = FakeClient(responses=[f"reply {i}" for i in range(60)])
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=variant,
        seed=seed,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.4).judge_day,
    )
    return store, clock, client, session


def _check_prompt(prompt: str, *, seed: int, day: int) -> None:
    low = prompt.lower()
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in low, (
            f"forbidden token {token!r} leaked (seed={seed} day={day}): {prompt[:600]}"
        )
    assert not FORBIDDEN_G_RE.search(low), (
        f"standalone 'g' leaked (seed={seed} day={day}): {prompt[:600]}"
    )
    # Persona core + life content present and bounded.
    assert "Nova" in prompt
    assert "Current activity:" in prompt
    assert "Active life arcs:" in prompt
    assert "Today's agenda:" in prompt
    assert len(prompt) <= MAX_PROMPT_CHARS


def test_forbidden_tokens_never_reach_assembled_prompt(tmp_path):
    """Battery: 3 seeds (each with a different mood variant) × 10 days =
    30 assembled prompts spanning multiple phases (menstrual → follicular →
    ovulatory), hours, mood draws and life states."""
    phases_seen: set[str] = set()
    prompts_checked = 0
    for seed, variant in zip((11, 22, 33), BATTERY_VARIANTS):
        store, clock, client, session = _battery_session(tmp_path, seed, variant)
        for day in range(10):
            clock.advance_to_day(day)
            clock.advance_hours(15.0 + (day % 4))  # vary the local hour
            result = session.on_message(BATTERY_MESSAGES[day % len(BATTERY_MESSAGES)])
            phases_seen.add(result.directive.trace.phase_label)
            _check_prompt(client.calls[-1]["system"], seed=seed, day=day)
            prompts_checked += 1
        store.close()
    assert prompts_checked == 30
    # Sanity: the battery actually spans multiple engine phases (if this
    # ever fails, the seeds no longer cover the phase space — adjust).
    assert len(phases_seen) >= 2, f"battery collapsed onto one phase: {phases_seen}"


def test_forbidden_tokens_absent_after_finalize_and_resume(tmp_path):
    """Finalize + reopen (memory L2/L3/L4 + life steps active) still clean."""
    store = SQLiteStore(tmp_path / "s.db")
    store.save_persona(_profile())
    clock = VirtualClock(t_h=19.0)
    client = FakeClient(responses=["reply a", "reply b", "reply c"])
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=7,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.4).judge_day,
        feedback=True,
    )
    session.on_message("My dog's name is Bruno.")
    session.on_message("I like hiking on weekends.")
    clock.advance_to_day(2)  # finalizes day 0 and 1 (memory close + life step)
    session.ensure_day(2)
    session.on_message("hello again")
    _check_prompt(client.calls[-1]["system"], seed=7, day=2)
    store.close()

    # Reopen: resume must keep the assembled prompt clean.
    store2 = SQLiteStore(tmp_path / "s.db")
    client2 = FakeClient(responses=["reply d"])
    session2 = Session(
        store2,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=7,
        client=client2,
        clock=VirtualClock(t_h=67.0),
        judge=ScriptedJudge(score=0.4).judge_day,
        feedback=True,
    )
    session2.on_message("one more")
    _check_prompt(client2.calls[-1]["system"], seed=7, day=2)
    store2.close()


def test_grounded_proactive_prompt_is_clean(tmp_path):
    """A grounded proactive firing (store-backed intent) never leaks either."""
    store = SQLiteStore(tmp_path / "s.db")
    store.save_persona(_profile())
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=["proactive reply"])
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=7,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.4).judge_day,
    )
    from harness.domain import ProactiveIntent

    intent = ProactiveIntent(
        id="pi_battery",
        reason="schedule",
        source_type="agenda_item",
        source_id="ag_0_a_arc_1",
        hook="You just finished the pottery class scheduled this afternoon.",
        created_t_h=10.0,
        valid_until_t_h=13.0,
        salience=0.5,
        evidence="agenda_item:ag_0_a_arc_1",
    )
    store.save_proactive_intent(intent)
    session.fire_proactive("schedule")
    _check_prompt(client.calls[-1]["system"], seed=7, day=0)
    assert intent.hook in client.calls[-1]["system"]
    store.close()
