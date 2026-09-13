"""Proactive scheduler — plan + fire spontaneous messages.

Each planned event hour becomes a :class:`ContactOpportunity` with NO semantic
reason; the runtime resolves a grounded :class:`ProactiveIntent` at opportunity
time. Event times come from sim/run_events' hazard process, modulated per day by
the day's stored judgement and BehaviorDirective.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

import sim.run_events as run_events
from engine.circadian import envelope
from engine.types import ADJ_SLOPE, DayRecord, PersonaParams, TimingParams
from harness.domain import AblationClaim, ContactOpportunity

#: A(s) — the previous-day score adjustment (monotone, bounded; engine-validated).
adj_from_score = run_events.adj_from_score

#: I(i) — initiative factor parameters: r_I = clip(exp(beta·(i-0.5)), *bounds).
INITIATIVE_BETA = 1.2
INITIATIVE_BOUNDS = (0.7, 1.3)

#: Hour at which the day's initiative is sampled from its BehaviorDirective.
INITIATIVE_SAMPLE_HOUR = 14.0

# --------------------------------------------------------------------------- #
#: State-vector channel names (E, S, R, A) in directive-native units.
STATE_VECTOR_NAMES: tuple[str, str, str, str] = (
    "energy", "initiative", "valence", "reactivity",
)
#: Weights for the state-factor exponent w·(x−x₀).
STATE_WEIGHTS: tuple[float, float, float, float] = (1.0, 1.6, 0.7, 0.9)
#: Neutral state vector x₀, where the state-factor exponent is zero.
STATE_NEUTRAL: tuple[float, float, float, float] = (0.5, 0.5, 0.0, 0.5)
#: Bounds clipping the per-day state factor.
STATE_FACTOR_BOUNDS: tuple[float, float] = (0.5, 2.0)
#: Divergence margins used by the timing ablation check.
COUNT_DIVERGENCE_MIN = 0.15
GAP_DIVERGENCE_MIN = 0.10
MIN_GAPS_FOR_GAP_LEG = 3

#: Reason taxonomy: schedule, callback, event, shared_interest, check_in.
REASON_SCHEDULE = "schedule"
REASON_CALLBACK = "callback"
REASON_EVENT = "event"
REASON_SHARED_INTEREST = "shared_interest"
REASON_CHECK_IN = "check_in"
VALID_REASONS = (REASON_SCHEDULE, REASON_CALLBACK, REASON_EVENT,
                 REASON_SHARED_INTEREST, REASON_CHECK_IN)
#: default validity window (hours) after the planned t_h, per reason
REASON_VALIDITY_H = {
    REASON_SCHEDULE: 3.0, REASON_CALLBACK: 6.0, REASON_EVENT: 4.0,
    REASON_SHARED_INTEREST: 12.0, REASON_CHECK_IN: 12.0,
}

#: Hours after desired_t_h that a ContactOpportunity stays valid.
OPPORTUNITY_VALIDITY_H = 3.0

#: Opportunity id derived from the planned event hour.
OPPORTUNITY_ID_FMT = "opp_{t_h:.3f}"


def build_opportunity(
    t_h: float,
    *,
    day: int,
    phase_label: str,
    timing: TimingParams,
    previous_score: float | None,
    initiative: float,
    state_factor: float | None = None,
) -> ContactOpportunity:
    """A ContactOpportunity for a planned event hour — and NOTHING more.

    There is deliberately NO semantic reason here: it is resolved later, at
    opportunity time, into a grounded ProactiveIntent. ``hazard_components``
    reports the multiplicative factors at ``t_h`` (``base`` is 1.0 — the Weibull
    baseline lives inside engine.timing). ``state_factor=None`` omits the
    ``state`` component.
    """
    init_mult = initiative_factor(initiative)
    score_mult = adj_from_score(previous_score, timing)
    components = {
        "base": 1.0,
        "circadian": float(envelope(t_h % 24.0, timing)),
        "phase": float(timing.phase_multipliers[phase_label]),
        "initiative": float(init_mult),
        "prior_score": float(score_mult),
    }
    if state_factor is not None:
        components["state"] = float(state_factor)
    return ContactOpportunity(
        id=OPPORTUNITY_ID_FMT.format(t_h=t_h),
        desired_t_h=float(t_h),
        created_t_h=float(t_h),
        valid_until_t_h=float(t_h) + OPPORTUNITY_VALIDITY_H,
        hazard_components=components,
        initiative_multiplier=float(init_mult),
        previous_score_multiplier=float(score_mult),
    )


def _opportunities_for_plan(
    event_hours: np.ndarray,
    *,
    days: int,
    seed: int,
    persona: PersonaParams,
    timing: TimingParams,
    store,
) -> dict[float, ContactOpportunity]:
    """Map each planned event hour to its ContactOpportunity (deterministic).

    Phase labels come from the same replay contract as run_events (same seed ⇒
    same labels); initiative and previous-day adjustment come from the store,
    missing ⇒ neutral. The individual multipliers are reported, never the
    combined A·I product.
    """
    phase_labels = run_events._precompute_phase_labels(days, seed, persona)
    opps: dict[float, ContactOpportunity] = {}
    for h in event_hours:
        day = int(h // 24.0)
        judgement = store.load_judgement(day - 1)
        previous_score = float(judgement["score"]) if judgement else None
        opps[float(h)] = build_opportunity(
            float(h), day=day, phase_label=phase_labels[day],
            timing=timing, previous_score=previous_score,
            initiative=day_initiative(store, day, timing),
            state_factor=state_factor(store, day, timing),
        )
    return opps


def _persist_opportunities(store, opps: dict[float, ContactOpportunity]) -> None:
    """Persist opportunities via the store's optional
    ``save_contact_opportunity`` seam (skipped when the store exposes none)."""
    save = getattr(store, "save_contact_opportunity", None)
    if save is not None:
        for opp in opps.values():
            save(opp)


def plan_proactive_events(
    days: int,
    seed: int,
    persona: PersonaParams,
    timing: TimingParams,
    scores: np.ndarray | None = None,
    state_factors: np.ndarray | None = None,
) -> np.ndarray:
    """Absolute hours (in [0, days*24)) of accepted proactive events.

    Deterministic given (seed, persona, timing, scores, state_factors).
    ``scores`` / ``state_factors`` are optional per-day arrays; None leaves that
    term uncoupled (tests and legacy callers only).
    """
    return run_events.run(
        days, seed, persona, timing, scores=scores, state_factors=state_factors
    )


def initiative_factor(
    initiative: float,
    *,
    beta: float = INITIATIVE_BETA,
    bounds: tuple[float, float] = INITIATIVE_BOUNDS,
) -> float:
    """I(i) — mechanical initiative multiplier: clip(exp(beta·(i-0.5)), *bounds).

    initiative=0.5 ⇒ 1.0; monotone and bounded.
    """
    return float(np.clip(np.exp(beta * (initiative - 0.5)), *bounds))


def _record_from_row(row: dict) -> DayRecord:
    """Rebuild a DayRecord from a store daily_state row (duplicated from
    session to avoid an import cycle)."""
    return DayRecord(
        t=int(row["day"]),
        m=float(row["m"]),
        g=float(row["g"]),
        arg=float(row["arg"]),
        p=float(row["p"]),
        M=int(row["M"]),
        score=float(row["score"] or 0.0),
        mu=float(row["mu"]),
        eta=float(row["eta"]),
        cycle_day=float(row["cycle_day"]),
        phase_label=row["phase_label"],
        seed=int(row["seed"]),
    )


def _directive_for_day(store, day: int, timing: TimingParams, *,
                       hour: float = INITIATIVE_SAMPLE_HOUR):
    """The day's BehaviorDirective through the PATCHABLE session seam.

    Deferred import (session imports scheduler); in production this resolves to
    ``harness.behavior.derive_behavior``, and a condition patch rebinds it so the
    neutral directive reaches the scheduler too. Missing state ⇒ None.
    """
    row = store.load_daily_state(day)
    if row is None:
        return None
    prev_row = store.load_daily_state(day - 1)
    from harness import session as session_mod  # deferred: session imports scheduler
    return session_mod.derive_behavior(
        _record_from_row(row),
        timing,
        hour=hour,
        previous=_record_from_row(prev_row) if prev_row is not None else None,
    )


def state_vector(store, day: int, timing: TimingParams, *,
                 hour: float = INITIATIVE_SAMPLE_HOUR) -> tuple[float, float, float, float]:
    """The day's latent-state vector x_d = (E, S, R, A).

    E/S/R/A are the directive's energy / initiative / valence / reactivity
    channels. Missing state ⇒ STATE_NEUTRAL (exp term ≡ 1.0).
    """
    directive = _directive_for_day(store, day, timing, hour=hour)
    if directive is None:
        return STATE_NEUTRAL
    return (float(directive.energy), float(directive.initiative),
            float(directive.valence), float(directive.reactivity))


def state_factor(store, day: int, timing: TimingParams, *,
                 hour: float = INITIATIVE_SAMPLE_HOUR) -> float:
    """The per-day multiplicative state term exp(w·(x_d − x₀)), clipped at
    STATE_FACTOR_BOUNDS. Neutral state ⇒ EXACTLY 1.0."""
    x = state_vector(store, day, timing, hour=hour)
    exponent = sum(
        w * (xi - xn) for w, xi, xn in zip(STATE_WEIGHTS, x, STATE_NEUTRAL)
    )
    return float(np.clip(np.exp(exponent), *STATE_FACTOR_BOUNDS))


def state_factors_for_plan(store, days: int, timing: TimingParams) -> np.ndarray:
    """Per-day state factors (days,) for the run_events ``state_factors`` seam.

    factor[d] is day d's OWN state term. A store without state rows yields all
    1.0 (uncoupled composition).
    """
    return np.asarray(
        [state_factor(store, d, timing) for d in range(days)], dtype=float
    )


def day_initiative(store, day: int, timing: TimingParams, *, hour: float = INITIATIVE_SAMPLE_HOUR) -> float:
    """The day's initiative (0..1) from its stored BehaviorDirective; missing
    state degrades to the neutral 0.5."""
    directive = _directive_for_day(store, day, timing, hour=hour)
    if directive is None:
        return 0.5
    return float(directive.initiative)


def day_scores(store, current_day: int, timing: TimingParams) -> np.ndarray:
    """Effective per-day scores array for a plan covering days 0..current_day.

    scores[i] = (A(score_i) · I(i+1) − 1) / ADJ_SLOPE, where score_i is day i's
    stored judge score (missing ⇒ A=1.0) and I(i+1) is day i+1's initiative
    factor. scores[current_day] is an unused placeholder (the engine reads
    scores[day-1]). Entry i is fixed once day i is judged and day i+1's state
    exists, so replans never drift already-persisted rows.
    """
    n = current_day + 1
    scores = np.zeros(n, dtype=float)
    for i in range(current_day):
        judgement = store.load_judgement(i)
        a = adj_from_score(float(judgement["score"]) if judgement else None, timing)
        init = day_initiative(store, i + 1, timing)
        scores[i] = (a * initiative_factor(init) - 1.0) / ADJ_SLOPE
    return scores


@dataclass
class ProactiveSchedule:
    """Planned event times + fire bookkeeping (+ their ContactOpportunities)."""

    event_hours: np.ndarray
    _fired: set[float] = None  # type: ignore[assignment]
    #: ContactOpportunity per planned event hour.
    opportunities: dict[float, ContactOpportunity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self._fired is None:
            self._fired = set()

    @classmethod
    def plan(
        cls,
        days: int,
        seed: int,
        persona: PersonaParams,
        timing: TimingParams,
        scores: np.ndarray | None = None,
        state_factors: np.ndarray | None = None,
    ) -> "ProactiveSchedule":
        return cls(event_hours=plan_proactive_events(
            days, seed, persona, timing, scores, state_factors
        ))

    def opportunity_for(self, t_h: float) -> ContactOpportunity | None:
        """The ContactOpportunity planned for ``t_h``, or None (rows injected
        directly via the store have no opportunity)."""
        return self.opportunities.get(float(t_h))

    def due_at(self, t_h: float) -> list[float]:
        """Planned event hours <= t_h that have not fired yet, ascending."""
        due = [
            float(h) for h in self.event_hours if h <= t_h and h not in self._fired
        ]
        return sorted(due)

    def mark_fired(self, t_h: float) -> None:
        self._fired.add(float(t_h))

    @classmethod
    def plan_and_persist(cls, days, seed, persona, timing, store, *,
                         reason: str = REASON_SCHEDULE,
                         scores=None) -> "ProactiveSchedule":
        """plan() then store.save_schedule_events(seed, [{t_h, day, reason} ...]).
        Idempotent (INSERT OR IGNORE). _fired is pre-seeded from the store: any
        planned hour whose row is no longer 'pending' counts as fired. Each
        planned hour gets a ContactOpportunity (NO semantic reason), persisted
        through the store's optional ``save_contact_opportunity`` seam. The latent
        state is always coupled (``state_factors_for_plan``); a store without
        state rows yields all-1.0 factors.
        """
        schedule = cls.plan(
            days, seed, persona, timing, scores=scores,
            state_factors=state_factors_for_plan(store, days, timing),
        )
        events = [
            {"t_h": float(h), "day": int(h // 24.0), "reason": reason}
            for h in schedule.event_hours
        ]
        store.save_schedule_events(seed, events)
        opps = _opportunities_for_plan(
            schedule.event_hours, days=days, seed=seed, persona=persona,
            timing=timing, store=store,
        )
        _persist_opportunities(store, opps)
        schedule.opportunities = opps
        pending = {float(r["t_h"]) for r in store.pending_schedule_events(seed)}
        schedule._fired = {
            float(h) for h in schedule.event_hours if float(h) not in pending
        }
        return schedule

    @classmethod
    def restore(cls, seed, store) -> "ProactiveSchedule":
        """Rebuild from store: event_hours = all rows' t_h for seed; _fired =
        every row whose status != 'pending'; opportunities from the store's
        optional ``load_contact_opportunities`` seam (empty otherwise). For
        restart-resume without re-planning."""
        rows = store.schedule_events_for_seed(seed)
        event_hours = np.asarray([float(r["t_h"]) for r in rows])
        fired = {float(r["t_h"]) for r in rows if r["status"] != "pending"}
        load_opps = getattr(store, "load_contact_opportunities", None)
        opportunities = {}
        if load_opps is not None:
            opportunities = {
                float(opp.desired_t_h): opp for opp in load_opps()
            }
        return cls(event_hours=event_hours, _fired=fired,
                   opportunities=opportunities)

    def mark_fired_persisted(self, t_h: float, fired_t_h: float, seed: int,
                             store) -> None:
        """self.mark_fired(t_h) + store.mark_schedule_fired(seed, t_h, fired_t_h)."""
        self.mark_fired(t_h)
        store.mark_schedule_fired(seed, t_h, fired_t_h)

    def next_pending(self, t_h: float) -> float | None:
        """Earliest pending event hour due at `t_h`, else the earliest pending
        future hour; None when nothing is pending.

        Pending hours with event_time <= t_h come first, so overdue rows are never
        stranded: the runtime evaluates each (still valid ⇒ fire, past validity ⇒
        expire).
        """
        pending = [float(h) for h in self.event_hours if h not in self._fired]
        overdue = [h for h in pending if h <= t_h]
        if overdue:
            return min(overdue)
        return min(pending) if pending else None


# --------------------------------------------------------------------------- #


def structured_no_state_timing_check(cell_records: dict, full_records: dict) -> bool:
    """Check logic of the STRUCTURED_NO_STATE timing claim.

    Records follow the domain.py convention (at least ``n_proactive``);
    ``proactive_times`` — ascending absolute hours of the condition's proactive
    messages — is read when present. Count leg (binding): |n_cell − n_full| /
    n_full >= COUNT_DIVERGENCE_MIN. Gap leg: |mean_gap_cell − mean_gap_full| /
    mean_gap_full >= GAP_DIVERGENCE_MIN, applied only when BOTH sides provide at
    least MIN_GAPS_FOR_GAP_LEG gaps. The caller aggregates over its seeds before
    calling this check.
    """
    n_cell = int(cell_records["n_proactive"])
    n_full = int(full_records["n_proactive"])
    if n_full <= 0:
        return False
    count_div = abs(n_cell - n_full) / float(n_full)
    if count_div < COUNT_DIVERGENCE_MIN:
        return False
    times_cell = sorted(float(t) for t in cell_records.get("proactive_times", ()))
    times_full = sorted(float(t) for t in full_records.get("proactive_times", ()))
    if len(times_cell) >= MIN_GAPS_FOR_GAP_LEG + 1 and \
            len(times_full) >= MIN_GAPS_FOR_GAP_LEG + 1:
        mean_gap_full = float(np.diff(np.asarray(times_full)).mean())
        if mean_gap_full > 0.0:
            mean_gap_cell = float(np.diff(np.asarray(times_cell)).mean())
            if abs(mean_gap_cell - mean_gap_full) / mean_gap_full < GAP_DIVERGENCE_MIN:
                return False
    return True


def structured_no_state_claim() -> AblationClaim:
    """The AblationClaim for STRUCTURED_NO_STATE (effect-size; tested on the real
    matrix)."""
    return AblationClaim(
        condition="STRUCTURED_NO_STATE",
        channel="timing",
        assertion=(
            "n_proactive differs from FULL by >= 15% and inter-contact "
            "mean-gap divergence >= 10% (when >=3 gaps are available on "
            "both sides)"
        ),
        check=structured_no_state_timing_check,
        min_days=4,  # Score feedback lands no earlier than day 2-3.
    )
