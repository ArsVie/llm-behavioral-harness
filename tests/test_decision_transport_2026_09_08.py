"""Guards for the four decision-lane fixes of 2026-09-08.

What went wrong live, and what each fix pins:

1. The aux call offered all three tool schemas, so an event pop-up came back
   as ``tool_decide_reply`` — or as plain prose with no tool at all. 20 of 25
   aux calls discarded in one evening. Measured against the live gateway:
   1-in-3 wrong tool with three schemas offered, 0-in-3 with one. A forced
   ``tool_choice`` would settle it outright but this model rejects one in
   thinking mode, so prose is bounded (fix 2) rather than prevented.
2. A failed decision is requeued, and nothing bounded the retries: the same
   steer was re-asked every turn, one model call each, 4 -> 4 -> 5 -> 7 per
   turn and climbing.
3. Decisions replayed into context as a PROSE summary, so the history held no
   evidence that tool calls happen here — every pop-up was a first-ever ask
   appended after real dialogue, and answering the person was the likeliest
   continuation.
4. A go/initiate verdict pasted the verdict's ``reason`` into the channel as
   her words ("...I'll send a warm in-character send-off and keep cooking":
   third person, an intention rather than the act) and closed the
   conversation BEFORE generating, so a user goodbye got silence.

The ``reason`` field itself is untouched by all of this. It is the engine's
audit trail and the reason the decision lane exists; it simply stopped being
her mouth. ``test_reason_is_still_recorded_for_the_engine_study`` pins that.
"""

from __future__ import annotations

import json

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import Interest, PersonaProfile
from harness.session import Session
from harness.steering import (
    MAX_ATTEMPTS,
    InMemorySteerBackend,
    SteeringQueue,
    wrap_steer_marker,
)
from harness.store import SQLiteStore
from harness.tools import PopupRequest, tools_identity

from tests.helpers.store import make_store

SEED = 8001


def _persona() -> PersonaProfile:
    return PersonaProfile(
        name="Lily", core="You are Lily.",
        interests=(Interest("anime", "exact", 0.8),), routines=(),
    )


def _session(tmp_path, *, responses=None, name="d.db"):
    store = make_store(tmp_path, name)
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=responses or ["ok"])
    session = Session(
        store=store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=SEED,
        client=client, clock=VirtualClock(t_h=9.0),
    )
    session._profile = profile
    return session, client, store


# -- the stored request now carries the payload that actually went out ----- #


def test_logged_decide_call_records_the_payload_that_went_out(tmp_path):
    """A stored call must be provably the call that was made.

    The ledger recorded the request WITHOUT ``tools``, so a live cache reading
    could not be reproduced from its own row. Three explanations of it survived
    longer than they should have because of that gap.
    """
    session, client, store = _session(tmp_path)
    request = PopupRequest(
        popup_kind="tool_decide_event",
        popup="{Event: gym, State: start, Time: 19:00}",
        tools=[{"name": "tool_decide_event"}, {"name": "tool_decide_reply"}],
        native=True, inputs={},
    )
    repro = session._popup_repro(
        request, [{"role": "user", "content": "hey"}], 4, 60.0
    )["repro"]
    store.close()

    assert repro["tool_names"] == ["tool_decide_event"]
    assert repro["tool_choice"] is None, "the identity records what went out"
    assert repro["tools_hash"] == tools_identity(
        [{"type": "function", "function": {"name": "tool_decide_event"}}]
    )[0]
    assert repro["reasoning_effort"] == session._thinking_effort


def _prefix_events(store) -> list[str]:
    rows = store.conn.execute(
        "SELECT detail FROM state_events WHERE event='prefix_invariant_violation'"
    ).fetchall()
    return [row[0] for row in rows]


