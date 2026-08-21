---
type: plan
title: Phase 2 — first vertical iteration of the harness
description: "Turning the validated Phase 1 engine into observable, traceable, subtle behavior — behavioral actuators (Wave P2.1) and a thirty-day emulation (Wave P2.2), before connecting a real LLM."
tags: [plan, phase-2, actuators, behavior, emulation]
timestamp: 2026-07-15
---

# Phase 2 — first vertical iteration of the harness

**Date:** 2026-07-15  
**Objective:** turn the validated Phase 1 engine into observable,
traceable and subtle behavior before connecting a real LLM.

## Product invariants

1. The complex emotional-regulation engine is preserved. `m`, `g`, `mu`,
   `eta`, the previous state and circadian energy remain distinct and
   auditable causes.
2. Valence and energy are orthogonal channels. States such as
   «content but tired» and «irritable but active» must exist.
3. The hormonal phase is a latent cause, not an acting label. It is recorded
   in the trace, but the prompt receives phenomenology, never phase stereotypes.
4. Low mood does not switch off affect. It reduces playfulness, speed or
   initiative before turning the companion cold or punishing.
5. State is shown through cadence, length, initiative, expressiveness and
   closure; not through statements like «my mood is low today».

## Review of the previous design

- **Kept:** `DECOUPLED_OFFSETS` engine, monthly memory `mu`, endogenous
  streaks `eta`, `m/g` cycle, circadian energy, reproducible RNG and trace.
- **Corrected:** «model-agnostic» now means API-compatible; brief pressure
  will need per-model-family profiles when the LLM is connected.
- **Made concrete:** fast affect stays in the model's conversation; the harness
  keeps slow state and momentum between days/sessions.
- **Brought forward:** the actuator layer (previously implicit in the assembler)
  becomes a contract tested before the LLM client, because it is the bridge
  between mathematical variance and perceived humanness.
- **Deferred:** full persona, persistent fictional life, schedule, SQLite,
  synthetic user, judge, real scheduler, Telegram and voice import.

## Wave P2.1 — behavioral actuators (implemented)

Own files:

- `harness/behavior.py`
- `tests/test_behavior.py`

Input: `DayRecord`, `TimingParams`, local hour and optional previous `DayRecord`.

Output: `BehaviorDirective` with continuous channels:

- valence, energy, momentum and reactivity;
- warmth, expressiveness, playfulness and reflection;
- initiative, length scale, suggested latency and tendency to close;
- short prompt brief;
- separate trace with phase, hormonal gain, `mu`, `eta` and mood delta.

Acceptance criteria:

- low mood keeps a warmth floor;
- energy is not conflated with valence;
- the previous state changes momentum without rewriting current mood;
- `g` changes reactivity subtly without biasing base warmth;
- the brief contains no numbers or hormonal names;
- all outputs deterministic for the same input.

## Wave P2.2 — thirty-day emulation (implemented)

Own files:

- `experiments/behavior_showcase.py`
- `tests/test_behavior_showcase.py`
- `results/behavior-showcase/30-day-behavior.png`
- `results/behavior-showcase/behavior-trace.json`
- `results/behavior-showcase/examples.md`

The emulation composes the existing engine with energy at 09:00, 14:00 and 20:00,
and derives the actuators for an evening conversation. The plot presents on a
single timeline mood, energy, affective texture and observable controls.
The JSON allows auditing causes; the Markdown teaches briefs from contrasting days.

## Gate of this iteration

1. New tests pass and the full Phase 1 suite does not regress.
2. The editable package imports `harness` and the experiment runs from CLI.
3. The thirty-day artifacts are reproducible with a fixed seed.
4. The visualization shows continuous variation, not personality jumps.
5. No brief reveals hormonal phase, internal numbers, or orders «acting sad».

## Proposed next wave

Connect this directive to a minimal prompt assembler and an injectable
OpenAI-compatible client. The decisive test of that wave will be a matrix of
responses to the same messages under contrasting states, followed by a blind
harness on/off ablation. Persona, schedule and persistent life come after
demonstrating that the actuator is audible without becoming a caricature.
