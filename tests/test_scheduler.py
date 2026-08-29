"""Scheduler + proactive firing tests (W-E2)."""

import numpy as np
import pytest

import sim.run_events as run_events
from engine.types import ADJ_SLOPE, PersonaParams, TimingParams
from engine.circadian import envelope
from harness.clock import VirtualClock
from harness.client import FakeClient
from harness.judge import ScriptedJudge
from harness.scheduler import (
    COUNT_DIVERGENCE_MIN,
    GAP_DIVERGENCE_MIN,
    INITIATIVE_BOUNDS,
    REASON_SCHEDULE,
    STATE_FACTOR_BOUNDS,
    STATE_NEUTRAL,
    STATE_VECTOR_NAMES,
    STATE_WEIGHTS,
    ProactiveSchedule,
    adj_from_score,
    build_opportunity,
    day_scores,
    initiative_factor,
    plan_proactive_events,
    state_factor,
    state_factors_for_plan,
    state_vector,
    structured_no_state_claim,
    structured_no_state_timing_check,
)
from harness.session import Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
SEED = 777


def test_plan_is_deterministic():
    a = plan_proactive_events(30, SEED, PERSONA, TIMING)
    b = plan_proactive_events(30, SEED, PERSONA, TIMING)
    assert np.array_equal(a, b)


def test_plan_respects_horizon_and_quiet_hours():
    days = 60
    events = plan_proactive_events(days, SEED, PERSONA, TIMING)
    assert len(events) > 0
    assert events[0] >= 0.0
    assert events[-1] < days * 24.0
    for t in events:
        assert envelope(t % 24.0, TIMING) >= 1e-9, f"event in quiet hours at {t % 24:.2f}h"