def test_the_prefix_invariant_logs_a_rewrite_and_is_silent_otherwise(tmp_path, monkeypatch):
    """Witness the append-only property at runtime, per lane.

    A rewrite at the head costs the entire prefix (measured), so the check has
    to exist — but it must never raise in a live run, and it must stay off
    unless asked for.
    """
    session, client, store = _session(tmp_path)
    lane = "chat"
    first = [{"role": "system", "content": "core"}, {"role": "user", "content": "hi"}]

    # Off by default: even a rewrite logs nothing.
    session._note_request_prefix(lane, first)
    session._note_request_prefix(lane, [{"role": "system", "content": "EDITED"}])
    assert _prefix_events(store) == []

    monkeypatch.setenv("HARNESS_PREFIX_INVARIANT", "1")
    session._last_request_pairs.clear()   # a fresh witness: the off-phase left state
    session._note_request_prefix(lane, first)
    session._note_request_prefix(lane, first + [{"role": "assistant", "content": "yo"}])
    assert _prefix_events(store) == [], "an append is not a violation"

    session._note_request_prefix(lane, [{"role": "system", "content": "EDITED"}])
    events = _prefix_events(store)
    store.close()
    assert len(events) == 1, "a rewritten head must be recorded exactly once"
    assert json.loads(events[0]) == {"lane": lane, "index": 0, "was": 3, "now": 1}


def _role_leak_events(store) -> list[str]:
    rows = store.conn.execute(
        "SELECT detail FROM state_events WHERE event='user_role_leak'"
    ).fetchall()
    return [row[0] for row in rows]


def test_a_harness_event_in_a_user_slot_is_recorded_and_tolerated(tmp_path):
    """The role convention, enforced at runtime (CONVENTIONS:27).

    User-role content is what the USER said; an event that turns up in a user
    slot is a leak worth knowing about. Checked on every call rather than behind
    a flag -- it is a substring pass, and it must log and carry on, never raise:
    a diagnostic sensor cannot be allowed to kill a live run.
    """
    session, client, store = _session(tmp_path)
    clean = [
        {"role": "system", "content": "core"},
        {"role": "user", "content": "hey, you up?"},
    ]
    session._note_request_prefix("chat", clean)
    assert _role_leak_events(store) == [], "real user text is not a leak"

    mixed = clean + [
        {"role": "user", "content": wrap_steer_marker("Event: he is back")}
    ]
    session._note_request_prefix("chat", mixed)     # must not raise
    events = _role_leak_events(store)
    store.close()
    assert len(events) == 1, "a harness event in a user slot must be recorded"
    assert json.loads(events[0]) == {"lane": "chat", "indices": [2]}


def test_the_prefix_invariant_is_scoped_per_lane(tmp_path, monkeypatch):
    """Legs legitimately differ: the decide leg EXTENDS the mainline array."""
    session, client, store = _session(tmp_path)
    monkeypatch.setenv("HARNESS_PREFIX_INVARIANT", "1")
    mainline = [{"role": "system", "content": "core"}, {"role": "user", "content": "hi"}]
    popup = mainline + [{"role": "system", "content": "card + popup"}]
    session._note_request_prefix("chat", mainline)
    session._note_request_prefix("tool_decide_event", popup)
    session._note_request_prefix("chat", mainline + [{"role": "user", "content": "more"}])
    events = _prefix_events(store)
    store.close()
    assert events == [], "different lanes are not compared with each other"


