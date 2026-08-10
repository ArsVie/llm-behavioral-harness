---
type: experiment-report
title: Iteration-2 vertical slice — mock validation (seed 5001, 7 days, 5 checkpoints)
description: "Preregistered mock vertical run: clean bootstrap (Gate-2 user), 7 accelerated days, 1 restart checkpoints, perturbation + recovery block (§17.3), event-chain probes (§17.2), 4-dimension judge protocol (§17.1/§17.4), mechanical audit with hard invariants, exact replay support (M3)."
tags: [llm-behavioral-harness, it2, vertical-slice, mock, seed-5001]
timestamp: 2026-08-10T11:48:20.293378+00:00
---


# Iteration-2 vertical slice — mock validation (seed 5001, 7 days, 5 checkpoints)

## Run summary

| Field | Value |
| --- | --- |
| seed | 5001 |
| days | 7 |
| checkpoints (end of day) | 7 |
| messages | 76 |
| proactive messages | 10 |
| condition | FULL |
| client | real |
| commit | 75b82a9d6f8b442bed0ab6b371889224eff1faa0 |

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
| fixture inserts (user/assistant) | {'user_ok': False, 'user_bad': [{'id': 4, 't_h': 19.09531277604414, 'content': "There's something I've been meaning to tell you... maybe another time."}, {'id': 6, 't_h': 19.154173686221945, 'content': "Hmm, I think you're being too optimistic there."}, {'id': 8, 't_h': 19.37233586497721, 'content': "You said you'd tell me about your day — so, what happened?"}, {'id': 10, 't_h': 19.42410644650855, 'content': "You said you'd tell me about your day — so, what happened?"}, {'id': 17, 't_h': 43.3278338669969, 'content': "You said you'd tell me about your day — so, what happened?"}, {'id': 19, 't_h': 43.56609899241563, 'content': "Never mind any of that. Have you seen the new lifting routine everyone's talking"}, {'id': 21, 't_h': 43.73300155532297, 'content': "That sounds nice, but I think you're missing something."}, {'id': 28, 't_h': 67.28800432850818, 'content': 'What happened after that?'}, {'id': 30, 't_h': 67.44184310500682, 'content': 'And how did that make you feel?'}, {'id': 32, 't_h': 67.78472183522351, 'content': 'Thanks, I needed this. Talk tomorrow?'}, {'id': 39, 't_h': 91.54416144657424, 'content': "That's not what I needed to hear right now. Can we just drop it?"}, {'id': 41, 't_h': 91.70342565758993, 'content': "It's nothing, really. I don't want to talk about it."}, {'id': 49, 't_h': 115.2781971452592, 'content': "That's not what I needed to hear right now. Can we just drop it?"}, {'id': 51, 't_h': 115.49963136124255, 'content': "There's something I've been meaning to tell you... maybe another time."}, {'id': 58, 't_h': 139.16103560284876, 'content': "Hey, about earlier: I shouldn't have cut you off like that. Sorry."}, {'id': 60, 't_h': 139.5036459515665, 'content': "That sounds nice, but I think you're missing something."}, {'id': 68, 't_h': 163.5448120545874, 'content': 'Do you remember what I told you about my sister? What do you think?'}, {'id': 70, 't_h': 163.8887122788516, 'content': "Wait — you mentioned you were into pottery, right? How's that going?"}, {'id': 72, 't_h': 164.00405579461201, 'content': "It's nothing, really. I don't want to talk about it."}, {'id': 74, 't_h': 164.0816197917795, 'content': 'Alright, that helped. Same time tomorrow?'}], 'assistant_ok': True, 'assistant_bad': []} |

## Metrics vs frozen thresholds

| Metric | Value | Bar | PASS/FAIL |
| --- | --- | --- | --- |
| M1_grounded_rate | 1.0 | >= 0.9 | PASS |
| M2_invalid_source_rate | 0.0 | — | — |
| M3_recall | 0.25 | >= 0.5 | FAIL |
| M4_false_recall | 0.0 | <= 0.4 | PASS |
| M5_arc_continuity | 1.0 | >= 0.9 | PASS |
| M6a_distinct_activities | 20 | — | — |
| M6b_mean_jaccard | 0.2607 | — | — |
| M6c_arc_days_mean | 3.0 | — | — |
| M6c_arc_days_min | 2 | — | — |
| M7_restart_loss | 0 | — | — |
| M8a_rho_init_tokens | 1.0 | > 0.0 | PASS |
| M8b_token_gap | 79.42 | — | — |
| M8c_rho_energy_delay | -0.3571 | — | — |
| M9_rho_proactive_prevscore | 0.6625 | — | — |
| M10_rho_proactive_initiative | 0.7115 | — | — |
| M11_leak_hits | 0 | — | — |

## Event-chain (§17.2)

AnyEvidence = at least one causally necessary event retrieved; LatestEvidence = the latest/currently-valid event retrieved; CompleteChain = ALL necessary events retrieved (one relevant fact never counts as continuity).

| Chain | Events | Covered | AnyEvidence | LatestEvidence | CompleteChain |
| --- | --- | --- | --- | --- | --- |
| sister_ana | 3 | [True, True, False] | YES | no | no |
| bike_swift | 3 | [True, False, False] | YES | no | no |
| job_change | 3 | [True, False, False] | YES | no | no |

## Perturbation + recovery (§17.3)

Negative interaction block on 1-indexed days []; baseline before, neutral recovery after. Latent = daily mood M; observable = daily mean initiative / max_tokens / response delay.

| Quantity | Baseline | Block mean | Deviation | Peak dev | Persistence (d) | Recovery (d) |
| --- | --- | --- | --- | --- | --- | --- |
| M | 4.2857 | 4.2857 | 0.0 | 0.0 | None | None |
| initiative | 0.3344 | 0.3344 | 0.0 | 0.0 | None | None |
| max_tokens | 463.5333 | 463.5333 | 0.0 | 0.0 | None | None |
| delay | 19.8934 | 19.8934 | 0.0 | 0.0 | None | None |
| failures during block | 0 / 0 proactive messages |

## State observability

| Quantity | Value |
| --- | --- |
| M8a rho(initiative, max_tokens) | 1.0 |
| M8b token gap | 79.42 |
| M8c rho(energy, delay) | -0.3571 |
| M10 rho(proactive, initiative) | 0.7115 |
| rho(M, initiative) | 0.7092 |
| rho(g, delay) | 0.5714 |
| delay sd (s) | 3.874 |
| initiative sd | 0.068 |
| arcs active/completed/abandoned | 2 / 0 / 0 |

## Judge protocol (§17.1/§17.4)

Four independent dimensions (never one collapsed score): persona_enactment (Persona enactment / identity consistency); trajectory_recall (Trajectory recall / temporal continuity); relational_quality (Relational quality); behavioral_dynamics (Behavioral dynamics / stochastic-state observability).

Judge families (>=2 on the final subset): opencode-flash (opencode-deepseek, deepseek-v4-flash); opencode-luna (opencode-gpt, gpt-5.6-luna). Blind, independently shuffled passes; per-dimension disagreement reported; an effect seen by only one judge family is not established companion behavior.

## Replay / reproducibility

| Field | Value |
| --- | --- |
| seed | 5001 |
| replay command | python -m experiments.companion_vertical_slice replay --out /home/vruizes/.hermes/projects/llm-behavioral-harness/results/it3-g3-smoke-night |
| M3 repro rows readable | yes (audit_mode store, repro_json populated) |

## Validation

validate_okf.py: **FAIL** — 1 violation(s).
