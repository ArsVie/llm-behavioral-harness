"""A1: availability-negotiation phase machine (G0 contract) tests.

Two layers, no LLM anywhere:

* pure-machine tests — the trigger arithmetic, defer(N) mapping, backstop
  clamp, converging pull and the responded-bool Inform marker, all on
  :mod:`harness.negotiation_state` directly;
* session-level tests — the full Inform-once -> Decide-loop wiring through
  :class:`harness.session.Session` with a FAKE DecisionRunner (scripted
  verdicts) over the real SQLiteStore, covering: Inform exactly once
  (value-checked marker, never key presence), go/skip/delay with both
  triggers re-armed per delay, the AFK-bomb release path, the window-close
  backstop (no model call, no re-arm past end_t_h), skippable vs
  unskippable decide requests, deterministic decision ids, restart resume,
  and the runtime park accessor.

Seed 158: the first conversation's closing_tendency draws all sit above the
day's closing tendency, so no incidental conversation close disturbs the
negotiation flow under test.
"""

from __future__ import annotations

import json

import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda
from harness.judge import ScriptedJudge
from harness.negotiation_contract import (
    NegotiationPhase,
    SHORT_AFK_H,
)
from harness.negotiation_state import (
    NegotiationState,
    decide_status_at,
    map_defer_n,
    next_trigger_t_h,
    pull_toward_go,
    rearm_after_delay,
    state_from_dict,
    state_to_dict,
    window_ending_at,
)
from harness.session import Session
from harness.steering import KIND_EVENT_POPUP
from harness.store import SQLiteStore
from harness.tools import DecisionConfig, DecisionResult

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 158


class FakeRunner:
    """Scripted DecisionRunner stand-in: pops verdicts in order; the same
    decision_id ALWAYS replays its first verdict (mirrors DecisionRunner's
    replay-by-decision_id) so idempotency assertions are honest."""

    def __init__(self, verdicts: list[dict]):
        self.verdicts = list(verdicts)
        self.calls: list[dict] = []
        self._replay: dict[str, dict] = {}

    def execute(self, decision_id, popup_kind, inputs, capabilities, call, *,
                day=None, t_h=None, delivered_t_h=None):
        self.calls.append({
            "decision_id": decision_id,
            "popup_kind": popup_kind,
            "inputs": dict(inputs),
            "day": day,
            "t_h": t_h,
            "delivered_t_h": delivered_t_h,
        })
        if decision_id not in self._replay:
            verdict = (
                self.verdicts.pop(0)
                if self.verdicts
                else {"initiate": False, "reason": "", "action": "abandon"}
            )
            self._replay[decision_id] = dict(verdict)
        return DecisionResult(
            decision_id=decision_id,
            popup_kind=popup_kind,
            verdict=self._replay[decision_id],
            source="model",
            transport="textual",
            record_id=None,
        )


def _item(start: float, end: float, *, item_id: str = "ag1",
          activity: str = "gym", source_type: str = "arc",
          salience: float = 0.8) -> AgendaItem:
    return AgendaItem(item_id, start, end, activity, source_type, "src1",
                      salience, "planned")


def _session(tmp_path, *, verdicts: list[dict], clock: VirtualClock,
             seed: int = SEED, agenda=None) -> tuple[Session, SQLiteStore, FakeRunner]:
    store = SQLiteStore(tmp_path / "s.db")
    if agenda is not None:
        store.save_agenda(0, agenda)
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=seed,
        client=FakeClient(responses=["ok"] * 40),  # type: ignore[arg-type]
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )
    runner = FakeRunner(verdicts)
    session._decision = runner  # type: ignore[assignment]
    return session, store, runner


def _st(**overrides) -> NegotiationState:
    base: dict = dict(
        item_id="ag1", activity="gym", source_type="arc",
        start_t_h=9.0, end_t_h=11.0, salience=0.8,
        phase=NegotiationPhase.DECIDE.value,
    )
    base.update(overrides)
    return NegotiationState(**base)


# --------------------------------------------------------------------------- #
# pure machine: defer(N) mapping
# --------------------------------------------------------------------------- #


