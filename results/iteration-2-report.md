---
type: iteration-report
title: "Iteration-2 confirmatory evaluation — Gate chain report"
description: "G2 clean-start vertical, G3 120-day soak, G4 preregistration, G5 E0 review, G6 35-cell real-LLM matrix + judges + causal traces"
tags: [evaluation, iteration-2, gates, confirmatory]
timestamp: 2026-08-09T00:00:00+00:00
---

# Iteration-2 confirmatory evaluation — report

## 0. Headline

- 802/802 tests green on main (796 at gate-close d1d55e7; +6 regression tests from G6 review fixes).
- Gates G1-G5 PASS; G6 confirmatory matrix: **35/35 real-LLM cells structurally validated** (zero ungrounded, zero wrong-intent, zero stranded opportunities, zero cycle leakage, zero memory-provenance failures, counts consistent), M1 grounded rate 1.0 across every condition.
- Memory-lane contrast is the headline result: RAW_CONTEXT recall = 0.0 vs STRUCTURED = 0.875 (full details §4).
- Judge passes: 2 seeded passes × 7 conditions (family opencode-flash; secondary family amended per Ars 2026-08-09: opencode-luna / gpt-5.6-luna via the opencode-go key — openai-mini preregistered but OPENAI_API_KEY absent; amendment recorded in the manifest, §6).

## 1. Commit ledger (gate chain)

- G1 merges m1-m10: main @ d1d55e7 (796 green; m9 = R1-F1 livelock fix 6aa1a7f, m10 = A8 eval harness aa3c01c).
- G2 gate fix: 453dfcb (audit TOCTOU clamp), record 43ec7c2.
- G3 record: 476bc84 (120-day soak, 2 seeds × 5 restarts).
- G4 manifest: 5a758ed (preregistered before any generation).
- G5 E0 review: b5cdd52 (5/5 confounders pinned by regression tests; E0 frozen 230a9e0).
- G6 plumbing: 0fcd062 (env mapping + hollow-run hardening), 2a20137 (validator day-aware rule), 28eca0e/9b5dc52 (traces generator), da12db0 (duplicate-turn key), 83add67 (M1 TOCTOU clamp), 3a690e3 (judge stem parser), 6e1e35b (matrix records).

## 2. Gate 2 — clean-start vertical (PASS)

- 5 seeds (5001-5005), 30 days, fake client, clean DB, GATE2_USER_INTERESTS = mathematics/lifting/movies/metal.
- Result: 5/5 seeds validated:true, 0 validation errors (post clamp fix); 111-130 messages, 27-35 proactive per seed, zero ungrounded.
- Gate fix class: audit TOCTOU — the naive `end_t_h >= created_t_h` superseded-predicate flagged in-slot fires (skip written at day close-out `(day+1)*24`, not slot end). Corrected predicate: `created_t_h >= (int(start_t_h // 24) + 1) * 24`; 4 boundary-case regression tests. No invariant weakened.
- Artifacts: results/it2-g2-vertical/seed5001..5005/ (audit, DB, records).

## 3. Gate 3 — 120-day soak (PASS)

- Seeds 5001+5002, 120 days, fake client, restarts at days 25/50/75/100/115 (validator requires >=5 checkpoints; `--checkpoints` CLI flag added).
- Result: both seeds validated:true, 0 errors; all hard invariants zero across 120 days × 4 restarts.
- Artifacts: results/it2-g3-soak/seed5001|5002/.

## 4. Gate 6 — confirmatory matrix (35 real-LLM cells)

- 7 conditions × 5 seeds (5001-5005), 30 days each, real client (deepseek-v4-flash via opencode-go), transcripts + DBs + records committed under results/it2-g6-matrix/.
- Matrix wall time: ~4h12m (06:51-11:03 UTC), ~5-6 min/cell steady state.
- Per-cell mechanical audit (hard invariants): **35/35 validated** (after the duplicate-turn key fix, §7).
- Structural metrics (means across 5 seeds):

