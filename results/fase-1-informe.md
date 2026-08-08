---
type: experiment-report
title: Phase 1 — Stochastic engine validation report (W4.1)
description: "W4.1 synthesis of waves w31–w35 — criteria 1–8 pass/fail table, variant choice, tuned defaults, open risks, and checkpoint decisions."
tags: [results, phase-1, validation, engine, informe]
timestamp: 2026-07-03
---

# Phase 1 — Stochastic engine validation report (W4.1)

**Date:** 2026-07-03
**Scope:** isolated stochastic engine + 90–120 day simulation, **no LLM** (Phase 1 objective of the plan).
**Inputs:** the 5 Wave 3 reports — [w31-baseline](w31-baseline/reporte.md) · [w32-variantes](w32-variantes/reporte.md) · [w33-barrido](w33-barrido/reporte.md) · [w34-temporizacion](w34-temporizacion/reporte.md) · [w35-shocks](w35-shocks/reporte.md) — plus an additional W4.1 verification (criterion 4 under tuned defaults, below).
**Code status:** 213 tests green (full `pytest`); all experiments reproducible with `python -m experiments.<id>` (fixed seeds documented in each report).

---

## Executive summary

The engine holds. With the **DESIGN.md defaults** 6 of 8 criteria pass; the two that fail — (4) variance modulated by g and (6) "human" lag-1 autocorrelation — share a single root cause: the fast binomial noise (N=10, Var≈2.4/day) buries the slow μ/η signal with σ_e=0.2. The sweep (W3.3) found the fix with a minimal change: **`rho_e=0.7, sigma_e=0.45`** (everything else unchanged), which puts autocorrelation in the center of the target range (0.39–0.41) and raises the g variance ratio to a mean of 1.31, without saturating or destabilizing the mean. The variant comparison (W3.2) confirms the design choice with data: **DECOUPLED_OFFSETS**. Timing (W3.4) passes entirely as configured. The score→μ loop (W3.5) reverts shocks as theory predicts and the stability bound is confirmed conservative, with a structural finding to keep in mind for Phase 3 (positive loop bias with `score_neutral=0`, see risks).

**Recommendation:** freeze `MoodVariant.DECOUPLED_OFFSETS` + the tuned defaults below as Phase 2 starting parameters, and review at the checkpoint the two marked decision points (strength of the g effect; loop bias).

---

## Criteria table (plan §validation + research/05 §6)

| # | Criterion | Threshold | DESIGN defaults | Tuned defaults | Verdict | Evidence |
|---|---|---|---|---|---|---|
| 1 | Stable M mean ≈ N·σ(logit λ)=6.0, no drift | mean ∈ [5.25, 6.75]; drift < 1.0 | 5.82–6.33, drift ≤ 0.98 (5/5) | mean 5.77 (10 seeds) | **PASS** | [w31](w31-baseline/mean_M_across_seeds.png) |
| 2 | Clean m/g waves of period ~L | m lag-28 autocorr > 0.5; amps ≈ B, A ±30% | 0.90–1.00; amp(m)=0.150, amp(g−1)≈0.25 (5/5) | unchanged (does not depend on ρ_e/σ_e) | **PASS** | [w31](w31-baseline/mg_decoupled_offsets_s101.png) |
| 3 | M histogram without saturation | frac(M∈{0,N}) < 10% | 0–1.1% | 3.0% | **PASS** | [w31](w31-baseline/mood_hist_decoupled_offsets_s101.png) |
| 4 | var(M) higher in high-g vs low-g | ratio > 1 | **FAIL** — 0.926 mean, 3/5 | **marginal PASS** — 1.308 mean, 7/10 | **marginal PASS** | [w33 heatmap](w33-barrido/05_A_B_var_ratio.png) + §verification below |
| 5 | μ drops with a streak and reverts in ~1/(1−ρ) d; stability bound | drop < μ_pre−0.15; reversion ∈ [1,8] d; monotonic order in k | drop 5/5; reversion 3–8 d (theoretical 2.8); ρ↑⇒reversion↑ (3.4/5.2/12.6 d); bound separates k=0.40/0.47/0.60 monotonically | — | **PASS** (note: literal threshold \|μ\|<0.6 fails due to λ bias, see risk R1) | [w35](w35-shocks/01_shock_mu_t.png), [w35 bound](w35-shocks/04_k_comparison_mu_and_sat.png) |
| 6 | lag-1 autocorr of M ∈ [0.2, 0.5] | per seed | **FAIL** — 0.113 mean, 0/5 | **PASS** — 0.411 mean, 9/10 (one seed 0.549) | **PASS (tuned)** | [w33 heatmap](w33-barrido/01_rho_e_sigma_e_autocorr.png), [verification](w33-barrido/08_verificacion_defaults_M_t.png) |
| 7 | Timing: hourly ⊂ envelope; 0 in quiet hours; mean ∈ [1,3]/d; increasing hazard; phase effect | see report | 0 violations (5/5); 1.36–1.59/d (5/5); gap mode 14.5 h ≫ 0; cv↓ with k_w (0.83→0.41); Spearman(mult, rate)=0.87; cap binds 5.8% of days | n/a (TimingParams unchanged) | **PASS** | [w34 hourly](w34-temporizacion/hourly_events_baseline_agg_s1001-1002-1003-1004-1005.png), [phase](w34-temporizacion/phase_rate_vs_multiplier.png) |
| 8a | Variants compared, differences documented | 3 structural contrasts | visible ORIGINAL mean-gain coupling (Δmean 0.40 vs −0.27); autocorr with η > without η; measurable B effect (corr 0.20 vs −0.11) | — | **PASS** | [w32](w32-variantes/metrics_barplot.png) |
| 8b | Sweep: "human" region + verified tuned defaults | non-empty region + verification with fresh seeds | region in all 3 grids (5+2+1 cells); proposal verified 4/4 metrics | — | **PASS** | [w33 heatmaps](w33-barrido/01_rho_e_sigma_e_autocorr.png) |
| 9 | Judge repeatability | — | — | — | **Phase 3** (out of scope, per plan) | — |

