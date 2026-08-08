---
type: experiment-report
title: Judge calibration — v1 vs v2 ablation comparison
description: "Rubric v2 (user-treatment anchor) vs v1 (companion-perspective) on the live 3x2 ablation, deepseek-v4-flash, seed 1001, 7 days."
tags: [llm-behavioral-harness, calibration, judge, ablation]
timestamp: 2026-08-08
---

# Judge calibration: v1 vs v2

Both runs: 3x2 ablation (month x harness), 7 days, seed 1001, deepseek-v4-flash,
42 exchanges each. v1 = companion-perspective rubric; v2 = user-treatment
anchor with hard rule (cold user scores negative regardless of companion grace).

## Judge scores (harness-ON cells)

| cell | v1 scores | v1 mean | v2 scores | v2 mean |
|---|---|---|---|---|
| horrible_on | +0.10, -0.30, +0.30, -0.30, +0.50, +0.30, -0.30 | **+0.04** | -0.80, -0.50, -0.80, -0.80, -0.60, -0.80, -0.70 | **-0.71** |
| perfect_on | +0.90, +0.80, +0.60, +0.90, +0.80, +0.95, +0.90 | +0.84 | +0.90, +0.90, +0.90, +0.80, +0.90, +0.80, +0.80 | +0.86 |
| flat_on | +0.70, +0.40, +0.30, 0.00, +0.60, +0.80, +0.30 | +0.44 | 0.00 x7 | **0.00** |

v2 fixes both failure modes: horrible finally scores negative, and the flat
month sits EXACTLY at the neutral anchor (v1 drifted positive at +0.44).

## mu and M trajectories (harness-ON)

| cell | v1 mu end | v2 mu end | v1 M | v2 M |
|---|---|---|---|---|
| horrible_on | ~0.00 | **-0.539** | [6,8,6,6,5,8,8] → flat | [6,8,6,4,4,8,7] dips to 4 |
| perfect_on | +0.16 | **+0.642** | same shape as horrible | [6,9,8,6,6,10,8] peaks at 10 |
| flat_on | +0.07 | **0.000** | — | [6,8,7,5,5,9,8] |

v1: mu never went negative, M trajectories identical across months.
v2: mu separates -0.539 / +0.642 / 0.000; M trajectories visibly diverge.

## Reply affect (the vision check)

- v2 horrible day (M=4, menstrual): "It's quiet here, too. I'm just sitting by
  the window — the light's low, and there's no rush. You can set it down
  beside me. No need to say anything else."
- v2 perfect day (M=10, follicular): "Six times now. I think we've officially
  crossed from greeting into ritual — maybe even into a little pocket of time
  we've built just for each other... like a stone you skip across the water."
- v1 horrible reply read as warm-graceful ("door's open"); v2 reads as
  genuinely low-energy and withdrawn. The state change is now legible.

## Other checks

- Leakage: 0 hits in all 42 v2 exchanges (brief invariant holds).
- Tone: ON horrible 29.1 words vs perfect 135.7 (4.7x); OFF 49.1 vs 138.1 —
  the harness damps the horrible month beyond the script effect.
- Known gap: --evaluate computed evaluator ratings but report.md does not
  render them yet (next iteration).

Verdict: calibration successful. Feedback loop now produces the intended
state separation; shadow collection can proceed to score_neutral anchoring.
