---
type: research-note
title: Design re-evaluation — post Phase −1
description: "Analytical re-derivation over the existing documents — feasibility, mood random-variable selection, proactive-message model, hormonal simulation, harness architecture."
tags: [design, reevaluation, mood, beta-binomial, weibull, architecture]
timestamp: 2026-07-01
---

# Design re-evaluation — post Phase −1

**Date:** 2026-07-01
**Scope:** feasibility · random-variable selection · proactive-message model · hormonal simulation · harness architecture.
**Method:** analytical re-derivation over the existing documents (initial plan, [DESIGN.md](../DESIGN.md), notes 01–03). No new research.

**Overall verdict:** the design holds; scope and phase plan are unchanged. Four structural (not cosmetic) corrections:

| Area | Verdict | Change |
|---|---|---|
| Feasibility | ✅ High, no changes | Virtual clock moves from implicit to requirement |
| Mood (random variable) | ⚠️ Fix parameterization | Decouple mean/gain from the cycle; beta-binomial; endogenous AR(1) term |
| Proactive messages | ❌ Reverse Phase −1 recommendation | Hawkes out of the POC; renewal with modulated Weibull hazard |
| Hormonal simulation | ⚠️ Refine | Per-cycle jitter; anchor spline option; the actuator is language |
| Architecture | ✅ Correct, harden | Seeded pure engine, append-only traceability, one channel per process |

---

## 1. Feasibility

No fundamental changes: Python monolith + SQLite + asyncio + OpenAI-compatible client is all boring technology (good). The two real technical risks:

1. **Engine parameter regime** — already mitigated by Phase 1 (simulation before wiring the LLM). The re-evaluation widens the sweep (see §6).
2. **The judge as a noisy sensor inside a feedback loop** — new emphasis, see §2.3. It is the only closed loop in the system, and nobody calibrates the sensor in the original plan.

Requirement that moves from "nice to have" to blocking: **virtual clock**. The global success criterion demands "a multi-day (accelerated) session" — that is impossible to retrofit if the engine, the scheduler and the persistence read real time directly. A `Clock` is injected from day 0 (§5).

Estimated effort unchanged: Phases 0–1 in days; full POC in weeks part-time.

---

## 2. Mood random variables

### 2.1 What the original formulation actually does

With neutral valence 0.5, `neutral_logit = logit(0.5) = 0`, and the plan's formula collapses to:

```
arg = (logit(λ) + μ)·α
```

Three observations:

**(a) α couples gain and mean.** The plan asks α to "amplify swings without biasing". That is only true at λ=0.5. With λ=0.60 (`logit ≈ 0.405`), the hormonal cycle alone (μ=0, A=0.25) moves the mean:

- high phase: `arg = 0.405·1.25 ≈ 0.507 → p ≈ 0.624`
- low phase: `arg = 0.405·0.75 ≈ 0.304 → p ≈ 0.575`

That is, α **also** shifts the mean mood (~±0.25 steps at N=10), synchronized with the cycle. This can be desirable (the phase affects the mean level — biologically plausible), but it is **entangled** with reactivity amplification and cannot be tuned separately. One knob, two effects.

**(b) The binomial variance is not tunable.** `Var[M] = N·p(1−p)` is fixed given p (sd ≈ 1.5 steps at p≈0.6). Two people with the same temperament but different daily volatility cannot be expressed: the only variance knob (N) is also the scale-resolution knob.

**(c) No endogenous autocorrelation.** Given p(t), the M(t) are white noise around a slow mean; the only day-to-day persistence comes from μ (events → judge). Humans show lag-1 mood inertia ≈ 0.3–0.5 **without external cause** (emotional-inertia literature). "Woke up in a bad mood again, for no reason" — the original model cannot produce such streaks, and that is core to the "non-robotic" goal.

**(d) The μ dynamics are fine.** Equilibrium under constant score s: `μ∞ = k·s/(1−ρ) = 0.5·s` (reasonable magnitude: a perfect streak moves p from 0.60 to ~0.71). Half-life of a shock: `ln 0.5/ln 0.7 ≈ 1.9 days`; full extinction in ~a week. Human and tunable.

### 2.2 Revised formulation (generalizes, does not replace)

```
m(t)    = B·sin(2π·(t − φ)/L)                       # cycle: MEAN shift
g(t)    = 1 + A·sin(2π·(t − φ)/L) + ε_t             # cycle: reactivity GAIN
η(t+1)  = ρ_e·η(t) + Normal(0, σ_e)                 # endogenous mood AR(1)
arg(t)  = logit(λ) + m(t) + g(t)·( μ(t) + η(t) )
p(t)    = sigmoid(arg(t))
M(t)    ~ BetaBinomial(N, p(t), ν)                  # ν=∞ ⇒ pure Binomial
μ(t+1)  = ρ·μ(t) + k·(score(t) − score_neutral)
```

