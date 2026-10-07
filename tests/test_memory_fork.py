"""The end-of-day memory fork: only what he said, quoted, becomes memory."""

from __future__ import annotations

import json

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.memory_fork import day_view, parse, task_for
from harness.session import Session
from harness.store import SQLiteStore

HIS = [
    {"id": 4, "content": "I'm an engineer at a survey company, long days lately."},
    {"id": 7, "content": "My sister Ana is visiting next week, remind me to clean."},
]


def _reply(*items):
    return json.dumps({"items": list(items)})


def test_an_item_without_his_words_is_dropped():
    raw = _reply(
        {"kind": "fact", "key": "Job", "fact": "He is an engineer at a survey company.",
         "quote": "I'm an engineer at a survey company", "weight": 0.8},
        {"kind": "fact", "key": "pet", "fact": "He has a cat.",
         "quote": "I have a cat", "weight": 0.9},
        {"kind": "reminder", "key": "clean", "fact": "Remind him to clean.",
         "quote": "remind me to clean", "weight": 0.6},
    )
    got = parse(raw, HIS)
    assert [(f.key, f.turn_id) for f in got.facts] == [("user:job", 4)]
    assert [(c[1], c[0]) for c in got.callbacks] == [(7, "remind me to clean")]
    assert got.dropped == 1
    assert got.weights == {4: 0.8, 7: 0.6}


def test_the_quote_check_forgives_curly_quotes_and_spacing():
    raw = _reply({"kind": "fact", "key": "job", "fact": "He works long days lately.",
                  "quote": "I’m an engineer  at a survey company", "weight": 1})
    assert len(parse(raw, HIS).facts) == 1


def test_garbage_yields_nothing():
    assert parse("sorry, no", HIS).facts == ()
    assert parse('{"items": "none"}', HIS).facts == ()


def test_the_task_lists_what_she_already_knows():
    text = task_for([("user:job", "He is an engineer.")])
    assert "- user:job: He is an engineer." in text


def _session(path, client):
    return Session(
        SQLiteStore(path), persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=5, client=client,
        clock=VirtualClock(t_h=10.0), judge=ScriptedJudge(0.5).judge_day,
    )


def test_the_day_close_writes_his_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MEMORY_FORK", "1")
    fork_reply = _reply({
        "kind": "fact", "key": "job", "fact": "He is an engineer at a survey company.",
        "quote": "engineer at a survey company", "weight": 0.8})
    client = FakeClient(responses=["hey you", {"content": fork_reply}])
    session = _session(tmp_path / "s.db", client)
    session.on_message("I'm an engineer at a survey company, long days lately.")
    session.clock.advance_hours(14.5)
    session.ensure_day(1)
    store = session.store
    [assertion] = store.list_assertions()
    assert (assertion.key, assertion.value) == (
        "user:job", "He is an engineer at a survey company.")
    [episode] = store.list_episodes()
    assert episode.verbatim_anchors == ("engineer at a survey company",)
    roles = [r["role"] for r in store.conn.execute("select role from llm_calls")]
    assert "aux_memory" in roles
    store.close()


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MEMORY_FORK", raising=False)
    client = FakeClient(responses=["hey you"])
    session = _session(tmp_path / "s.db", client)
    session.on_message("I'm an engineer at a survey company.")
    session.clock.advance_hours(14.5)
    session.ensure_day(1)
    assert session.store.list_assertions() == []
    assert len(client.calls) == 1
    session.store.close()


def test_the_memory_fork_sends_the_mainline_reasoning_effort(tmp_path, monkeypatch):
    """Same bytes and same reasoning_effort as her turn, or the fork's prefix
    never banks (live 2026-09-29)."""
    monkeypatch.setenv("HARNESS_MEMORY_FORK", "1")
    monkeypatch.setenv("HARNESS_THINKING_EFFORT", "low")
    client = FakeClient(responses=["hey you", {"content": _reply()}])
    session = _session(tmp_path / "s.db", client)
    session.on_message("I'm an engineer at a survey company.")
    session.clock.advance_hours(14.5)
    session.ensure_day(1)
    main, fork_call = client.calls[0], client.calls[-1]
    assert main["reasoning_effort"] == "low"
    assert fork_call["reasoning_effort"] == main["reasoning_effort"]
    session.store.close()