def test_the_chat_leg_stores_its_wire_identity(tmp_path):
    """The mainline leg stores its decode controls AND that it offered no tools.

    The eval-cell path enriches its own repro rows, so this asserts on the LIVE
    path — the rows a running bot writes.
    """
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=["ok"])
    session = Session(
        store=store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=SEED,
        client=client, clock=VirtualClock(t_h=9.0),
    )
    session._profile = profile
    session.on_message("hey")
    row = store.conn.execute(
        "SELECT repro_json FROM llm_calls WHERE role='chat' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    store.close()

    assert row is not None and row[0], "the chat leg logged no reconstructable call"
    repro = json.loads(row[0])
    assert "reasoning_effort" in repro
    assert repro["tools_hash"] is None
    assert repro["tool_names"] == []
    assert repro["tool_choice"] is None


def test_a_textual_decision_records_that_it_offered_nothing(tmp_path):
    """No tools is a FACT worth storing, not an absence to be inferred."""
    session, client, store = _session(tmp_path)
    request = PopupRequest(
        popup_kind="tool_decide_event", popup="x",
        tools=[{"name": "tool_decide_event"}], native=False, inputs={},
    )
    repro = session._popup_repro(request, [], 4, 60.0)["repro"]
    store.close()
    assert (repro["tools_hash"], repro["tool_names"], repro["tool_choice"]) == (
        None, [], None
    )


# -- 1. the requested tool is the only one offered, and it is required ----- #


def test_popup_call_requires_exactly_the_requested_tool(tmp_path):
    session, client, store = _session(tmp_path, responses=["hi", "ok"])
    session.on_message("hey")
    session._popup_request_call(PopupRequest(
        popup_kind="tool_decide_event",
        popup="{Event: gym, State: start, Time: 19:00}",
        tools=[{"name": "tool_decide_event"}, {"name": "tool_decide_reply"},
               {"name": "tool_decide_proactive"}],
        native=True, inputs={},
    ))
    call = client.calls[-1]
    store.close()

    offered = [t["function"]["name"] for t in call["tools"]]
    assert offered == ["tool_decide_event"], (
        f"offered {offered} — a pop-up asks ONE named question, so the other "
        "schemas must not be on the table"
    )
    # tool_choice is not sent at all. It cannot be narrowed on this model
    # (always thinking; a forced choice 400s, verified against the live
    # gateway 2026-09-08), and sending the only remaining value buys nothing:
    # on the real decide body, omitted parsed 6/6 while "auto" parsed 5/6,
    # same cache, no latency penalty (n=6 per arm, interleaved). Narrowing the
    # MENU is the fix that is actually available; a named choice breaks calls.
    assert call["tool_choice"] is None


def test_unknown_kind_falls_back_rather_than_offering_nothing(tmp_path):
    """A wrong-tool verdict is recoverable; no tool at all is not."""
    session, client, store = _session(tmp_path, responses=["hi", "ok"])
    session.on_message("hey")
    session._popup_request_call(PopupRequest(
        popup_kind="tool_decide_unknown",
        popup="{}",
        tools=[{"name": "tool_decide_event"}],
        native=True, inputs={},
    ))
    call = client.calls[-1]
    store.close()
    assert call["tools"], "no tools offered for an unknown kind"
    assert call["tool_choice"] is None, "the field is never sent"


# -- 2. the retry budget is bounded --------------------------------------- #


def test_a_steer_is_abandoned_once_its_retry_budget_runs_out():
    backend = InMemorySteerBackend()
    queue = SteeringQueue(backend)
    steer_id = queue.enqueue("event_popup", {"event": "gym"}, 0, 9.0)

    for attempt in range(MAX_ATTEMPTS):
        drained = queue.drain_pending("idle", f"turn-{attempt}", 9.0 + attempt)
        assert [s.steer_id for s in drained] == [steer_id], (
            f"attempt {attempt} did not deliver"
        )
        queue.requeue(steer_id)

    # Budget exhausted: no further delivery, and the row is terminal.
    assert queue.drain_pending("idle", "turn-final", 20.0) == []
    assert backend.storage[steer_id]["status"] == "abandoned"
    assert backend.storage[steer_id]["attempts"] == MAX_ATTEMPTS


def test_abandonment_survives_a_restart(tmp_path):
    """The counter is persisted, so a restart cannot reset the budget."""
    store = SQLiteStore(tmp_path / "attempts.db")
    try:
        queue = SteeringQueue(store)
        sid = queue.enqueue("event_popup", {"event": "gym"}, 0, 9.0)
        for i in range(MAX_ATTEMPTS):
            queue.drain_pending("idle", f"t{i}", 9.0)
            queue.requeue(sid)
        # A fresh queue over the same store sees the exhausted budget.
        assert SteeringQueue(store).drain_pending("idle", "t-new", 9.0) == []
        row = store.conn.execute(
            "SELECT status, attempts FROM steering_queue WHERE id = ?", (sid,)
        ).fetchone()
        assert row["status"] == "abandoned"
        assert row["attempts"] == MAX_ATTEMPTS
    finally:
        store.close()


def test_a_healthy_steer_is_never_abandoned():
    backend = InMemorySteerBackend()
    queue = SteeringQueue(backend)
    sid = queue.enqueue("event_popup", {"event": "gym"}, 0, 9.0)
    drained = queue.drain_pending("idle", "turn-0", 9.0)
    assert [s.steer_id for s in drained] == [sid]
    assert backend.storage[sid]["status"] == "delivered"
    assert backend.storage[sid]["attempts"] == 0


# -- 3. decisions replay as a native tool exchange ------------------------ #


def _record(store, **over):
    payload = dict(
        day=0, t_h=9.5, popup_kind="tool_decide_event", event_id="ag_1",
        event_label="gym", state_label="end", time="9.5",
        inputs_json=json.dumps({"event_label": "gym"}),
        raw_reply=json.dumps([{
            "id": "call_provider_abc", "type": "function",
            "function": {"name": "tool_decide_event", "arguments": "{}"},
        }]),
        verdict_json=json.dumps(
            {"initiate": False, "action": "abandon", "reason": "not tonight"}
        ),
        source="model", transport="native", delivered_t_h=9.5,
        budget_consumed=0, replay_id="steer-1",
    )
    payload.update(over)
    return store.record_decision(**payload)


def test_decision_replays_as_an_assistant_tool_call_plus_result(tmp_path):
    session, client, store = _session(tmp_path, responses=["hi"])
    session.on_message("hey")
    _record(store)
    turns = session._context_turns()
    store.close()

    calls = [t for t in turns if t.get("tool_calls")]
    results = [t for t in turns if t.get("role") == "tool"]
    assert calls and results, "the exchange is not in native form"
    fn = calls[0]["tool_calls"][0]
    assert fn["function"]["name"] == "tool_decide_event"
    assert "not tonight" in fn["function"]["arguments"]
    # The provider's own call id is reused, so the replay is the real one.
    assert fn["id"] == "call_provider_abc"
    assert results[0]["tool_call_id"] == fn["id"]
    # The result is the SERVER's outcome, not a restatement of her reason.
    assert "skipped gym" in results[0]["content"]
    assert "not tonight" not in results[0]["content"]


def test_a_textual_decision_still_forms_a_valid_pair(tmp_path):
    """No provider id (textual/server-drawn) still needs a matching pair."""
    session, client, store = _session(tmp_path, responses=["hi"])
    session.on_message("hey")
    _record(store, raw_reply='tool_decide_event: {"initiate": false}',
            transport="textual")
    turns = session._context_turns()
    store.close()
    call = next(t for t in turns if t.get("tool_calls"))["tool_calls"][0]
    result = next(t for t in turns if t.get("role") == "tool")
    assert call["id"].startswith("call_decision_")
    assert result["tool_call_id"] == call["id"]


def test_tool_keys_survive_the_trip_to_the_provider(tmp_path):
    """The copy into the request must not drop the pairing.

    Every call site used to copy role and content only, which would strand an
    assistant tool_calls message with no result — providers reject that.
    """
    session, client, store = _session(tmp_path, responses=["hi", "second"])
    session.on_message("hey")
    _record(store)
    session.on_message("again")
    sent = client.calls[-1]["messages"]
    store.close()
    assert any(m.get("tool_calls") for m in sent), "tool_calls was dropped"
    assert any(m.get("tool_call_id") for m in sent), "tool result was dropped"


def test_a_malformed_decision_row_never_breaks_context(tmp_path):
    session, client, store = _session(tmp_path, responses=["hi"])
    session.on_message("hey")
    store.conn.execute(
        "INSERT INTO decision_records (day, t_h, popup_kind, inputs_json, "
        "verdict_json, source, transport) VALUES (0, 9.5, 'tool_decide_event',"
        " '{bad', 'also bad', 'model', 'native')"
    )
    store.conn.commit()
    turns = session._context_turns()
    store.close()
    assert turns


# -- 4. reason is recorded, never spoken ---------------------------------- #


def test_reason_is_still_recorded_for_the_engine_study(tmp_path):
    """The audit trail is the point of the decision lane — it must survive.

    Only its use as DIALOGUE was removed.
    """
    session, client, store = _session(tmp_path, responses=["hi"])
    session.on_message("hey")
    rid = _record(store)
    row = store.conn.execute(
        "SELECT verdict_json FROM decision_records WHERE id = ?", (rid,)
    ).fetchone()
    store.close()
    assert json.loads(row["verdict_json"])["reason"] == "not tonight"
