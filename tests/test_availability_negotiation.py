"""Availability-negotiation scenario tests (G0 contract): the real Session
negotiation state machine and DecisionRunner over scripted scenarios, asserting
agenda status, decision records, exactly-once inform and window termination."""

from __future__ import annotations

import pytest

from harness.negotiation_contract import PULL_PER_DELAY, SHORT_AFK_H
from harness.store import SQLiteStore

from experiments.negotiation_scenarios import (
    CLOSE_REASON_FOLLOWED,
    SCENARIOS,
    run_scenario,
)


def _run(tmp_path, sid: str):
    """Run one scenario on a fresh audit store; returns (result, store). The
    store is left OPEN so the test can query it; the test closes it."""
    store = SQLiteStore(tmp_path / f"{sid}.db", audit_mode=True)
    result = run_scenario(store, SCENARIOS[sid])
    return result, store


def _decide_records(result) -> list[dict]:
    return [r for r in result.decision_records
            if r["inputs"].get("phase") == "decide"]


def _inform_records(result) -> list[dict]:
    return [r for r in result.decision_records
            if r["inputs"].get("phase") == "inform"]


def _forced_records(result) -> list[dict]:
    return [r for r in result.decision_records if r["source"] == "backstop"]


def test_retain_repeated_delay_then_go(tmp_path):
    """User keeps actively talking past the boundary: repeated delay, she stays
    past start_t_h, then goes on a companion turn."""
    result, store = _run(tmp_path, "retain")
    try:
        assert result.agenda_status == "completed"
        assert result.informed is True

        informs = _inform_records(result)
        decides = _decide_records(result)
        assert len(informs) == 1
        assert len(decides) == 4  # 3 delays + go, one per companion turn
        # The heads-up landed BEFORE the window opened; nothing decided there.
        assert informs[0]["t_h"] == pytest.approx(18.95, abs=1e-6)
        assert all(d["t_h"] >= 19.0 for d in decides)

        # Inform: one tool_decide_event record, phase=inform, mention
        inform = informs[0]
        assert inform["popup_kind"] == "tool_decide_event"
        assert inform["inputs"]["phase"] == "inform"
        assert inform["inputs"]["skippable"] is True
        assert inform["inputs"]["state_label"] == "inform"
        assert inform["replay_id"] == "neg-retain-gym-inform"
        mention = inform["verdict"].get("message") or inform["verdict"].get(
            "reason"
        )
        assert "gym" in mention

        # The three delays: server-filled defer_turns, rising delay_count and pull-to-go
        delays = decides[:-1]
        for i, d in enumerate(delays):
            assert d["verdict"]["action"] == "defer"
            assert d["verdict"]["defer_turns"] == 1  # "just a sec" -> N=1
            assert d["inputs"]["delay_count"] == i
            assert d["inputs"]["pull"] == pytest.approx(
                round(i * PULL_PER_DELAY, 4)
            )
            assert d["t_h"] > 19.0
            assert d["replay_id"] == f"neg-retain-gym-decide-{i}"

        # the go lands on his companion turn, not on a bomb
        go = decides[-1]
        assert go["verdict"]["action"] == "follow"
        assert go["replay_id"] == "neg-retain-gym-decide-3"
        assert go["t_h"] == pytest.approx(19.26, abs=1e-6)

        # Graceful close via the existing close path with the distinct reason
        convs = result.conversations
        assert len(convs) == 1
        assert convs[0].close_reason == CLOSE_REASON_FOLLOWED
        assert convs[0].closed_t_h == pytest.approx(go["t_h"], abs=1e-6)
        closes = [e for e in result.audit_events
                  if e["event"] == "conversation_closed"]
        assert len(closes) == 1
        assert "reason=followed_event" in closes[0]["detail"]
        # only the heads-up mention rides the channel; the go closes through the turn
        assert [t for _, t, _ in result.channel_out] == [
            "I've got gym soon — just letting you know"
        ]
    finally:
        store.close()


def test_release_afk_bomb_fires_decide_go(tmp_path):
    """User goes quiet before the window opens: the AFK bomb fires a decide leg,
    then the scripted go."""
    result, store = _run(tmp_path, "release")
    try:
        assert result.agenda_status == "completed"
        informs = _inform_records(result)
        decides = _decide_records(result)
        assert len(informs) == 1
        assert len(decides) == 1
        # the heads-up ran ahead of the window and decided nothing
        assert informs[0]["t_h"] == pytest.approx(18.95, abs=1e-6)
        go = decides[0]
        assert go["verdict"]["action"] == "follow"
        # the AFK bomb instant, armed at the heads-up, not a user turn
        assert go["t_h"] == pytest.approx(18.95 + SHORT_AFK_H, abs=1e-6)
        assert go["t_h"] > 19.0                     # inside the window
        assert go["replay_id"] == "neg-release-gym-decide-0"
        assert go["inputs"]["delay_count"] == 0
        assert len(result.model_calls) == 2  # inform + the decide leg
        convs = result.conversations
        assert convs[0].close_reason == CLOSE_REASON_FOLLOWED
        assert convs[0].closed_t_h == pytest.approx(go["t_h"], abs=1e-6)
    finally:
        store.close()


