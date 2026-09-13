"""The decision rides the turn's own generation (owner ruling 2026-09-12).

"it should be a tool call on the mainline context and just that". A separate
request is a second voice: same card, but not the arriving user turn, and its
output has to be spliced back into the context at a position it did not come
from -- measured live on 2026-09-12, call #6 -> #7 moved the card from index 2
to 5 and took the cache from 896 to 640 of 1345.

What these tests pin:
* the drain COLLECTS (no model call) and injects the pop-up into the turn;
* the turn offers exactly the function set its collected kinds need;
* the turn's own output answers the pop-up, once, and a re-ask falls back to a
  real call;
* the verdict's effects are applied after the generation, oldest boundary
  first, before the reply is persisted.
"""

from __future__ import annotations

from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.tools import DecisionConfig
from tests.helpers.store import make_session, make_store


def _session(tmp_path, responses=()):
    store = make_store(tmp_path)
    session = make_session(
        store,
        client=FakeClient(responses=list(responses)),
        clock=VirtualClock(t_h=9.0),
        decision_config=DecisionConfig(),
    )
    return session, store


class _Drain:
    """The bits of a drain that phase B touches (duck-typed on purpose)."""

    def __init__(self):
        self.notices: list[str] = []
        self.proactive_out: list[tuple[str, str]] = []
        self.suppress_reply = False
        self.injections: list[str] = []


def test_the_drain_collects_instead_of_deciding(tmp_path):
    session, _store = _session(tmp_path)
    session._collect_decisions = True
    result = session._execute_decision(
        decision_id="d1",
        popup_kind="tool_decide_event",
        inputs={"event_id": "ag1", "event_label": "gym", "state_label": "in_30",
                "time": "9.0"},
        steer=object(),
        day=0,
        t_h=9.0,
    )
    assert result is None, "nothing is decided during the drain"
    assert len(session._deferred_decisions) == 1
    record = session._deferred_decisions[0]
    assert record.popup_kind == "tool_decide_event"
    assert record.t_h == 9.0
    # The function set that kind needs, and only that: offering all three let
    # the model answer an event pop-up with tool_decide_reply (1-in-3 live).
    names = [t["function"]["name"] for t in record.tools]
    assert names == ["tool_decide_event"]


def test_the_popup_block_is_injected_into_the_turn(tmp_path):
    session, _store = _session(tmp_path)
    session._collect_decisions = True
    drain = _Drain()
    session._active_drain = drain
    session._execute_decision(
        decision_id="d1", popup_kind="tool_decide_event",
        inputs={"event_id": "ag1", "event_label": "gym", "state_label": "in_30",
                "time": "9.0"},
        steer=object(), day=0, t_h=9.0,
    )
    assert drain.injections, "the model has to see the question"
    assert "gym" in drain.injections[0]


def test_no_decisions_means_no_tools_at_all(tmp_path):
    session, _store = _session(tmp_path)
    assert session._deferred_decision_tools() is None


def test_only_the_collected_kinds_are_offered_once_each(tmp_path):
    session, _store = _session(tmp_path)
    session._collect_decisions = True
    for index in (1, 2):
        session._execute_decision(
            decision_id=f"d{index}", popup_kind="tool_decide_event",
            inputs={"event_id": f"ag{index}", "event_label": "gym",
                    "state_label": "in_30", "time": "9.0"},
            steer=object(), day=0, t_h=9.0,
        )
    tools = session._deferred_decision_tools()
    assert [t["function"]["name"] for t in tools] == ["tool_decide_event"]


def test_the_turns_own_tool_call_answers_the_popup(tmp_path):
    session, _store = _session(tmp_path)
    session._last_tool_calls = [{
        "id": "c1", "type": "function",
        "function": {"name": "tool_decide_event", "arguments": "{}"},
    }]
    served = session._served_for("tool_decide_event")
    assert served is not None and served.tool_calls == session._last_tool_calls
    assert session._served_for("tool_decide_reply") is None


def test_a_marker_reply_answers_the_popup_too(tmp_path):
    session, _store = _session(tmp_path)
    session._last_marker_reply = 'tool_decide_event: {"initiate": "yes"}'
    served = session._served_for("tool_decide_event")
    assert served is not None and served.tool_calls is None
    assert "initiate" in (served.text or "")


def test_the_served_reply_is_consumed_once(tmp_path):
    session, _store = _session(tmp_path)
    from harness.tools import RawReply

    session._served_reply = RawReply(text="served", tool_calls=None)
    assert session._decision_model_call(None).text == "served"
    assert session._served_reply is None, "one generation answers one pop-up"


def test_phase_b_applies_verdicts_oldest_boundary_first(tmp_path):
    session, _store = _session(tmp_path)
    seen: list[float] = []

    def apply(steer, *, day, t_h, notices, proactive_out, drain):
        seen.append(steer.t_h)
        # The no-reply verdict is aggregated by _finish_deferred_decisions,
        # which is what this asserts: the stub only returns the outcome.
        return "suppress" if steer.t_h == 7.0 else "consumed"

    setattr(session, "_apply_steer", apply)
    session._collect_decisions = True
    for boundary in (9.0, 7.0, 8.0):
        session._execute_decision(
            decision_id=f"d{boundary}", popup_kind="tool_decide_event",
            inputs={"event_id": "ag1", "event_label": "gym",
                    "state_label": "in_30", "time": str(boundary)},
            steer=type("S", (), {"t_h": boundary, "steer_id": 1, "payload": {},
                                 "kind": "event_popup"})(),
            day=0, t_h=boundary,
        )
    drain = _Drain()
    session._finish_deferred_decisions(drain, 0, 9.0)
    assert seen == [7.0, 8.0, 9.0], "a catch-up batch resolves when it happened"
    assert drain.suppress_reply is True
    assert session._deferred_decisions == []
