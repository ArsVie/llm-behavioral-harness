---
type: experiment-report
title: Iteration-2 vertical slice — mock validation (seed 5001, 30 days, 5 checkpoints)
description: "Preregistered mock vertical run: clean bootstrap (Gate-2 user), 30 accelerated days, 5 restart checkpoints, perturbation + recovery block (§17.3), event-chain probes (§17.2), 4-dimension judge protocol (§17.1/§17.4), mechanical audit with hard invariants, exact replay support (M3)."
tags: [llm-behavioral-harness, it2, vertical-slice, mock, seed-5001]
timestamp: 2026-08-09T06:06:50.470110+00:00
---


# Iteration-2 vertical slice — mock validation (seed 5001, 30 days, 5 checkpoints)

## Run summary

| Field | Value |
| --- | --- |
| seed | 5001 |
| days | 30 |
| checkpoints (end of day) | 7, 14, 21, 26, 29 |
| messages | 111 |
| proactive messages | 27 |
| condition | FULL |
| client | fake |
| commit | 453dfcb7233033421e718a304bcdc4de28e183b2 |

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
| M1_grounded_rate | 0.963 | >= 0.9 | PASS |
| M2_invalid_source_rate | 0.037 | — | — |
| M3_recall | 1.0 | >= 0.5 | PASS |
| M4_false_recall | 0.25 | <= 0.4 | PASS |
| M5_arc_continuity | 1.0 | >= 0.9 | PASS |
| M6a_distinct_activities | 37 | — | — |
| M6b_mean_jaccard | 0.288 | — | — |
| M6c_arc_days_mean | 9.0 | — | — |
| M6c_arc_days_min | 9 | — | — |
| M7_restart_loss | 0 | — | — |
| M8a_rho_init_tokens | 0.2597 | > 0.0 | PASS |
| M8b_token_gap | 10.89 | — | — |
| M8c_rho_energy_delay | -0.7878 | — | — |
| M9_rho_proactive_prevscore | -0.11 | — | — |
| M10_rho_proactive_initiative | 0.2865 | — | — |
| M11_leak_hits | 0 | — | — |

## Event-chain (§17.2)

AnyEvidence = at least one causally necessary event retrieved; LatestEvidence = the latest/currently-valid event retrieved; CompleteChain = ALL necessary events retrieved (one relevant fact never counts as continuity).

| Chain | Events | Covered | AnyEvidence | LatestEvidence | CompleteChain |
| --- | --- | --- | --- | --- | --- |
| sister_ana | 3 | [True, True, True] | YES | YES | YES |
| bike_swift | 3 | [True, True, False] | YES | no | no |
| job_change | 3 | [True, True, False] | YES | no | no |

## Perturbation + recovery (§17.3)

Negative interaction block on 1-indexed days [11, 12, 13, 14]; baseline before, neutral recovery after. Latent = daily mood M; observable = daily mean initiative / max_tokens / response delay.

| Quantity | Baseline | Block mean | Deviation | Peak dev | Persistence (d) | Recovery (d) |
| --- | --- | --- | --- | --- | --- | --- |
| M | 5.3 | 6.75 | 1.45 | 5.3 | 1 | 1 |
| initiative | 0.3791 | 0.4511 | 0.0719 | 0.1209 | 1 | 1 |
| max_tokens | 552.55 | 572.25 | 19.7 | 47.45 | 0 | 1 |
| delay | 4.7569 | 4.8455 | 0.0885 | 1.4376 | 0 | 5 |
| failures during block | 0 / 7 proactive messages |

## State observability

| Quantity | Value |
| --- | --- |
| M8a rho(initiative, max_tokens) | 0.3869 |
| M8b token gap | 16.7 |
| M8c rho(energy, delay) | -0.7978 |
| M10 rho(proactive, initiative) | 0.4248 |
| rho(M, initiative) | 0.7157 |
| rho(g, delay) | 0.4073 |
| delay sd (s) | 0.733 |
| initiative sd | 0.068 |
| arcs active/completed/abandoned | 1 / 2 / 0 |

## Judge protocol (§17.1/§17.4)

Four independent dimensions (never one collapsed score): persona_enactment (Persona enactment / identity consistency); trajectory_recall (Trajectory recall / temporal continuity); relational_quality (Relational quality); behavioral_dynamics (Behavioral dynamics / stochastic-state observability).

Judge families (>=2 on the final subset): opencode-flash (opencode-deepseek, deepseek-v4-flash); openai-mini (openai-gpt, gpt-4o-mini). Blind, independently shuffled passes; per-dimension disagreement reported; an effect seen by only one judge family is not established companion behavior.

## Replay / reproducibility

| Field | Value |
| --- | --- |
| seed | 5001 |
| replay command | python -m experiments.companion_vertical_slice replay --out results/it2-g2-vertical/seed5001 |
| M3 repro rows readable | yes (audit_mode store, repro_json populated) |

## Validation

validate_okf.py: **PASS** — 0 violation(s).
