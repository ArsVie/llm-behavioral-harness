"""Guards for the 2026-09-07 prompt-content pass.

Four changes, each with a guard that fails if it silently regresses:

1. No absolute virtual hour (``t_h``) reaches any model-visible surface --
   the state card, agenda windows, pop-up inputs, steer blocks and proactive
   hooks all render HH:MM.
2. The pop-up aux call is a byte-identical EXTENSION of the mainline call,
   not a request carrying its own prefix (DeepSeek matches strictly from
   token 0, in 64-token units, so a differing system message costs the whole
   prefix on a call that fires at every event boundary).
3. Decisions the model made are projected back into its context, so the
   decision lane stops being write-only.
4. The stable prefix leads with the persona and carries neither the closing
   guidance nor the steer trust prose.
"""

from __future__ import annotations

import json
import re

import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock, duration, hhmm
from harness.domain import AgendaItem, Interest, PersonaProfile
from harness.prompts import STEER_TRUST_RULE, SYSTEM_CORE_WITH_TOOLS
from harness.proactive import compose_hook
from harness.session import Session
from harness.steering import KIND_EVENT_POPUP, Steer, render_steer_block
from harness.store import SQLiteStore
from harness.tools import render_popup

#: An absolute virtual hour that is unmistakably not a wall clock: t_h 80.98
#: is day 3, 08:59 local -- the value the live run put in front of the model.
LIVE_T_H = 80.98

#: Matches a bare decimal hour ("80.98", "19.5", "4.0h") -- the leak shape.
_RAW_HOUR = re.compile(r"\b\d{1,3}\.\d+\s*h?\b")


def _persona() -> PersonaProfile:
    return PersonaProfile(
        name="Lily",
        core="You are Lily, sharp and unimpressed until proven otherwise.",
        interests=(Interest("alternative music", "music", 0.8),),
        routines=(),
    )


def _session(tmp_path, *, t_h: float = LIVE_T_H, responses=None):
    store = SQLiteStore(tmp_path / "s.db")
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=responses or ["fine, what broke."])
    session = Session(
        store=store,
        persona=PersonaParams(),
        timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=3,
        client=client,
        clock=VirtualClock(t_h=t_h),
    )
    session._profile = profile
    return session, client, store


# -- 1. time renders as a wall clock, never as an engine coordinate -------- #


@pytest.mark.parametrize(
    "t_h, expected",
    [(0.0, "00:00"), (80.98, "08:59"), (78.96, "06:58"), (19.5, "19:30"),
     (23.9999, "00:00")],
)
def test_hhmm_renders_24h_wall_clock(t_h, expected):
    assert hhmm(t_h) == expected


def test_duration_never_renders_a_decimal_hour():
    assert duration(0.0) == "just now"
    assert duration(1.0 / 3.0) == "20m"
    assert duration(4.0) == "4h 00m"
    assert not _RAW_HOUR.search(duration(4.0))


def test_popup_inputs_render_hhmm_but_persist_raw():
    """The RAW ``t_h`` stays in the recorded inputs (replay + audit); the
    conversion happens at the render boundary."""
    inputs = {
        "event_id": "ag_3_r_00",
        "event_label": "morning coffee",
        "state_label": "start",
        "time": 78.96,
    }
    block = render_popup("tool_decide_event", inputs)
    assert "Time: 06:58" in block
    assert "78.96" not in block
    # the caller's dict is untouched -- decision_records keeps the raw value
    assert inputs["time"] == 78.96


def test_popup_accepts_the_legacy_stringified_time():
    """Live rows stored ``"78.96"`` as a string; it must still render."""
    block = render_popup(
        "tool_decide_event",
        {"event_label": "morning coffee", "state_label": "start", "time": "78.96"},
    )
    assert "Time: 06:58" in block


def test_steer_block_renders_hhmm():
    steer = Steer(steer_id=1, day=3, t_h=LIVE_T_H, kind=KIND_EVENT_POPUP,
                  payload={"event": "morning coffee", "state": "start"})
    block = render_steer_block(steer)
    assert "Time: 08:59" in block
    assert not _RAW_HOUR.search(block)


def test_proactive_hook_window_is_a_wall_clock():
    """The hook renders VERBATIM into the state card, so it is a
    model-visible surface -- the live run showed ``(80.0-80.7h)``."""
    item = AgendaItem(
        "ag_3_i_music", 80.0, 80.74, "watch a video on alternative music",
        "interest", "alternative music", 0.8, "planned",
    )
    hook = compose_hook(item, "schedule")
    assert hook == "Agenda: watch a video on alternative music (08:00–08:44)"
    assert not _RAW_HOUR.search(hook)


