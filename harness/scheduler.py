"""Proactive scheduler — plan + fire spontaneous messages (W-E2, A7; it2 A3).

The scheduler answers ONLY "should she consider contacting the user now?" —
each planned event hour becomes a :class:`ContactOpportunity` (NO semantic
reason; invariant 3). Semantic motivation is resolved afterward, at
opportunity time, by the runtime's IntentResolver into a grounded
:class:`ProactiveIntent` (invariant 4).

Reuses the PROVEN composition from sim/run_events (envelope × phase × adj,
Weibull hazard + thinning, queue guards) instead of reimplementing the
process. Since A7 the timing feedback is LIVE: the runtime plans only the
CURRENT day with a per-day effective-scores array encoding the previous
day's real judge score and the day's behavioral initiative:

    h(tau,t) = h0(tau) * C(t) * P(t) * A(score_{d-1}) * I(t)

    A(s)   = adj_from_score(s) = clip(1 + ADJ_SLOPE·s, *adj_bounds)
             (the engine's monotone bounded previous-day adjustment)
    I(i)   = initiative_factor(i) = clip(exp(beta·(i-0.5)), *bounds)
             (mechanical multiplier from BehaviorDirective.initiative)
    scores[d-1] = (A(score_{d-1}) * I(d) - 1) / ADJ_SLOPE

so that the engine's own adj_from_score(scores[d-1]) reproduces the product
A(score_{d-1})·I(d) (clipped at adj_bounds). `scores=None` ⇒ adj ≡ 1 is kept
ONLY for tests/legacy callers — live scheduling (runtime._replan) always
passes a concrete array (never None).

B5 (iteration-3, closes F4): the latent state now reaches the hazard as a
preregistered multiplicative term. The store-backed live path
(plan_and_persist) derives the day's state vector x_d = (E, S, R, A) from
the day's BehaviorDirective — resolved through the PATCHABLE harness.session
seam so eval condition patches reach the scheduler — and feeds
exp(w·(x_d − x₀)) (STATE_WEIGHTS, STATE_NEUTRAL) into run_events' new
`state_factors` seam:

    h(tau,t) = h0(tau) * C(t) * P(t) * A(score_{d-1}) * I(d) * exp(w·(x_d − x₀))

Under STRUCTURED_NO_STATE's neutral-directive patch x_d ≡ x₀, so the term
collapses to exactly 1.0 and the timing channel ablates. The seam parameter
defaults to None: every caller that does not opt in is byte-identical
(adj-only composition). The Weibull base h0(τ) is plan-frozen and untouched.

Guards inherited from run_events.run:
  - zero events in quiet hours (envelope = 0 by construction);
  - min gap between accepted events (15 min default);
  - daily cap (3 default);
  - max-gap forcing (48 h) — if the hazard would let silence exceed it, a
    contact is forced at the first awake instant.

`ProactiveSchedule` tracks which planned events have fired; the async
runtime (harness/runtime.py) fires due events by pacing the virtual clock
to each event's hour (sim/run_async.py is the entrypoint).

Restart recovery (A7): `next_pending(t_h)` surfaces PENDING events with
event_time <= t_h (overdue-visible: at now == event_time the event MUST be
visible, and overdue rows are never stranded). The runtime then evaluates
each overdue event — still valid ⇒ fire, past its validity window ⇒ expire.
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

#: Representative hour used to derive the day's initiative from its
#: BehaviorDirective (initiative is hourly via circadian energy; the
#: scheduler is per-day, so the directive is sampled at the diurnal peak).
INITIATIVE_SAMPLE_HOUR = 14.0

# --------------------------------------------------------------------------- #
# B5 — preregistered latent-state → timing coupling (iteration-3, closes F4)
# --------------------------------------------------------------------------- #
# The day's latent state enters the hazard multiplicatively through the
# run_events modulator seam:
#
#     h(τ, t) = h₀(τ) · C(t) · P(t) · A(score_{d−1}) · I(d) · exp(w·(x_d − x₀))
#
# with x_d = (E, S, R, A) the day's state vector (below) and x₀ the neutral
# directive point. All four channels come from the day's BehaviorDirective,
# resolved through the PATCHABLE harness.session seam (see
# ``_directive_for_day``) — so the STRUCTURED_NO_STATE neutral-directive
# patch (cvs_common.apply_condition_patches) flattens x_d to x₀ EXACTLY and
# the exp term collapses to 1.0: the ablation finally reaches timing (F4).
# The Weibull base h₀ and its parameters are plan-frozen and untouched.
#
# Weights are FROZEN for iteration 3 (B5 report §weights; G4 manifest):
#: (E, S, R, A) — energy, social drive (initiative), mood (valence),
#: reactivity, all in the directive's native units ([0,1] except valence).
STATE_VECTOR_NAMES: tuple[str, str, str, str] = (
    "energy", "initiative", "valence", "reactivity",
)
#: (w_E, w_S, w_R, w_A). Chosen from the REAL state ranges (engine records,
#: 90 d × 5 seeds: E∈[0.70,0.83], S∈[0.375,0.615], R∈[−0.8,0.8],
#: A∈[0.42,0.84]) so the exponent w·(x−x₀) spans ≈ [−0.5, +1.2] ⇒ per-day
#: factor ≈ [0.6, 3.3] natural (clipped at STATE_FACTOR_BOUNDS), mean ≈ 1.6
#: in FULL vs exactly 1.0 flat under NO_STATE. Measured acceptance effect
#: (90 d × 5 seeds, live composition): mean |Δn|/n_FULL = 0.175 (per-seed
#: ≥ 0.091), mean |Δgap|/gap_FULL = 0.215.
STATE_WEIGHTS: tuple[float, float, float, float] = (1.0, 1.6, 0.7, 0.9)
#: x₀ — the neutral directive's channel values (energy=0.5, initiative=0.5,
#: valence=0.0, reactivity=0.5): the point where exp(w·(x−x₀)) ≡ 1.0.
STATE_NEUTRAL: tuple[float, float, float, float] = (0.5, 0.5, 0.0, 0.5)
#: Safety clip on the per-day factor (bounded-factor convention, mirroring
#: adj_bounds / INITIATIVE_BOUNDS). Keeps the thinning majorant (mod_ub)
#: bounded and the coupling within the preregistered range.
STATE_FACTOR_BOUNDS: tuple[float, float] = (0.5, 2.0)
#: Preregistered divergence margins for the STRUCTURED_NO_STATE timing
#: claim (B5 report §claim; the G2 pre-flight registry uses them).
COUNT_DIVERGENCE_MIN = 0.15
GAP_DIVERGENCE_MIN = 0.10
MIN_GAPS_FOR_GAP_LEG = 3

#: Reason taxonomy (full DESIGN taxonomy; schedule | callback are the slice's
#: original members, the rest are added for gates + runtime).
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

#: How long a ContactOpportunity stays plausible (hours after desired_t_h).
#: The scheduler answers ONLY "should she consider contacting now?"; this is
#: the window in which that answer stays yes. The later semantic resolution
#: (IntentResolver) further bounds the intent by the reason's own validity.
OPPORTUNITY_VALIDITY_H = 3.0

#: Idempotent opportunity id for a planned event hour (re-plans regenerate
#: the same hours, hence the same ids — INSERT OR IGNORE stays clean).
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

    The Weibull/hazard process says "a plausible time to consider initiating
    contact"; there is deliberately NO semantic reason on the opportunity
    (invariant 3 — semantic motivation is resolved later, at opportunity
    time, into a grounded ProactiveIntent). ``hazard_components`` reports
    the multiplicative factors of the frozen modulator composition
    (envelope × phase × A(score_{d-1}) × I(day), plus the B5 ``state``
    factor exp(w·(x−x₀)) when the state coupling is active) at ``t_h`` for
    auditability; ``base`` is 1.0 because the Weibull baseline h0(τ) lives
    inside engine.timing.next_event (engine frozen — not separately
    recoverable). ``state_factor=None`` (default) keeps the components dict
    byte-identical to the uncoupled composition.
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

    Phase labels come from the SAME replay contract run_events uses
    (``_precompute_phase_labels`` — same seed ⇒ same labels), initiative from
    the day's stored BehaviorDirective (missing state ⇒ neutral 0.5), and
    the previous-day adjustment from the REAL stored judgement (missing ⇒
    A ≡ 1.0). The individual multipliers are reported, never the combined
    A·I product (no double counting in the audit trail).
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
    """Persist opportunities through the store's public seam when it exists.

    The A7 store has no contact_opportunities table yet (flagged in the A3
    handoff); the optional ``save_contact_opportunity`` seam is duck-typed so
    a store that grows one is used without any scheduler change.
    """
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
    `scores` optional per-day array feeding the adj term; None ⇒ adj ≡ 1
    (tests/legacy only — live scheduling always passes `day_scores` output).
    `state_factors` optional per-day multiplicative state term (B5 — the
    caller precomputes exp(w·(x−x₀)) via ``state_factors_for_plan``); None ⇒
    uncoupled composition, byte-identical to the pre-B5 behavior.
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

    initiative=0.5 ⇒ 1.0 (neutral); higher initiative ⇒ factor > 1 (more
    frequent contact), lower ⇒ factor < 1. Monotone and bounded.
    """
    return float(np.clip(np.exp(beta * (initiative - 0.5)), *bounds))


