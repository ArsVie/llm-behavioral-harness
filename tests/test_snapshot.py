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
from harness.domain import AgendaItem, DailyAgenda, Interest, PersonaProfile, Routine
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

#: Allowed numeric content: clock-shaped times and the temporal day index.
FORBIDDEN_FLOAT_RE = re.compile(r"\d+\.\d+")
CLOCK_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
DAY_INDEX_RE = re.compile(r"day \d+")


def numeric_leak(prompt: str) -> list[str]:
    """Digits remaining after masking clock times and the temporal line's
    day index — the only numeric content allowed (G2)."""
    masked = CLOCK_TIME_RE.sub("T:T", prompt)
    masked = DAY_INDEX_RE.sub("day N", masked)
    return re.findall(r"\d+", masked)

#: Battery messages avoid words containing the forbidden tokens.
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


def _ensure_activity_at(store, session, day: int, t_h: float) -> None:
    """Guarantee an agenda item IN PROGRESS at ``t_h``.

    The battery's structural assertions require ``Current activity:`` in
    the prompt. Under NOW semantics (WS1, design §2.1) an activity is only
    current when an item is genuinely in progress at that hour — the old
    highest-salience fallback is gone, so a 15:00 message may have no
    activity. The life-generated agenda is kept and a guaranteed
    in-progress item (highest salience) is merged in; the forbidden-token
    scan still covers the real generated agenda and all other sections.
    """
    session.ensure_day(day)
    agenda = store.load_agenda(day)
    items = agenda.items if agenda is not None else ()
    extra = AgendaItem(
        f"batt_{day}_{t_h:.0f}",
        t_h - 0.5,
        t_h + 0.5,
        "battery walk",
        "interest",
        "batt",
        0.9,
        "planned",
    )
    if extra.id in {it.id for it in items}:
        return  # already merged (resume: the item survived in the store)
    store.save_agenda(day, DailyAgenda(day, tuple(items) + (extra,)))


def _check_prompt(prompt: str, *, seed: int, day: int) -> None:
    low = prompt.lower()
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in low, (
            f"forbidden token {token!r} leaked (seed={seed} day={day}): {prompt[:600]}"
        )
    assert not FORBIDDEN_G_RE.search(low), (
        f"standalone 'g' leaked (seed={seed} day={day}): {prompt[:600]}"
    )
    # Only clock times and the temporal day index are numeric content.
    assert not FORBIDDEN_FLOAT_RE.search(prompt), (
        f"raw engine float leaked (seed={seed} day={day}): {prompt[:600]}"
    )
    leaked = numeric_leak(prompt)
    assert not leaked, (
        f"unexpected numeric content {leaked!r} (seed={seed} day={day}): {prompt[:600]}"
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
            _ensure_activity_at(store, session, day, clock.now_h())
            result = session.on_message(BATTERY_MESSAGES[day % len(BATTERY_MESSAGES)])
            phases_seen.add(result.directive.trace.phase_label)
            _check_prompt(client.calls[-1]["system"], seed=seed, day=day)
            prompts_checked += 1
        store.close()
    assert prompts_checked == 30
    # The battery spans multiple engine phases.
    assert len(phases_seen) >= 2, f"battery collapsed onto one phase: {phases_seen}"


TEMPORAL_LINE_RE = re.compile(
    r"It is \d{2}:\d{2}, [A-Z][a-z]+ (morning|afternoon|evening|night) — day \d+\."
)


def test_time_aware_anchored_battery_clean(tmp_path):
    """G2/G3 on REAL anchored assembled prompts: with the G3 anchor attached
    (epoch0 2026-08-15T13:30:00Z, tz America/Chihuahua), 3 days × turns at
    ~15:00 the temporal section renders (line + partition), the line reads
    the right weekday for the REAL date, and the numeric scan stays clean —
    the temporal line's times and the agenda's clock times are the ONLY
    numeric content allowed."""
    from datetime import datetime, timezone

    from harness.anchor import RealTimeAnchor

    anchor = RealTimeAnchor(
        epoch0_s=datetime(2026, 8, 15, 13, 30, 0, tzinfo=timezone.utc).timestamp(),
        t_h0=7.5,
        tz="America/Chihuahua",
    )
    weekdays = ["Saturday", "Sunday", "Monday"]
    for day in range(3):
        store, clock, client, session = _battery_session(tmp_path, 44, MoodVariant.ORIGINAL)
        store.attach_anchor(anchor)
        clock.advance_to_day(day)
        clock.advance_hours(15.0)
        _ensure_activity_at(store, session, day, clock.now_h())
        session.on_message("hello there")
        prompt = client.calls[-1]["system"]
        # G3: temporal line present, correct weekday + day index
        assert f"It is 15:00, {weekdays[day]} afternoon — day {day}." in prompt, (
            f"temporal line wrong (day={day}): {prompt[:600]}"
        )
        # partition labels present (life agenda spans the day)
        assert "Done earlier:" in prompt or "Happening now:" in prompt
        assert "Later today:" in prompt
        # The numeric scan holds on the anchored prompts too.
        _check_prompt(prompt, seed=44, day=day)
        store.close()
        assert TEMPORAL_LINE_RE.search(prompt)


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
    _ensure_activity_at(store, session, 2, clock.now_h())
    session.on_message("hello again")
    _check_prompt(client.calls[-1]["system"], seed=7, day=2)
    store.close()

    # Reopen: the assembled prompt stays clean.
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
    _ensure_activity_at(store2, session2, 2, 67.0)
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
    _ensure_activity_at(store, session, 0, 10.0)
    session.fire_proactive("schedule")
    _check_prompt(client.calls[-1]["system"], seed=7, day=0)
    assert intent.hook in client.calls[-1]["system"]
    store.close()