---

## Additional W4.1 verification — criterion (4) under tuned defaults

W3.3 verified its proposal against 4 metrics (mean, sd, autocorr, saturation) but not the variance ratio by g. Run separately: `PersonaParams(rho_e=0.7, sigma_e=0.45)`, DECOUPLED_OFFSETS, 90 days, 10 seeds `[66,77,88,99,110,101,202,303,404,505]`:

```
var_ratio  mean = 1.308   (> 1 in 7/10 seeds; range 0.43–2.34)
ac1        mean = 0.411   (in [0.2,0.5]: 9/10)
mean(M)    mean = 5.77    sd(M) mean = 2.13    mean saturation = 3.0%
```

Reading: tripling the stationary sd of η (0.23 → 0.63) gives g more deviation to amplify and the effect goes from invisible (0.93) to present (1.31). It remains **marginal at 90 days per seed** (3/10 seeds fall below 1 by chance): the footprint exists in aggregate but is not guaranteed in a short window. If per-individual-cycle perceptibility is wanted, the heatmap [05_A_B_var_ratio](w33-barrido/05_A_B_var_ratio.png) indicates raising A (0.25 → 0.4), at the cost of raising g_max and lowering the admissible k bound (0.448 → 0.404 with A=0.4). **Decision for the checkpoint.**

---

## Variant choice

**`MoodVariant.DECOUPLED_OFFSETS`** — confirmed by data (W3.2), not only by design:

- ORIGINAL couples level and reactivity in a single term (`(logit λ + μ)·g`): Δmean by g of +0.40 steps with no knob to turn it off, and without η there are no endogenous streaks (autocorr 0.097, the lowest). Discarded.
- DECOUPLED loses the phase-driven mean shift (corr(MA7(M), m) = −0.11 ≈ noise): the cycle only modulates variance, invisible as "mood by phase".
- DECOUPLED_OFFSETS buys the three orthogonal knobs (B level, A reactivity, ρ_e/σ_e streaks) at the cost of one extra parameter; all leave separately measurable footprints.

---

## Proposed tuned defaults (Phase 2 start)

```python
PersonaParams(
    N=10, lam=0.60, nu=math.inf,
    k=0.15, rho=0.70,
    rho_e=0.70,     # ← tuned (was 0.50)
    sigma_e=0.45,   # ← tuned (was 0.20)
    B=0.15, A=0.25, sigma_eps=0.03,
    L_mean=28.0, L_sd=1.5, phi=0.0,   # φ drawn per companion in production
    score_neutral=0.0,                # ← recalibrate in Phase 3 (see R1)
)
TimingParams()  # unchanged: validated entirely in W3.4
```

Metrics with this configuration (10 seeds): mean 5.77 · sd 2.13 · autocorr 0.41 · saturation 3.0% · var_ratio 1.31. The mean lands ~0.2 steps below the theoretical 6.0 — the larger slow noise interacts with the sigmoid concavity above p>0.5; cosmetic and within criterion (1) range.

---

## Open risks

