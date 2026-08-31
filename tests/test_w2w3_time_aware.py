"""W2+W3 — time-aware agenda + state-card sectioning (Track A-2).

Gate evidence:
- G3 (time correctness): the current-time/day line and the agenda
  partition match ``anchor.real_at`` at known t_h fixtures. Anchor:
  epoch0 = 2026-08-15T13:30:00Z, t_h0 = 7.5, tz America/Chihuahua →
  t_h 15.4 reads "It is 15:24, Saturday afternoon — day 0.".
- G5 (affect unchanged): the AFFECTIVE BEARING section carries the
  pre-wave renderer's output byte-identically — only its position/header
  changed.
- Section ordering: TEMPORAL FRAME / AFFECTIVE BEARING / BEHAVIORAL
  BEARING / CURRENT INTENT in fixed order.
- G2 (masking): unanchored runs omit the temporal section entirely (never
  fall back to raw t_h); the only numeric content in the assembled prompt
  is clock-shaped times (HH:MM) and the temporal line's day index.
- Agenda transitions: the session hook persists planned→completed as
  windows pass and the rendered partition agrees with the stored status.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor
from harness.assembler import (
    AFFECTIVE_HEADER,
    BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER,
    CURRENT_INTENT_PLACEHOLDER,
    MAX_PROMPT_CHARS,
    TEMPORAL_HEADER,
    assemble_snapshot,
)
from harness.behavior import _render_brief
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import (
    AgendaItem,
    BehaviorBrief,
    CompanionSnapshot,
    CurrentActivity,
    DailyAgenda,
    Interest,
    MemoryContext,
    PersonaProfile,
    Routine,
    Turn,
)
from harness.judge import ScriptedJudge
from harness.prompts import (
    ACTIVITY_HEADER,
    MOOD_BRIEF_HEADER,
    AVAILABILITY_HIGH,
    AVAILABILITY_LOW,
    AVAILABILITY_MID,
)
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()

#: Anchor fixture: 2026-08-15T13:30:00Z at t_h 7.5 in America/Chihuahua
#: (UTC-6 in August) → t_h 15.4 is 15:24 local, Saturday, virtual day 0.
G3_EPOCH0_S = datetime(2026, 8, 15, 13, 30, 0, tzinfo=timezone.utc).timestamp()


def _g3_anchor() -> RealTimeAnchor:
    return RealTimeAnchor(epoch0_s=G3_EPOCH0_S, t_h0=7.5, tz="America/Chihuahua")


def _persona() -> PersonaProfile:
    return PersonaProfile(
        name="Nova",
        core="You are Nova, a warm companion with an off-screen life of your own.",
        interests=(Interest("pottery", "exact", 0.9),),
        routines=(Routine("morning walk", 0.38, 0.5, 0.8, 0.3),),
    )


def _brief(
    initiative: float = 0.6,
    reactivity: float = 0.5,
    energy: float = 0.7,
    closing_tendency: float = 0.3,
) -> BehaviorBrief:
    return BehaviorBrief(
        valence=0.5, energy=energy, reactivity=reactivity, warmth=0.8,
        expressiveness=0.6, playfulness=0.5, reflectiveness=0.4,
        initiative=initiative, response_length_scale=1.0, response_delay_s=3.0,
        closing_tendency=closing_tendency,
    )


def _g3_agenda(day: int = 0) -> tuple[AgendaItem, ...]:
    """The G3 agenda fixture: morning coffee (06:58–07:46), a 15:00–16:00
    pottery slot, and an evening walk (20:00–21:00), all 'planned'."""
    base = day * 24.0
    return (
        AgendaItem("ag_coffee", base + 6 + 58 / 60, base + 7 + 46 / 60,
                   "morning coffee", "routine", "r1", 0.9, "planned"),
        AgendaItem("ag_pottery", base + 15.0, base + 16.0,
                   "pottery", "arc", "a1", 0.8, "planned"),
        AgendaItem("ag_walk", base + 20.0, base + 21.0,
                   "evening walk", "routine", "r2", 0.5, "planned"),
    )


def _snapshot(agenda=None, brief: BehaviorBrief | None = None) -> CompanionSnapshot:
    agenda = _g3_agenda() if agenda is None else agenda
    brief = _brief() if brief is None else brief
    return CompanionSnapshot(
        persona=_persona(),
        current_behavior=brief,
        current_activity=CurrentActivity(t_h=15.4, item=agenda[1],
                                         description="pottery"),
        agenda=agenda,
        life_arcs=(),
        memory_context=MemoryContext(
            recent_turns=(Turn("user", "hi", 9.0),),
            session_context=(),
            episodes=(),
            user_model=None,
            evidence_anchors=(),
        ),
        recent_conversation=(),
        proactive_intent=None,
    )


def _prose() -> str:
    return _render_brief(valence=0.5, energy=0.7, momentum=0.1,
                         warmth=0.8, playfulness=0.5, reflectiveness=0.4)


# Time correctness (current-time line + agenda partition)


def test_g3_temporal_line_and_partition_at_15_4():
    """t_h 15.4 under the G3 anchor → 'It is 15:24, Saturday afternoon —
    day 0.' and the partition: coffee done earlier, pottery happening now,
    evening walk later today."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                               t_h=15.4, anchor=_g3_anchor())
    assert TEMPORAL_HEADER in prompt
    assert "It is 15:24, Saturday afternoon — day 0." in prompt
