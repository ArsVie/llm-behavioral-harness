---
type: experiment-report
title: Iteration-2 vertical slice — mock validation (seed 5005, 30 days, 5 checkpoints)
description: "Preregistered mock vertical run: clean bootstrap (Gate-2 user), 30 accelerated days, 5 restart checkpoints, perturbation + recovery block (§17.3), event-chain probes (§17.2), 4-dimension judge protocol (§17.1/§17.4), mechanical audit with hard invariants, exact replay support (M3)."
tags: [llm-behavioral-harness, it2, vertical-slice, mock, seed-5005]
timestamp: 2026-08-09T06:07:10.203556+00:00
---


# Iteration-2 vertical slice — mock validation (seed 5005, 30 days, 5 checkpoints)

## Run summary

| Field | Value |
| --- | --- |
| seed | 5005 |
| days | 30 |
| checkpoints (end of day) | 7, 14, 21, 26, 29 |
| messages | 116 |
| proactive messages | 32 |
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
| M1_grounded_rate | 0.9062 | >= 0.9 | PASS |
| M2_invalid_source_rate | 0.0938 | — | — |
| M3_recall | 1.0 | >= 0.5 | PASS |
| M4_false_recall | 0.125 | <= 0.4 | PASS |
| M5_arc_continuity | 1.0 | >= 0.9 | PASS |
| M6a_distinct_activities | 40 | — | — |
| M6b_mean_jaccard | 0.3722 | — | — |
| M6c_arc_days_mean | 22.0 | — | — |
| M6c_arc_days_min | 22 | — | — |
| M7_restart_loss | 0 | — | — |
| M8a_rho_init_tokens | 0.379 | > 0.0 | PASS |
| M8b_token_gap | 7.32 | — | — |
| M8c_rho_energy_delay | -0.7731 | — | — |
| M9_rho_proactive_prevscore | 0.0598 | — | — |
| M10_rho_proactive_initiative | 0.1098 | — | — |
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
| M | 4.8 | 6.75 | 1.95 | 3.8 | 0 | 2 |
| initiative | 0.3762 | 0.4741 | 0.0979 | 0.1288 | 0 | 2 |
| max_tokens | 546.35 | 595.4167 | 49.0667 | 53.65 | 4 | 1 |
| delay | 4.6825 | 4.7986 | 0.1161 | 1.4189 | 0 | 7 |
| failures during block | 0 / 5 proactive messages |

## State observability

| Quantity | Value |
| --- | --- |
| M8a rho(initiative, max_tokens) | 0.5294 |
| M8b token gap | 19.02 |
| M8c rho(energy, delay) | -0.7946 |
| M10 rho(proactive, initiative) | 0.1199 |
| rho(M, initiative) | 0.835 |
| rho(g, delay) | 0.5155 |
| delay sd (s) | 0.777 |
| initiative sd | 0.06 |
| arcs active/completed/abandoned | 2 / 1 / 1 |

## Judge protocol (§17.1/§17.4)

Four independent dimensions (never one collapsed score): persona_enactment (Persona enactment / identity consistency); trajectory_recall (Trajectory recall / temporal continuity); relational_quality (Relational quality); behavioral_dynamics (Behavioral dynamics / stochastic-state observability).

Judge families (>=2 on the final subset): opencode-flash (opencode-deepseek, deepseek-v4-flash); openai-mini (openai-gpt, gpt-4o-mini). Blind, independently shuffled passes; per-dimension disagreement reported; an effect seen by only one judge family is not established companion behavior.

## Replay / reproducibility

| Field | Value |
| --- | --- |
| seed | 5005 |
| replay command | python -m experiments.companion_vertical_slice replay --out results/it2-g2-vertical/seed5005 |
| M3 repro rows readable | yes (audit_mode store, repro_json populated) |

## Validation

validate_okf.py: **PASS** — 0 violation(s).