Each term does **one** thing:

| Term | Semantics | Knob |
|---|---|---|
| `logit(λ)` | stable temperament | λ |
| `m(t)` | the cycle shifts the mean level | B |
| `g(t)` | the cycle amplifies reactivity — multiplies only deviations (μ+η), not the temperament | A |
| `η(t)` | causeless mood streaks | ρ_e, σ_e |
| beta-binomial | per-person daily volatility | ν |
| `μ(t)` | event memory (judge) | k, ρ |

Scale checks: stationary sd of η = `σ_e/√(1−ρ_e²) ≈ 0.23` with (0.5, 0.2) → ±0.5–1 typical step, subtle but persistent ✓. Beta-binomial with ν=4: variance multiplier `1+(N−1)/(ν+1) = 2.8` → sd ×1.67 ✓ (implementation: `p_day ~ Beta(p·ν, (1−p)·ν)` and then `Bin(N, p_day)`).

**The original formulation is a special case:** `B=0, η≡0, ν=∞`, with the gain also applied to the temperament. Phase 1 runs the sweep comparing three variants — (i) original, (ii) decoupled without offsets (B=0), (iii) fully decoupled — and chooses by the plots, as the plan always wanted. Nothing is decided by authority; it is decided by simulation.

Intra-day unchanged: `arg_h = arg(t) + c(h)`, without re-sampling the binomial.

### 2.3 The judge as a noisy sensor in the loop

`μ ← score` is the only closed loop in the system, and LLM judges have bias (verbosity, sycophancy toward pleasant tone) and variance. A judge that systematically scores +0.3 sets a permanent `μ∞ = +0.15`: the companion drifts to "cheerful" no matter what the user does. Concrete mitigations:

- **Calibrate `score_neutral` empirically**: mean of the judge over a reference set of conversations, not assumed to be 0.
- Rubric with anchored scale + forced JSON output + temperature 0.
- **Test-retest in Phase 3**: score the same conversation N times; if sd > ~0.2, average passes or lower `k`.
- Winsorize extreme scores.

### 2.4 Binomial vs PAD — decision confirmed

The discrete 0..N scalar stays for the POC. Sharpened reasons: (i) an interpretable scalar is trivially injectable into the prompt and auditable; (ii) the judge's feedback is a scalar — updating 3 PAD dimensions from a 1-dimensional score is an attribution problem we do not want; (iii) the PAD clamp the literature recommended is implicit in the sigmoid (saturation = ceiling/floor effects, desirable). The only thing PAD actually added in real value is rescued cheaply: a derived **energy channel** (circadian + phase) separate from valence, because "tired but happy" and "energetic but irritable" are distinct human states that a single scalar cannot express. Two fields in the state, zero new dynamics.

---

## 3. Proactive-message model

### 3.1 Hawkes: Phase −1 recommendation reversed

Hawkes self-excitation over **our own messages** means, literally, that each proactive ping spawns an expected η ≈ 0.44 further pings. That is a nag-burst generator — exactly the anti-pattern that note 03 forbids as a hard constraint. The legitimate Hawkes case (cadence within a conversation: response-triggers-response) **this scheduler does not program** — reactive replies are immediate, not scheduled. Conclusion: Hawkes leaves the POC path. It remains as a documented extension in its useful form: **cross-excitation over user activity** (user active today → slightly higher rate), which is a different thing.

### 3.2 Gamma: the plan's intuition was right, the mechanics were not

The plan chose Gamma for non-memorylessness, and that property is exactly the desirable one: **increasing hazard** with elapsed time — "the longer we go without talking, the more I feel like writing to you". But a homogeneous Gamma renewal does not integrate the diurnal envelope cleanly (you would have to reject samples against circadian weights, a kludge), and the Gamma hazard has no closed form (incomplete gamma), which complicates modulation.

### 3.3 POC model: renewal with modulated Weibull hazard

The Weibull with k_w>1 has the same aging property with a closed-form hazard:

```
h(τ, t) = (k_w/θ)·(τ/θ)^(k_w−1) · circ(hour(t)) · phase(t) · adj(yesterday_score)
```

- `τ` = time since the last interaction; `k_w > 1` ⇒ increasing hazard. **k_w = 1 reduces to exponential/NHPP**, so the model contains NHPP as a special case.
- `circ(·)` — diurnal envelope, ≈0 during quiet hours (no 3am by construction, not by patch).
- `phase(·)` — cycle-phase multipliers (0.6–1.4, from note 02).
- `adj(·)` — previous-day adjustment, **bounded to [0.7, 1.3]**: the score→frequency loop is self-stabilizing (good day → more pings → if they annoy → worse score → fewer pings), but it is bounded anyway to prevent drift.
- Simulation by **thinning** against an upper bound of the hazard. ~30 lines of NumPy.
- **Hard guards outside the process** (in the queue, not at generation): 15 min minimum gap, daily cap, quiet hours, per-reason validity window.