# partition groups in order, past items kept and labeled
    assert "Done earlier:\n- morning coffee (06:58–07:46)" in prompt
    assert "Happening now:\n- pottery (15:00–16:00)" in prompt
    assert "Later today:\n- evening walk (20:00–21:00)" in prompt
    # group order fixed: done < now < later
    assert prompt.index("Done earlier:") < prompt.index("Happening now:") < \
        prompt.index("Later today:")


def test_g3_before_window_coffee_is_later_today():
    """At t_h before 06:58 the coffee item is 'Later today' (nothing done)."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                               t_h=6.0, anchor=_g3_anchor())
    assert "It is 06:00, Saturday morning — day 0." in prompt
    assert "Done earlier:" not in prompt
    assert "Happening now:" not in prompt
    assert "Later today:\n- morning coffee (06:58–07:46)" in prompt
    assert "- pottery (15:00–16:00)" in prompt
    assert "- evening walk (20:00–21:00)" in prompt


def test_g3_weekday_period_and_day_index_from_real_date():
    """The weekday and day period come from the REAL date, the day index is
    the virtual day: t_h 20.0 is 20:00 Saturday evening; t_h 25.5 has rolled
    to Sunday (real date) on virtual day 1."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                               t_h=20.0, anchor=_g3_anchor())
    assert "It is 20:00, Saturday evening — day 0." in prompt
    prompt2 = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                                t_h=25.5, anchor=_g3_anchor())
    assert "It is 01:30, Sunday night — day 1." in prompt2


def test_g3_partition_honors_status_buckets():
    """completed/skipped items are 'Done earlier' even before their window
    time; shifted items are 'Later today' (moved, not done)."""
    items = (
        dataclasses.replace(_g3_agenda()[0], status="completed"),
        dataclasses.replace(_g3_agenda()[2], status="shifted"),
    )
    prompt = assemble_snapshot(_snapshot(agenda=items), prompt_brief=_prose(),
                               t_h=6.0, anchor=_g3_anchor())
    assert "Done earlier:\n- morning coffee (06:58–07:46)" in prompt
    assert "Later today:\n- evening walk (20:00–21:00)" in prompt


# Unanchored runs omit the temporal section entirely


def test_unanchored_omits_temporal_section():
    """Replay / unanchored runs render NO temporal line and NO partition —
    the section is omitted entirely; t_h is never rendered raw."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose())
    assert TEMPORAL_HEADER not in prompt
    assert "It is " not in prompt
    for label in ("Done earlier", "Happening now", "Later today"):
        assert label not in prompt
    # partial args are equally unanchored
    p1 = assemble_snapshot(_snapshot(), prompt_brief=_prose(), t_h=15.4)
    p2 = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                           anchor=_g3_anchor())
    assert TEMPORAL_HEADER not in p1
    assert TEMPORAL_HEADER not in p2


# Section ordering


def test_state_card_sections_in_fixed_order():
    """Headers appear in order TEMPORAL FRAME < AFFECTIVE BEARING <
    BEHAVIORAL BEARING < CURRENT INTENT, and the reserved slot sits before
    the (unchanged) activity section."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                               t_h=15.4, anchor=_g3_anchor())
    order = [
        prompt.index(TEMPORAL_HEADER),
        prompt.index(AFFECTIVE_HEADER),
        prompt.index(BEHAVIORAL_HEADER),
        prompt.index(CURRENT_INTENT_HEADER),
    ]
    assert order == sorted(order)
    assert prompt.index(CURRENT_INTENT_HEADER) < prompt.index(ACTIVITY_HEADER)


