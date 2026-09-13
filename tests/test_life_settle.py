"""Her life runs on her own clock.

Startup (and every event instant) resolves the pending backlog with no
user message anywhere: the day block and the card are in place BEFORE the
decisions, the decide rounds carry no user row, and nothing is generated
or sent — contact to the user only ever rides the proactive lane.
"""

from __future__ import annotations

import json

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import RealTimeAnchor
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.judge import ScriptedJudge
from harness.negotiation_contract import HEADS_UP_LEAD_H
from harness.session import Session
from harness.steering import STEER_MARKER_OPEN
from harness.tools import DecisionConfig, TOOL_PAYLOAD
from tests.helpers import make_store

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 4242


def _item(start: float, end: float, activity: str,
          item_id: str) -> AgendaItem:
    return AgendaItem(item_id, start, end, activity, "arc", "arc1",
                      0.8, "planned")


def _session(store, *, client, t_h: float = 14.0) -> Session:
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client,
        clock=VirtualClock(t_h=t_h),
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )


def _decide(call_id: str, initiate: bool = True) -> dict:
    return {"content": "", "tool_calls": [{
        "id": call_id, "name": "tool_decide_event",
        "arguments_json": json.dumps(
            {"initiate": initiate, "reason": "ready"}
        ),
    }]}


def _roles(call) -> list[str]:
    return [m.get("role") for m in call["messages"]]


def test_startup_resolves_the_backlog_before_any_user_message(tmp_path):
    """The morning resolves at startup: agenda + card before the decisions,
    decide rounds with no user row, no generation, no outbound."""
    store = make_store(tmp_path)
    store.attach_anchor(RealTimeAnchor(epoch0_s=0.0, t_h0=0.0, tz="UTC"))
    store.save_agenda(0, DailyAgenda(0, (
        _item(7.92, 8.67, "math practice", "ag1"),
        _item(11.0, 11.57, "grind out a 3x5 back squat", "ag2"),
    )))
    client = FakeClient(responses=[_decide("c1"), _decide("c2")])
    session = _session(store, client=client)

    drain = session.settle_pending("startup")

    # Two decide rounds for the two starts; the ends are consumed silently.
    assert len(client.calls) == 2, "startup: decide rounds only, no reply"
    records = store.decisions_for_day(0)
    assert [r["verdict"]["initiate"] for r in records] == [True, True]

    # The day block went into the stream FIRST and nothing user-shaped
    # exists anywhere in the startup requests.
    stored = store.recent_messages()
    assert stored and stored[0]["role"] == "system"
    assert "math practice" in stored[0]["content"]
    assert all(m["role"] != "user" for m in stored)
    for call in client.calls:
        assert "user" not in _roles(call), "no user row at startup"
        assert call["tools"] == TOOL_PAYLOAD
    # The agenda rode BEFORE the decisions; the steer rode last.
    first = client.calls[0]["messages"]
    assert "math practice" in first[0]["content"], "agenda precedes the decide"
    assert STEER_MARKER_OPEN in first[-1]["content"]
    # The second round reads the first decision (pair extends round to round).
    assert "tool" in _roles(client.calls[1])

    # Now the user shows up, later than the resolutions.
    session.clock.advance_hours(0.5)
    session.on_message("Hi? Anyone there?")

    generation = client.calls[2]
    roles = _roles(generation)
    # Day block -> state card -> resolved pairs -> user row. The card is a
    # stream row here (not a tail block); the user row rides last.
    assert roles[:2] == ["system", "system"], roles
    assert roles[-1] == "user", roles
    assert [r for r in roles if r in ("assistant", "tool")] == [
        "assistant", "tool", "assistant", "tool",
    ], roles


def test_next_event_instant_surveys_forward(tmp_path):
    store = make_store(tmp_path)
    store.attach_anchor(RealTimeAnchor(epoch0_s=0.0, t_h0=0.0, tz="UTC"))
    store.save_agenda(0, DailyAgenda(0, (
        _item(17.0, 18.85, "anime", "ag1"),
        _item(21.83, 22.58, "history", "ag2"),
    )))
    session = _session(store, client=FakeClient(responses=[]))

    # Nothing crossed yet: the earliest wake is the first heads-up.
    assert session.next_event_instant(14.0) == 17.0 - HEADS_UP_LEAD_H

    # Mid-item: only its end remains ahead of the next item's heads-up.
    assert session.next_event_instant(18.0) == 18.85

    # Past everything: nothing ahead.
    assert session.next_event_instant(23.0) is None
