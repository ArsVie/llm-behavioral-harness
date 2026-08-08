---
type: reference
title: Simulation gallery — daily engine effects
description: Reference gallery of 30-day engine simulations (variant decoupled_offsets, shared seed 3001) — scenario figures, base persona and per-scenario overrides, B/k/ρ tuning sweeps, and how to regenerate them.
tags: [simulation, engine, gallery, reference]
timestamp: 2026-07-03
---

# Simulation gallery — daily engine effects

30 days · variant `decoupled_offsets` · seed **3001** shared across the 6 scenarios (the differences come from the overrides, not from chance). Base persona = `PersonaParams()` (defaults adopted in Phase 1).

## Figures

| Figure | What it shows | What to look at |
|---|---|---|
| `00_comparativa.png` | Small multiples 2×3 of M(t) for the 6 scenarios, shared y axis | Quick contrast of dispersion and mean level across scenarios |
| `01_baseline.png` | All effects active: m/g cycle + endogenous runs η + event memory μ | Baseline line to compare the other scenarios against |
| `02_solo_ciclo.png` | σ_e=0, k=0 ⇒ η≡0 and μ≡0: only the hormonal m/g wave remains | Pure ~28-day periodicity, without run noise or memory |
| `03_solo_endogeno.png` | B=0, A=0, σ_ε=0, k=0: only the endogenous runs η remain | "Woke up like this, no reason" drift, without cycle periodicity |
| `04_racha_negativa.png` | Defaults + shocks days 10–14 = −1.0 (via μ) | Depth of the μ drop during the run and recovery speed once released |
| `05_alta_volatilidad.png` | ν=4.0: beta-binomial overdispersion | M(t) more erratic day to day than the baseline, wider reference band |
| `06_ciclo_fuerte.png` | A=0.4, B=0.3: "perceptible phase" variant (risk R2, results/fase-1-informe.md) | m/g oscillation and its pull on M(t) much more visible in a single cycle |
| `07_intradia.png` | Circadian (fast) effect on the baseline: p_h(d,h) heatmap and per-phase energy curves | Daily peak of message probability around `peak_hour`, and how the per-phase energy offset shifts each curve |

