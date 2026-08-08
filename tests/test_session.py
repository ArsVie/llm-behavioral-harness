"""Session e2e loop tests (W-E1).

The critical invariants:
  1. The session's mood sequence replays EXACTLY like `sim.run_daily` for the
     same seed (RNG consumption order is frozen).
  2. Shadow mode records judge scores WITHOUT moving mu; feedback mode moves
     mu by k*(score - neutral) per day.
  3. Days finalize only once; state/messages/judgements are persisted.
  4. Resume restores mu/eta from the latest daily_state and continues.
"""

import math

import numpy as np

import sim.run_daily as run_daily
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.clock import VirtualClock
from harness.client import FakeClient
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345


def _session(tmp_path, *, feedback=False, judge_score=0.5, replies=None, synthetic_score=False):
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    client = FakeClient(responses=replies or ["ok!"])
    judge = ScriptedJudge(score=judge_score)
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client,
        clock=clock,
        judge=judge.judge_day,
        feedback=feedback,
        synthetic_score=synthetic_score,
    )
    return store, clock, client, session


def test_first_message_rolls_over_day_zero(tmp_path):
    store, clock, client, session = _session(tmp_path)
    clock.advance_hours(19.0)
    result = session.on_message("hello there")
    assert result.day == 0
    assert result.reply == "ok!"
    state = store.load_daily_state(0)
    assert state is not None
    assert 0 <= state["M"] <= 10
    msgs = store.messages_for_day(0)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # directive exists and the client saw a system prompt with guidance
    assert "Current behavioral guidance" in client.calls[0]["system"]
    store.close()


def test_replay_matches_run_daily(tmp_path):
    """Session M(t) must equal run_daily M(t) for the same seed (synthetic
    score mode replicates the score RNG draw; judge mode intentionally
    consumes no RNG, so parity is only guaranteed in synthetic mode)."""
    store, clock, client, session = _session(
        tmp_path, feedback=True, synthetic_score=True
    )
    # run 5 days: one message each evening, then advance to next day
    for day in range(5):
        clock.advance_to_day(day)
        clock.advance_hours(19.0)
        session.on_message(f"day {day} message")
        clock.advance_to_day(day + 1)
        session.ensure_day(day + 1)
    expected = run_daily.run(5, SEED, VARIANT, PERSONA).M
    got_values: list[int] = []
    for d in range(5):
        state = store.load_daily_state(d)
        assert state is not None, f"missing daily_state row for day {d}"
        got_values.append(state["M"])
    got = np.asarray(got_values)
    assert np.array_equal(got, expected), f"replay mismatch: {got} vs {expected}"
    store.close()


def test_shadow_mode_does_not_move_mu(tmp_path):
    store, clock, client, session = _session(tmp_path, feedback=False, judge_score=0.9)
    clock.advance_hours(19.0)
    session.on_message("warm message")
    clock.advance_to_day(1)
    session.ensure_day(1)
    judgement = store.load_judgement(0)
    assert judgement is not None
    assert judgement["score"] == 0.9
    assert judgement["shadow"] == 1
    state1 = store.load_daily_state(1)
    assert state1 is not None
    assert state1["mu"] == 0.0  # untouched in shadow mode
    store.close()


def test_feedback_mode_moves_mu(tmp_path):
    store, clock, client, session = _session(tmp_path, feedback=True, judge_score=0.8)
    clock.advance_hours(19.0)
    session.on_message("great day")
    clock.advance_to_day(1)
    session.ensure_day(1)
    state1 = store.load_daily_state(1)
    assert state1 is not None
    # mu' = rho*0 + k*0.8 = 0.144 (k=0.18)
    assert math.isclose(state1["mu"], 0.18 * 0.8, abs_tol=1e-9)
    j0 = store.load_judgement(0)
    assert j0 is not None and j0["shadow"] == 0
    store.close()


def test_no_interaction_day_scores_zero(tmp_path):
    store, clock, client, session = _session(tmp_path, feedback=True)
    clock.advance_hours(19.0)
    session.on_message("hi")
    clock.advance_to_day(3)  # days 1,2 have no messages
    session.ensure_day(3)
    j1 = store.load_judgement(1)
    assert j1 is not None and j1["score"] == 0.0
    assert "no interaction" in j1["justification"]
    store.close()


def test_day_finalized_only_once(tmp_path):
    store, clock, client, session = _session(tmp_path, feedback=True, judge_score=0.3)
    clock.advance_hours(19.0)
    session.on_message("hi")
    clock.advance_to_day(2)
    session.ensure_day(2)
    rows = store.conn.execute("SELECT * FROM judgements").fetchall()
    assert len(rows) == 2  # days 0 and 1, each once
    store.close()