def test_no_raw_hour_anywhere_in_a_live_turn(tmp_path):
    """End-to-end: nothing the model receives contains a decimal hour."""
    session, client, store = _session(tmp_path)
    session.on_message("hi, my machine crashed")
    call = client.calls[-1]
    payload = call["system"] + "\n" + "\n".join(
        m["content"] or "" for m in call["messages"]
    )
    store.close()
    leaks = _RAW_HOUR.findall(payload)
    assert not leaks, f"raw virtual hours reached the model: {leaks}"


# -- 2. the pop-up call extends the mainline prefix ------------------------ #


def test_popup_call_reuses_the_mainline_stable_prefix(tmp_path):
    """The aux call must send the SAME system message as the mainline call.

    Before this pass it sent the legacy full three-tier string -- state card
    inside the system message, minute-resolution clock line and all -- so
    every decision call presented a prefix no other call had ever sent.
    """
    session, client, store = _session(tmp_path)
    session.on_message("hey")
    mainline_system = client.calls[-1]["system"]

    from harness.tools import PopupRequest

    client.responses.append('tool_decide_event: {"initiate": false, "reason": "busy"}')
    session._popup_request_call(
        PopupRequest(
            popup_kind="tool_decide_event",
            popup="{Event: gym, State: start, Time: 19:00}",
            tools=[],
            native=False,
            inputs={},
        )
    )
    popup_call = client.calls[-1]
    store.close()

    assert popup_call["system"] == mainline_system
    # ... and the pop-up rides as system input, not as something the user said
    assert popup_call["messages"][-1]["role"] == "system"


# -- 3. decisions are projected back into context -------------------------- #


def test_recorded_decision_reappears_in_later_context(tmp_path):
    """A verdict the model gave must be readable by the model next turn."""
    session, client, store = _session(
        tmp_path, responses=["first.", "second."]
    )
    session.on_message("hey")
    store.record_decision(
        day=3,
        t_h=LIVE_T_H,
        popup_kind="tool_decide_event",
        event_id="ag_3_i_music",
        event_label="watch a video on alternative music",
        state_label="start",
        time="80.0",
        inputs_json=json.dumps(
            {"event_label": "watch a video on alternative music",
             "state_label": "start", "time": 80.0}
        ),
        raw_reply="{}",
        verdict_json=json.dumps(
            {"initiate": False,
             "reason": "He just opened up about the Singapore plan."}
        ),
        source="model",
        transport="native",
        delivered_t_h=LIVE_T_H,
        budget_consumed=0,
        replay_id="steer-41",
    )
    turns = session._context_turns()
    store.close()

    # 2026-09-08: the decision replays as a NATIVE tool exchange, not a prose
    # block. The prose form recorded the decision but taught the model
    # nothing, so a pop-up arrived with no precedent that tool calls happen
    # here and the likeliest continuation was to answer the person instead.
    calls = [t for t in turns if t.get("tool_calls")]
    assert calls, "no assistant tool_calls message in the replayed context"
    call = calls[0]["tool_calls"][0]
    assert call["function"]["name"] == "tool_decide_event"
    assert "Singapore plan" in call["function"]["arguments"]

    results = [t for t in turns if t.get("role") == "tool"]
    assert results, "the tool call has no result message"
    assert results[0]["tool_call_id"] == call["id"], (
        "tool result does not reference its call — providers reject this"
    )
    # The result says what the SERVER did, with a wall clock, not the
    # model's own reason (already in the arguments).
    assert "let" in results[0]["content"]
    assert "08:59" in results[0]["content"]

    # The pair must be adjacent and in order.
    idx = turns.index(calls[0])
    assert turns[idx + 1] is results[0]


def test_context_projection_survives_a_malformed_decision_row(tmp_path):
    """A bad audit row must never break context assembly."""
    session, client, store = _session(tmp_path)
    session.on_message("hey")
    store.conn.execute(
        "INSERT INTO decision_records (day, t_h, popup_kind, inputs_json, "
        "verdict_json, source, transport) VALUES (?,?,?,?,?,?,?)",
        (3, LIVE_T_H, "tool_decide_event", "{not json", "also not json",
         "model", "native"),
    )
    store.conn.commit()
    turns = session._context_turns()
    store.close()
    assert turns  # the transcript still assembles


# -- 4. what left the stable prefix ---------------------------------------- #


def test_stable_prefix_leads_with_the_persona(tmp_path):
    session, client, store = _session(tmp_path)
    session.on_message("hey")
    system = client.calls[-1]["system"]
    store.close()
    assert system.startswith("You are Lily,")
    assert SYSTEM_CORE_WITH_TOOLS in system
    assert system.index("You are Lily,") < system.index(SYSTEM_CORE_WITH_TOOLS)