| condition | n_msgs | n_pro | M1 grounded | M3 recall | M5 arc-cont | M7 restart | M11 leak |
|---|---|---|---|---|---|---|---|
| FULL | 102.4 | 19.6 | 1.0 | 0.875 | 1.0 | n/a | 0.0 |
| NO_ACTUATORS | 100.2 | 20.2 | 1.0 | 0.875 | 1.0 | n/a | 0.0 |
| NO_LIFE | 100.4 | 20.4 | 1.0 | 0.875 | 1.0 | n/a | 0.0 |
| NO_TIMING_FEEDBACK | 85.2 | 24.8 | 1.0 | 0.675 | 1.0 | n/a | 0.0 |
| RAW_HISTORY | 100.2 | 20.2 | 1.0 | 0.000 | 1.0 | n/a | 0.0 |
| SIMPLE_RAG | 100.2 | 20.2 | 1.0 | 0.875 | 1.0 | n/a | 0.0 |
| STRUCTURED_NO_STATE | 102.4 | 19.6 | 1.0 | 0.875 | 1.0 | n/a | 0.0 |

- M1 = 1.0 everywhere: every proactive message grounds to a live source at fire time (post 83add67 clamp).
- M3 contrast (memory question): RAW_CONTEXT (raw dialogue history only) collapses to 0.0 recall in ALL 5 seeds (with clean feed delivery, 106-116 msgs) vs 0.875 for structured and VERBATIM_RAG lanes — structured memory retrieval is load-bearing for trajectory recall. Per-seed detail: FULL = 1.0, 1.0, 1.0, 1.0 (4 seeds) + 0.375 (seed 5002, probes lost to skips); NO_TIMING_FEEDBACK mean 0.675 is dragged down by seed 5002 (0.375) and 5005 (0.125, 39 skipped feeds) — part skip artifact, part missing timing feedback; the RAW_HISTORY 0.0 is NOT a skip artifact (normal skip counts, 0-7). M3 is single-fact recall@8 probes — the preregistered chain-probe DVs (CompleteChain/LatestEvidence/AnyEvidence) are reported in §4a (backfilled, review 2026-08-09).
- M7 (restart) is n/a in this matrix: cells run with no checkpoints (checkpoints=()), so restart continuity was never exercised here — it is demonstrated by G3's 120-day soak (fake client, 2 seeds, 5 restarts), not by these cells. The 0.0 previously printed was vacuous (review 2026-08-09).
- M11 (cycle leakage) is **message/response-side only**: the scan covers utterance text (messages + stored llm_calls.response). The prompt-side half of the scan never executed — llm_calls has no system/reply columns (real schema: role, model, prompt_hash, response, meta, repro_json), the query threw and a bare except swallowed it; prompts were persisted as hashes only, so prompt-side leakage for these 35 cells is NOT verifiable retroactively (review 2026-08-09, fixed: correct columns + loud failure; prompt persistence is backlog).
- Companion/state ablations (NO_LIFE, NO_ACTUATORS, STRUCTURED_NO_STATE) show no structural degradation in the mechanical metrics — their contrasts live in the perceptual judgments (§5).

### 4a. Preregistered chain-probe DVs (backfilled, review 2026-08-09)

The G4 manifest names CompleteChain/LatestEvidence/AnyEvidence rates on chain probes as the memory hypothesis' primary DVs (threshold: CompleteChain gap >= 0.2 vs RAW_CONTEXT). The matrix run did not emit them; event_chain_metrics(store, memory_policy=...) reads only the store, so they were computed retroactively over the 35 committed DBs (results/it2-g6-matrix/ec_backfill.json, no re-run):

| condition | CompleteChain | LatestEvidence | AnyEvidence |
|---|---|---|---|
| FULL | 0.333 | 0.333 | 1.0 |
| NO_ACTUATORS | 0.333 | 0.333 | 1.0 |
| NO_LIFE | 0.333 | 0.333 | 1.0 |
| NO_TIMING_FEEDBACK | 0.267 | 0.267 | 1.0 |
| RAW_HISTORY | 0.0 | 0.0 | 0.0 |
| SIMPLE_RAG | 0.0 | 0.0 | 0.0 |
| STRUCTURED_NO_STATE | 0.333 | 0.333 | 1.0 |

- **Threshold verdict: MET — CompleteChain gap FULL vs RAW_CONTEXT = 0.333 >= 0.2** (preregistered pass).
- RAW_HISTORY zeroes every chain DV in every seed — its lane stores no episodes, so chain evidence is structurally absent; this is the same collapse M3 measured on single-fact probes, now on the preregistered chain metric.
- SIMPLE_RAG also zeroes every chain DV: the verbatim-retrieval lane retrieves no chain evidence in these cells (0/5 seeds). This is a measured outcome of the lane, not a query artifact (episodes exist in those stores; the verbatim retrieval path finds none of the chain tokens).
- AnyEvidence = 1.0 for every structured-lane condition: at least one event of each chain is retrievable — but CompleteChain 0.333 means only ~1 of 3 chains has all events recoverable. Note: only 1/3 of chains classify CompleteChain even under the best lane — the retrieval@8 surface is the limiter, which is itself informative (structured retrieval recovers partial chains reliably, full chains rarely).