def _record_from_row(row: dict) -> DayRecord:
    """Rebuild a DayRecord from a store daily_state row (same mapping as
    session._record_from_row; duplicated here to avoid an import cycle —
    session imports scheduler)."""
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

    Resolved as ``harness.session.derive_behavior`` via a deferred import
    (session imports scheduler, so the module cannot import it at load
    time). In production that attribute IS ``harness.behavior.derive_behavior``
    (same object), so behavior is byte-identical; under an eval condition
    patch (cvs_common.apply_condition_patches rebinds the attribute) the
    neutral directive reaches the scheduler too — this is the seam that
    makes STRUCTURED_NO_STATE flatten the state vector. Missing state (no
    daily row) ⇒ None (callers degrade to neutral, as day_initiative always
    has).
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
    """The day's latent-state vector x_d = (E, S, R, A) (B5).

    E = directive.energy — circadian energy at the sampled hour (cycle/phase
    level; engine.circadian); S = directive.initiative — the social-drive
    channel; R = directive.valence — normalized mood; A = directive.reactivity
    — hormonal-gain/momentum reactivity. All four are directive channels, so
    the STRUCTURED_NO_STATE neutral patch flattens them to STATE_NEUTRAL
    exactly. Missing state ⇒ STATE_NEUTRAL (exp term ≡ 1.0), mirroring the
    day_initiative degradation contract.
    """
    directive = _directive_for_day(store, day, timing, hour=hour)
    if directive is None:
        return STATE_NEUTRAL
    return (float(directive.energy), float(directive.initiative),
            float(directive.valence), float(directive.reactivity))


