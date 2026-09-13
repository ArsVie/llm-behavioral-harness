"""The event steer as designed: heads-up, ONE decision, honest replay.

The pop-up is a steer injected into main context for an event, carrying the
event, its time and its state, plus a brief instruction. The model answers
with one tri-state field. Three boundaries, one decision:

    start_t_h - HEADS_UP_LEAD_H   HEADS_UP -- "this is about to start, get
                                  ready" (only while he is there). NO verdict.
    start_t_h                     the ONE decision: initiate yes/no/defer.
                                  Re-offered only on her own defer, which is
                                  his window to talk her out of going.
    end_t_h                       nothing. No model call.

What each test here pins, and what it went wrong as:

* The decision used to be offered TWICE — once at each boundary — and the
  end leg's ``reason`` was captured as the item's "outcome". A verdict
  rationale is not a record of what happened, and the end boundary has
  nothing left to decide: ``life.transition_past_windows`` already resolves
  a passed window deterministically.
* ``initiate`` used to be a bool with a parallel optional ``action``, so a
  model that answered ``"defer"`` had it read as falsey and became a skip.
* Past boundaries were all enqueued stamped ``now`` and drained as one pile,
  which asked her at 18:41 whether to initiate a 07:28 coffee. Her day runs
  whether or not anyone watched it, so a resume replays the morning in the
  order the morning happened, each pop-up carrying the time the event
  actually arrived.
"""

from __future__ import annotations

from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.negotiation_contract import HEADS_UP_LEAD_H
from harness.negotiation_state import NegotiationState, decide_status_at
from harness.tools import DecisionConfig, _event_verdict
from tests.helpers.store import make_session, make_store


def _item(start: float, end: float, activity: str = "gym",
          item_id: str = "ag1") -> AgendaItem:
    return AgendaItem(item_id, start, end, activity, "arc", "arc1",
                      0.8, "planned")


def _popups(store) -> list[dict]:
    return [dict(r) for r in store.conn.execute(
        "SELECT t_h, payload_json FROM steering_queue ORDER BY id"
    ).fetchall()]


# the tri-state verdict


def test_defer_is_a_defer_not_a_falsey_skip():
    # The bug: `_as_bool("defer")` returned None, `initiate` stayed False and
    # `action` stayed None, so a deferral was applied as an abandon.
    v = _event_verdict({"initiate": "defer", "reason": "just a sec"})
    assert v["action"] == "defer"
    assert v["initiate"] is False      # a defer is not an initiation
    assert v["reason"] == "just a sec"


def test_yes_and_no_map_onto_the_canonical_actions():
    assert _event_verdict({"initiate": "yes", "reason": "r"})["action"] == "follow"
    assert _event_verdict({"initiate": "yes", "reason": "r"})["initiate"] is True
    assert _event_verdict({"initiate": "no", "reason": "r"})["action"] == "abandon"
    assert _event_verdict({"initiate": "no", "reason": "r"})["initiate"] is False


def test_the_model_may_name_its_own_defer_turns_clamped():
    assert _event_verdict(
        {"initiate": "defer", "reason": "r", "turns": 3}
    )["defer_turns"] == 3
    # clamped to [DEFER_N_MIN, DEFER_N_MAX], never trusted raw
    assert _event_verdict(
        {"initiate": "defer", "reason": "r", "turns": 99}
    )["defer_turns"] == 4
    assert _event_verdict(
        {"initiate": "defer", "reason": "r", "turns": 0}
    )["defer_turns"] == 1
    # a bool is not a turn count (True is an int in Python)
    assert "defer_turns" not in _event_verdict(
        {"initiate": "defer", "reason": "r", "turns": True}
    )
    # turns on a non-defer is meaningless and dropped
    assert "defer_turns" not in _event_verdict(
        {"initiate": "yes", "reason": "r", "turns": 3}
    )


def test_the_pre_tristate_shape_still_parses_for_replay():
    # Recorded verdicts predate the tri-state; a bool leaves `action` alone
    # rather than deriving one, so old rows replay byte-identically.
    v = _event_verdict({"initiate": True, "reason": "r"})
    assert v == {"initiate": True, "reason": "r", "action": None}
    v = _event_verdict({"initiate": False, "reason": "r", "action": "defer"})
    assert v["action"] == "defer" and v["initiate"] is False


# the heads-up


