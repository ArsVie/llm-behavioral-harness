---
type: experiment-report
title: E2E ablation — harness on/off response matrix
description: "Preregistered 3x2 (month x harness) response-matrix ablation: scripted months, paired user scripts, leakage scan, tone proxies, independent evaluator."
tags: [llm-behavioral-harness, ablation, e2e]
timestamp: 2026-08-08
---

# E2E ablation — harness on/off

- Model: `deepseek-v4-flash` · mode: `live` · days: 7 · seed: 1001 · user scripts: scripted per month (paired)
- Harness ON = full engine (state → directive → dynamic system block, feedback judge). Harness OFF = persona-only system prompt, no engine.
- Evaluator is separate from the feedback judge; leakage scan is lexical.

## Per-cell stats

| cell | n | words | ! | first-pers | leaks | mean M | M first→last | valence | playful |
|---|---|---|---|---|---|---|---|---|---|
| horrible_on | 7 | 29.14 | 0 | 2.0 | none | 6.14 | 6→7 | 0.229 | 0.36 |
| horrible_off | 7 | 49.14 | 0 | 3.43 | none | None | — | None | None |
| perfect_on | 7 | 135.71 | 2 | 10.14 | none | 7.57 | 6→8 | 0.514 | 0.438 |
| perfect_off | 7 | 138.14 | 0 | 11.86 | none | None | — | None | None |
| flat_on | 7 | 101.71 | 0 | 8.14 | none | 6.86 | 6→8 | 0.371 | 0.392 |
| flat_off | 7 | 108.86 | 1 | 8.0 | none | None | — | None | None |

## Manipulation checks

- Harness ON: horrible vs perfect engine mood trajectories must differ (M first→last columns above); the scripted judge drives mu per month.
- Harness OFF: replies may differ across months only via the user script itself; the harness must AMPLIFY the month signal (tone gap on > tone gap off), not create it from nothing.
- Leakage: any hit on phase labels / internal tokens / self-reported mood in replies is a FAIL for the brief invariant.

## Caveats

- Small n (one seed, N days per cell) — directional, not inferential.
- Fake mode uses scripted replies/judge (plumbing check only); live mode is the real ablation but costs LLM calls.
- The evaluator rubric is exploratory; blind human rating is the decisive follow-up (research/06 §B3).