def state_factor(store, day: int, timing: TimingParams, *,
                 hour: float = INITIATIVE_SAMPLE_HOUR) -> float:
    """The per-day multiplicative state term exp(w·(x_d − x₀)) (B5).

    Clipped at STATE_FACTOR_BOUNDS (bounded-factor convention, same as
    adj/initiative). Under the neutral directive x_d == STATE_NEUTRAL, so
    the factor is EXACTLY 1.0 — the exp term collapses to a constant and
    STRUCTURED_NO_STATE ablates the timing channel.
    """
    x = state_vector(store, day, timing, hour=hour)
    exponent = sum(
        w * (xi - xn) for w, xi, xn in zip(STATE_WEIGHTS, x, STATE_NEUTRAL)
    )
    return float(np.clip(np.exp(exponent), *STATE_FACTOR_BOUNDS))


def state_factors_for_plan(store, days: int, timing: TimingParams) -> np.ndarray:
    """Per-day state factors (days,) for the run_events B5 seam.

    factor[d] is day d's OWN state term (the day's directive drives that
    day's contact timing — consistent with the per-day initiative design).
    Deterministic; a store without state rows yields all 1.0, which is
    byte-identical to the uncoupled composition.
    """
    return np.asarray(
        [state_factor(store, d, timing) for d in range(days)], dtype=float
    )


def day_initiative(store, day: int, timing: TimingParams, *, hour: float = INITIATIVE_SAMPLE_HOUR) -> float:
    """The day's initiative (0..1) from its stored BehaviorDirective.

    Mechanical path: the directive is resolved through the PATCHABLE session
    seam (``_directive_for_day``) — the same function in production, neutral
    under STRUCTURED_NO_STATE's patch so the A·I timing fold ablates with
    the state vector. Missing state (should not happen for the current day)
    degrades to the neutral 0.5.
    """
    directive = _directive_for_day(store, day, timing, hour=hour)
    if directive is None:
        return 0.5
    return float(directive.initiative)