def test_window_close_forced_skip_recorded(tmp_path):
    """User holds her past end_t_h: forced skip recorded, no model call at or
    after end_t_h."""
    result, store = _run(tmp_path, "window-close")
    try:
        assert result.agenda_status == "skipped"
        recs = result.decision_records
        # heads-up + two decide legs + the backstop record (store seam)
        assert recs[0]["inputs"]["phase"] == "inform"
        assert recs[1]["inputs"]["phase"] == "decide"
        assert recs[2]["inputs"]["phase"] == "decide"
        assert recs[3]["source"] == "backstop"

        # a delay on his in-window turn, then another at the AFK bomb
        assert all(r["verdict"]["action"] == "defer" for r in recs[1:3])
        assert all(r["verdict"]["defer_turns"] == 1 for r in recs[1:3])
        assert recs[1]["t_h"] == pytest.approx(19.05, abs=1e-6)
        assert recs[2]["t_h"] == pytest.approx(19.05 + SHORT_AFK_H, abs=1e-6)

        forced = recs[-1]
        assert forced["source"] == "backstop"
        assert forced["transport"] == "server_draw"
        assert forced["raw_reply"] is None          # the backstop makes no call
        assert forced["verdict"]["forced_skip"] is True
        assert "missed it entirely" in forced["verdict"]["reason"]
        assert forced["verdict"]["action"] == "abandon"
        assert forced["t_h"] == pytest.approx(19.5, abs=1e-6)  # end_t_h
        assert forced["replay_id"] == "neg-window-gym-decide-2"

        # no model call at/after end_t_h; heads-up + the two decide legs
        assert len(result.model_calls) == 3
        assert len(_forced_records(result)) == 1

        # skip: the conversation continues (no close)
        convs = result.conversations
        assert len(convs) == 1
        assert convs[0].close_reason is None

        # loud audit trail: the forced resolution is a state event
        resolved = [e for e in result.audit_events
                    if e["event"] == "negotiation_resolved"]
        assert len(resolved) == 1
        assert "outcome=forced" in resolved[0]["detail"]
    finally:
        store.close()


def test_unskippable_routine_heads_up_deterministic_go(tmp_path):
    """Routine source_type: Inform is a heads-up not a negotiation — Decide
    is offered with skippable=False and the scripted verdicts still go."""
    result, store = _run(tmp_path, "unskippable")
    try:
        assert result.agenda_status == "completed"
        recs = result.decision_records
        assert [r["inputs"]["phase"] for r in recs] == ["inform", "decide"]
        inform, go = recs
        # both phases carry the unskippable flag
        assert inform["inputs"]["skippable"] is False
        assert go["inputs"]["skippable"] is False
        assert go["verdict"]["action"] == "follow"
        assert go["replay_id"] == "neg-class-1-decide-0"
        # deterministic go regardless of the pleading turns: exactly one leg
        assert len(result.model_calls) == 2  # inform + the single decide
        # the heads-up still goes through the channel exactly once
        mentions = [t for k, t, _ in result.channel_out
                    if k == "event_popup" and "class" in t]
        assert len(mentions) == 1
        convs = result.conversations
        assert convs[0].close_reason == CLOSE_REASON_FOLLOWED
    finally:
        store.close()


def test_no_nag_inform_exactly_once_across_delays(tmp_path):
    """Inform emits exactly once; no re-announcement across 3 delays."""
    result, store = _run(tmp_path, "no-nag")
    try:
        assert result.agenda_status == "completed"
        informs = _inform_records(result)
        decides = _decide_records(result)
        # exactly one inform record, on the inform replay id
        assert len(informs) == 1
        assert informs[0]["replay_id"] == "neg-no-nag-gym-inform"

        # exactly one channel message contains the mention
        mention_hits = [t for _, t, _ in result.channel_out if "gym" in t]
        assert len(mention_hits) == 1
        # only the heads-up mention rides the channel; the go is an ordinary reply
        assert len(result.channel_out) == 1  # the mention only

        # 3 delays with rising delay_count, then the go at the 4th decide leg
        # (19.26) — the natural close replaces the reply and closes
        delays = [r for r in decides if r["verdict"].get("action") == "defer"]
        assert len(delays) == 3
        assert [d["inputs"]["delay_count"] for d in delays] == [0, 1, 2]
        assert [d["replay_id"] for d in delays] == [
            f"neg-no-nag-gym-decide-{i}" for i in range(3)
        ]
        assert all(d["verdict"]["defer_turns"] == 1 for d in delays)
        go = [r for r in decides if r["verdict"].get("action") == "follow"]
        assert len(go) == 1
        assert go[0]["replay_id"] == "neg-no-nag-gym-decide-3"
        assert go[0]["t_h"] == pytest.approx(19.26, abs=1e-6)
        convs = result.conversations
        assert convs[0].close_reason == CLOSE_REASON_FOLLOWED
        assert convs[0].closed_t_h == pytest.approx(19.26, abs=1e-6)
    finally:
        store.close()