def test_plan_respects_daily_cap():
    days = 90
    events = plan_proactive_events(days, SEED, PERSONA, TIMING)
    day_counts = {}
    for t in events:
        day = int(t // 24.0)
        day_counts[day] = day_counts.get(day, 0) + 1
    assert max(day_counts.values()) <= TIMING.daily_cap


def test_plan_daily_rate_sane():
    days = 90
    events = plan_proactive_events(days, SEED, PERSONA, TIMING)
    rate = len(events) / days
    assert 0.5 <= rate <= 4.0


def test_schedule_bookkeeping():
    schedule = ProactiveSchedule(event_hours=np.asarray([5.0, 10.0, 20.0]))
    assert schedule.due_at(6.0) == [5.0]
    schedule.mark_fired(5.0)
    assert schedule.due_at(30.0) == [10.0, 20.0]
    assert schedule.next_pending(6.0) == 10.0
    schedule.mark_fired(10.0)
    assert schedule.due_at(30.0) == [20.0]
    # A7: an overdue PENDING event is visible, not skipped.
    assert schedule.next_pending(30.0) == 20.0
    schedule.mark_fired(20.0)
    assert schedule.next_pending(30.0) is None


# --------------------------------------------------------------------------- #
# A7 restart regression: next_pending must surface overdue pending events
# --------------------------------------------------------------------------- #


def _schedule(*hours):
    return ProactiveSchedule(event_hours=np.asarray(list(hours), dtype=float))


def test_next_pending_visible_at_exact_event_time():
    # Restart exactly AT the event: now == event_time ⇒ it MUST be visible.
    schedule = _schedule(10.0)
    assert schedule.next_pending(10.0) == 10.0


def test_next_pending_visible_ten_minutes_after():
    # Restart 10 min after the event: overdue, must still be returned.
    schedule = _schedule(10.0)
    assert schedule.next_pending(10.0 + 10.0 / 60.0) == 10.0


def test_next_pending_overdue_within_validity_window():
    # An overdue event inside its validity window is returned (and the
    # runtime fires it); the next future event is NOT preferred over it.
    schedule = _schedule(10.0, 14.0)
    assert schedule.next_pending(12.0) == 10.0


def test_next_pending_overdue_beyond_validity_window():
    # Even far beyond the validity window the row is surfaced (the runtime
    # decides fire-vs-expire from the validity window, not next_pending).
    schedule = _schedule(10.0, 30.0)
    assert schedule.next_pending(48.0) == 10.0


def test_next_pending_multiple_overdue_earliest_first():
    # Several overdue pending events → earliest is returned first, then the
    # next, and a future event only after all overdue ones are consumed.
    schedule = _schedule(10.0, 11.0, 20.0)
    assert schedule.next_pending(15.0) == 10.0
    schedule.mark_fired(10.0)
    assert schedule.next_pending(15.0) == 11.0
    schedule.mark_fired(11.0)
    assert schedule.next_pending(15.0) == 20.0


def test_next_pending_fired_rows_never_returned():
    schedule = _schedule(10.0, 12.0)
    schedule.mark_fired(10.0)
    assert schedule.next_pending(11.0) == 12.0  # overdue fired row skipped
    schedule.mark_fired(12.0)
    assert schedule.next_pending(20.0) is None


def test_restore_keeps_overdue_rows_pending(tmp_path):
    # restore() seeds _fired only from non-pending rows — overdue pending
    # rows survive a restart and are surfaced by next_pending (the bug).
    store = SQLiteStore(tmp_path / "s.db")
    try:
        store.save_schedule_events(SEED, [
            {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        ])
        restored = ProactiveSchedule.restore(SEED, store)
        assert restored.next_pending(10.0) == 10.0
        assert restored.next_pending(10.2) == 10.0
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# A7 timing feedback: A(score_{d-1}) · I(t) enters the hazard
# --------------------------------------------------------------------------- #


def test_a_mapping_is_monotone_and_bounded():
    # A(s) = adj_from_score: worse score ⇒ lower adjustment, better ⇒ higher.
    assert adj_from_score(-1.0, TIMING) < adj_from_score(0.0, TIMING) < adj_from_score(1.0, TIMING)
    for s in (-2.0, -1.0, 0.0, 1.0, 2.0):
        assert TIMING.adj_bounds[0] <= adj_from_score(s, TIMING) <= TIMING.adj_bounds[1]


def test_initiative_factor_neutral_monotone_bounded():
    assert initiative_factor(0.5) == pytest.approx(1.0)
    assert initiative_factor(0.2) < 1.0 < initiative_factor(0.8)
    lo, hi = INITIATIVE_BOUNDS
    assert lo <= initiative_factor(0.0) <= hi
    assert lo <= initiative_factor(1.0) <= hi


def test_worse_previous_day_score_lowers_hazard():
    # The full A7 formula: plan the same days with effective scores built
    # from a bad vs a good previous-day judgement — fewer accepted events.
    days = 150
    lo = np.full(days, (adj_from_score(-0.9, TIMING) - 1.0) / ADJ_SLOPE)
    hi = np.full(days, (adj_from_score(0.9, TIMING) - 1.0) / ADJ_SLOPE)
    events_lo = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=lo)
    events_hi = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=hi)
    assert len(events_lo) < len(events_hi)


def test_higher_initiative_raises_hazard():
    days = 150
    low_i = np.full(days, (initiative_factor(0.2) - 1.0) / ADJ_SLOPE)
    high_i = np.full(days, (initiative_factor(0.8) - 1.0) / ADJ_SLOPE)
    events_low = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=low_i)
    events_high = plan_proactive_events(days, SEED, PERSONA, TIMING, scores=high_i)
    assert len(events_low) < len(events_high)


def _daily_row(day, *, M=6, mu=0.0, eta=0.0, phase_label="phase_a"):
    return {
        "day": day, "M": M, "m": 0.0, "g": 0.7, "p": 0.5, "arg": 0.0,
        "mu": mu, "eta": eta, "cycle_day": float(day), "phase_label": phase_label,
        "seed": SEED, "score": None,
    }


