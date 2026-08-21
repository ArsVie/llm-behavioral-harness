---
type: plan
title: Phase 1 — Wave-based task plan (parallel subagents)
description: "Isolated stochastic engine + 60–90 day simulation with validated plots, no LLM — 16 tasks in 5 parallel waves with contract-first, disjoint file ownership."
tags: [plan, phase-1, waves, engine, simulation]
timestamp: 2026-07-03
---

# Phase 1 — Wave-based task plan (parallel subagents)

**Date:** 2026-07-03
**Phase objective:** isolated stochastic engine + 60–90 day simulation with validated plots, **no LLM**. Mathematical specification in [DESIGN.md](../DESIGN.md); expanded criteria in [research/05-reevaluacion-diseno.md](../research/05-reevaluacion-diseno.md) §6.
**Wave rule:** zero dependencies between tasks of the same wave. Dependencies only between waves (each wave consumes what earlier ones froze).

## Context for a new session (read before executing)

1. **Reading order:** [README](../README.md) → [DESIGN.md](../DESIGN.md) (complete specification: engine equations, **default-parameter table**, data model) → [research/05-reevaluacion-diseno.md](../research/05-reevaluacion-diseno.md) §6 (expanded validation criteria) → this plan. The critique [research/06](../research/06-critica-objetivo-implementacion.md) provides priority context (tiers) but does not block Phase 1.
2. **Scope agreed with the user:** local single-user PoC, **without** safety/wellbeing guardrails (archived in `research/deferred/`); engine complexity is intentional (perceived emotional variance at day/month scale is the objective); the mathematical formulations are the user's own — the arXiv 2508.11829 paper is only initial inspiration.
3. **Environment (known quirks):** the project lives at `\\wsl.localhost\ubuntu\home\vruizes\.hermes\projects\llm-behavioral-harness` (Windows view, the one used by Read/Write/Edit/Glob) = `/home/vruizes/.hermes/projects/llm-behavioral-harness` (WSL view). **The Bash tool showed a desynchronized view of this tree on at least one occasion** (files written with Write not visible via Bash); to move/copy files use PowerShell with the `\\wsl.localhost\...` path, and for content use the file tools. Where and how to run Python/pytest **is not verified** — verifying and documenting it in `CONVENTIONS.md` is an explicit W0.1 deliverable.
4. **Open decisions W0.1 can take without asking:** values not fixed in DESIGN (e.g., default quiet hours, ~23:00–08:00; exact day ranges per cycle phase) — choose reasonably and document in `types.py`.

---

## Parallelization principles

1. **Contract-first.** Types, signatures and conventions are frozen in Wave 0 (`engine/types.py` + stubs). Every later task codes against that contract without seeing the others' work.
2. **Modules do not import each other.** `mood` receives `m`, `g` as floats — it does not import `cycle`. `timing` receives a `Callable` modulator — it does not import `circadian`. Composition happens only in the Wave 2 drivers. This is what makes the waves parallelizable *and* is the correct architecture (modules testable in isolation).
3. **Disjoint file ownership.** Each task owns its files (module + its test + its results folder) and touches nobody else's. Shared files (`types.py`, `conftest.py`, `pyproject.toml`) belong to Wave 0 and are read-only afterwards.
4. **Self-verification.** Each task runs its own tests/figures and reports pass/fail — no agent needs another agent's output from its own wave.
5. **Reproducibility.** RNG via `numpy.random.SeedSequence` with hierarchical spawning (companion → day); every figure/experiment fixes and reports its seed.

---

## Target file structure

```
llm-behavioral-harness/
├── pyproject.toml, README, CONVENTIONS.md      (Wave 0)
├── engine/
│   ├── types.py        (Wave 0 — FROZEN)     dataclasses, enums, signatures
│   ├── rng.py          (Wave 0)                 SeedSequence, per-day spawn
│   ├── mood.py         (W1.1)                  beta-binomial logit, μ, η, variants
│   ├── cycle.py        (W1.2)                  m(t), g(t), L redraw, phases
│   ├── circadian.py    (W1.3)                  c(h), energy, circ(t) envelope
│   ├── timing.py       (W1.4)                  Weibull hazard + thinning
│   └── validation.py   (W1.7)                  config validation + stability bound
├── sim/
│   ├── metrics.py      (W1.5)                  acceptance metrics (arrays → floats)
│   ├── plots.py        (W1.6)                  figures (SimResult → png)
│   ├── run_daily.py    (W2.1)                  day-by-day loop + synthetic score
│   └── run_events.py   (W2.2)                  timing event stream
├── tests/
│   ├── conftest.py     (Wave 0)
│   └── test_<module>.py  (each task its own)
├── experiments/        (W3.x — one script per experiment)
└── results/<experiment>/  (figures + reporte.md per experiment)
```

---

## Contract to freeze in Wave 0 (summary)