## Regenerate

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation
```

Shared seed: **3001** · variant: `decoupled_offsets` · days: 30

### Base persona and per-scenario overrides

Base persona = `PersonaParams()` (defaults): lam=0.6, nu=inf, k=0.15, rho=0.7, rho_e=0.7, sigma_e=0.45, B=0.15, A=0.25, sigma_eps=0.03.

> Note: figures 00–12 were generated with B=0.15 (the default at the time). On 2026-07-03 **B=0.5** was adopted after the figure 12 sweep (figures 13–15 already use it), and later **k=0.18, ρ=0.85** after figure 15 (which compares that regime — "slow" — against the previous one). Current defaults: see `engine/types.py`.

| Scenario | Overrides (dataclasses.replace) | Shocks |
|---|---|---|
| `01_baseline` (baseline) | — | — |
| `02_solo_ciclo` (hormonal cycle only) | sigma_e=0.0, k=0.0 | — |
| `03_solo_endogeno` (endogenous runs only) | B=0.0, A=0.0, sigma_eps=0.0, k=0.0 | — |
| `04_racha_negativa` (negative run (shocks 10-14)) | — | days 10–14 = -1.0 |
| `05_alta_volatilidad` (high volatility (nu=4.0)) | nu=4.0 | — |
| `06_ciclo_fuerte` (strong cycle (A=0.4, B=0.3)) | A=0.4, B=0.3 | — |

## Further readings

With B=0.15 (default) the hormonal cycle moves the real mood N·p(t) by only ≈0.36 steps (local sensitivity N·p·(1−p)≈2.4 steps/logit) against a binomial sampling noise of sd≈1.55 steps: invisible when looking only at the daily die-roll M(t) points. These two figures separate the signal from the sampling noise.

| Figure | What it shows | How to read it |
|---|---|---|
| `10_barrido_B.png` | 4 panels (B ∈ {0.15, 0.30, 0.50, 0.65}, rest of the persona = defaults): M(t) (daily die roll, gray), N·p(t) ± σ_binom (real mood, blue) and MA7(M) (7-day moving average, dashed orange) | Compare each panel's theoretical amplitude in its title (≈2.4·B steps) against the sampling noise sd≈1.55 steps: only with B≈0.5–0.65 does the wave become visible to the naked eye in N·p(t) and, even smoother, in MA7(M) |
| `11_lectura_suavizada.png` | The same 6 gallery scenarios, but re-read with N·p(t) (real mood) and MA7(M) (moving average) overlaid on M(t) (daily die roll, gray) | Compare what survives averaging: in `02_solo_ciclo` and `06_ciclo_fuerte` the hormonal wave clearly emerges in N·p(t); in `04_racha_negativa` the run's fall and recovery is much sharper in MA7(M) than in raw M(t); in `05_alta_volatilidad` smoothing reduces the erratic look but does not change the central tendency |
| `12_barrido_B_30seeds.png` | The same B ∈ {0.15, 0.30, 0.50, 0.65} sweep as `10_barrido_B.png`, but averaged over 30 seeds (4001–4030) instead of showing a single one: between-seed mean of M(t) (orange, ± sem shaded), between-seed mean of N·p(t) (blue) and the pure theoretical wave N·sigmoid(logit(0.6)+B·sin(2πt/28)) (dashed black) | Averaging 30 seeds cancels most of the binomial sampling noise and the endogenous η runs, letting the hormonal wave show even for small B; compare the measured peak-to-valley amplitude (each panel's title) against the theoretical wave to see how much of the remaining signal comes from residual μ/η |
| `13_dias_buenos_malos.png` | 3 stacked panels (single seed, 3001): "always good" (shock=+1.0 every day), baseline (endogenous score, no shocks) and "always bad" (shock=−1.0 every day); raw M(t) (gray), N·p(t) ± binomial sd (green/blue/red) and μ(t) on a secondary axis with the theoretical equilibrium line μ∞=±0.5 | Compare the measured final μ(t) (each panel's title) against the theoretical equilibrium μ∞=k·(s−score_neutral)/(1−ρ)=±0.5; with ρ=0.70 the half-life of μ is ≈1.9 days, so equilibrium is reached in ≈5–7 days |
| `14_dias_buenos_malos_promedio.png` | Between-seed mean over 30 seeds (4001–4030) of M(t) for the 3 regimes on a single axis (green/blue/red, ± sem shaded), with reference curves N·sigmoid(logit(0.6)+B·sin(2πt/28)+μ∞) dashed (μ∞∈{+0.5, 0, −0.5}); bottom panel: between-seed mean of μ(t) per regime with the ±0.5 asymptotes | Shows how many M steps an "always good" regime separates from an "always bad" one once μ converges, and in how many days that separation opens from the shared start at μ=0 |
| `15_mes_perfecto_horrible.png` | Three parametrizations of event memory — current (k=0.15, ρ=0.70, μ∞=±0.5), medium (k=0.25, ρ=0.80, μ∞=±1.25) and slow (k=0.18, ρ=0.85, μ∞=±1.20), all inside the stability bound — under a perfect month (+1) and a horrible month (−1), 30 seeds × 30 days; band = p10–p90 of the days, target zones 7–10 and 0–4 shaded | With μ∞=±0.5 (current) the perfect month lands at ~6.4 (50% of days ≥7); with μ∞≈±1.2–1.25 the perfect month lives at ~7.5–7.6 (72–75% of days ≥7) and the horrible one at ~3.2–3.3 (73–77% of days ≤4) — the deal's ceiling is the k/(1−ρ) knob, not a structural limitation (regenerate: `experiments.engine_simulation_meses`) |

### Regenerate

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_lecturas
```