def test_termination_always_delay_resolves_by_end(tmp_path):
    """No configuration loops forever: an always-delay model still lands on
    a terminal outcome by end_t_h (backstop forced skip, no model call)."""
    result, store = _run(tmp_path, "termination")
    try:
        assert result.agenda_status == "skipped"
        decides = _decide_records(result)
        # bounded: decide legs ran while the model delayed, then the backstop
        assert len(decides) == 5  # 5 delay legs
        assert all(r["verdict"]["action"] == "defer" for r in decides)
        assert all(r["verdict"]["defer_turns"] == 1 for r in decides)
        assert all(r["t_h"] < 20.0 for r in decides)
        assert [d["inputs"]["delay_count"] for d in decides] == [0, 1, 2, 3, 4]

        forced = _forced_records(result)
        assert len(forced) == 1
        forced = forced[0]
        assert forced["source"] == "backstop"
        assert forced["t_h"] == pytest.approx(20.0, abs=1e-6)  # end_t_h
        assert "missed it entirely" in forced["verdict"]["reason"]
        assert forced["verdict"]["action"] == "abandon"
        assert forced["raw_reply"] is None
        assert forced["replay_id"] == "neg-term-gym-decide-5"

        # the model was consulted once per decide leg, all before end_t_h
        assert len(result.model_calls) == 6  # heads-up + 5 decide legs
        resolved = [e for e in result.audit_events
                    if e["event"] == "negotiation_resolved"]
        assert len(resolved) == 1
        assert "outcome=forced" in resolved[0]["detail"]
    finally:
        store.close()


def test_termination_delay_rearm_past_end_resolves_immediately(tmp_path):
    """A delay whose re-arm would land at/after end_t_h resolves immediately
    (clamp): no re-arm past the window, no further model call."""
    result, store = _run(tmp_path, "termination-clamp")
    try:
        assert result.agenda_status == "skipped"
        recs = result.decision_records
        assert recs[0]["inputs"]["phase"] == "inform"
        assert recs[1]["inputs"]["phase"] == "decide"
        assert recs[2]["inputs"]["phase"] == "decide"
        assert recs[3]["source"] == "backstop"

        delay = recs[2]
        assert delay["verdict"]["action"] == "defer"
        assert delay["verdict"]["defer_turns"] == 1
        # the delay itself happened inside the ending window
        assert delay["inputs"]["window_ending"] is True
        assert delay["t_h"] == pytest.approx(19.15, abs=1e-6)

        forced = recs[3]
        assert forced["source"] == "backstop"
        # the delay's re-arm (19.15 + SHORT_AFK_H) lands at/after end 19.3
        assert forced["t_h"] == pytest.approx(19.15, abs=1e-6)
        assert "window closed" in forced["verdict"]["reason"]
        assert forced["verdict"]["forced_skip"] is True
        assert forced["verdict"]["action"] == "abandon"
        assert forced["raw_reply"] is None
        # the clamp resolves under the current delay index; the forced row shares its id
        assert forced["replay_id"] == "neg-clamp-gym-decide-1"

        # no decide at/after end_t_h; heads-up + the two decide legs
        assert len(result.model_calls) == 3
        resolved = [e for e in result.audit_events
                    if e["event"] == "negotiation_resolved"]
        assert len(resolved) == 1
        assert "outcome=forced" in resolved[0]["detail"]
    finally:
        store.close()


def test_scenarios_deterministic_replay(tmp_path):
    """Same seed + fresh store -> byte-identical observable outcome (fixed
    seed, virtual clock — the contract's deterministic-replay floor)."""
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    store1 = SQLiteStore(tmp_path / "d1" / "s.db", audit_mode=True)
    store2 = SQLiteStore(tmp_path / "d2" / "s.db", audit_mode=True)
    try:
        r1 = run_scenario(store1, SCENARIOS["retain"])
        r2 = run_scenario(store2, SCENARIOS["retain"])

        def _seq(result):
            return [
                (rec["replay_id"], round(rec["t_h"], 6), rec["verdict_json"])
                for rec in result.decision_records
            ]

        assert _seq(r1) == _seq(r2)
        assert r1.agenda_status == r2.agenda_status
        assert r1.channel_out == r2.channel_out
    finally:
        store1.close()
        store2.close()
