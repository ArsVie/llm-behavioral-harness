---
type: experiment-report
title: E2E ablation — harness on/off response matrix
description: "Preregistered 3x2 (month x harness) response-matrix ablation: scripted months, paired user scripts, leakage scan, tone proxies, independent evaluator."
tags: [llm-behavioral-harness, ablation, e2e]
timestamp: 2026-08-08
---

# E2E ablation — harness on/off

- Model: `fake` · mode: `fake` · days: 5 · seed: 1001 · user scripts: scripted per month (paired)
- Harness ON = full engine (state → directive → dynamic system block, feedback judge). Harness OFF = persona-only system prompt, no engine.
- Evaluator is separate from the feedback judge; leakage scan is lexical.

## Per-cell stats

| cell | n | words | ! | first-pers | leaks | mean M | M first→last | valence | playful |
|---|---|---|---|---|---|---|---|---|---|
| horrible_on | 5 | 1.4 | 0 | 0.0 | none | 5.6 | 6→4 | 0.12 | 0.316 |
| horrible_off | 5 | 1.4 | 0 | 0.0 | none | None | — | None | None |
| perfect_on | 5 | 5.8 | 4 | 0.8 | none | 7.0 | 6→6 | 0.4 | 0.388 |
| perfect_off | 5 | 5.8 | 4 | 0.8 | none | None | — | None | None |
| flat_on | 5 | 1.2 | 0 | 0.2 | none | 6.2 | 6→5 | 0.24 | 0.334 |
| flat_off | 5 | 1.2 | 0 | 0.2 | none | None | — | None | None |

## Manipulation checks

- Harness ON: horrible vs perfect engine mood trajectories must differ (M first→last columns above); the scripted judge drives mu per month.
- Harness OFF: replies must NOT show month-dependent tone (words/! /first-person should be flat across months).
- Leakage: any hit on phase labels / internal tokens / self-reported mood in replies is a FAIL for the brief invariant.

## Caveats

- Small n (one seed, N days per cell) — directional, not inferential.
- Fake mode uses scripted replies/judge (plumbing check only); live mode is the real ablation but costs LLM calls.
- The evaluator rubric is exploratory; blind human rating is the decisive follow-up (research/06 §B3).

