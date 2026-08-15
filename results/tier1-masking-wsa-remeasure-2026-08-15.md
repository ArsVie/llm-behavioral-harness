# WS-A re-measure: FULL vs STRUCTURED_NO_STATE (model-free, n=500)

- Date: 2026-08-15 — branch wip/tier1-masking (fixed day-0 plan path)
- Driver: `experiments/tier1_wsa_remeasure.py` → `experiments.cvs_common.run_cell` (fake client, perturb=True, 30 days, checkpoints [7, 14, 21, 26, 29], default memory policy)
- Model-free: deterministic fake client + scripted judge; counts flow through the real AsyncRuntime → engine.timing.next_event path; engine/ and harness/ untouched.
- Seeds: 5001..5500 (same base as the G0 500-seed rerun); paired by seed.
- Elapsed: 2257.8 s (workers=16).

## Per-arm proactive counts (per 30-day run)

| Arm | mean | sd | 95% CI | min | max |
|---|---|---|---|---|---|
| FULL | 47.654 | 3.909072 | 47.310528 – 47.997472 | 37 | 60 |
| STRUCTURED_NO_STATE | 41.524 | 3.143435 | 41.247801 – 41.800199 | 31 | 52 |

## Delta (FULL − SNS, paired by seed)

- Absolute: mean 6.13 proactives, 95% CI [5.745521, 6.514479]
- Relative (mean-based): 14.76%
- Relative (paired ratios): mean 15.25%, 95% CI [14.26%, 16.24%]

## Verdict on the ≈ +10% prediction

- Prediction: FULL exceeds SNS by ~+10% (basis: stored-run S_d mean 1.3123, G0 rerun).
- Verdict: **REFUTED** (target 10% ∈ paired-ratio CI: no; delta>0 at 95%: yes — measured +15.25%, CI [14.26%, 16.24%]).

## Pre-fix A/B (sign-flip check, same recipe at 653de09)

The identical recipe (same driver, same seeds, fake client) was run against the
pre-fix code in a temporary worktree at commit 653de09 (pre-fix
`plan_and_persist(scores=None)` day-0 plan confirmed in that tree's
experiments/cvs_common.py:1072):

| Run | FULL mean | SNS mean | Δabs [95% CI] | Δrel paired [95% CI] |
|---|---|---|---|---|
| Pre-fix (653de09) | 47.508 | 41.518 | 5.99 [5.60, 6.38] | +14.92% [13.92%, 15.91%] |
| Fixed (772b0f0+) | 47.654 | 41.524 | 6.13 [5.75, 6.51] | +15.25% [14.26%, 16.24%] |

Fix contribution: FULL +0.146 proactives/30d (~+0.3%), not significant
(per-arm se ≈ 0.175 ⇒ Δse ≈ 0.247); SNS unchanged (+0.006, replicate noise).

## Attribution correction (important)

The brief's mechanism story — "FULL ≡ SNS on day 0 → the −1.35% artifact; the
fix flips it to ~+10%" — is falsified in both parts:

1. The ~+15% state effect is PRE-EXISTING: pre-fix code at n=500 measures
   +14.92%, not −1.35%. The recorded −1.35% (it3, n=5, real model) was noise,
   not a day-0 artifact.
2. The day-0 fix's measurable contribution is ~+0.3pp (one day of thirty), not
   a ~10pp flip.

The WS-A fix remains correct and required (it aligns the experiment paths with
production `_replan` semantics — real day-0 state, fired-event integrity,
INSERT OR IGNORE never resurrects fired rows — and G0's "−1.35% was the
up-front-neutral-plan artifact" attribution is hereby corrected: the effect was
masked by n=5 measurement noise, not by the neutral day-0 plan).

## Model-free note

All counts come from the deterministic fake client path (`run_cell(fake=True)`, `DeterministicClient` + `DeterministicJudge`). These are NOT LLM outputs; they measure the engine/scheduler behavior (state → plan → proactive firing) under each condition. The it3 free-check showed fake-client proactive counts match real-cell counts (FULL=48, SNS=45, NTF=54 at seeds 5001–5005). The day-0 plan here goes through the FIXED path: `session.ensure_day(0)` then `day_scores(store, 0, timing)` (commit 772b0f0); the pre-fix path planned day 0 with `scores=None` (neutral, all arms identical on day 0).

Full per-seed data: `tier1-masking-wsa-remeasure-2026-08-15.json`.