def test_resume_restores_state(tmp_path):
    store, clock, client, session = _session(tmp_path, feedback=True, judge_score=0.6)
    clock.advance_hours(19.0)
    session.on_message("first")
    clock.advance_to_day(1)
    session.ensure_day(1)
    session.on_message("second")
    mu_before = session.mood_state.mu
    eta_before = session.mood_state.eta

    clock2 = VirtualClock(t_h=clock.now_h())
    client2 = FakeClient(responses=["resumed reply"])
    session2 = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client2,
        clock=clock2,
        judge=ScriptedJudge(score=0.6).judge_day,
        feedback=True,
    )
    assert session2.current_day == 1
    assert math.isclose(session2.mood_state.mu, mu_before, abs_tol=1e-12)
    assert math.isclose(session2.mood_state.eta, eta_before, abs_tol=1e-12)
    # continuing produces day 2 rollover only after finalizing day 1
    clock2.advance_to_day(2)
    clock2.advance_hours(19.0)
    result = session2.on_message("third")
    assert result.day == 2
    j1 = store.load_judgement(1)
    assert j1 is not None and j1["score"] == 0.6
    store.close()


def test_resume_across_reopened_store(tmp_path):
    """Review gap 15a: crash-restart flow — a NEW SQLiteStore on the same
    path must resume identically (fresh connection, same file)."""
    path = tmp_path / "s.db"
    store = SQLiteStore(path)
    clock = VirtualClock(t_h=19.0)
    session = Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=FakeClient(responses=["a", "b"]), clock=clock,
        judge=ScriptedJudge(score=0.4).judge_day, feedback=True,
    )
    session.on_message("hi")
    clock.advance_to_day(1)
    session.ensure_day(1)
    mu_before = session.mood_state.mu
    eta_before = session.mood_state.eta
    store.close()

    store2 = SQLiteStore(path)  # fresh connection, same file
    session2 = Session(
        store2, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=FakeClient(responses=["c"]), clock=VirtualClock(t_h=43.0),
        judge=ScriptedJudge(score=0.4).judge_day, feedback=True,
    )
    assert session2.current_day == 1
    assert math.isclose(session2.mood_state.mu, mu_before, abs_tol=1e-12)
    assert math.isclose(session2.mood_state.eta, eta_before, abs_tol=1e-12)
    store2.close()


def test_resume_from_finalized_latest_day(tmp_path):
    """Review gap 15b / finding #1: latest day has a judgement but no
    rollover beyond it (clean shutdown). Resume must re-apply that day's
    end-of-day update so continuation matches a fresh run."""
    store, clock, client, session = _session(tmp_path, feedback=True, judge_score=0.8)
    clock.advance_hours(19.0)
    session.on_message("warm day")
    # Finalize day 0 WITHOUT rolling to day 1 (shutdown path).
    session.finalize_current()
    mu_after = session.mood_state.mu
    eta_after = session.mood_state.eta
    store.close()

    store2 = SQLiteStore(tmp_path / "s.db")
    session2 = Session(
        store2, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=FakeClient(responses=["next"]), clock=VirtualClock(t_h=43.0),
        judge=ScriptedJudge(score=0.8).judge_day, feedback=True,
    )
    assert session2.current_day == 0
    assert math.isclose(session2.mood_state.mu, mu_after, abs_tol=1e-12)
    assert math.isclose(session2.mood_state.eta, eta_after, abs_tol=1e-12)
    # Continuing: rollover to day 1 must use the re-applied state.
    session2.on_message("next day")
    state1 = store2.load_daily_state(1)
    assert state1 is not None
    assert math.isclose(state1["mu"], mu_after, abs_tol=1e-9)
    store2.close()


def test_synthetic_mode_no_interaction_day_parity(tmp_path):
    """Review gap 15e: synthetic mode with a silent day must still replicate
    run_daily (the score draw happens even with no transcript)."""
    store, clock, client, session = _session(
        tmp_path, feedback=True, synthetic_score=True
    )
    clock.advance_hours(19.0)
    session.on_message("day 0")
    clock.advance_to_day(3)  # days 1 and 2 silent
    session.ensure_day(3)
    expected = run_daily.run(3, SEED, VARIANT, PERSONA).M
    got_values: list[int] = []
    for d in range(3):
        state = store.load_daily_state(d)
        assert state is not None
        got_values.append(state["M"])
    assert got_values == list(expected)
    j1 = store.load_judgement(1)
    assert j1 is not None and j1["score"] != 0.0  # synthetic score, not "no interaction"
    store.close()


def test_session_logs_llm_calls(tmp_path):
    """Review gap 15c: session writes to llm_calls."""
    store, clock, client, session = _session(tmp_path)
    clock.advance_hours(19.0)
    session.on_message("hi")
    calls = store.conn.execute("SELECT * FROM llm_calls").fetchall()
    assert len(calls) == 1
    assert calls[0]["prompt_hash"]
    assert calls[0]["response"] == "ok!"
    store.close()


def test_directive_tracks_phase_and_hour(tmp_path):
    store, clock, client, session = _session(tmp_path)
    clock.advance_hours(23.0)  # late night
    result = session.on_message("goodnight")
    assert result.hour == 23.0
    assert result.directive.trace.phase_label in {
        "menstrual", "follicular", "ovulatory", "luteal_early", "luteal_late"
    }
    store.close()