def test_heads_up_is_queued_ahead_of_the_window(tmp_path):
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(19.0, 21.0),)))
    clock = VirtualClock(t_h=18.9)      # inside the lead, before the window
    client = FakeClient(responses=[
        'tool_decide_event: {"message": "gym in a bit"}',
        "reply",
    ])
    session = make_session(store, client=client, clock=clock,
                           decision_config=DecisionConfig())
    session.on_message("hey")

    rows = _popups(store)
    assert len(rows) == 1
    assert "heads_up" in rows[0]["payload_json"]
    # stamped at the lead instant, not at `now`
    assert abs(rows[0]["t_h"] - (19.0 - HEADS_UP_LEAD_H)) < 1e-9
    # a mention, no verdict: the item is untouched and still planned
    assert [it.status for it in store.list_agenda_items(day=0)] == ["planned"]
    store.close()


def test_no_heads_up_for_a_window_that_already_opened(tmp_path):
    # A lead instant for an event that already started is nothing to warn
    # about. Only the start (and end) boundaries are queued.
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0),)))
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": "no", "reason": "not now"}',
        "reply",
    ])
    session = make_session(store, client=client, clock=clock,
                           decision_config=DecisionConfig())
    session.on_message("hey")

    states = [r["payload_json"] for r in _popups(store)]
    assert not any("heads_up" in p for p in states)
    assert any('"state": "start"' in p or "'state': 'start'" in p
               for p in states)
    store.close()


def test_nothing_decides_between_the_heads_up_and_the_window():
    # The heads-up moves the phase to DECIDE, but the window is not open
    # yet: no leg is due, and the companion turns he spends being warned are
    # NOT deducted from the turns she gets to decide in.
    st = NegotiationState(
        item_id="ag1", activity="gym", source_type="arc",
        start_t_h=19.0, end_t_h=21.0, salience=0.8,
        phase="decide", informed=True, turns_to_decide=2,
    )
    assert decide_status_at(st, now=18.9, companion_turn=True) == "waiting"
    assert st.turns_to_decide == 2          # not spent
    assert decide_status_at(st, now=19.01, companion_turn=True) == "waiting"
    assert st.turns_to_decide == 1          # inside the window it counts


# replay: original time, original order


def test_past_boundaries_replay_in_the_order_they_happened(tmp_path):
    """A resume mid-day: two windows already passed. Each pop-up is stamped
    with its OWN boundary and they queue in chronological order across
    items, not per-item (which used to interleave A-start, A-end, B-start).
    """
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (
        _item(7.0, 9.0, "morning coffee", "ag-coffee"),
        _item(8.0, 12.0, "sketching", "ag-sketch"),
    )))
    clock = VirtualClock(t_h=18.7)
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": "yes", "reason": "had it"}',
        'tool_decide_event: {"initiate": "yes", "reason": "drew a bit"}',
        "reply",
    ])
    session = make_session(store, client=client, clock=clock,
                           decision_config=DecisionConfig())
    session.on_message("hey, sorry — long day")

    rows = _popups(store)
    stamps = [round(r["t_h"], 4) for r in rows]
    # coffee start 7.0, sketch start 8.0, coffee end 9.0, sketch end 12.0 —
    # chronological across BOTH items, and none stamped 18.7.
    assert stamps == sorted(stamps)
    assert stamps == [7.0, 8.0, 9.0, 12.0]
    assert 18.7 not in stamps

    # Only the two START boundaries asked anything; both ends were silent.
    records = store.decisions_for_day(0)
    assert len(records) == 2
    assert [round(r["t_h"], 4) for r in records] == [7.0, 8.0]
    # the model was shown the ORIGINAL time, not the drain instant
    assert records[0]["time"] == "7.0"
    store.close()


def test_end_boundary_never_calls_the_model(tmp_path):
    """The window resolves server-side (planned -> completed) with no
    verdict and no captured "outcome" text.
    """
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (_item(9.0, 11.0, "pottery"),)))
    clock = VirtualClock(t_h=9.5)
    # ONE generation answers the pop-up AND speaks: the verdict is a tool call
    # in the mainline context, so the canned order is one response per turn.
    client = FakeClient(responses=[
        'tool_decide_event: {"initiate": "yes", "reason": "in the mood"}',
    ])
    session = make_session(store, client=client, clock=clock,
                           decision_config=DecisionConfig())
    session.on_message("hey")

    clock.advance_hours(2.0)            # 11:30 — the window closed
    client.responses.append("reply two")
    result = session.on_message("still there?")

    assert result.reply == "reply two"  # no pop-up consumed it
    assert not client.responses
    # exactly one decision for the item, at its start
    records = store.decisions_for_day(0)
    assert len(records) == 1
    assert round(records[0]["t_h"], 4) == 9.0
    item = store.list_agenda_items(day=0)[0]
    assert item.status == "completed"   # resolved by the passed window
    assert item.outcome is None         # no rationale masquerading as one
    store.close()
