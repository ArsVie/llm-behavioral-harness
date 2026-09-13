"""Aux calls fork the mainline request; only its bytes are shared."""

from __future__ import annotations

import json

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.day_planner import PlanSlot, plan_day
from harness.domain import AgendaItem, DailyAgenda
from harness.judge import RUBRIC, judge_day
from harness.session import Session
from harness.tools import DecisionConfig
from tests.helpers.store import make_store


def _session(tmp_path, client, *, t_h=10.0, agenda=True, anchored=True):
    store = make_store(tmp_path)
    if anchored:
        store.attach_anchor(RealTimeAnchor(epoch0_s=0.0, t_h0=0.0, tz="UTC"))
    if agenda:
        store.save_agenda(0, DailyAgenda(0, (
            AgendaItem("ag1", 9.0, 11.0, "pottery", "arc", "arc1", 0.8, "planned"),
        )))
    else:
        store.save_agenda(0, DailyAgenda(0, ()))
    session = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=4242,
        client=client, clock=VirtualClock(t_h=t_h),
        decision_config=DecisionConfig(),
    )
    return store, session


# -- the stamped-stream parity ----------------------------------------- #

def test_the_popup_leg_reuses_the_stamped_mainline_bytes(tmp_path):
    """An anchored run stamps user turns; both lanes share the same bytes up
    to the card, and the generation adds the round's decision pair as rows."""
    client = FakeClient(responses=[
        # The decide round draws the verdict...
        {"content": "", "tool_calls": [{"id": "c2", "name": "tool_decide_event",
         "arguments_json": "{\"initiate\": \"yes\", \"reason\": \"in the mood\"}"}]},
        # ...then the generation speaks.
        "main reply",
    ])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("hey")
        assert len(client.calls) == 2, "decide round + generation"
        main, popup = client.calls[1], client.calls[0]
        # The stamp is really on the wire (guards a silent un-stamp on both sides).
        assert any(
            (m.get("role") == "user" and " | Time: 10:00" in (m.get("content") or ""))
            for m in main["messages"]
        )
        assert main["system"] == popup["system"] and main["system"]
        m, p = main["messages"], popup["messages"]
        # Byte-identical head: the stamped user turn rides both lanes.
        head = len(p) - 1
        assert m[:head] == p[:head]
        # The generation then carries the round's decision pair as rows...
        assert [x["role"] for x in m[head:head + 2]] == ["assistant", "tool"]
        # ...and the tail extends: the card survives as the last block.
        assert m[-1]["role"] == p[-1]["role"] == "system"
        assert m[-1]["content"] in p[-1]["content"]
    finally:
        store.close()


def test_aux_task_request_extends_the_last_mainline_request(tmp_path):
    """The pair the judge/planner forks carry: same prefix, task folded in."""
    client = FakeClient(responses=["hi there"])
    store, session = _session(tmp_path, client, agenda=False)
    try:
        session.on_message("hey")
        assert len(client.calls) == 1, "empty agenda: the chat leg alone"
        main = client.calls[0]
        pair = session._aux_task_request("TASK: rate the day")
        assert pair is not None
        system, messages = pair
        assert system == main["system"]
        # The stream AS OF NOW extends the mainline request: everything the
        # mainline sent stays at the head; the card + task ride as one trailing block.
        head = main["messages"][:-1]
        assert messages[: len(head)] == head
        assert messages[len(head)]["role"] == "assistant"
        assert "hi there" in messages[len(head)]["content"]
        assert messages[-1]["role"] == "system"
        assert main["messages"][-1]["content"] in messages[-1]["content"]
        assert "TASK: rate the day" in messages[-1]["content"]
    finally:
        store.close()


def test_no_mainline_still_carries_the_base_prefix(tmp_path):
    """A call before any turn rides the SAME system prompt as the first turn."""
    client = FakeClient(responses=["hi there"])
    store, session = _session(tmp_path, client, agenda=False)
    try:
        pair = session._aux_task_request("TASK")
        assert pair is not None, "a pre-turn call still gets the base prefix"
        system, messages = pair
        assert messages == [{"role": "user", "content": "TASK"}]
        assert session._day_block is not None
        session.on_message("hey")
        first_turn = client.calls[0]
        assert first_turn["system"] == system, (
            "the boot pair and the first turn share one base prefix"
        )
    finally:
        store.close()


# -- the judge fork ---------------------------------------------------- #

