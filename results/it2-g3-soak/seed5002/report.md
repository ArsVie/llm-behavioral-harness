---
type: experiment-report
title: Iteration-2 vertical slice — mock validation (seed 5002, 120 days, 5 checkpoints)
description: "Preregistered mock vertical run: clean bootstrap (Gate-2 user), 120 accelerated days, 5 restart checkpoints, perturbation + recovery block (§17.3), event-chain probes (§17.2), 4-dimension judge protocol (§17.1/§17.4), mechanical audit with hard invariants, exact replay support (M3)."
tags: [llm-behavioral-harness, it2, vertical-slice, mock, seed-5002]
timestamp: 2026-08-09T06:25:39.072939+00:00
---


# Iteration-2 vertical slice — mock validation (seed 5002, 120 days, 5 checkpoints)

## Run summary

| Field | Value |
| --- | --- |
| seed | 5002 |
| days | 120 |
| checkpoints (end of day) | 25, 50, 75, 100, 115 |
| messages | 315 |
| proactive messages | 87 |
| condition | FULL |
| client | fake |
| commit | b5cdd52392c1fd959c7987d6eb3b8812dc662c4c |

## Mechanical audit

| Hard invariant | Value (must be 0) |
| --- | --- |
| ungrounded_proactive | 0 |
| wrong_intent | 0 |
| restart_state_loss | 0 |
| stranded_opportunities | 0 |
| cycle_state_leakage | 0 |
| memory_provenance_failures | 0 |
| duplicate_turns | 0 |
| life_dead_duration | 0 |
| i4_violations | 0 |
| fixture inserts (user/assistant) | {'user_ok': True, 'user_bad': [], 'assistant_ok': True, 'assistant_bad': []} |

## Metrics vs frozen thresholds

| Metric | Value | Bar | PASS/FAIL |
| --- | --- | --- | --- |
| M1_grounded_rate | 0.931 | >= 0.9 | PASS |
| M2_invalid_source_rate | 0.069 | — | — |
| M3_recall | 0.375 | >= 0.5 | FAIL |
| M4_false_recall | 0.0 | <= 0.4 | PASS |
| M5_arc_continuity | 1.0 | >= 0.9 | PASS |
| M6a_distinct_activities | 54 | — | — |
| M6b_mean_jaccard | 0.3306 | — | — |
| M6c_arc_days_mean | 8.0 | — | — |
| M6c_arc_days_min | 3 | — | — |
| M7_restart_loss | 0 | — | — |
| M8a_rho_init_tokens | 0.4146 | > 0.0 | PASS |
| M8b_token_gap | 11.57 | — | — |
| M8c_rho_energy_delay | -0.6454 | — | — |
| M9_rho_proactive_prevscore | 0.1755 | — | — |
| M10_rho_proactive_initiative | -0.0305 | — | — |
| M11_leak_hits | 0 | — | — |

## Event-chain (§17.2)

AnyEvidence = at least one causally necessary event retrieved; LatestEvidence = the latest/currently-valid event retrieved; CompleteChain = ALL necessary events retrieved (one relevant fact never counts as continuity).

| Chain | Events | Covered | AnyEvidence | LatestEvidence | CompleteChain |
| --- | --- | --- | --- | --- | --- |
| sister_ana | 3 | [True, True, False] | YES | no | no |
| bike_swift | 3 | [True, True, False] | YES | no | no |
| job_change | 3 | [True, True, False] | YES | no | no |

## Perturbation + recovery (§17.3)

Negative interaction block on 1-indexed days [11, 12, 13, 14]; baseline before, neutral recovery after. Latent = daily mood M; observable = daily mean initiative / max_tokens / response delay.

| Quantity | Baseline | Block mean | Deviation | Peak dev | Persistence (d) | Recovery (d) |
| --- | --- | --- | --- | --- | --- | --- |
| M | 4.5 | 4.5 | 0.0 | 5.5 | 0 | 1 |
| initiative | 0.3719 | 0.4483 | 0.0764 | 0.1822 | 0 | 12 |
| max_tokens | 553.7 | 582.25 | 28.55 | 46.3 | 15 | 12 |
| delay | 4.6974 | 5.133 | 0.4356 | 1.6997 | 0 | 1 |
| failures during block | 0 / 3 proactive messages |

## State observability

| Quantity | Value |
| --- | --- |
| M8a rho(initiative, max_tokens) | 0.5404 |
| M8b token gap | 20.52 |
| M8c rho(energy, delay) | -0.6542 |
| M10 rho(proactive, initiative) | 0.199 |
| rho(M, initiative) | 0.7508 |
| rho(g, delay) | 0.4775 |
| delay sd (s) | 0.792 |
| initiative sd | 0.077 |
| arcs active/completed/abandoned | 2 / 11 / 0 |

## Judge protocol (§17.1/§17.4)

Four independent dimensions (never one collapsed score): persona_enactment (Persona enactment / identity consistency); trajectory_recall (Trajectory recall / temporal continuity); relational_quality (Relational quality); behavioral_dynamics (Behavioral dynamics / stochastic-state observability).

Judge families (>=2 on the final subset): opencode-flash (opencode-deepseek, deepseek-v4-flash); openai-mini (openai-gpt, gpt-4o-mini). Blind, independently shuffled passes; per-dimension disagreement reported; an effect seen by only one judge family is not established companion behavior.

## Replay / reproducibility

| Field | Value |
| --- | --- |
| seed | 5002 |
| replay command | python -m experiments.companion_vertical_slice replay --out results/it2-g3-soak/seed5002 |
| M3 repro rows readable | yes (audit_mode store, repro_json populated) |

## Validation

validate_okf.py: **PASS** — 0 violation(s).