def test_steer_trust_prose_left_the_prefix():
    assert STEER_TRUST_RULE == ""
    assert "steer" not in SYSTEM_CORE_WITH_TOOLS.lower()


def test_closing_guidance_never_renders_in_a_live_turn(tmp_path):
    session, client, store = _session(tmp_path)
    session.on_message("hey")
    call = client.calls[-1]
    payload = call["system"] + "\n".join(
        m["content"] or "" for m in call["messages"]
    )
    store.close()
    assert "Closing guidance:" not in payload


# -- 5. the context stream is append-only, not a sliding window ------------ #


def _payload(call) -> str:
    """The exact bytes sent, in order — what a prefix cache matches on."""
    return call["system"] + "\x00" + "\x00".join(
        f"{m['role']}\x01{m['content'] or ''}" for m in call["messages"]
    )


def test_request_n_plus_1_extends_request_n(tmp_path):
    """The cache contract, asserted directly.

    Every turn's payload must START WITH the previous turn's payload minus
    its volatile tail. A sliding window fails this the moment history passes
    the window: front-truncation moves the first byte after the system
    message, and a prefix cache matches strictly from token 0.
    """
    session, client, store = _session(
        tmp_path, responses=[f"reply {i}." for i in range(30)]
    )
    prefixes = []
    for i in range(20):
        session.clock.advance_hours(0.05)
        session.on_message(f"message {i}")
        call = client.calls[-1]
        # Everything except the trailing state card is the durable part.
        durable = dict(call)
        durable["messages"] = [
            m for m in call["messages"] if m["role"] != "system"
        ]
        prefixes.append(_payload(durable))
    store.close()

    for earlier, later in zip(prefixes, prefixes[1:]):
        assert later.startswith(earlier), (
            "request N+1 is not an extension of request N — the context read "
            "truncated from the front"
        )


def test_context_read_is_anchored_to_the_epoch_not_the_tail(tmp_path):
    """20 turns is well past the old 12-row window; all of them survive."""
    session, client, store = _session(
        tmp_path, responses=[f"reply {i}." for i in range(30)]
    )
    for i in range(20):
        session.clock.advance_hours(0.05)
        session.on_message(f"message {i}")
    turns = session._context_turns()
    store.close()
    texts = [t["content"] for t in turns]
    assert any("message 0" in t for t in texts), "oldest turn was truncated away"
    assert any("message 19" in t for t in texts)


def test_compaction_moves_the_epoch_only_at_a_day_boundary(tmp_path):
    """The epoch is a boundary operation: it must not move mid-day.

    Dropping messages shifts every byte after the drop, so doing it per turn
    pays a full re-prefill every turn and buys nothing.
    """
    import harness.session as session_mod

    session, client, store = _session(
        tmp_path, responses=[f"reply {i}." for i in range(40)]
    )
    # Force compaction to be eligible on any history at all.
    original_after = session_mod.CONTEXT_COMPACT_AFTER_MESSAGES
    original_keep = session_mod.CONTEXT_RETAIN_MESSAGES
    session_mod.CONTEXT_COMPACT_AFTER_MESSAGES = 4
    session_mod.CONTEXT_RETAIN_MESSAGES = 2
    try:
        for i in range(6):
            session.clock.advance_hours(0.05)
            session.on_message(f"message {i}")
        assert session.context_epoch_id() == 0, "epoch moved mid-day"

        session.clock.advance_hours(24.0)
        session.ensure_day(session.clock.day())
        assert session.context_epoch_id() > 0, "epoch did not move at rollover"
    finally:
        session_mod.CONTEXT_COMPACT_AFTER_MESSAGES = original_after
        session_mod.CONTEXT_RETAIN_MESSAGES = original_keep
        store.close()


def test_compaction_is_recorded_as_an_explainable_event(tmp_path):
    """A prefix break must be explainable from the audit log alone."""
    import harness.session as session_mod

    session, client, store = _session(
        tmp_path, responses=[f"reply {i}." for i in range(40)]
    )
    session_mod.CONTEXT_COMPACT_AFTER_MESSAGES = 4
    session_mod.CONTEXT_RETAIN_MESSAGES = 2
    try:
        for i in range(6):
            session.clock.advance_hours(0.05)
            session.on_message(f"message {i}")
        session.clock.advance_hours(24.0)
        session.ensure_day(session.clock.day())
        events = [e["event"] for e in store.events_since(0)]
        assert "context_compacted" in events
    finally:
        session_mod.CONTEXT_COMPACT_AFTER_MESSAGES = 400
        session_mod.CONTEXT_RETAIN_MESSAGES = 200
        store.close()
