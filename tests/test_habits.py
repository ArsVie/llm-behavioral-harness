"""Her days from her habits: kind and stage per interest, time of day and
weekly frequency drawn once and saved, a commitment the day fits around,
errands that are background, and progress from what happened."""

from __future__ import annotations

import dataclasses

from engine.rng import stream_rng
from harness import habits, life
from harness.channels.telegram import ControlCommand
from harness.clock import VirtualClock
from harness.commands import CommandContext, handle_command
from harness.domain import AgendaItem, DailyAgenda, Interest, PersonaProfile
from harness.negotiation_contract import is_skippable
from tests.helpers.store import make_session, make_store

SEED = 21

PERSONA = PersonaProfile(
    name="Lily", core="You are Lily.", routines=(),
    interests=(
        Interest("lifting", "exact", 0.9),
        Interest("history", "exact", 0.7),
        Interest("anime", "exact", 0.75),
        Interest("alternative music", "independent", 0.3),
        Interest("pottery", "adjacent", 0.6),
        Interest("poetry", "adjacent", 0.5),
    ),
)


def _agenda(day, store, plan=None, weekday="Tuesday"):
    return life.generate_agenda(
        day, PERSONA, [], store, stream_rng(SEED, life.LIFE_STREAM, day),
        plan=plan or habits.derive_plan(SEED, PERSONA), weekday=weekday,
    )


def test_kind_follows_the_interest():
    assert habits.kind_of("history") == habits.SKILL
    assert habits.kind_of("lifting") == habits.SKILL
    assert habits.kind_of("anime") == habits.TASTE
    assert habits.kind_of("alternative music") == habits.TASTE


def test_levels_are_spread():
    drawn = habits.draw_habits(SEED, PERSONA.interests)
    tops = [h for h in drawn.values()
            if h.stage == habits.STAGES[h.kind][-1]]
    assert [h.interest for h in tops] == ["lifting"]  # one master, the strongest
    seconds = [h for h in drawn.values()
               if h.stage == habits.STAGES[h.kind][-2]]
    assert 1 <= len(seconds) <= 2
    for h in drawn.values():
        assert h.part in habits.PARTS and 1 <= h.per_week <= 6


def test_the_plan_is_drawn_once_and_saved(tmp_path):
    store = make_store(tmp_path)
    first = habits.load_plan(store, SEED, PERSONA)
    changed = dict(first.habits)
    changed["lifting"] = habits.record_sessions(changed["lifting"], 5)
    habits.save_habits(store, changed)
    again = habits.load_plan(store, SEED + 1, PERSONA)  # a different seed draws nothing
    assert again.habits == changed
    assert again.commitment == first.commitment


def test_a_stage_rises_after_enough_sessions():
    habit = habits.Habit("poetry", habits.SKILL, "curious", "evening", 2)
    habit = habits.record_sessions(habit, 2)
    assert (habit.stage, habit.sessions) == ("curious", 2)
    habit = habits.record_sessions(habit, 1)
    assert (habit.stage, habit.sessions) == ("starting", 0)
    top = habits.Habit("lifting", habits.SKILL, "master", "evening", 5)
    assert habits.record_sessions(top, 500).stage == "master"


def test_durations_come_from_kind_stage_and_the_overrides():
    rng = stream_rng(SEED, 99)
    curious = habits.Habit("poetry", habits.SKILL, "curious", "evening", 2)
    practised = habits.Habit("lifting", habits.SKILL, "practised", "evening", 5)
    for _ in range(50):
        assert 10 / 60 <= habits.duration_h(curious, rng) <= 30 / 60
        assert 45 / 60 <= habits.duration_h(practised, rng) <= 90 / 60
    episodes = habits.Habit("anime", habits.TASTE, "fan", "late", 3)
    studio = habits.Habit("pottery", habits.SKILL, "starting", "afternoon", 2)
    for _ in range(50):
        minutes = round(habits.duration_h(episodes, rng) * 60)
        assert minutes % 24 == 0 and 48 <= minutes <= 72
        assert 2.0 <= habits.duration_h(studio, rng) <= 3.0


def test_the_commitment_comes_from_her_persona():
    student = habits.infer_commitment(SEED, "You are a student, 6 hours a day.")
    assert (student.kind, student.hours) == (habits.SCHOOL, 6.0)
    worker = habits.infer_commitment(SEED, "You work at a bakery.")
    assert (worker.kind, worker.hours) == (habits.JOB, 4.0)
    silent = habits.infer_commitment(SEED, "You are Lily.")
    assert silent.kind in (habits.JOB, habits.SCHOOL)  # drawn once, saved
    assert silent == habits.infer_commitment(SEED, "You are Lily.")