def test_day_scores_use_previous_day_judgement_and_initiative():
    from tests.helpers import SeamStore

    def scores_for(score_prev, initiative_day):
        store = SeamStore()
        store.save_daily_state(0, _daily_row(0))
        store.save_daily_state(1, _daily_row(1))
        if score_prev is not None:
            store.save_judgement(0, score_prev, "j", None, shadow=True)
        # day_initiative reads the stored directive; override by writing the
        # derived directive's initiative back into a fixed M via derive.
        # Simpler: drive initiative through the day-1 record's mood (M) and
        # previous record (momentum) — higher M ⇒ higher initiative.
        return day_scores(store, 1, TIMING)[0]

    low = scores_for(-0.9, None)
    high = scores_for(0.9, None)
    assert low < high  # better previous day ⇒ larger effective score

    neutral = scores_for(0.0, None)
    # Monotone in the judgement across the whole range.
    assert low < neutral < high


def test_day_scores_missing_judgement_falls_back_neutral():
    from tests.helpers import SeamStore
    store = SeamStore()
    store.save_daily_state(0, _daily_row(0))
    store.save_daily_state(1, _daily_row(1))
    scores = day_scores(store, 1, TIMING)
    assert len(scores) == 2
    # No judgement: A ≡ 1.0; day 1 initiative ≈ neutral-ish ⇒ effective ≈ 0.
    assert scores[0] == pytest.approx(0.0, abs=0.35)


def test_day_scores_shape_covers_current_day_only():
    from tests.helpers import SeamStore
    store = SeamStore()
    store.save_daily_state(0, _daily_row(0))
    store.save_daily_state(1, _daily_row(1))
    store.save_daily_state(2, _daily_row(2))
    store.save_judgement(0, 0.3, "j", None, shadow=True)
    store.save_judgement(1, -0.2, "j", None, shadow=True)
    scores = day_scores(store, 2, TIMING)
    assert scores.shape == (3,)
    # entries for judged days are real; the current day is a placeholder
    assert scores[0] != 0.0 and scores[1] != 0.0
    assert scores[2] == 0.0


def test_fire_proactive_creates_proactive_message(tmp_path):
    from engine.types import MoodVariant
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=["proactive hello!"])
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=client,
        clock=VirtualClock(t_h=10.0),
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    result = session.fire_proactive()
    assert result.reply == "proactive hello!"
    msgs = store.messages_for_day(0)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["proactive"] == 1
    # fresh transcript → system-only payload; no trailing user request
    last_call = client.calls[-1]
    assert last_call["messages"][-1]["role"] == "system"
    assert "reaching out first" in last_call["system"]


def test_fire_proactive_validates_reason(tmp_path):
    from engine.types import MoodVariant
    store = SQLiteStore(tmp_path / "s.db")
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=FakeClient(),
        clock=VirtualClock(t_h=10.0),
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    with pytest.raises(ValueError, match="reason"):
        session.fire_proactive(reason="nagging")


def test_session_with_schedule_end_to_end(tmp_path):
    """Clock advance → due proactive events fire in order."""
    from engine.types import MoodVariant
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=[f"proactive #{i}" for i in range(20)])
    clock = VirtualClock(t_h=8.0)
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    schedule = ProactiveSchedule.plan(5, SEED, PERSONA, TIMING)
    # advance through day 0: fire whatever is due
    clock.advance_hours(16.0)  # to 24:00
    due = schedule.due_at(clock.now_h())
    for t in due:
        if t > clock.now_h():
            clock.advance_hours(t - clock.now_h())
        session.fire_proactive()
        schedule.mark_fired(t)
    assert len(due) > 0
    proactives = store.conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE proactive = 1"
    ).fetchone()["n"]
    assert proactives == len(due)
    store.close()


# --------------------------------------------------------------------------- #
# B5 — latent state → timing coupling (iteration-3, closes F4)
# --------------------------------------------------------------------------- #
# The day's state vector (E, S, R, A) is derived from the day's
# BehaviorDirective via the PATCHABLE harness.session seam; the preregistered
# weights STATE_WEIGHTS turn it into a per-day multiplicative factor
# exp(w·(x − x₀)) that rides the run_events modulator. Under STRUCTURED_NO_STATE's
# neutral-directive patch the vector collapses to STATE_NEUTRAL and the term
# to exactly 1.0 — the ablation finally reaches the timing channel.