```python
# engine/types.py
class MoodVariant(Enum): ORIGINAL; DECOUPLED; DECOUPLED_OFFSETS
  # ORIGINAL:  arg = (logit λ + μ)·g          (plan §3, B≡0, η≡0, ν=∞)
  # DECOUPLED: arg = logit λ + g·(μ + η)      (B≡0)
  # DECOUPLED_OFFSETS: arg = logit λ + m + g·(μ + η)

@dataclass(frozen=True) class PersonaParams:   # N, lam, nu, k, rho, rho_e, sigma_e, B, A, sigma_eps, L_mean, L_sd, phi, score_neutral
@dataclass(frozen=True) class TimingParams:    # k_w, theta_h, peak_hour, diurnal_amp, quiet_hours, phase_multipliers, adj_bounds, min_gap_min, daily_cap
@dataclass class CycleState:                   # cycle_day, L_current
@dataclass class MoodState:                    # mu, eta
@dataclass class DayRecord:                    # t, m, g, arg, p, M, score, mu, eta, cycle_day, phase_label, seed
@dataclass class SimResult:                    # params, variant, records: list[DayRecord] (+ properties as arrays)

# Key signatures (stubs with docstrings in Wave 0):
cycle.step(state, params, rng)            -> (m: float, g: float, phase_label: str, state)
mood.step(state, params, m, g, variant, rng) -> (M: int, p: float, arg: float)
mood.update(state, params, score)         -> state          # μ ← ρμ + k(score − neutral)
mood.step_endogenous(state, params, rng)  -> state          # η AR(1)
circadian.c(h, params)                    -> float          # intra-day valence
circadian.energy(h, phase_label, params)  -> float
circadian.envelope(h, params)             -> float          # [0,1], 0 during quiet hours
timing.next_event(t_now, t_last_interaction, modulator: Callable[[float], float], params, rng) -> float
validation.check(persona, timing)         -> list[str]      # errors; includes bound k < 2(1−ρ)/g_max (worst case p(1−p)=0.25, g_max=1+A+3σ_ε)
```

Synthetic score (closes the loop in simulation, driver W2.1): `score = clip(2·(M/N − 0.5) + Normal(0, 0.2), −1, 1)`, with scripted overrides (shocks).

---

## Wave 0 — Contracts + scaffolding (sequential, 1 task)

| ID | Task | Deliverable | Executor |
|---|---|---|---|
| W0.1 | Minimal scaffolding + frozen contract | `pyproject.toml` (numpy/scipy/matplotlib/pytest), folder structure, complete `engine/types.py`, `engine/rng.py`, stubs with signatures + docstrings for all modules, `tests/conftest.py`, `CONVENTIONS.md` (includes **runtime verification**: which tool runs Python/pytest in this WSL/Windows environment and how — tested and documented here, de-risks all waves) | Main session (not a subagent — the contract defines everything else) |

> Phase note: W0.1 advances only the portion of Phase 0 that Phase 1 needs (package + numerical config). LLM client, CLI and SQLite remain Phase 0 and are not touched here.

**Gate → Wave 1:** `pytest` runs (even an empty collection), `import engine.types` works, CONVENTIONS.md says how to run.

---

## Wave 1 — Engine modules (7 parallel tasks)

Each task: implements its module against `types.py`, writes its test file, runs pytest on its files, reports. Does not read or write other tasks' files.

| ID | Module (own files) | Content | Task acceptance tests | Model |
|---|---|---|---|---|
| W1.1 | `engine/mood.py`, `tests/test_mood.py` | The 3 `arg` variants; beta-binomial with special case `ν=∞` → exact binomial (`p_day ~ Beta(pν,(1−p)ν)` → `Bin(N,p_day)`); μ and η | ν=∞ reproduces binomial (statistical test); μ recursion vs closed form `μ∞=k·s/(1−ρ)`; stationary sd of η ≈ `σ_e/√(1−ρ_e²)`; the 3 variants agree when B=0, η≡0, ν=∞ and g≡1 | sonnet |
| W1.2 | `engine/cycle.py`, `tests/test_cycle.py` | Sinusoidal m(t), g(t) on the cycle clock; redraw `L_i~N(28,1.5)` on cycle completion; phase labels (5 phases by day ranges) | Mean/amplitude of m,g correct; L redrawn with correct stats; correct phase at boundaries; periodicity ~L (autocorrelation) | haiku |
| W1.3 | `engine/circadian.py`, `tests/test_circadian.py` | `c(h)` cosine (peak 14:00, ±0.25), energy channel (circadian+phase), `envelope(h)` with quiet hours = 0 and smooth transition | Values at anchor hours; envelope=0 in quiet hours; continuity; energy differs by phase | haiku |
| W1.4 | `engine/timing.py`, `tests/test_timing.py` | Hazard `h(τ,t)=(k_w/θ)(τ/θ)^{k_w−1}·modulator(t)`; next-event sampling by **thinning** with correct upper bound | With modulator≡1: gaps ~ Weibull (KS test); `k_w=1` → exponential (memoryless); increasing hazard for k_w>1 (gaps with mode>0); with step modulator: zero events where modulator=0; rate scales with the multiplier | sonnet |
| W1.5 | `sim/metrics.py`, `tests/test_metrics.py` | Pure array→float functions: mean/sd of M, lag-1 autocorr, variance ratio high-g vs low-g, reversion time after shock, gap stats (daily mean, mode, burstiness), hourly histogram vs envelope | Each metric verified on synthetic series with known value | sonnet |
| W1.6 | `sim/plots.py`, `tests/test_plots.py` | Standard figures from `SimResult`/arrays: M(t) series with band, m/g(t), M histogram, μ/η(t), hourly event histogram, per-variant comparison. Single style, seed in the title | Smoke: generates png without error from synthetic fixtures; deterministic filenames | haiku |
| W1.7 | `engine/validation.py`, `tests/test_validation.py` | Validation of `PersonaParams`/`TimingParams`: ranges, **stability bound** `k < 2(1−ρ)/g_max` with `g_max=1+A+3σ_ε`, `adj_bounds⊂[0.5,1.5]`, coherent quiet hours | Valid configs pass; each violation yields its own error; exact bound boundary | haiku |