def test_map_defer_n_patterns_and_clamp():
    assert map_defer_n("a bit longer") == 2
    assert map_defer_n("just a sec") == 1
    assert map_defer_n("just a moment") == 1
    assert map_defer_n("just a minute") == 1
    assert map_defer_n("just one minute") == 2  # no pattern: default
    assert map_defer_n("a few more messages") == 3
    assert map_defer_n("3 more turns") == 3
    assert map_defer_n("three more messages") == 2  # no digit: default
    assert map_defer_n("9 more turns") == 4          # clamped to DEFER_N_MAX
    assert map_defer_n("stay with me") == 2          # default
    assert map_defer_n("") == 2                      # default


# --------------------------------------------------------------------------- #
# pure machine: decide triggers + backstop
# --------------------------------------------------------------------------- #


def test_decide_status_forced_at_window_close():
    st = _st(end_t_h=10.0)
    assert decide_status_at(st, now=10.0, companion_turn=True) == "forced"
    assert decide_status_at(st, now=11.0, companion_turn=False) == "forced"
    assert st.turns_to_decide == 0  # forced: no state mutation


def test_decide_status_turn_trigger_and_decrement():
    st = _st(turns_to_decide=1, afk_deadline_t_h=None)
    # one turn passes without a decide: the counter decrements
    assert decide_status_at(st, now=9.5, companion_turn=True) == "waiting"
    assert st.turns_to_decide == 0
    # the next companion turn (a fresh instant) fires
    assert decide_status_at(st, now=9.6, companion_turn=True) == "due"


def test_decide_status_afk_bomb_and_at_most_once_per_instant():
    st = _st(turns_to_decide=5, afk_deadline_t_h=9.5)
    assert decide_status_at(st, now=9.6, companion_turn=True) == "due"
    st.last_decide_at_t_h = 9.6
    # the SAME instant never fires again (poll-loop guard)
    assert decide_status_at(st, now=9.6, companion_turn=True) == "waiting"
    # a later instant fires again while the bomb stays fired
    assert decide_status_at(st, now=9.7, companion_turn=False) == "due"


def test_decide_status_inactive():
    st = _st(phase=NegotiationPhase.INFORM.value)
    assert decide_status_at(st, now=9.5, companion_turn=True) == "inactive"
    st.phase = NegotiationPhase.DECIDE.value
    st.resolved_t_h = 10.0
    assert decide_status_at(st, now=9.5, companion_turn=True) == "inactive"


def test_rearm_after_delay_both_triggers():
    st = _st()
    ok = rearm_after_delay(st, now=10.0, last_user_turn_t_h=9.9, n=2)
    assert ok
    assert st.delay_count == 1
    assert st.turns_to_decide == 1                      # N-1 turns
    assert st.afk_deadline_t_h == pytest.approx(9.9 + SHORT_AFK_H)


def test_rearm_after_delay_refuses_past_window_close():
    st = _st(end_t_h=11.0)
    # last user turn 10.9 -> bomb at ~11.067 >= end: refused, no re-arm
    ok = rearm_after_delay(st, now=10.9, last_user_turn_t_h=10.9, n=4)
    assert ok is False
    assert st.delay_count == 0
    assert st.afk_deadline_t_h is None
    assert st.turns_to_decide == 0


# --------------------------------------------------------------------------- #
# pure machine: converging pull + window-close flag
# --------------------------------------------------------------------------- #


def test_pull_toward_go_rises_with_delays():
    st = _st(delay_count=0)
    assert pull_toward_go(st) == 0.0
    st.delay_count = 1
    assert pull_toward_go(st) == pytest.approx(0.15)
    st.delay_count = 10
    assert pull_toward_go(st) == 1.0  # capped


def test_window_ending_flag():
    st = _st(end_t_h=11.0)
    assert window_ending_at(st, now=10.0) is False   # 1h left
    assert window_ending_at(st, now=10.9) is True    # ~0.1h left


# --------------------------------------------------------------------------- #
# pure machine: persisted state + responded-bool marker
# --------------------------------------------------------------------------- #