def test_day_view_keeps_the_events_and_drops_her_state():
    from harness.assembler import AFFECTIVE_HEADER
    from harness.prompts import TURN_STYLE

    rows = [
        {"role": "system", "content": "Monday's plan:\n- classes (09:30–13:30)"},
        {"role": "system", "content": TURN_STYLE},
        {"role": "system", "content": f"{AFFECTIVE_HEADER}\nCurrent bearing: bright."},
        {"role": "system", "content": "STATE UPDATE:\n\nCurrent activity: classes"},
        {"role": "system", "content": ("STATE UPDATE:\n\nAvailability: calmly present."
                                       "\n\nYou are reaching out first. Agenda: gym")},
        {"role": "system", "content": "STATE UPDATE:\n\nRelevant memories:\n- cake"},
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "content": "recorded", "tool_call_id": "c"},
        {"role": "assistant", "content": "[smug] hi"},
        {"role": "system", "content": "Call ended, 21:00–21:10 (10 min)."},
    ]
    view = day_view(rows, plan_header="Monday's plan:")
    assert [(r["role"], r["content"]) for r in view] == [
        ("system", "Current activity: classes"),
        ("system", "You are reaching out first. Agenda: gym"),
        ("user", "hey"),
        ("assistant", "[smug] hi"),
        ("system", "Call ended, 21:00–21:10 (10 min)."),
    ]


def test_the_memory_fork_reads_only_its_day(tmp_path, monkeypatch):
    """Owner ruling 2026-10-03: the memory fork is pruned to the day it
    remembers; her system prefix and tool menu stay, so they still bank."""
    monkeypatch.setenv("HARNESS_MEMORY_FORK", "1")
    client = FakeClient(responses=["hey you", {"content": _reply()},
                                   "morning", "again", {"content": _reply()}])
    session = _session(tmp_path / "s.db", client)
    session.on_message("yesterday's words")
    session.clock.advance_hours(24.0)
    session.on_message("I'm an engineer at a survey company.")
    session.clock.advance_hours(0.5)
    session.on_message("long days lately")
    session.clock.advance_hours(14.0)
    session.ensure_day(2)
    main, fork_call = client.calls[-2], client.calls[-1]
    assert fork_call["system"] == main["system"]
    assert fork_call["tools"] == main["tools"]
    sent = [m for m in fork_call["messages"]]
    text = "\n".join(str(m.get("content") or "") for m in sent)
    assert "survey company" in text and "long days lately" in text
    assert "yesterday's words" not in text
    assert "AFFECTIVE BEARING" not in text and "STATE UPDATE" not in text
    assert "Everything above is from today" in sent[-1]["content"]
    assert all(m["role"] != "tool" and not m.get("tool_calls") for m in sent)
    session.store.close()


def test_a_moment_and_the_day_are_remembered_with_the_day_named(tmp_path, monkeypatch):
    """Live 2026-10-01: the date night left no episode, a plan for "tomorrow"
    stayed a current preference, and the day's summary was a placeholder."""
    from harness.domain import MemoryKind
    from harness.memory import DAY_SUMMARY_KV

    assert "on Wednesday, September 30" in task_for(
        [], today="Wednesday, September 30")
    monkeypatch.setenv("HARNESS_MEMORY_FORK", "1")
    told = "On Thursday we finally watched the anime together. He stayed up late."
    fork_reply = json.dumps({"summary": told, "items": [
        {"kind": "moment", "key": "anime_night", "fact": "We watched anime together.",
         "quote": "best night in a while", "weight": 0.9},
        {"kind": "reminder", "key": "cake", "fact": "He wants cake on Friday.",
         "quote": "cake on friday", "weight": 0.5},
    ]})
    client = FakeClient(responses=["hey you", {"content": fork_reply}])
    session = _session(tmp_path / "s.db", client)
    session.on_message("best night in a while, cake on friday?")
    session.clock.advance_hours(14.5)
    session.ensure_day(1)
    store = session.store
    assert store.list_assertions() == []
    episodes = {e.category: e for e in store.list_episodes()}
    assert episodes[MemoryKind.SHARED_EPISODE].summary == "We watched anime together."
    assert episodes[MemoryKind.CALLBACK].summary == "callback: He wants cake on Friday."
    assert store.get_kv(DAY_SUMMARY_KV.format(day=0)) == told
    # the conversation closes after the day did: it keeps her words
    [sid] = {m["session_id"] for m in store.messages_for_day(0) if m["role"] == "user"}
    assert session._memory.close_session(sid, ended_at_t_h=25.0).summary == told
    store.close()