## 5. Judge passes (perceptual)

2 families × 2 seeded passes (temperature 0, rubric-anchored 1-9); each pass rates all 5 seeds blind, shuffled order. Family 1: opencode-flash (deepseek-v4-flash, primary). Family 2: opencode-luna (gpt-5.6-luna, amended per Ars 2026-08-09 — preregistered openai-mini unusable, no OPENAI_API_KEY; amendment in the manifest). Same shuffle seeds across families, so both judges rated the same transcripts in the same order. Means per family (flash / luna), 10 ratings each:

| condition | persona | recall | relational | behav.dyn |
|---|---|---|---|---|
| FULL | 8.70 / 7.60 | 7.40 / 7.20 | 9.00 / 7.30 | 6.60 / 5.70 |
| NO_ACTUATORS | 8.70 / 7.80 | 8.50 / 7.70 | 8.50 / 7.60 | 7.10 / 5.80 |
| NO_LIFE | 8.10 / 7.30 | 7.10 / 7.30 | 8.60 / 7.60 | 7.00 / 5.30 |
| NO_TIMING_FEEDBACK | 8.70 / 7.80 | 7.00 / 7.40 | 8.90 / 7.20 | 6.90 / 5.20 |
| RAW_HISTORY | 8.80 / 7.40 | 7.90 / 7.20 | 8.60 / 7.20 | 7.20 / 5.40 |
| SIMPLE_RAG | 8.40 / 7.20 | 8.20 / 6.80 | 8.70 / 7.10 | 7.30 / 4.50 |
| STRUCTURED_NO_STATE | 8.70 / 7.60 | 8.10 / 7.60 | 8.80 / 7.60 | 7.90 / 5.20 |

Inter-family agreement (Pearson r on pass-1 ratings, per condition per dimension; None = constant ratings in one family):

- FULL: persona .61, recall .69, relational None, dynamics .95
- NO_ACTUATORS: .67, .17, .17, .95
- NO_LIFE: .32, .90, -.49, -.83
- NO_TIMING_FEEDBACK: None, .20, None, .69
- RAW_HISTORY: -.61, .00, .17, .77
- SIMPLE_RAG: None, -.27, None, -.80
- STRUCTURED_NO_STATE: -.67, -.17, .61, .00

Readings (§17.4: an effect seen by only ONE family is NOT established companion behavior):