def test_state_roundtrip_and_responded_bool_marker():
    st = _st(informed=True, delay_count=2, turns_to_decide=3,
             afk_deadline_t_h=10.5, last_decide_at_t_h=10.0,
             phase=NegotiationPhase.RESOLVED_GO.value,
             resolved_action="follow", resolved_t_h=10.2)
    st2 = state_from_dict(state_to_dict(st))
    assert st2 is not None
    assert st2.informed is True          # VALUE check
    assert st2.delay_count == 2
    assert st2.turns_to_decide == 3
    assert st2.afk_deadline_t_h == 10.5
    assert st2.phase == NegotiationPhase.RESOLVED_GO.value
    assert st2.resolved_action == "follow"
    # a snapshot WITHOUT the informed key restores False — key absence
    # must never read as informed (the responded-bool discipline)
    bare = state_to_dict(_st(informed=False))
    del bare["informed"]
    st3 = state_from_dict(bare)
    assert st3 is not None and st3.informed is False
    # corrupt snapshots are skipped, never fatal
    assert state_from_dict({"garbage": 1}) is None


def test_next_trigger_t_h_park():
    st = _st(afk_deadline_t_h=9.8, end_t_h=11.0)
    assert next_trigger_t_h(st, 9.5) == pytest.approx(9.8)
    # afk already fired but the window is still open: park at the backstop
    assert next_trigger_t_h(st, 9.9) == pytest.approx(11.0)
    assert next_trigger_t_h(st, 11.5) is None       # window closed
    st2 = _st(afk_deadline_t_h=10.5, end_t_h=11.0)
    assert next_trigger_t_h(st2, 9.5) == pytest.approx(10.5)
    st3 = _st(afk_deadline_t_h=None, end_t_h=11.0)
    assert next_trigger_t_h(st3, 9.5) == pytest.approx(11.0)  # backstop park
    st4 = _st(resolved_t_h=10.0)
    assert next_trigger_t_h(st4, 9.5) is None


# --------------------------------------------------------------------------- #
# session: Inform once -> Decide loop (retain path)
# --------------------------------------------------------------------------- #


def test_inform_once_then_decide_loop_delay_then_go(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "I've got gym soon"},          # inform
        {"initiate": False, "reason": "a bit longer",
         "action": "defer"},                                         # decide 0
        {"initiate": True, "reason": "ok heading out",
         "action": "follow"},                                        # decide 1
    ])

    r1 = session.on_message("hi")                 # opens the conversation
    assert r1.reply == "ok"

    clock.advance_hours(1.5)                      # 9.5: boundary crossed
    r2 = session.on_message("morning")            # start popup -> INFORM
    assert r2.proactive_out == (("event_popup", "I've got gym soon"),)
    st = session._negotiations["ag1"]
    assert st.informed is True
    assert st.phase == NegotiationPhase.DECIDE.value
    assert st.turns_to_decide == 0                # NEXT turn decides
    # the inform turn itself never decides
    assert [c["decision_id"] for c in runner.calls] == [
        "neg-ag1-inform"
    ]
    inform = runner.calls[0]
    assert inform["inputs"]["phase"] == "inform"
    assert inform["inputs"]["skippable"] is True  # arc = discretionary

    clock.advance_hours(0.5)                      # 10.0
    r3 = session.on_message("still here")         # decide leg 0 -> delay
    assert r3.reply == "ok"                       # delay keeps the reply
    st = session._negotiations["ag1"]
    assert st.delay_count == 1
    assert st.turns_to_decide == 1                # "a bit longer" -> N=2
    assert st.afk_deadline_t_h == pytest.approx(10.0 + SHORT_AFK_H)
    assert runner.calls[1]["decision_id"] == "neg-ag1-decide-0"
    decide0 = runner.calls[1]
    assert decide0["inputs"]["phase"] == "decide"
    assert decide0["inputs"]["delay_count"] == 0
    assert decide0["inputs"]["pull"] == 0.0
    assert decide0["inputs"]["window_ending"] is False

    clock.advance_hours(0.3)                      # 10.3 > afk bomb 10.167
    r4 = session.on_message("?")                  # decide leg 1 -> go
    assert r4.reply == ""                         # her close is the ONLY msg
    assert r4.proactive_out == (("event_popup", "ok heading out"),)
    assert session.open_conversation_id() is None
    assert [it.status for it in store.list_agenda_items(0)] == ["completed"]
    assert runner.calls[2]["decision_id"] == "neg-ag1-decide-1"
    assert runner.calls[2]["inputs"]["delay_count"] == 1
    assert runner.calls[2]["inputs"]["pull"] == pytest.approx(0.15)
    st = session._negotiations["ag1"]
    assert st.phase == NegotiationPhase.RESOLVED_GO.value
    assert st.resolved_action == "follow"
    # the graceful close carries the distinct close_reason
    closes = [e for e in store.events_since(0)
              if e["event"] == "conversation_closed"]
    assert closes and "reason=followed_event" in closes[-1]["detail"]
    store.close()