B5_SEEDS = (5001, 5002, 5003, 5004, 5005)
#: 90 days ≈ three full 28-day cycles — enough to sample the mood/cycle
#: extremes the coupling operates on (the 30-day matrix cells are a subset;
#: the mechanism is identical, the estimates are stabler).
B5_DAYS = 90
B5_PHASES = ("menstrual", "follicular", "ovulatory", "luteal_early", "luteal_late")


def _stateful_rows(days: int, seed: int) -> dict[int, dict]:
    """Deterministic daily rows with day-to-day latent-state variation:
    mood M wanders 2..10, hormonal gain g 0.7..1.0, real cycle phases."""
    rows = {}
    for d in range(days):
        rows[d] = {
            "day": d,
            "M": 2 + (d * 7 + seed * 3) % 9,
            "m": 0.0,
            "g": 0.7 + 0.05 * ((d * 3 + seed) % 7),
            "p": 0.5,
            "arg": 0.0,
            "mu": 0.0,
            "eta": 0.0,
            "cycle_day": float(d % 28),
            "phase_label": B5_PHASES[(d // 6) % 5],
            "seed": seed,
            "score": None,
        }
    return rows


def _store_with_state(days: int, seed: int):
    from tests.helpers import SeamStore

    store = SeamStore()
    for d, row in _stateful_rows(days, seed).items():
        store.save_daily_state(d, row)
    return store


def _engine_store(days: int, seed: int):
    """A store populated with REAL engine day records (sim.run_daily —
    deterministic, engine.rng only: init_rng + day_rng, the reserved streams)."""
    from sim.run_daily import run
    from engine.types import MoodVariant
    from tests.helpers import SeamStore

    result = run(days, seed, MoodVariant.DECOUPLED_OFFSETS, PERSONA)
    store = SeamStore()
    for r in result.records:
        store.save_daily_state(r.t, {"day": r.t, "M": r.M, "m": r.m, "g": r.g,
                                     "p": r.p, "arg": r.arg, "mu": r.mu,
                                     "eta": r.eta, "cycle_day": r.cycle_day,
                                     "phase_label": r.phase_label,
                                     "seed": r.seed, "score": None})
    return store


def _plan_condition(days: int, seed: int, store, *, neutral: bool) -> np.ndarray:
    """Plan with the B5 coupling under FULL or under STRUCTURED_NO_STATE.

    Mirrors the runtime path exactly: effective scores from ``day_scores``
    (whose initiative fold resolves through the PATCHABLE session seam) plus
    the state factors from ``state_factors_for_plan`` (same seam). Under
    ``neutral`` the eval harness's neutral-directive patch is applied, so
    both the state vector AND the A·I fold collapse to their neutral values.
    """
    from experiments.cvs_common import _neutral_behavior
    import harness.session as session_mod

    original = session_mod.derive_behavior
    try:
        if neutral:
            session_mod.derive_behavior = _neutral_behavior
        scores = day_scores(store, days - 1, TIMING)
        factors = state_factors_for_plan(store, days, TIMING)
    finally:
        session_mod.derive_behavior = original
    return plan_proactive_events(days, seed, PERSONA, TIMING,
                                 scores=scores, state_factors=factors)


def test_state_vector_maps_directive_channels():
    store = _store_with_state(3, SEED)
    v = state_vector(store, 0, TIMING)
    assert dict(zip(STATE_VECTOR_NAMES, v)) == {
        "energy": v[0], "initiative": v[1], "valence": v[2], "reactivity": v[3],
    }
    assert len(STATE_WEIGHTS) == len(STATE_NEUTRAL) == len(v) == 4
    # All channels are directive channels in [0,1] except valence ∈ [-1,1].
    assert 0.0 <= v[0] <= 1.0 and 0.0 <= v[1] <= 1.0
    assert -1.0 <= v[2] <= 1.0 and 0.0 <= v[3] <= 1.0


def test_state_vector_missing_state_is_neutral():
    store = _store_with_state(1, SEED)  # only day 0
    assert state_vector(store, 5, TIMING) == STATE_NEUTRAL
    assert state_factor(store, 5, TIMING) == 1.0


def test_state_factor_low_vs_high_state_day():
    store = _engine_store(B5_DAYS, SEED)

    def _m(day: int) -> int:
        row = store.load_daily_state(day)
        assert row is not None
        return int(row["M"])

    lows = [d for d in range(B5_DAYS) if _m(d) <= 3]
    highs = [d for d in range(B5_DAYS) if _m(d) >= 8]
    assert lows and highs, "engine records must contain low and high mood days"
    lo = min(state_factor(store, d, TIMING) for d in lows)
    hi = max(state_factor(store, d, TIMING) for d in highs)
    assert lo < 1.0 < hi
    assert STATE_FACTOR_BOUNDS[0] <= lo <= STATE_FACTOR_BOUNDS[1]
    assert STATE_FACTOR_BOUNDS[0] <= hi <= STATE_FACTOR_BOUNDS[1]
    # The mapping is deterministic.
    assert state_factor(store, lows[0], TIMING) == state_factor(store, lows[0], TIMING)


def test_state_factors_flat_under_structured_no_state_patch():
    """The neutral directive patch collapses the term to EXACTLY 1.0 every
    day — this is what makes STRUCTURED_NO_STATE ablate the timing channel."""
    from experiments.cvs_common import _neutral_behavior
    import harness.session as session_mod

    store = _engine_store(B5_DAYS, SEED)
    original = session_mod.derive_behavior
    try:
        session_mod.derive_behavior = _neutral_behavior
        factors = state_factors_for_plan(store, B5_DAYS, TIMING)
    finally:
        session_mod.derive_behavior = original
    assert factors.shape == (B5_DAYS,)
    assert np.all(factors == 1.0)


def test_day_scores_fold_neutral_under_no_state_patch():
    """day_initiative resolves through the same patched seam, so the A·I
    timing fold ablates with the state vector under STRUCTURED_NO_STATE."""
    from experiments.cvs_common import _neutral_behavior
    import harness.session as session_mod

    store = _engine_store(3, SEED)
    original = session_mod.derive_behavior
    try:
        session_mod.derive_behavior = _neutral_behavior
        scores = day_scores(store, 2, TIMING)
    finally:
        session_mod.derive_behavior = original
    # No judgements ⇒ A ≡ 1; neutral directive ⇒ I ≡ 1; effective score ≡ 0.
    assert scores[0] == pytest.approx(0.0, abs=1e-9)
    assert scores[1] == pytest.approx(0.0, abs=1e-9)


def test_state_coupling_off_by_default_byte_identical():
    events_default = plan_proactive_events(B5_DAYS, SEED, PERSONA, TIMING)
    events_ones = plan_proactive_events(
        B5_DAYS, SEED, PERSONA, TIMING, state_factors=np.ones(B5_DAYS)
    )
    assert np.array_equal(events_default, events_ones)


def test_state_coupling_multiplies_hazard():
    low = np.full(B5_DAYS, 0.6)
    high = np.full(B5_DAYS, 1.6)
    events_low = plan_proactive_events(B5_DAYS, SEED, PERSONA, TIMING, state_factors=low)
    events_high = plan_proactive_events(B5_DAYS, SEED, PERSONA, TIMING, state_factors=high)
    assert len(events_low) < len(events_high)
    assert np.diff(events_low).mean() > np.diff(events_high).mean()


def test_state_factors_shape_validated():
    with pytest.raises(ValueError, match="state_factors"):
        run_events.run(B5_DAYS, SEED, state_factors=np.ones(B5_DAYS + 1))


def test_plan_and_persist_applies_state_coupling():
    """The store-backed live path couples state by default: populated store ⇒
    real factors and the 'state' hazard component; empty store ⇒ all-1.0
    factors (byte-identical event hours)."""
    store = _engine_store(B5_DAYS, SEED)
    sched = ProactiveSchedule.plan_and_persist(B5_DAYS, SEED, PERSONA, TIMING, store)
    assert sched.opportunities
    opp = next(iter(sched.opportunities.values()))
    assert "state" in opp.hazard_components
    assert STATE_FACTOR_BOUNDS[0] <= opp.hazard_components["state"] <= STATE_FACTOR_BOUNDS[1]

    empty = _store_with_state(0, SEED)
    sched_empty = ProactiveSchedule.plan_and_persist(B5_DAYS, SEED, PERSONA, TIMING, empty)
    assert np.array_equal(sched_empty.event_hours,
                          plan_proactive_events(B5_DAYS, SEED, PERSONA, TIMING))


def test_build_opportunity_without_state_is_byte_identical():
    opp = build_opportunity(
        10.0, day=0, phase_label="follicular", timing=TIMING,
        previous_score=None, initiative=0.5,
    )
    assert "state" not in opp.hazard_components
    assert set(opp.hazard_components) == {"base", "circadian", "phase",
                                          "initiative", "prior_score"}


def test_structured_no_state_ablates_timing_across_five_seeds():
    """B5 acceptance: STRUCTURED_NO_STATE vs FULL (real engine state, live
    runtime composition, 90 days × 5 seeds) diverges in proactive count
    (mean >= COUNT_DIVERGENCE_MIN, every seed above a 5% floor, FULL always
    above NO_STATE) AND inter-contact mean gap (mean >= GAP_DIVERGENCE_MIN)."""
    count_divs = []
    gap_divs = []
    for seed in B5_SEEDS:
        store = _engine_store(B5_DAYS, seed)
        full = _plan_condition(B5_DAYS, seed, store, neutral=False)
        no_state = _plan_condition(B5_DAYS, seed, store, neutral=True)
        assert len(full) > 0 and len(no_state) > 0
        assert len(full) > len(no_state), (
            f"seed {seed}: FULL ({len(full)}) must exceed NO_STATE ({len(no_state)})"
        )
        count_divs.append((len(full) - len(no_state)) / len(full))
        mean_gap_full = float(np.diff(full).mean())
        mean_gap_ns = float(np.diff(no_state).mean())
        gap_divs.append(abs(mean_gap_ns - mean_gap_full) / mean_gap_full)
        assert count_divs[-1] >= 0.05, (
            f"seed {seed}: count divergence {count_divs[-1]:.3f} below floor"
        )
    assert np.mean(count_divs) >= COUNT_DIVERGENCE_MIN, (
        f"mean count divergence {np.mean(count_divs):.3f} < "
        f"{COUNT_DIVERGENCE_MIN}"
    )
    assert np.mean(gap_divs) >= GAP_DIVERGENCE_MIN, (
        f"mean gap divergence {np.mean(gap_divs):.3f} < {GAP_DIVERGENCE_MIN}"
    )


def test_structured_no_state_claim_check_logic():
    """The G2 claim: passes on a divergent pair, fails on an identical pair,
    and falls back to the count leg when too few gaps are available."""
    from harness.domain import AblationClaim

    claim = structured_no_state_claim()
    assert isinstance(claim, AblationClaim)
    assert claim.condition == "STRUCTURED_NO_STATE"
    assert claim.channel == "timing"
    assert COUNT_DIVERGENCE_MIN == 0.15 and GAP_DIVERGENCE_MIN == 0.10

    full = {"n_proactive": 40, "proactive_times": [10.0, 30.0, 50.0, 70.0, 90.0]}
    cell = {"n_proactive": 30, "proactive_times": [12.0, 36.0, 60.0, 84.0, 108.0]}
    assert claim.check(cell, full) is True
    assert claim.check(full, full) is False

    # Too few gaps on both sides ⇒ the count leg alone decides.
    small_full = {"n_proactive": 4, "proactive_times": [10.0, 30.0, 50.0]}
    small_cell = {"n_proactive": 3, "proactive_times": [12.0, 34.0, 56.0]}
    assert claim.check(small_cell, small_full) is True
    assert claim.check(small_full, small_full) is False

    # The check function itself is importable and stateless.
    assert structured_no_state_timing_check(cell, full) is True