def test_judge_fork_sends_the_pair_and_parses():
    client = FakeClient(responses=[json.dumps({"score": 0.5, "justification": "ok"})])
    seen = []

    def fork(rubric):
        seen.append(rubric)
        return "SYS", [
            {"role": "user", "content": "stream"},
            {"role": "system", "content": "card + task"},
        ]

    result = judge_day("", client, fork=fork)
    assert result.score == 0.5
    assert seen == [RUBRIC], "the judge owns its task text"
    call = client.calls[0]
    assert call["system"] == "SYS"
    assert call["messages"] == [
        {"role": "user", "content": "stream"},
        {"role": "system", "content": "card + task"},
    ]
    assert call["temperature"] == 0.0 and call["json_mode"] is True


def test_judge_without_a_fork_keeps_the_standalone_prompt():
    client = FakeClient(responses=[json.dumps({"score": 0.2, "justification": "x"})])
    result = judge_day("day-1: hi", client, fork=lambda rubric: None)
    assert result.score == 0.2
    call = client.calls[0]
    assert call["system"] == "You are a careful interaction judge. Score precisely."
    assert "Transcript:\nday-1: hi" in call["messages"][0]["content"]


def test_finalize_day_judges_on_the_fork_and_appends_nothing(tmp_path):
    """The judgement rides the mainline prefix; the conversation is untouched."""
    client = FakeClient(responses=[
        "hi there",
        {"content": json.dumps({"score": 0.3, "justification": "fine"}),
         "usage": {"prompt_tokens": 712, "completion_tokens": 40}},
    ])
    store, session = _session(tmp_path, client, agenda=False)
    try:
        session.on_message("hey")
        main = client.calls[0]
        before = len(store.messages_for_day(0))
        session.finalize_day(0)
        judge_call = client.calls[-1]
        assert judge_call["system"] == main["system"]
        # Prefix property: everything the mainline sent stays at the head
        # (the reply joined the stream after it), then the task rides last.
        head = main["messages"][:-1]
        assert judge_call["messages"][: len(head)] == head
        assert judge_call["messages"][-1]["role"] == "system"
        assert main["messages"][-1]["content"] in judge_call["messages"][-1]["content"]
        assert "Rate how the USER treated" in judge_call["messages"][-1]["content"]
        assert len(store.messages_for_day(0)) == before, (
            "the verdict is a judgement row, never a conversation message"
        )
        # The rich surface carries usage, so the ledger keeps the judge's tokens.
        row = store.conn.execute(
            "SELECT prompt_tokens, completion_tokens FROM llm_calls"
            " WHERE role = 'aux_judge'"
        ).fetchone()
        assert tuple(row) == (712, 40)
    finally:
        store.close()


# -- the planner fork -------------------------------------------------- #

def _slot():
    return PlanSlot("arc", "arc1", "finish the current piece",
                    "her project 'woodworking' (next: finish the current piece)")


def test_plan_day_uses_the_fork_pair_and_keeps_the_result_out():
    reply = json.dumps({"activities": ["carve a chess knight"]})
    client = FakeClient(responses=[reply])
    seen = {}

    def fork(task):
        seen["task"] = task
        return "SYS", [
            {"role": "user", "content": "stream"},
            {"role": "system", "content": "card + task"},
        ]

    planned = plan_day(name="Lily", weekday="Sunday", arcs=[], slots=[_slot()],
                       client=client, fork=fork)
    assert planned == ["carve a chess knight"]
    assert "planning her own day" in seen["task"], "the full task rides in"
    call = client.calls[0]
    assert call["system"] == "SYS"
    assert call["messages"] == [
        {"role": "user", "content": "stream"},
        {"role": "system", "content": "card + task"},
    ]
    assert call["json_mode"] is True and call["reasoning_effort"] == "low"
    assert call["temperature"] == 0.9


def test_plan_day_without_a_fork_keeps_the_standalone_prompt():
    client = FakeClient(responses=[json.dumps({"activities": ["x"]})])
    planned = plan_day(name="Lily", weekday="Sunday", arcs=[], slots=[_slot()],
                       client=client, fork=lambda task: None)
    assert planned == ["x"]
    call = client.calls[0]
    assert call["system"] == "You return JSON only."
    assert call["messages"][0]["role"] == "user"


def test_a_fork_that_cannot_build_falls_back_to_standalone():
    """A broken fork must not cost the day: templates + standalone call."""
    client = FakeClient(responses=[json.dumps({"activities": ["x"]})])

    def bad_fork(task):
        raise RuntimeError("boom")

    planned = plan_day(name="Lily", weekday="Sunday", arcs=[], slots=[_slot()],
                       client=client, fork=bad_fork)
    assert planned == ["x"]
    assert client.calls[0]["system"] == "You return JSON only."
