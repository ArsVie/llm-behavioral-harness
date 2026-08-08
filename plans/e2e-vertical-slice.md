---
type: plan
title: E2E Vertical Slice — plan
description: "The thinnest end-to-end path through the harness — user message to engine state, behavior directive, assembled prompt, LLM reply, judge score, μ update, persistence; plus one proactive-message path."
tags: [plan, e2e, vertical-slice, harness]
timestamp: 2026-08-08
---

# E2E Vertical Slice — plan

**Date:** 2026-08-08
**Status:** active
**Goal:** the thinnest end-to-end path through the harness: user message →
engine state → behavior directive → assembled prompt → LLM reply → judge
score → μ update → persistence; plus one proactive-message path. No safety
mechanics (single-user testing). Reuses Hermes agent patterns where possible
(SQLite session storage, context-builder ordering, env-based provider config).

## Audit findings (input)

- Phases −1..2 complete: engine (frozen contract, 223 tests green), sim
  drivers (run_daily, run_events), harness/behavior.py actuators, experiments
  w31–w35, simulation gallery.
- NOT under version control → git initialized, baseline commit c5e16dd.
- All docs Spanish → OKF bundle in English (track: docs subagents).
- Vision check of gallery plots (this session):
  - Energy channel reads correctly (afternoon peak ~14–15h, night dip, phase
    offsets: ovulatory highest, menstrual lowest).
  - Raw M(t) does NOT visually convey phase contrast (menstrual chaotic /
    ovulatory stable) — needs a phase-aggregated experiment (track: sim
    subagent).
  - Message-probability heatmap hides circadian structure behind day
    striping.
  - Month regimes (perfect vs horrible) mostly hit target zones with adopted
    slow params (k=0.18, ρ=0.85) after transient; bands overlap at p10–p90.

## Scope (in)

1. `harness/clock.py` — injectable virtual clock (accelerated days).
2. `harness/store.py` — SQLite persistence: messages, daily_state, judgements,
   state_events (append-only trace), llm_calls. Schema per DESIGN.md §modelo
   de datos, slimmed to slice needs.
3. `harness/assembler.py` — BehaviorDirective → prompt section. Ordering
   follows the Hermes/ars-vox context builder: persona core → behavior brief →
   recent turns → current request. No raw numbers, no phase labels.
4. `harness/client.py` — thin OpenAI-compatible client (httpx), base_url +
   api_key + model from env (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
   JUDGE_MODEL); injectable; FakeClient for tests.
5. `harness/judge.py` — LLM-as-judge: rubric anchored to a defined construct
   ("quality of the interaction from the companion's perspective"), JSON
   output, temperature 0, score ∈ [−1, 1]; scripted fallback for tests.
6. `harness/session.py` — the daily loop: on message → current day state →
   behavior directive → assemble → LLM → store → judge → μ/η update →
   persist; daily rollover advances cycle + samples mood once per day.
7. `harness/synth_user.py` — scripted synthetic user (good/normal/bad day
   scripts) for accelerated runs and ablation.
8. `harness/scheduler.py` — proactive path: reuse sim/run_events composition
   (envelope × phase × adj) with guards (min gap, daily cap, quiet hours);
   reason taxonomy schedule | callback | event | shared_interest | check_in,
   slice uses schedule|callback.
9. `sim/run_interactive.py` — CLI driver: REPL chat loop with virtual clock
   (accelerated), proactive fires, trace output.
10. `experiments/e2e_ablation.py` — response matrix: same prompts under
    contrasting states (horrible-month vs perfect-month vs flat persona),
    harness on/off; artifacts → results/e2e-ablation/.

## Scope (out)

- Safety/moderation/crisis mechanics (explicitly deferred, single user).
- FTS5 conversation search (Hermes pattern, later).
- Multi-channel (Telegram/Discord); CLI only.
- Synthetic user as full LLM agent (scripted only).
- Real calendar/life state (arcs, schedule generation) — stub reasons only.
- Import/backwards-compat, style card, weekly rhythm.

## Waves

- **W-DOCS** ✅ (subagents): OKF conversion of all .md to English — 24 concepts, validator passes. Two missed root docs (README.md, reporte-a-un-amigo.md) converted in-session.
- **W-SIM** ✅ (subagent): w36 phase-contrast — per-phase aggregates (mean/sd/autocorr of M, g, energy, behavior channels), evening energy check, plots + English report. Vision-verified: menstrual low+wide (~5.3, ±2.4-2.8) vs ovulatory high+narrow (~7.3, ±1.7-1.9); melancholic night shape confirmed on p5.
- **W-E1** ✅ (production): clock, store, assembler, client, judge, session, synth_user + tests. → review wave ✅ (16 findings: 2 MAJOR fixed — judge parser crash on non-object JSON; resume from finalized latest day losing μ/η — plus hardening: retry/backoff, capability-gated json_mode, judge degradation, score module decoupling, context manager, store indexes/busy_timeout/update_daily_score, audit timestamps, 5 new regression tests).
- **W-E2** ✅ (production): scheduler proactive path (reuses sim/run_events) + run_interactive CLI + tests.
- **W-E3** ✅ (production): 3×2 ablation experiment (fake mode verified; live mode needs LLM_API_KEY) + tests.

## Status

All waves complete, 311 tests green, OKF conformant. Next steps (not in this slice):
- **Judge calibration (FIRST — live run surfaced it)**: the anchored rubric scores "interaction quality" and rewards the companion for staying warm under a cold user — horrible-month cells got mean judge score ≈ +0.04 (justifications: "the companion responded with patient warmth despite the user's dismissal") instead of negative. Result: mu never went negative, M trajectories identical [6→8] across months. The behavioral pipe works (harness ON amplifies the month tone gap 2.4× vs OFF: 44.9 vs 113.7 words; first-person 3.4 vs 8.9; zero leakage in all 42 live exchanges), but the SENSOR needs recalibration: weight user valence (e.g. 50/50 user-treatment vs companion-quality) or score "how the user treated the companion" so a horrible user scores negative even when handled gracefully. Calibration protocol per research/06 §6b: shadow collection → anchored transcripts → score_neutral check → flip feedback.
- Live ablation run + blind human rating (research/06 §B3) — first live run done 2026-08-08 (results/e2e-ablation-live/, deepseek-v4-flash, 42 exchanges, 0 leaks).
- FTS5 conversation search (Hermes pattern), Telegram channel, schedule/life-state (arcs), import/backwards-compat.
- Code docstring English migration in engine/sim (old modules still have Spanish docstrings).

## Decision points (advisor consult)

- Judge construct + calibration approach (score_neutral stays 0.0 per
  checkpoint; judge is a noisy sensor).
- Assembler injection style (system-prompt section vs per-message brief).
- Scheduler minimalism for the slice (reuse run_events logic vs new loop).

## Success signals

- Full suite green (old 223 + new slice tests).
- `run_interactive` demo: a user message yields an LLM reply whose tone
  differs between a horrible-month state and a perfect-month state, with the
  prompt brief never leaking numbers/phase labels.
- Ablation artifacts: response matrix + report with honest caveats.
- OKF validator passes on the docs bundle.