def test_a_day_fits_around_the_commitment(tmp_path):
    store = make_store(tmp_path)
    plan = habits.LifePlan(habits.derive_plan(SEED, PERSONA).habits,
                           habits.user_commitment(SEED, habits.JOB, 4.0, 10.0))
    for day in range(10):
        weekday = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                   "Saturday", "Sunday")[day % 7]
        agenda = _agenda(day, store, plan, weekday)
        work = [it for it in agenda.items if it.source_type == "commitment"]
        hobbies = [it for it in agenda.items if it.source_type in ("arc", "interest")]
        if day % 7 < 5:
            assert len(work) == 1
            assert work[0].start_t_h == day * 24 + 10.0
            assert len(hobbies) <= life.HOBBIES_ON_COMMITMENT_DAY
            if work[0].status == "skipped":
                assert work[0].outcome == "called in sick"
        else:
            assert work == []
            assert len(hobbies) <= life.HOBBIES_ON_FREE_DAY
        planned = [it for it in agenda.items if it.status == "planned"]
        for a, b in zip(planned, planned[1:]):
            assert a.end_t_h <= b.start_t_h + 1e-9
        for it in planned:
            local = it.start_t_h - day * 24
            assert life.AWAKE_START_H <= local and it.end_t_h - day * 24 <= life.AWAKE_END_H


def test_light_has_only_the_small_things(tmp_path):
    store = make_store(tmp_path)
    plan = habits.LifePlan(habits.derive_plan(SEED, PERSONA).habits, habits.LIGHT)
    kinds = set()
    for day in range(7):
        kinds |= {it.source_type for it in _agenda(day, store, plan).items}
    assert "commitment" not in kinds
    assert "meal" not in kinds


def test_errands_are_background_and_the_commitment_is_fixed():
    assert not is_skippable("commitment")


def test_errands_never_pop_up(tmp_path):
    store = make_store(tmp_path)
    store.log_event(0, 0.0, "popup_boundary_check", "booted earlier")
    store.save_agenda(0, DailyAgenda(0, (
        AgendaItem("m", 12.5, 13.0, "laundry", "errand", "laundry", 0.25, "planned"),
        AgendaItem("h", 13.0, 14.0, "lifting", "interest", "lifting", 0.9, "planned"),
    )))
    from harness.tools import DecisionConfig
    session = make_session(store, clock=VirtualClock(t_h=14.5),
                           decision_config=DecisionConfig())
    session._enqueue_event_popups(0, 14.5)
    queued = {s["payload"]["item_id"] for s in store.pending_steers()}
    assert queued == {"h"}


def test_arcs_and_stages_move_only_with_completed_sessions(tmp_path):
    store = make_store(tmp_path)
    plan = habits.load_plan(store, SEED, PERSONA)
    before = plan.habits["poetry"]
    agenda = DailyAgenda(0, (
        AgendaItem("a", 10.0, 11.0, "x", "interest", "poetry", 0.5, "planned"),
        AgendaItem("b", 12.0, 13.0, "y", "interest", "poetry", 0.5, "skipped"),
    ))
    store.save_agenda(0, agenda)
    life.step_life(0, PERSONA, [], agenda, store,
                   stream_rng(SEED, life.LIFE_STREAM, 0), plan=plan)
    after = habits.load_plan(store, SEED, PERSONA).habits["poetry"]
    assert habits.record_sessions(before, 1) == after


def test_the_planner_sees_what_came_of_her_sessions(tmp_path):
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (
        AgendaItem("a", 10.0, 11.0, "squats", "interest", "lifting", 0.9, "completed"),
        AgendaItem("b", 12.0, 13.0, "a poem", "interest", "poetry", 0.5, "skipped",
                   outcome="too tired"),
        AgendaItem("c", 13.0, 13.5, "laundry", "errand", "laundry", 0.25, "completed"),
    )))
    rows = store.recent_outcomes(before_day=1)
    assert [(r["activity"], r["status"], r["outcome"]) for r in rows] == [
        ("a poem", "skipped", "too tired"), ("squats", "completed", None)]


def _ctx(store, clock, **hooks):
    return CommandContext(store=store, clock=clock, persona_exists=True, **hooks)


def test_commitment_command_saves_the_setting(tmp_path):
    store = make_store(tmp_path)
    seen = []
    ctx = _ctx(store, VirtualClock(), request_commitment=lambda *a: seen.append(a) or "ok")
    assert handle_command(ControlCommand("commitment", "school 5 09:30", 1), ctx) == "ok"
    assert seen == [("user", "school", 5.0, 9.5)]
    handle_command(ControlCommand("commitment", "light", 1), ctx)
    assert seen[-1][0] == "light"
    assert handle_command(ControlCommand("commitment", "school 30", 1), ctx).startswith("hours")


