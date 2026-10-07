---
type: experiment-report
title: W3.1 — Baseline experiment
description: "90-day baseline with default PersonaParams, decoupled_offsets variant, 5 seeds — criteria (1) stable mean, (2) m/g waves, (3) no saturation, (4) g variance ratio, (6) lag-1 autocorrelation."
tags: [results, w31, baseline, engine, criteria]
timestamp: 2026-07-03
---

# W3.1 — Baseline experiment

90 days, default `PersonaParams()`, variant `decoupled_offsets`, 5 fixed seeds: [101, 202, 303, 404, 505].

Reference theoretical mean: N·sigmoid(logit λ) = 10·sigmoid(logit 0.6) = **6.0000**.

## Criterion (1) — stable M mean

Threshold: per-seed global mean ∈ [5.25, 6.75] AND no drift (|mean(days 0–44) − mean(days 45–89)| < 1.0).

| Seed | mean(M) | mean(0–44) | mean(45–89) | \|drift\| | PASS/FAIL |
|---|---|---|---|---|---|
| 101 | 6.244 | 6.267 | 6.222 | 0.044 | PASS |
| 202 | 5.822 | 5.378 | 6.267 | 0.889 | PASS |
| 303 | 6.333 | 6.311 | 6.356 | 0.044 | PASS |
| 404 | 6.022 | 6.511 | 5.533 | 0.978 | PASS |
| 505 | 6.233 | 6.178 | 6.289 | 0.111 | PASS |

**Aggregate (1):** PASS (5/5 seeds in range without drift).

## Criterion (2) — clean m/g waves, period ~L

Threshold: autocorrelation of m at lag 28 > 0.5 (note: L is redrawn per cycle ~N(28,1.5), so the peak blurs); empirical amplitude of m ≈ B=0.15 (±30%) and of g−1 ≈ A=0.25 (±30%, after subtracting the σ_ε=0.03 noise).

Amplitude of m: peak-to-peak/2 (noise-free, m(d)=B·sin(2πd/L) is deterministic given d). Amplitude of g−1: two estimators — peak-to-peak/2 (biased upward by ε) and "de-noised" via variance: A_est=√(2·max(Var(g−1)−σ_ε²,0)), assuming Var(A·sin θ)≈A²/2 for a phase θ that covers the cycle ~uniformly over 90 days.

| Seed | autocorr m lag28 | amp(m) pp/2 | amp(g−1) pp/2 | amp(g−1) de-noised | PASS/FAIL |
|---|---|---|---|---|---|
| 101 | 0.983 | 0.1499 | 0.2832 | 0.2497 | PASS |
| 202 | 0.902 | 0.1500 | 0.2898 | 0.2457 | PASS |
| 303 | 0.996 | 0.1499 | 0.3084 | 0.2535 | PASS |
| 404 | 0.970 | 0.1500 | 0.2916 | 0.2454 | PASS |
| 505 | 0.970 | 0.1500 | 0.2978 | 0.2496 | PASS |

**Aggregate (2):** PASS (5/5 seeds).

## Criterion (3) — M histogram without saturation

Threshold: fraction of days with M==0 or M==N < 0.1 per seed.

| Seed | saturated fraction | PASS/FAIL |
|---|---|---|
| 101 | 0.0111 | PASS |
| 202 | 0.0111 | PASS |
| 303 | 0.0000 | PASS |
| 404 | 0.0111 | PASS |
| 505 | 0.0000 | PASS |

**Aggregate (3):** PASS (5/5 seeds).

## Criterion (4) — var(M) higher with high g

Threshold: `var_ratio_by_gain(M, g) > 1.0` in ≥ 4 of 5 seeds.

| Seed | var_ratio_by_gain | PASS/FAIL |
|---|---|---|
| 101 | 1.062 | PASS |
| 202 | 0.607 | FAIL |
| 303 | 1.323 | PASS |
| 404 | 1.007 | PASS |
| 505 | 0.629 | FAIL |

**Aggregate (4):** FAIL (3/5 seeds with ratio > 1.0). Mean ratio across seeds: **0.926**.

## Criterion (6) — lag-1 autocorrelation of M

Threshold: lag-1 autocorr of M ∈ [0.2, 0.5] per seed.

| Seed | lag-1 autocorr(M) | PASS/FAIL |
|---|---|---|
| 101 | 0.0869 | FAIL |
| 202 | 0.1357 | FAIL |
| 303 | 0.1768 | FAIL |
| 404 | 0.1176 | FAIL |
| 505 | 0.0460 | FAIL |

**Aggregate (6):** FAIL (0/5 seeds in range). Mean across seeds: **0.1126**.

**Diagnosis (honest FAIL, no parameter fitting — that is W3.3's job):** the measured mean lag-1 autocorr (0.113) is consistent with the earlier smoke measurements (~0.16) reported in the task statement. The autocorrelation of M(t) combines two variance sources: (a) fast binomial noise, decorrelated day to day (Var≈N·p(1−p), no memory), and (b) the slow correlated component coming from μ (judge memory, half-life ~1.9 d with ρ=0.70) and the m,g cycle (period ~28 d). With N=10 and p≈0.6, Var_binomial≈N·p(1−p)≈2.4 per day is large against the slow components' amplitude (B=0.15, A=0.25 in the logit argument), so it dilutes M's observable autocorrelation even though μ and η are themselves autocorrelated. This points to the slow-signal/fast-noise ratio — not the autocorrelation formula — as what W3.3's sweep must raise (e.g., by raising k, lowering N relative to the argument amplitude, or raising B/A within the stability bound).

## Figures

- `mood_series_decoupled_offsets_s101.png`
- `mg_decoupled_offsets_s101.png`
- `mood_hist_decoupled_offsets_s101.png`
- `mu_eta_decoupled_offsets_s101.png`
- `mean_M_across_seeds.png`

## Reading

3/5 aggregate criteria PASS. The M mean stabilizes near the theoretical value (6.00) without appreciable drift between the first and second half of the 90 days, and the histogram does not saturate against the 0/N edges (the N=10 scale with λ=0.6 leaves ample margin on both sides). The m and g waves are visible and their empirical amplitude falls within the ±30% tolerance, although m's autocorrelation at the exact lag 28 is somewhat attenuated by the per-cycle L redraw ~N(28,1.5) (the real period oscillates around 28 rather than being fixed). The gain g does amplify M's variance in the high-g regime in most seeds. The worrying point is (6): M's lag-1 autocorr sits below the expected human range — it is the fast binomial noise (small N, p far from 0/1) competing with the slow μ/η/cycle signal, as documented in the diagnosis above; raising that signal/noise ratio without breaking the stability bound k < 2(1−ρ)/g_max is left to W3.3.