def test_skip_keeps_conversation_open(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
        {"initiate": False, "reason": "skipping for you",
         "action": "abandon"},                                         # decide 0
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform
    clock.advance_hours(0.5)
    r = session.on_message("still here")      # decide 0 -> skip
    assert r.reply == "ok"                    # conversation continues
    assert r.proactive_out == ()
    assert session.open_conversation_id() == "conv-0"
    assert [it.status for it in store.list_agenda_items(0)] == ["skipped"]
    st = session._negotiations["ag1"]
    assert st.phase == NegotiationPhase.RESOLVED_SKIP.value
    store.close()


# --------------------------------------------------------------------------- #
# session: AFK-bomb release path (runtime wake, no turn)
# --------------------------------------------------------------------------- #


def test_release_afk_bomb_fires_decide_go(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
        {"initiate": True, "reason": "I should go",
         "action": "follow"},                                          # decide 0
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform (last user turn 9.5)
    assert session.next_negotiation_trigger_t_h(
        clock.now_h()
    ) == pytest.approx(9.5 + SHORT_AFK_H)
    clock.advance_hours(0.3)                  # 9.8: bomb fired
    outs = session.check_negotiation(clock.now_h())
    assert outs == (("event_popup", "I should go"),)
    assert session.open_conversation_id() is None
    assert [it.status for it in store.list_agenda_items(0)] == ["completed"]
    assert runner.calls[-1]["decision_id"] == "neg-ag1-decide-0"
    # a second wake at the same instant is a no-op (at-most-once)
    outs2 = session.check_negotiation(clock.now_h())
    assert outs2 == ()
    assert len(runner.calls) == 2
    store.close()


# --------------------------------------------------------------------------- #
# session: window-close backstop
# --------------------------------------------------------------------------- #


def test_backstop_forced_skip_no_model_call(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform only
    clock.advance_hours(2.0)                  # 11.5 >= end_t_h
    outs = session.check_negotiation(clock.now_h())
    assert outs == ()
    assert len(runner.calls) == 1  # NO decide model call
    assert [it.status for it in store.list_agenda_items(0)] == ["skipped"]
    st = session._negotiations["ag1"]
    assert st.phase == NegotiationPhase.RESOLVED_FORCED.value
    assert st.resolved_action == "forced"
    recs = store.decisions_for_day(0)
    assert recs[-1]["source"] == "backstop"
    assert recs[-1]["replay_id"] == "neg-ag1-decide-0"
    assert recs[-1]["verdict"]["forced_skip"] is True
    assert "missed it entirely" in recs[-1]["verdict"]["reason"]
    # resolved: no more parks, no more decides
    assert session.next_negotiation_trigger_t_h(clock.now_h()) is None
    store.close()


def test_delay_rearm_past_window_close_resolves_forced(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
        {"initiate": False, "reason": "a bit longer",
         "action": "defer"},                                          # decide 0
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform
    clock.advance_hours(1.4)                  # 10.9: 6 virtual minutes left
    r = session.on_message("please stay")     # decide 0 -> delay refused
    assert r.reply == "ok"                    # ordinary reply proceeds
    st = session._negotiations["ag1"]
    assert st.phase == NegotiationPhase.RESOLVED_FORCED.value
    assert st.delay_count == 0                # NO re-arm happened
    assert st.turns_to_decide == 0
    assert [it.status for it in store.list_agenda_items(0)] == ["skipped"]
    store.close()


# --------------------------------------------------------------------------- #
# session: skippable vs unskippable (routine = heads-up, unskippable)
# --------------------------------------------------------------------------- #


def test_routine_item_unskippable_flag_on_both_phases(tmp_path):
    agenda = DailyAgenda(
        0, (_item(9.0, 11.0, item_id="cls", activity="class",
                  source_type="routine"),)
    )
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "class starts soon"},            # inform
        {"initiate": True, "reason": "going to class",
         "action": "follow"},                                          # decide 0
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform (heads-up)
    assert runner.calls[0]["inputs"]["skippable"] is False
    assert runner.calls[0]["inputs"]["event_id"] == "cls"
    clock.advance_hours(0.5)
    session.on_message("still here")          # decide 0
    assert runner.calls[1]["inputs"]["skippable"] is False
    assert runner.calls[1]["inputs"]["phase"] == "decide"
    assert [it.status for it in store.list_agenda_items(0)] == ["completed"]
    store.close()


# --------------------------------------------------------------------------- #
# session: no open conversation at the boundary -> plain start popup
# --------------------------------------------------------------------------- #


def test_no_conversation_at_boundary_plain_start_popup(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=9.5)             # NO prior conversation
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": True, "reason": "ready to go"},                   # plain
    ])
    r = session.on_message("hello")           # conversation opens NOW
    # the negotiation did not activate (the conversation opened AFTER the
    # boundary) — the existing tool_decide_event semantics run unchanged
    assert "ag1" not in session._negotiations
    assert r.proactive_out == (("event_popup", "ready to go"),)
    assert runner.calls[0]["decision_id"].startswith("steer-")
    store.close()


# --------------------------------------------------------------------------- #
# session: restart resume + no-nag re-delivery
# --------------------------------------------------------------------------- #


def test_restart_resumes_negotiation_and_never_re_informs(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
        {"initiate": False, "reason": "a bit longer",
         "action": "defer"},                                          # decide 0
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform
    clock.advance_hours(0.5)
    session.on_message("still here")          # decide 0 -> delay(2)
    st = session._negotiations["ag1"]
    assert st.delay_count == 1 and st.informed is True
    session.store.close()

    # restart: a fresh session over the same store rebuilds the state
    clock2 = VirtualClock(t_h=10.4)
    store2 = SQLiteStore(tmp_path / "s.db")
    session2 = Session(
        store2, persona=PERSONA, timing=TIMING, variant=VARIANT,
        seed=SEED, client=FakeClient(responses=["ok"]), clock=clock2,
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )
    runner2 = FakeRunner([
        {"initiate": True, "reason": "going now",
         "action": "follow"},                                          # decide 1
    ])
    session2._decision = runner2  # type: ignore[assignment]
    st2 = session2._negotiations["ag1"]
    assert st2.informed is True               # Inform NOT re-fired
    assert st2.delay_count == 1               # decide index continues
    assert st2.phase == NegotiationPhase.DECIDE.value
    assert session2.open_conversation_id() == "conv-0"

    r = session2.on_message("ok go")          # decide leg 1 -> go
    assert r.reply == "" and r.proactive_out == (("event_popup", "going now"),)
    # ONLY the decide-1 leg: the restart never re-ran inform or decide-0
    assert [c["decision_id"] for c in runner2.calls] == [
        "neg-ag1-decide-1"
    ]
    store2.close()


def test_redelivered_start_popup_consumed_no_second_inform(tmp_path):
    """A re-delivered START pop-up for an item whose negotiation exists
    (e.g. an interrupted-turn requeue) is consumed without a model call —
    Inform fires exactly once per event."""
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
        {"initiate": False, "reason": "a bit longer",
         "action": "defer"},                                          # decide 0
    ])
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform (1 call)
    assert len(runner.calls) == 1

    # simulate an interrupted-turn requeue: the START pop-up is pending
    # again and drains at the next boundary
    session._steering.enqueue(
        KIND_EVENT_POPUP,
        {"event_id": "ag1", "event": "gym", "state": "start",
         "time": 9.5, "item_id": "ag1"},
        0, 9.5,
    )
    clock.advance_hours(0.5)
    r = session.on_message("still here")      # decide 0 -> delay
    assert r.reply == "ok"
    ids = [c["decision_id"] for c in runner.calls]
    assert ids == ["neg-ag1-inform", "neg-ag1-decide-0"]  # no 2nd inform
    st = session._negotiations["ag1"]
    assert st.informed is True and st.delay_count == 1
    store.close()