def day_scores(store, current_day: int, timing: TimingParams) -> np.ndarray:
    """Effective per-day scores array for a plan covering days 0..current_day.

    scores[i] = (A(score_i) · I(i+1) − 1) / ADJ_SLOPE for i < current_day,
    where score_i is the REAL judge score of day i (store.load_judgement;
    missing ⇒ A=1.0 neutral) and I(i+1) is day i+1's initiative factor.
    scores[current_day] is an unused placeholder (the engine reads
    scores[day-1], and day 0's adj is 1 by construction). The engine's
    adj_from_score(scores[d-1]) then equals clip(A(score_{d-1})·I(d), bounds)
    — the A·I term of the A7 hazard modulator. Deterministic, and stable
    across replans: entry i is fixed once day i is judged (score_i) and day
    i+1's state exists (initiative_i+1), both true the first time the plan
    covers day i+1, so re-planning never drifts already-persisted rows.
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
    #: ContactOpportunity per planned event hour (created by the scheduler at
    #: plan time; NO semantic reason on it — resolution happens at fire time).
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
        Idempotent (INSERT OR IGNORE). Returns a schedule whose _fired set is
        pre-seeded from the store: any planned hour whose row is no longer
        'pending' (i.e. already fired/expired) is treated as fired. Each
        planned hour also gets a ContactOpportunity (NO semantic reason);
        opportunities are persisted through the store's optional
        ``save_contact_opportunity`` seam when present (A7 gap — flagged in
        the A3 handoff) and always carried in-memory on the schedule.

        B5: the store-backed live path ALWAYS couples the latent state
        (``state_factors_for_plan`` — the day's directive through the
        patchable session seam). A store without state rows yields all-1.0
        factors, i.e. byte-identical event hours; under STRUCTURED_NO_STATE's
        neutral patch the factors collapse to exactly 1.0, ablating the
        timing channel (F4).
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
        every row whose status != 'pending'; opportunities = the store's
        persisted ContactOpportunities when the optional
        ``load_contact_opportunities`` seam exists (A7 gap otherwise —
        restored schedules carry no opportunities and the runtime resolves
        with the bare event hour). For restart-resume without re-planning."""
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
        """Earliest pending event hour due at `t_h`, else the earliest
        pending future hour; None when nothing is pending.

        A7 restart fix: pending events with event_time <= t_h are VISIBLE
        (at now == event_time the event must be found, and overdue rows are
        never stranded). Overdue events are returned first — the runtime
        evaluates each (still valid ⇒ fire, past validity ⇒ expire).
        """
        pending = [float(h) for h in self.event_hours if h not in self._fired]
        overdue = [h for h in pending if h <= t_h]
        if overdue:
            return min(overdue)
        return min(pending) if pending else None


# --------------------------------------------------------------------------- #
# B5 — AblationClaim for STRUCTURED_NO_STATE (channel="timing")
# --------------------------------------------------------------------------- #


def structured_no_state_timing_check(cell_records: dict, full_records: dict) -> bool:
    """Check logic of the STRUCTURED_NO_STATE timing claim (for B8's registry).

    Records follow the domain.py convention (at least ``n_proactive``); the
    B5 claim additionally reads ``proactive_times`` — ascending absolute
    hours of the condition's proactive messages — when present. Margins are
    the preregistered constants COUNT_DIVERGENCE_MIN / GAP_DIVERGENCE_MIN:

      * count leg (binding): |n_cell − n_full| / n_full >= 0.15;
      * gap leg: |mean_gap_cell − mean_gap_full| / mean_gap_full >= 0.10,
        applied only when BOTH sides provide at least MIN_GAPS_FOR_GAP_LEG
        gaps (a short 3-day preflight may not — the count leg alone then
        decides; the acceptance test exercises both legs over 30 days).

    The pre-flight driver should aggregate over its seeds (summed
    ``n_proactive``, pooled ``proactive_times``) before calling this check.
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
    """The AblationClaim for STRUCTURED_NO_STATE — B8's pre-flight registry
    (the orchestrator wires it at G2; see the B5 report §claim). This is
    the MANIFEST claim (effect-size, COUNT_DIVERGENCE_MIN = 0.15) — tested
    on the REAL matrix at G5. The pre-flight GATE uses the split
    low-bar null-detector instead (cvs_preflight._timing_channel_gate_check,
    GATE_MIN_DIVERGENCE); this claim stays untouched."""
    return AblationClaim(
        condition="STRUCTURED_NO_STATE",
        channel="timing",
        assertion=(
            "n_proactive differs from FULL by >= 15% and inter-contact "
            "mean-gap divergence >= 10% (when >=3 gaps are available on "
            "both sides)"
        ),
        check=structured_no_state_timing_check,
        min_days=4,  # el feedback de puntuación no puede aterrizar antes del día 2-3
    )
