"""The day planner: planned text replaces templates for arcs and interests, a day
follows on from recorded outcomes, and failure keeps the template day intact."""

from __future__ import annotations

import json

import pytest

from engine.rng import stream_rng
from harness import life
from harness.day_planner import (
    MAX_ACTIVITY_CHARS,
    PLANNER_BUDGET_S,
    PLANNER_REASONING_EFFORT,
    PlanSlot,
    build_request,
    parse_plan,
    plan_day,
)
from harness.domain import Interest, LifeArc, PersonaProfile, Routine

from tests.helpers.store import make_store

SEED = 8001


def _persona() -> PersonaProfile:
    return PersonaProfile(
        name="Lily",
        core="You are Lily.",
        interests=(
            Interest("mathematics", "exact", 0.9),
            Interest("anime", "exact", 0.8),
            Interest("pottery", "independent", 0.7),
            Interest("archaeology", "adjacent", 0.6),
        ),
        routines=(Routine("morning coffee", 0.29, 0.5, 1.0, 0.5),),
    )


def _arcs() -> list[LifeArc]:
    return [LifeArc(id="arc_1", name="learning pottery", interest="pottery",
                    started_day=0, progress=0.4, status="active",
                    next_intention="finish the current piece")]


class Planner:
    """A client that returns a well-formed plan, recording what it was sent."""

    def __init__(self, activities=None, reply=None):
        self.activities = activities or [
            "get through the chapter on the antonine plague",
            "trim the foot of the bowl that keeps cracking",
        ]
        self.reply = reply
        self.prompts: list[str] = []
        self.kwargs: dict = {}

    def chat_with_meta(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        self.kwargs = kw
        payload = self.reply
        if payload is None:
            payload = json.dumps({"activities": self.activities})

        class R:
            content = payload
        return R()


# -- parsing and validation ------------------------------------------------ #


def test_parse_accepts_the_documented_shape_and_a_bare_list():
    assert parse_plan('{"activities": ["a thing", "another"]}', 2) == [
        "a thing", "another"]
    assert parse_plan('["a thing"]', 1) == ["a thing"]
    assert parse_plan("here you go:\n[\"a thing\"]\nhope that helps", 1) == ["a thing"]


@pytest.mark.parametrize("reply", ["", "no json here", "{", "{}", "[]"])
def test_parse_rejects_junk(reply):
    assert parse_plan(reply, 2) in (None, [""] * 2)


def test_an_overlong_activity_is_dropped_not_truncated():
    """A paragraph must never reach the agenda."""
    long = "x" * (MAX_ACTIVITY_CHARS + 1)
    parsed = parse_plan(json.dumps({"activities": [long, "a real thing"]}), 2)
    assert parsed == ["", "a real thing"]


def test_trailing_period_and_whitespace_are_normalised():
    parsed = parse_plan(json.dumps({"activities": ["  a  thing.  "]}), 1)
    assert parsed == ["a thing"]


# -- the request carries what a plan needs --------------------------------- #


def test_request_labels_hers_versus_shared():
    """``Interest.bucket`` reaches the planner request as HERS/SHARED labels."""
    slots = [
        PlanSlot("interest", "pottery", "practice pottery",
                 "her interest in pottery (HERS)", bucket="independent"),
        PlanSlot("interest", "anime", "read about anime",
                 "her interest in anime (SHARED)", bucket="exact"),
    ]
    request = build_request("Lily", "Saturday", _arcs(), slots, ())
    assert "pottery (HERS)" in request
    assert "anime (SHARED)" in request
    assert "Saturday" in request


def test_request_carries_recorded_outcomes_for_continuity():
    slots = [PlanSlot("arc", "arc_1", "finish the current piece", "her project")]
    outcomes = [{"activity": "throw a bowl", "outcome": "the rim cracked again"}]
    request = build_request("Lily", "Tuesday", _arcs(), slots, outcomes)
    assert "throw a bowl → the rim cracked again" in request


def test_planner_call_uses_low_effort_and_json_mode():
    client = Planner()
    plan_day(name="Lily", weekday="Friday", arcs=_arcs(),
             slots=[PlanSlot("arc", "arc_1", "fallback", "her project")],
             client=client)
    assert client.kwargs["reasoning_effort"] == PLANNER_REASONING_EFFORT == "low"
    assert client.kwargs["json_mode"] is True
    assert client.kwargs["max_tokens"] is None


# -- failure is never fatal ------------------------------------------------ #


@pytest.mark.parametrize("client", [None, "not a client"])
def test_no_usable_client_keeps_the_templates(client):
    assert plan_day(name="Lily", weekday="Monday", arcs=(),
                    slots=[PlanSlot("arc", "a", "fallback", "x")],
                    client=client) is None


def test_a_hanging_planner_falls_back_within_the_budget():
    """The rollover must not stall on a slow provider."""
    import time

    class Hangs:
        def chat_with_meta(self, messages, **kw):
            time.sleep(30)

    started = time.monotonic()
    lines: list[str] = []
    result = plan_day(name="Lily", weekday="Monday", arcs=(),
                      slots=[PlanSlot("arc", "a", "fallback", "x")],
                      client=Hangs(), logger=lines.append, budget_s=0.5)
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 5.0, f"rollover blocked {elapsed:.1f}s on a hung provider"
    assert any("no reply within" in line for line in lines)


def test_the_budget_is_bounded():
    assert 60 <= PLANNER_BUDGET_S <= 120


# -- integration with the seeded agenda ------------------------------------ #


def _generate(store, *, client, day=0, persona=None, arcs=None):
    persona = persona or _persona()
    arcs = arcs if arcs is not None else _arcs()
    return life.generate_agenda(
        day, persona, arcs, store, stream_rng(SEED, life.LIFE_STREAM, day),
        planner_client=client, weekday="Saturday",
    )


def test_planned_text_replaces_templates_for_arcs_and_interests(tmp_path):
    store = make_store(tmp_path, "plan.db")
    try:
        agenda = _generate(store, client=Planner(activities=[
            "get through the chapter on the antonine plague",
            "trim the foot of the bowl that keeps cracking",
            "work through a chapter of the stats book",
        ]))
        planned = [i.activity for i in agenda.items
                   if i.source_type in ("arc", "interest")]
        assert planned, "no arc or interest items were generated"
        for text in planned:
            assert not text.startswith(("practice ", "read about ",
                                        "watch a video on ")), text
    finally:
        store.close()


def test_routines_are_never_planned(tmp_path):
    """Routines are never planned: "morning coffee" stays the same every day."""
    store = make_store(tmp_path, "routine.db")
    try:
        agenda = _generate(store, client=Planner(activities=["x"] * 6))
        routines = [i.activity for i in agenda.items if i.source_type == "routine"]
        assert routines == ["morning coffee"]
    finally:
        store.close()


def test_the_planner_never_perturbs_the_seeded_schedule(tmp_path):
    """Windows, ids, sources and salience must be identical with and without a
    planner — it supplies TEXT and nothing else, so replay is unaffected."""
    a = make_store(tmp_path, "a.db")
    b = make_store(tmp_path, "b.db")
    try:
        plain = _generate(a, client=None)
        planned = _generate(b, client=Planner(activities=["x", "y", "z", "w"]))
        def _schedule(agenda):
            return [
                (i.id, i.start_t_h, i.end_t_h, i.source_type, i.source_id,
                 i.salience, i.status)
                for i in agenda.items
            ]

        assert _schedule(plain) == _schedule(planned)
        # ... and the ORDER is the engine's, not a side effect of planning.
        assert [i.id for i in planned.items] == sorted(
            (i.id for i in planned.items),
            key=lambda x: [j.start_t_h for j in planned.items
                           if j.id == x][0],
        )
    finally:
        a.close()
        b.close()


def test_a_planner_failure_leaves_the_template_day_intact(tmp_path):
    class Boom:
        def chat_with_meta(self, messages, **kw):
            raise RuntimeError("provider down")

    a = make_store(tmp_path, "pa.db")
    b = make_store(tmp_path, "pb.db")
    try:
        plain = _generate(a, client=None)
        broken = _generate(b, client=Boom())
        assert [i.activity for i in plain.items] == [i.activity for i in broken.items]
    finally:
        a.close()
        b.close()


# -- the loop that makes it worth having ----------------------------------- #


def test_yesterdays_recorded_outcome_reaches_todays_plan(tmp_path):
    """A day follows on from what actually happened: yesterday's recorded outcome
    reaches today's plan."""
    store = make_store(tmp_path, "cont.db")
    try:
        day0 = _generate(store, client=Planner(activities=[
            "throw a wide bowl on the wheel", "reread the chapter on bayes",
        ]), day=0)
        target = next(i for i in day0.items if i.source_type in ("arc", "interest"))
        store.set_agenda_item_outcome(target.id, "the rim cracked again")

        client = Planner()
        _generate(store, client=client, day=1)

        assert client.prompts, "the planner was not called on day 1"
        request = client.prompts[-1]
        assert "the rim cracked again" in request, (
            "yesterday's outcome never reached today's plan"
        )
    finally:
        store.close()


def test_an_elapsed_window_alone_records_no_outcome(tmp_path):
    """An outcome must be something she decided or said — never a side effect of
    time passing."""
    store = make_store(tmp_path, "elapsed.db")
    try:
        agenda = _generate(store, client=None, day=0)
        item = agenda.items[0]
        store.update_agenda_item_status(item.id, "completed")
        assert store.recent_outcomes(before_day=1) == []
    finally:
        store.close()


# -- the interest bucket now reaches the day ------------------------------- #


def _interest_share(store, persona, name: str, days: int = 300) -> float:
    """How often ``name`` is drawn as a fraction of all interest items."""
    hits = total = 0
    for day in range(days):
        agenda = life.generate_agenda(
            day, persona, [], store, stream_rng(SEED, life.LIFE_STREAM, day),
        )
        for item in agenda.items:
            if item.source_type == "interest":
                total += 1
                hits += item.source_id == name
    return hits / total if total else 0.0


def test_the_bucket_weight_measurably_changes_what_she_does(tmp_path):
    """``BUCKET_WEIGHT`` must be observable in the DRAW: a low-salience independent
    interest is picked well above its salience-only rate when it applies."""
    persona = PersonaProfile(
        name="Lily", core="You are Lily.", routines=(),
        interests=(
            Interest("mathematics", "exact", 0.9),
            Interest("anime", "exact", 0.9),
            Interest("archaeology", "adjacent", 0.9),
            Interest("pottery", "independent", 0.30),
        ),
    )
    store = make_store(tmp_path, "bucket.db")
    try:
        share = _interest_share(store, persona, "pottery")
    finally:
        store.close()
    assert share > 0.15, (
        f"the independent interest surfaced {share:.1%} of the time — at or "
        "below the salience-only rate (12.5%), so the bucket is not reaching "
        "the draw"
    )


def test_bucket_weight_favours_the_independent_slice():
    assert life.BUCKET_WEIGHT["independent"] > life.BUCKET_WEIGHT["exact"]
    assert life.BUCKET_WEIGHT["exact"] == life.BUCKET_WEIGHT["adjacent"]
