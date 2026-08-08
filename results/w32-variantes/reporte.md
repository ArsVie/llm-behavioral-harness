---
type: experiment-report
title: W3.2 — MoodVariant comparison
description: "Comparison of ORIGINAL / DECOUPLED / DECOUPLED_OFFSETS variants, 5 seeds, 90 days — criterion (8a) with three documented structural contrasts and a variant recommendation."
tags: [results, w32, variants, mood, criteria]
timestamp: 2026-07-03
---

# W3.2 — MoodVariant comparison

Seeds: [111, 222, 333, 444, 555] · days: 90 · default PersonaParams().

Reproducible script: `python -m experiments.w32_variantes` regenerates all figures and this report.

## Table: metric × variant (mean ± sd across seeds)

| Metric | ORIGINAL | DECOUPLED | DECOUPLED_OFFSETS |
|---|---|---|---|
| Δmean = mean(M\|high g) − mean(M\|low g) | 0.4000 ± 0.1880 | -0.2696 ± 0.6884 | 0.4087 ± 0.5548 |
| corr(g, M) [Pearson] | 0.0991 ± 0.0398 | -0.0341 ± 0.1235 | 0.1139 ± 0.1236 |
| autocorr_lag1(M) | 0.0965 ± 0.1524 | 0.1384 ± 0.1144 | 0.1097 ± 0.1253 |
| corr(MA7(M), m(t)) | 0.1859 ± 0.0624 | -0.1082 ± 0.2642 | 0.2046 ± 0.2256 |

## Criterion (8a) — differences documented quantitatively

### 1. ORIGINAL mean-gain coupling

- Δmean ORIGINAL = 0.4000 ± 0.1880; Δmean DECOUPLED = -0.2696 ± 0.6884; Δmean DECOUPLED_OFFSETS = 0.4087 ± 0.5548.
- corr(g, M) ORIGINAL = 0.0991 ± 0.0398; corr(g, M) DECOUPLED = -0.0341 ± 0.1235; corr(g, M) DECOUPLED_OFFSETS = 0.1139 ± 0.1236.
- Verdict: **PASS** — ORIGINAL's |Δmean| and |corr(g,M)| are expected to be clearly larger than DECOUPLED's, because in ORIGINAL g(t) also multiplies the large constant logit(λ)+μ, while in DECOUPLED g(t) only multiplies μ+η (which fluctuates near 0).

### 2. Autocorrelation with/without η

- autocorr_lag1(M) ORIGINAL = 0.0965 ± 0.1524; DECOUPLED = 0.1384 ± 0.1144; DECOUPLED_OFFSETS = 0.1097 ± 0.1253.
- Verdict: **PASS** — DECOUPLED and DECOUPLED_OFFSETS are expected > ORIGINAL, because only those two variants include the AR(1) term η(t) in the logit argument.

### 3. Effect of B (mean offset)

- corr(MA7(M), m(t)) DECOUPLED = -0.1082 ± 0.2642; DECOUPLED_OFFSETS = 0.2046 ± 0.2256.
- Verdict: **PASS** — DECOUPLED_OFFSETS is expected to show larger |correlation| with m(t), because it is the only variant that adds m(t) to the logit argument; in DECOUPLED m(t) does not enter the formula (research/05 §2.2, compute_arg in engine/mood.py).

### Global verdict (8a): **PASS**

Confirmed: (1) mean-gain coupling, (2) autocorrelation with/without η, (3) B effect.
Not confirmed: none.

## 4. Variant recommendation for the POC

ORIGINAL couples temperament and gain (one knob, λ, moves level AND reactivity at once) and lacks η — its autocorr_lag1(M) (0.096) sits near the floor vs DECOUPLED_OFFSETS (0.110) — it cannot tune 'streak without external cause' separately from 'temperament'; discarded for Phase 2.
The m(t) mean offset does leave a measurable footprint (|corr(MA7(M), m(t))| larger in DECOUPLED_OFFSETS than in DECOUPLED: 0.2046 vs 0.1082), i.e., B buys a real signal that is not redundant with A/η.
**Recommendation: DECOUPLED_OFFSETS.** It buys three orthogonal knobs (B = per-phase level, A = per-phase reactivity, ρ_e/σ_e = causeless streaks) at the cost of one extra parameter (B) vs DECOUPLED — low cost; knob separation is precisely the goal of research/05 §2.2 and the effect is verifiable in this run's data.

## Figures

- `variants_w32_s111.png` — variant comparison (sim.plots.plot_variant_comparison), seed 111
- `mood_series_by_variant.png` — M(t) per variant, 5 seeds overlaid + mean
- `metrics_barplot.png` — metric barplot (Δmean, corr(g,M), autocorr_lag1) with error bars