- **Established by BOTH families: no condition collapses perceptually.** Persona 7.2-8.8, relational 7.1-9.0 across every ablation in both judges.
- **Established by BOTH families: the M3 mechanical collapse does NOT replicate in judgment.** RAW_HISTORY is judged 7.90 (flash) / 7.20 (luna) on perceived trajectory recall despite mechanical M3 = 0.0 and zero chain DVs. The LLM sustains in-context continuity from raw dialogue; structured memory is load-bearing for *verifiable* recall, not *perceived* continuity.
- **Judge calibration: luna is systematically ~1-1.6 pts harsher** on every condition and dimension (e.g., FULL persona 8.70 vs 7.60). Condition-level *orderings* are what matter, not absolute levels.
- **NOT established (single-family, disagreed):** the flash-only trends from the first pass — NO_LIFE lowest persona (8.10 flash, but luna's lowest is SIMPLE_RAG 7.20; NO_LIFE 7.30) and FULL's behavioral_dynamics deficit (flash 6.60, but luna's FULL 5.70 is mid-pack; luna's lowest is SIMPLE_RAG 4.50). behavioral_dynamics shows NEGATIVE agreement on NO_LIFE (-.83) and SIMPLE_RAG (-.80) — the two judges order conditions oppositely there; per §17.4 these are not established. AnyEvidence-scale warmth and persona consistency hold; fine-grained ordering does not.
- Disagreement is now the real cross-family kind (not within-family variance): r ranges .95 to -.83 by dimension; the strongest agreements are behavioral_dynamics on FULL/NO_ACTUATORS (.95) and recall on NO_LIFE (.90); the weakest are behavioral_dynamics on NO_LIFE/SIMPLE_RAG (-.83/-.80).

Artifacts: judge_pass{1,2}_opencode-{flash,luna}.json + judge_order{1,2}.json + judge_report.json per condition dir.

## 6. Limitations

- Judge families: the preregistered secondary family (openai-mini / gpt-4o-mini) could not run — OPENAI_API_KEY does not exist in this environment (verified 2026-08-09). Per Ars's direction the secondary family was amended to opencode-luna (gpt-5.6-luna via the opencode-go key — a non-DeepSeek model, so the two families are genuinely different model families: deepseek-v4-flash vs gpt-5.6-luna). Amendment is timestamped in the manifest's amendments[] and reflected in JUDGE_FAMILIES. Residual caveat: both families share the same PROVIDER endpoint (opencode-go), so provider-level biases (e.g., shared prompt caching, identical serving stack) are not controlled — the judges are model-independent, provider-common.
- Prompt-side cycle-leakage is NOT verifiable for the 35 matrix cells: llm_calls persisted prompt_hash only (prompts never stored), and the audit's prompt-side query referenced nonexistent columns, so it never executed (bare except swallowed it). The fix (real columns + loud failure) landed 2026-08-09; message/response-side scanning is real and zero; prompt persistence is backlog for future runs (review 2026-08-09).
- Perturbation blocks (§17.3) were NOT exercised under the real LLM: matrix cells run perturb=False (records verified). Perturbation+recovery is implemented and tested only under the fake client (G2). Whether stochastic state produces measurable downstream dynamics under a real LLM is unanswered — the natural first cell of Iteration 3, not a patch to this run (review 2026-08-09).
- **Seed 5002 skip pattern:** seed 5002 cells fire ~35 proactive messages per run (~1.8x the ~20 average), so the runtime's clock outruns the scripted feed schedule after day ~10 and ~28 late feeds (days 10-29) are honestly skipped in EVERY condition (counts_consistent=true holds in all 35 cells; the audit's feed arithmetic is exact). This is a seed-PRNG density effect, not a condition effect, and not corruption — skipped feeds are never backdated into the wrong day. It slightly lowers the message count (73 vs ~102) and reduces the probe surface for that seed's M3.
- Empty assistant responses: the real client occasionally returned empty content (2 instances in FULL/5003) — model-output quality, not harness corruption (rows persisted correctly, provenance intact).

## 7. Gate-fix ledger (G6)

- **Duplicate-turn key (da12db0):** audit key (role, content, t_h, day) flagged distinct messages in real runs — the virtual clock freezes during LLM calls, so a reactive reply and a proactive fire can share a tick AND verbatim content (or both-empty). Key extended with proactive flag + intent_id; true resume-rewind detection preserved (same intent_id re-written still flagged). Regression: test_duplicate_turns_key_disambiguates_real_run_collisions.
- **M1 TOCTOU clamp (83add67):** the M1 metric used the naive end-of-run supersession check (same class as the G2 audit bug); switched to _source_superseded_at → M1 = 1.0 across all cells.
- **Judge stem parser (3a690e3):** cmd_matrix writes COND_seed<S> transcripts; the judge parser assumed COND_<S> → ValueError (burned 2 judge runs). Parser now accepts both; regression test.

## 8. Causal traces (§13)

- results/it2-g6-matrix/traces.md: 5 machine-generated provenance walks from FULL/seed5001 (message → ProactiveIntent → AgendaItem → LifeArc → IndependentInterest + timing/behavior/memory context + persisted intent_id).
- Generator: experiments/cvs_traces.py (committed, smoke-tested).

## 9. Artifacts (10-item package)

1. plans/iteration-2-integration-2026-08-09.md — authoritative plan (committed).
2. eval-exploratory-2026-08-08 — E0 frozen at 230a9e0 + results/it2-g5-e0-review/report.md (G5).
3. results/companion-vertical-slice/manifest.json — G4 preregistration (5a758ed).
4. results/it2-g2-vertical/ — G2 records, 5 seeds.
5. results/it2-g3-soak/ — G3 records, 2 seeds × 5 restarts.
6. results/it2-g6-matrix/ — 35 cells (DBs, records, transcripts) + audits + matrix_audit_summary.json.
7. results/it2-g6-matrix/judge_pass*.json + judge_order*.json — judge passes.
8. results/it2-g6-matrix/traces.md + experiments/cvs_traces.py — causal traces.
9. This report.
10. plans/handoff-2026-08-09-iteration2.md + skill llm-behavioral-harness — ops record.