- **R1 — Structural positive loop bias (for Phase 3).** With `score_neutral=0` and λ=0.6, the synthetic score inherits M's mean (E[score]≈+0.2) and μ drifts to a positive equilibrium; at or near the bound, runaway is asymmetric upward with probability ~1 (W3.5 finding, 5/5 seeds and 3/3 k values). It is not an engine bug — it is exactly the "off-center judge" phenomenon DESIGN already requires calibrating in Phase 3 (empirical `score_neutral`). For future simulations with a centered loop: `score_neutral ≈ 2·(λ−0.5)`.
- **R2 — Criterion (4) marginal.** The g effect on variance is real but weak at 90 days (7/10 seeds). Raising A=0.4 would make it robust at the cost of tightening the stability bound. Product decision: should the "reactive phase" be perceptible in a single cycle, or is aggregate enough?
- **R3 — Statistical power n=5 (W3.2).** The three variant contrasts were confirmed directionally, but with between-seed sd on the order of the mean (overlapping intervals in the barplot). The cited magnitudes have high uncertainty; the directions are consistent with the formula structure.
- **R4 — Low k_w × max_gap interaction.** With k_w=1 (flat hazard) the 48 h guard dominates the gap distribution (spurious peak at ~47.5 h, W3.4 §2). Irrelevant with the default k_w=2; review if some future tuning lowers k_w toward 1.
- **R5 — daily_cap.** Binds 5.8% of days with defaults (max 10% per seed) — acceptable, but it is a real cut of the high tail of the rate; documented in case Phase 4 (real scheduler) observes fewer "intense days" than expected.

---

## Next step (plan checkpoint)

This report is the input for the **joint parameter review before wiring the LLM**. Decisions at the checkpoint: (a) adopt the tuned defaults as-is, or also raise A (R2); (b) `score_neutral` in simulation (R1); (c) declare the `engine/types.py` contract frozen for Phase 2 (LLM client + SQLite + CLI from the rest of Phase 0, then persona + schedule + reactive chat). Criterion (9) — judge repeatability — remains scheduled for Phase 3, as the plan mandates.

---

## Checkpoint resolution (2026-07-03, user decision)

- **(a) Tuned defaults ADOPTED as-is** (`rho_e=0.7`, `sigma_e=0.45`; A stays at 0.25). Applied in `engine/types.py` and reflected in the DESIGN.md table; full suite green after the change (213 tests).
- **(b) R1 resolved by design:** the slightly positive loop bias is considered **desirable** — `score_neutral` stays at 0.0 on purpose. The Phase 3 empirical judge calibration remains in force, but its goal becomes controlling the bias magnitude, not eliminating it.
- **Reference gallery:** 30-day simulations of emotional cycles under different daily effects (baseline, cycle only, endogenous only, negative streak, high volatility, strong cycle, intra-day effect) in [`engine_simulation/`](../engine_simulation/README.md), generated with `python -m experiments.engine_simulation` (seed 3001).
- **(3rd iteration, same day) Month-scale event memory and energy-only circadian.** (a) `k=0.18, ρ=0.85` adopted (were 0.15/0.70): the deal ceiling rises to μ∞=±1.2 — a perfect month lives at ~7.5 mean (72% of days ≥7) and a horrible one at ~3.3 (73% of days ≤4), vs 6.4/4.6 with the previous values ([15_mes_perfecto_horrible.png](../engine_simulation/15_mes_perfecto_horrible.png)); within the stability bound (0.18 < 0.224) and with a 4.3-day half-life — isolated days weigh little, streaks accumulate. Raising ρ instead of k was preferred to avoid amplifying the judge's daily noise. (b) **The circadian stops touching valence**: `arg_h = arg + c(h)` discarded; the intra-day signal is expressed only through the energy channel (DESIGN §Circadian modulation revised; `circadian.c` remains as a utility). (c) Timing continues with envelope × phase × adj — energy will **not** be the single frequency control; rate←energy unification deferred to a Phase 4 A/B experiment. Three tests that assumed the old defaults were fixed to explicit parameters; suite green (213).
- **(2nd iteration, same day) `B` rises from 0.15 to 0.5.** Point-data analysis showed that with B=0.15 the cycle moves real mood only ±0.36 steps against sampling noise sd≈1.55 — invisible even in the weekly moving average. The B sweep averaged over 30 seeds (`engine_simulation/12_barrido_B_30seeds.png`) showed monotonic scaling (corr with the theoretical wave: 0.54 → 0.77 → 0.87 → 0.93 for B = 0.15/0.3/0.5/0.65) and that **B=0.5 is the minimum at which the monthly arc is legible in observable behavior**; additionally the positive loop (score→μ) amplifies the wave (~3.3 measured peak-trough steps vs 2.4 theoretical). B does not enter the stability bound (only A via g_max). Adopted in `engine/types.py` and DESIGN.md.