def test_current_intent_reserved_slot():
    """CURRENT INTENT is a fixed placeholder slot (masking-clean, no
    numbers) that never carries the proactive hook — the hook stays in its
    own section."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose())
    assert f"{CURRENT_INTENT_HEADER}\n{CURRENT_INTENT_PLACEHOLDER}" in prompt
    assert "No active intent." in prompt
    assert not re.search(r"\d", CURRENT_INTENT_PLACEHOLDER)


# AFFECTIVE BEARING verbatim


def test_g5_affective_bearing_verbatim_across_bands():
    """Across a battery of channel bands, the AFFECTIVE section is the
    pre-wave renderer's output byte-identically: the mood line
    ('Current behavioral guidance: <prompt_brief>') and the availability
    line, contiguous under the new header — nothing re-rendered."""
    cases = (
        # (brief overrides, expected availability line)
        ({"energy": 0.8}, AVAILABILITY_HIGH),
        ({"energy": 0.2}, AVAILABILITY_LOW),
        ({"energy": 0.5}, AVAILABILITY_MID),
    )
    for overrides, availability in cases:
        brief = dataclasses.replace(_brief(), **overrides)
        prose = _render_brief(valence=0.5, energy=overrides["energy"],
                              momentum=0.1, warmth=0.8, playfulness=0.5,
                              reflectiveness=0.4)
        prompt = assemble_snapshot(_snapshot(brief=brief), prompt_brief=prose)
        mood_line = f"{MOOD_BRIEF_HEADER} {prose}"
# the lines are contiguous inside the section, verbatim
        assert mood_line in prompt
        assert availability in prompt
        assert f"{AFFECTIVE_HEADER}\n{mood_line}\n{availability}" in prompt
        assert "Current bearing:" in prompt


def test_g5_affective_wording_frozen_across_anchoring():
    """Anchoring adds the TEMPORAL section but never touches the AFFECTIVE
    wording (renderer-neutral promise)."""
    anchored = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                                 t_h=15.4, anchor=_g3_anchor())
    unanchored = assemble_snapshot(_snapshot(), prompt_brief=_prose())
    a = anchored[anchored.index(AFFECTIVE_HEADER):]
    u = unanchored[unanchored.index(AFFECTIVE_HEADER):]
    assert a == u


# BEHAVIORAL BEARING


def test_behavioral_bearing_prose_bands():
    """Initiative/reactivity/persistence render as band prose — never raw
    floats; persistence maps to 1 - closing_tendency."""
    high = _brief(initiative=0.9, reactivity=0.9, closing_tendency=0.05)
    prompt = assemble_snapshot(_snapshot(brief=high), prompt_brief=_prose())
    assert "You tend to reach out first and carry the conversation forward." in prompt
    assert "You respond quickly and pick up on what is said." in prompt
    assert "You tend to stay in the conversation and see it through." in prompt

    low = _brief(initiative=0.1, reactivity=0.1, closing_tendency=0.9)
    prompt = assemble_snapshot(_snapshot(brief=low), prompt_brief=_prose())
    assert "You mostly follow the user's lead, letting them set the pace." in prompt
    assert "You respond at your own pace, unhurried." in prompt
    assert "Your participation tends to wind down quickly." in prompt

    # mid band (defaults)
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose())
    assert "You reach out when something matters" in prompt
    assert "You respond readily to what is said." in prompt
    assert "You stay in the conversation while it keeps meaning something." in prompt

    for p in (prompt,):
        assert not re.search(r"\d+\.\d+", p), "raw channel floats leaked"


def test_behavioral_bearing_absent_without_brief():
    prompt = assemble_snapshot(
        dataclasses.replace(_snapshot(), current_behavior=None)
    )
    assert BEHAVIORAL_HEADER not in prompt


# Numeric-content scan on real assembled prompts

CLOCK_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
DAY_INDEX_RE = re.compile(r"day \d+")
FLOAT_RE = re.compile(r"\d+\.\d+")


def _numeric_leak(prompt: str) -> list[str]:
    """Digits that are neither clock-shaped times (HH:MM — the temporal
    line's 15:24, the agenda's 06:58) nor the temporal line's day index."""
    masked = CLOCK_TIME_RE.sub("T:T", prompt)
    masked = DAY_INDEX_RE.sub("day N", masked)
    return re.findall(r"\d+", masked)