def test_today_off_enters_her_stream(tmp_path):
    store = make_store(tmp_path)
    session = make_session(store, clock=VirtualClock(t_h=8.0))
    session.ensure_day(0)
    store.save_agenda(0, DailyAgenda(0, (
        AgendaItem("w", 10.0, 14.0, "work", "commitment", "job", 0.6, "planned"),
        AgendaItem("h", 15.0, 16.0, "lifting", "interest", "lifting", 0.9, "planned"),
    )))
    ctx = _ctx(store, session.clock,
               request_day_edit=lambda off, start: session.edit_today(off=off, start_h=start))
    assert handle_command(ControlCommand("today", "off", 1), ctx) == "work is off today"
    work = next(it for it in store.list_agenda_items(day=0) if it.id == "w")
    assert (work.status, work.outcome) == ("skipped", "day off")
    rows = [m for m in store.recent_messages() if m["role"] == "system"]
    assert rows and "no work today" in rows[-1]["content"]


def test_today_moved_makes_room(tmp_path):
    store = make_store(tmp_path)
    session = make_session(store, clock=VirtualClock(t_h=8.0))
    session.ensure_day(0)
    store.save_agenda(0, DailyAgenda(0, (
        AgendaItem("w", 10.0, 14.0, "work", "commitment", "job", 0.6, "planned"),
        AgendaItem("h", 15.0, 16.0, "lifting", "interest", "lifting", 0.9, "planned"),
    )))
    reply = session.edit_today(start_h=13.0)
    assert reply == "work moved to 13:00–17:00 today"
    items = {it.id: it for it in store.list_agenda_items(day=0)}
    assert (items["w"].start_t_h, items["w"].end_t_h) == (13.0, 17.0)
    assert (items["h"].status, items["h"].outcome) == ("skipped", "made way for work")


# live 2026-09-27: what the first fresh-boot day showed


def test_an_arc_not_yet_begun_is_invisible():
    from harness.domain import LifeArc
    later = LifeArc("a1", "deep dive into pottery", "pottery", 8, 0.8, "active",
                    "finish the current piece")
    begun = LifeArc("a2", "learning poetry", "poetry", 0, 0.1, "active",
                    "prepare the materials")
    assert life.started_arcs([later, begun], 0) == [begun]
    assert life.started_arcs([later, begun], 8) == [later, begun]


def test_a_routine_covers_its_interest_for_the_day(tmp_path):
    from harness.domain import Routine
    assert life.routine_covers("math practice", "mathematics")
    assert life.routine_covers("read history", "history")
    assert not life.routine_covers("morning coffee", "mathematics")
    persona = PersonaProfile(
        name="Lily", core="You are Lily.",
        routines=(Routine("read history", 0.9, 0.75, 1.0, 0.8),),
        interests=PERSONA.interests,
    )
    plan = habits.derive_plan(SEED, persona)
    plan = habits.LifePlan(
        {n: dataclasses.replace(h, per_week=6) for n, h in plan.habits.items()},
        habits.LIGHT)
    store = make_store(tmp_path)
    for day in range(14):
        agenda = life.generate_agenda(
            day, persona, [], store, stream_rng(SEED, life.LIFE_STREAM, day),
            plan=plan, weekday="Sunday")
        sources = [it.source_id for it in agenda.items]
        if "read history" in sources:
            assert "history" not in sources, sources


def test_a_line_a_little_over_the_ask_is_kept():
    import json

    from harness.day_planner import ASK_ACTIVITY_CHARS, parse_plan
    line = "reread the chapter on how long it took anyone to accept negative numbers and argue with the margins"
    assert len(line) > ASK_ACTIVITY_CHARS
    assert parse_plan(json.dumps({"activities": [line]}), 1) == [line]


def test_one_check_per_instant(tmp_path):
    store = make_store(tmp_path)
    store.log_event(0, 0.0, "popup_boundary_check", "booted earlier")
    from harness.tools import DecisionConfig
    session = make_session(store, clock=VirtualClock(t_h=10.0),
                           decision_config=DecisionConfig())
    session._enqueue_event_popups(0, 10.0)
    session._enqueue_event_popups(0, 10.0)
    checks = [e for e in store.events_since(0) if e["event"] == "popup_boundary_check"]
    assert len(checks) == 2  # the boot one and one at 10:00


def test_the_provider_session_id_survives_a_restart(tmp_path):
    from experiments.live_companion import run_session_id
    store = make_store(tmp_path)
    first = run_session_id(store)
    assert first.startswith("harness-")
    assert run_session_id(store) == first