# --------------------------------------------------------------------------- #
# session: park accessor + state events
# --------------------------------------------------------------------------- #


def test_real_runner_pipeline_inform_message_and_defer_turns(tmp_path):
    """A1 + A2 interop through the REAL DecisionRunner: the inform leg
    parses the A2 mention-only verdict (``{message: str}``), the decide leg
    parses the pinned schema, and a defer verdict carries the SERVER-FILLED
    ``defer_turns`` (the model never emits N) — which the session's re-arm
    uses as its N."""
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    store = SQLiteStore(tmp_path / "s.db")
    store.save_agenda(0, agenda)
    client = FakeClient(responses=[
        "ok",                                                    # T1 main
        'tool_decide_event: {"message": "gym starts in a bit"}', # T2 inform
        "ok",                                                    # T2 main
        'tool_decide_event: {"initiate": false, "reason": "a bit longer", '
        '"action": "defer"}',                                    # T3 decide 0
        "ok",                                                    # T3 main
        'tool_decide_event: {"initiate": true, "reason": "going", '
        '"action": "follow"}',                                   # T4 decide 1
    ])
    session = Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT,
        seed=SEED, client=client, clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )  # the REAL runner (no fake)
    session.on_message("hi")
    clock.advance_hours(1.5)
    r = session.on_message("morning")
    assert r.proactive_out == (("event_popup", "gym starts in a bit"),)
    records = store.decisions_for_day(0)
    assert records[-1]["verdict"] == {"message": "gym starts in a bit"}
    clock.advance_hours(0.5)
    session.on_message("still here")          # decide 0 -> defer
    records = store.decisions_for_day(0)
    defer = records[-1]
    assert defer["verdict"]["action"] == "defer"
    assert defer["verdict"]["defer_turns"] == 2   # SERVER-FILLED
    st = session._negotiations["ag1"]
    assert st.delay_count == 1 and st.turns_to_decide == 1
    clock.advance_hours(0.3)                  # past the AFK bomb
    session.on_message("?")                   # decide 1 -> go
    assert session.open_conversation_id() is None
    assert [it.status for it in store.list_agenda_items(0)] == ["completed"]
    store.close()


def test_park_accessor_and_persisted_state_events(tmp_path):
    agenda = DailyAgenda(0, (_item(9.0, 11.0),))
    clock = VirtualClock(t_h=8.0)
    session, store, runner = _session(tmp_path, clock=clock, agenda=agenda, verdicts=[
        {"initiate": False, "reason": "gym's at 9"},                   # inform
    ])
    assert session.next_negotiation_trigger_t_h(clock.now_h()) is None
    session.on_message("hi")
    clock.advance_hours(1.5)
    session.on_message("morning")             # inform
    assert session.next_negotiation_trigger_t_h(
        clock.now_h()
    ) == pytest.approx(9.5 + SHORT_AFK_H)
    # the state snapshots are persisted as state events (restart recovery)
    snaps = [e for e in store.events_since(0)
             if e["event"] == "negotiation_state"]
    assert len(snaps) >= 1
    last = state_from_dict(json.loads(snaps[-1]["detail"]))
    assert last is not None and last.informed is True
    store.close()