**Gate → Wave 2:** full pytest green; quick signature review against the contract (main session).

---

## Wave 2 — Simulation drivers (2 parallel tasks)

| ID | Own files | Content | Task acceptance | Model |
|---|---|---|---|---|
| W2.1 | `sim/run_daily.py`, `tests/test_run_daily.py` | 60–90 day day-by-day loop composing cycle+mood(+c for reference arg_h); synthetic score with scripted overrides (programmable negative streaks); produces `SimResult`; minimal CLI (`--days --seed --variant --params yaml`) | Runs 90 days deterministically with fixed seed; shock scripts appear in the records; integration smoke | sonnet |
| W2.2 | `sim/run_events.py`, `tests/test_run_events.py` | Continuous event stream composing timing+circadian envelope+phase multipliers+adj(score); respects min_gap and daily_cap (queue guards); produces timestamps | Runs 90 simulated days; deterministic; guards verified (no gap<min, no day>cap) | sonnet |

Both depend on complete Wave 1; nothing between them.

**Gate → Wave 3:** both drivers run end-to-end with default params.

---

## Wave 3 — Validation experiments (5 parallel tasks)

Each task: one script in `experiments/`, results and figures in `results/<id>/`, and a short `reporte.md` with pass/fail per criterion. Fixed, multiple seeds (≥5) where stats are involved.

| ID | Experiment (own results) | Validates (criterion) | Model |
|---|---|---|---|
| W3.1 | **Baseline**: 90 days, default params, DECOUPLED_OFFSETS variant | (1) M mean stable ≈ `N·sigmoid(logit λ)` with bounded deviations; (2) clean m/g waves of period ~L; (3) M histogram without saturation; (4) var(M) higher in high-g vs low-g; (6) lag-1 autocorr ∈ [0.2, 0.5] | sonnet |
| W3.2 | **Variant comparison**: ORIGINAL vs DECOUPLED vs DECOUPLED_OFFSETS, same seeds | (8a) documented differences: visible ORIGINAL mean-gain coupling; autocorr with/without η; reasoned variant recommendation | sonnet |
| W3.3 | **Parameter sweep**: grid over A, k, ρ, B, ρ_e, σ_e, ν (respecting the stability bound); metric heatmaps | (8b) "human" regime region (stable mean + live variance + in-range autocorr); tuned-defaults proposal | sonnet |
| W3.4 | **Timing validation**: hourly histogram, gaps, per-phase rates, k_w ∈ {1, 1.5, 2, 3} | (7) hourly profile inside the envelope; 0 events in quiet hours; daily mean ∈ [1,3]; visible increasing hazard (gap mode > 0 for k_w>1); measurable phase-multiplier effect | sonnet |
| W3.5 | **Shocks and loop stability**: programmed negative streaks; k near the bound | (5) μ drops and reverts in ~`1/(1−ρ)` days; empirical verification of the stability bound (stable just below, oscillates/diverges above) | sonnet |

No dependencies between experiments: each imports the Waves 1–2 infrastructure and writes only in its own folder.

**Gate → Wave 4:** the 5 reports exist with figures.

---

## Wave 4 — Synthesis and acceptance (sequential, 1 task)

| ID | Task | Deliverable | Executor |
|---|---|---|---|
| W4.1 | Aggregate the 5 reports, criteria 1–8 table with pass/fail and evidence figure, reasoned variant choice + tuned defaults, open risks | `results/fase-1-informe.md` — input for the joint parameter review before wiring the LLM (plan checkpoint) | Main session (needs full context and presents to the user) |

Criterion (9) of the expanded plan — judge repeatability — belongs to Phase 3 and is out of scope.

---

## Execution mechanics

- **One batched agent call per wave** (all wave tasks in a single message → run concurrently). Prompt per task: read `types.py` + `CONVENTIONS.md` + its row of this plan; the files it owns; run its tests; report pass/fail + summary.
- **Isolation:** no worktree needed (disjoint file ownership); optional if preferred.
- **Gates:** between waves, the main session runs full pytest and verifies the contract — the only synchronization point.
- **Count:** 16 tasks, 5 waves. Critical path ≈ W0 + max(W1) + max(W2) + max(W3) + W4; waves 1 and 3 dominate and run in pure parallel.