Starting point: `k_w = 2.0`, `θ ≈ 13.5 h` (Weibull mean = θ·Γ(1.5) ≈ 12 h base; with the diurnal envelope killing the nighttime mass that leaves ~1–2 contacts/day, within the target). Phase 1 validates the hourly histogram and the mean.

---

## 4. Hormonal simulation

1. **Mean/gain separation** — the change from §2.2: `m(t)` and `g(t)` on the same cycle clock, tunable separately.
2. **The real curve is not sinusoidal.** The per-phase anchors from note 02 (menstrual −0.3 · follicular +0.1 · ovulatory +0.4 · early-luteal +0.1 · late-luteal −0.2) are asymmetric: narrow ovulatory peak, gradual luteal decline. The sinusoid is the right starting approximation; if the simulation shows the asymmetry matters, it is replaced by a **5-anchor spline** (or two harmonics) — local change, same interface.
3. **Per-cycle jitter, not per-person.** Realistic biological irregularity is **between** cycles: on completing a cycle, `L_i ~ Normal(28, 1.5)` is redrawn (a `cycle_day` counter that resets). More realistic than a fixed L with a single jitter, and equally trivial.
4. **The actuator is language.** The inspiration paper's replicable finding is that emotions emerge from **natural-language descriptions** of the state, not from numbers in the prompt. The assembler translates the numeric state (M, energy, phase, agenda) into a short natural-language brief; raw numbers do not modulate the model's tone. This is a first-class design piece, not a prompt detail.
5. **Weekly rhythm.** Weekday/weekend enters the schedule generator. Cheap and greatly increases plausibility (nobody "goes to the office" on a Sunday).
6. The state exposes `cycle_day` and `cycle_phase` (label) for the assembler and the rate multipliers.

---

## 5. Architecture

The engine/channels boundary and the modular monolith are confirmed. Hardening:

1. **Injected virtual clock** (`Clock`) in engine, scheduler and persistence. No direct real-time reads. A success-criterion requirement (accelerated days), not an optimization.
2. **Pure, reproducible engine:** the stochastic engine is an I/O-free module with explicit RNG (NumPy `Generator`), seeded per companion and per day; the seed is persisted in `daily_state`. Enables deterministic replay of any day ("why was she grumpy on Tuesday?") and makes Phase 1 and the unit tests trivial.
3. **Append-only traceability:** `state_events` table with every state transition and its causes (success criterion (a) demands "mood variation traceable to the variables" — that is an event log, not an UPDATE on `daily_state`). Plus an `llm_calls` table with every prompt/response, including the judge — essential for debugging persona drift and for judging the judge.
4. **One active channel per process** in the POC. `python-telegram-bot` and `discord.py` are asyncio-native and APScheduler can share the loop, but there is no reason to pay that complexity now; the common `Channel` interface already leaves the door open.
5. **Import: the hormonal phase is not reconstructible.** φ is not observable in a conversation export; pretending to infer it is false precision. It initializes randomly; what is reconstructed: history, summarized memory, and initial mood from the sentiment of recent days.
6. **40/40/20 by construction, not by verification.** Fixed interest taxonomy as a data file; the **quotas** are sampled first (4/4/2 per 10) and the LLM only decorates the chosen slots. The proportions are exact by construction — the plan's "LLM generation drifts → re-sample" risk (§9) disappears.

---

## 6. Phase 1 validation — expanded criteria

In addition to the plan's 5 criteria:

6. **Lag-1 autocorrelation of M** in target range 0.2–0.5 (validates η; the original model gives ~0 except via scores).
7. **Hourly histogram of proactive firings** within the circadian envelope; zero events in quiet hours; daily mean in [1, 3]; visible increasing hazard (gap distribution with mode > 0).
8. Sweep comparing the three mood-model variants (§2.2) in addition to A, k, ρ; B, ρ_e, σ_e, ν are added to the sweep.
9. (Phase 3) **Judge repeatability**: test-retest sd on the same conversation < 0.2, and `score_neutral` calibrated empirically.

---

## 7. Changes applied

- [DESIGN.md](../DESIGN.md): mood formula (§2.2), timing model (§3.3), cycle with per-cycle jitter and m/g (§4), cross-cutting architecture decisions (§5), data model (`state_events`, `llm_calls`, RNG seed, η), parameter table.
- [00-sintesis-fase-menos-1.md](00-sintesis-fase-menos-1.md): supersession note on the NHPP+Hawkes recommendation.
- The values remain priors to validate in Phase 1; this re-evaluation changes **structure** (decomposition and orthogonal knobs), not certainty about the numbers.