def test_anchored_prompt_numeric_content_is_only_clock_and_day():
    """The temporal line's times and the agenda's clock times are the ONLY
    numeric content allowed in an anchored assembled prompt (G2)."""
    prompt = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                               t_h=15.4, anchor=_g3_anchor())
    assert not FLOAT_RE.search(prompt), "raw engine floats leaked"
    assert _numeric_leak(prompt) == [], f"unexpected numeric content: {_numeric_leak(prompt)}"


# Replay determinism


def test_replay_determinism_same_snapshot_same_bytes():
    """Same snapshot + same anchor/t_h → byte-identical prompts (anchored
    and unanchored), across repeated calls."""
    a1 = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                           t_h=15.4, anchor=_g3_anchor())
    a2 = assemble_snapshot(_snapshot(), prompt_brief=_prose(),
                           t_h=15.4, anchor=_g3_anchor())
    assert a1 == a2
    u1 = assemble_snapshot(_snapshot(), prompt_brief=_prose())
    u2 = assemble_snapshot(_snapshot(), prompt_brief=_prose())
    assert u1 == u2
    assert len(a1) <= MAX_PROMPT_CHARS and len(u1) <= MAX_PROMPT_CHARS


# Session integration: transitions persist and the render agrees


def _session(store, clock, responses=("ok", "ok", "ok")):
    client = FakeClient(responses=list(responses))
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
    return session, client


def test_session_transition_persisted_and_render_agrees(tmp_path):
    """A turn at t_h 15.4 (anchored): the coffee window has passed → the
    store records 'completed' and the state card shows it under 'Done
    earlier'; pottery is in progress and the walk is later."""
    store = SQLiteStore(tmp_path / "s.db")
    store.save_persona(_persona())
    store.attach_anchor(_g3_anchor())
    clock = VirtualClock(t_h=0.0)
    session, client = _session(store, clock)
    session.ensure_day(0)
    store.save_agenda(0, DailyAgenda(0, _g3_agenda()))
    clock.advance_hours(15.4)
    session.on_message("hello there")

    # persisted status: planned -> completed for the passed window only
    stored = store.load_agenda(0)
    assert stored is not None
    by_id = {it.id: it.status for it in stored.items}
    assert by_id == {"ag_coffee": "completed",
                     "ag_pottery": "planned",
                     "ag_walk": "planned"}

    system = client.calls[-1]["system"]
    assert "It is 15:24, Saturday afternoon — day 0." in system
    assert "Done earlier:\n- morning coffee (06:58–07:46)" in system
    assert "Happening now:\n- pottery (15:00–16:00)" in system
    assert "Later today:\n- evening walk (20:00–21:00)" in system
    assert not FLOAT_RE.search(system), "raw floats leaked into the live prompt"
    store.close()


def test_session_before_window_stays_planned_and_renders_later(tmp_path):
    """A turn at t_h 06:00: nothing has passed — coffee stays 'planned' in
    the store and renders under 'Later today'."""
    store = SQLiteStore(tmp_path / "s.db")
    store.save_persona(_persona())
    store.attach_anchor(_g3_anchor())
    clock = VirtualClock(t_h=0.0)
    session, client = _session(store, clock)
    session.ensure_day(0)
    store.save_agenda(0, DailyAgenda(0, _g3_agenda()))
    clock.advance_hours(6.0)
    session.on_message("hello there")

    stored = store.load_agenda(0)
    assert stored is not None
    assert {it.id: it.status for it in stored.items} == {
        "ag_coffee": "planned", "ag_pottery": "planned", "ag_walk": "planned",
    }
    system = client.calls[-1]["system"]
    assert "It is 06:00, Saturday morning — day 0." in system
    assert "Later today:\n- morning coffee (06:58–07:46)" in system
    assert "Done earlier:" not in system
    store.close()


def test_session_unanchored_prompt_has_no_temporal_section(tmp_path):
    """The live unanchored path (no anchor attached) never renders the
    temporal section — replay parity of the prompt shape."""
    store = SQLiteStore(tmp_path / "s.db")
    store.save_persona(_persona())
    clock = VirtualClock(t_h=15.4)
    session, client = _session(store, clock)
    session.on_message("hello there")
    system = client.calls[-1]["system"]
    assert TEMPORAL_HEADER not in system
    assert "It is " not in system
    assert "Done earlier" not in system
    # the sectioned card (affective/behavioral/intent) still renders
    assert AFFECTIVE_HEADER in system
    assert BEHAVIORAL_HEADER in system
    assert CURRENT_INTENT_HEADER in system
    store.close()
