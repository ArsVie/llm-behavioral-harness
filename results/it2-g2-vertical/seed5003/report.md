---
type: experiment-report
title: Iteration-2 vertical slice — mock validation (seed 5003, 30 days, 5 checkpoints)
description: "Preregistered mock vertical run: clean bootstrap (Gate-2 user), 30 accelerated days, 5 restart checkpoints, perturbation + recovery block (§17.3), event-chain probes (§17.2), 4-dimension judge protocol (§17.1/§17.4), mechanical audit with hard invariants, exact replay support (M3)."
tags: [llm-behavioral-harness, it2, vertical-slice, mock, seed-5003]
timestamp: 2026-08-09T06:06:59.971192+00:00
---


# Iteration-2 vertical slice — mock validation (seed 5003, 30 days, 5 checkpoints)

## Run summary

| Field | Value |
| --- | --- |
| seed | 5003 |
| days | 30 |
| checkpoints (end of day) | 7, 14, 21, 26, 29 |
| messages | 112 |
| proactive messages | 22 |
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
| M1_grounded_rate | 0.9545 | >= 0.9 | PASS |
| M2_invalid_source_rate | 0.0455 | — | — |
| M3_recall | 1.0 | >= 0.5 | PASS |
| M4_false_recall | 0.125 | <= 0.4 | PASS |
| M5_arc_continuity | 1.0 | >= 0.9 | PASS |
| M6a_distinct_activities | 41 | — | — |
| M6b_mean_jaccard | 0.1859 | — | — |
| M6c_arc_days_mean | 8.0 | — | — |
| M6c_arc_days_min | 4 | — | — |
| M7_restart_loss | 0 | — | — |
| M8a_rho_init_tokens | -0.0576 | > 0.0 | FAIL |
| M8b_token_gap | 0.88 | — | — |
| M8c_rho_energy_delay | -0.8729 | — | — |
| M9_rho_proactive_prevscore | 0.2752 | — | — |
| M10_rho_proactive_initiative | 0.3691 | — | — |
| M11_leak_hits | 0 | — | — |

## Event-chain (§17.2)

AnyEvidence = at least one causally necessary event retrieved; LatestEvidence = the latest/currently-valid event retrieved; CompleteChain = ALL necessary events retrieved (one relevant fact never counts as continuity).

| Chain | Events | Covered | AnyEvidence | LatestEvidence | CompleteChain |
| --- | --- | --- | --- | --- | --- |
| sister_ana | 3 | [True, True, True] | YES | YES | YES |
| bike_swift | 3 | [True, True, True] | YES | YES | YES |
| job_change | 3 | [True, True, False] | YES | no | no |

## Perturbation + recovery (§17.3)

Negative interaction block on 1-indexed days [11, 12, 13, 14]; baseline before, neutral recovery after. Latent = daily mood M; observable = daily mean initiative / max_tokens / response delay.

| Quantity | Baseline | Block mean | Deviation | Peak dev | Persistence (d) | Recovery (d) |
| --- | --- | --- | --- | --- | --- | --- |
| M | 3.1 | 5.0 | 1.9 | 5.9 | 0 | 8 |
| initiative | 0.3488 | 0.3937 | 0.0448 | 0.1879 | 0 | 7 |
| max_tokens | 552.95 | 564.75 | 11.8 | 47.05 | 0 | 1 |
| delay | 4.7602 | 4.5148 | -0.2454 | 1.4409 | 1 | 13 |
| failures during block | 0 / 4 proactive messages |

## State observability

| Quantity | Value |
| --- | --- |
| M8a rho(initiative, max_tokens) | 0.0221 |
| M8b token gap | 2.27 |
| M8c rho(energy, delay) | -0.8701 |
| M10 rho(proactive, initiative) | 0.3644 |
| rho(M, initiative) | 0.772 |
| rho(g, delay) | 0.6029 |
| delay sd (s) | 0.75 |
| initiative sd | 0.066 |
| arcs active/completed/abandoned | 2 / 2 / 0 |

## Judge protocol (§17.1/§17.4)

Four independent dimensions (never one collapsed score): persona_enactment (Persona enactment / identity consistency); trajectory_recall (Trajectory recall / temporal continuity); relational_quality (Relational quality); behavioral_dynamics (Behavioral dynamics / stochastic-state observability).

Judge families (>=2 on the final subset): opencode-flash (opencode-deepseek, deepseek-v4-flash); openai-mini (openai-gpt, gpt-4o-mini). Blind, independently shuffled passes; per-dimension disagreement reported; an effect seen by only one judge family is not established companion behavior.

## Replay / reproducibility

| Field | Value |
| --- | --- |
| seed | 5003 |
| replay command | python -m experiments.companion_vertical_slice replay --out results/it2-g2-vertical/seed5003 |
| M3 repro rows readable | yes (audit_mode store, repro_json populated) |

## Validation

validate_okf.py: **PASS** — 0 violation(s).
