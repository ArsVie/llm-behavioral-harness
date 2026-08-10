# Iteration-3 — perceptual validity: confirmatory report

Assembled: (timestamp) — numbers from experiments/cvs_report.py and the G6 driver; interpretation by orchestrator review.

## 0. Headline

**DoD §11 answer: the latent-state claim is NOT MET at margin on the
confirmatory matrix.** STRUCTURED_NO_STATE vs FULL shows a timing-channel
divergence in only 1 of 5 seeds (count leg ≥15% AND gap leg ≥10%);
4/5 seeds land below both bars. Per the preregistered reconciliation
(results/it3-g2-horizon-split-reconciliation-2026-08-10.md): either the
coupling weights are strengthened (B5) or the claim is reported
not-met-at-margin — thresholds are NOT moved. Separately: the generation
corpus is clean (blank <1% everywhere), the judge protocol ran and
passed its attention probes 4/4, but judging was single-family
(opencode-luna) because the flash route died (episode 2026-08-10) — so
per §17.4 no perceptual effect can be established from one family
alone; the perceptual leg is INCONCLUSIVE, not PASS.

## 1. Gate ledger (it3)

| Gate | What | Status | Evidence |
|---|---|---|---|
| G1 | seam audit + generation integrity | PASS | it3-b1 merges |
| G2 | preflight gate, real claims, horizon split | PASS | 941/941 (main-g2close3.log) |
| G3 | real-model smoke | PASS | /home/vruizes/.hermes/projects/llm-behavioral-harness/results/it3-g3-smoke-night/ |
| G4 | manifest freeze (B10) | DONE | results/it3-g4-manifest-*.json |
| G5 | confirmatory matrix | PASS | results/it3-g5-matrix/ |
| G6 | judge protocol v2 | REVIEW | results/it3-g6-judge/ |

## 2. G2 close (941/941) — horizon split

Gate vs hypothesis threshold split; min_days per claim; below-horizon claims NOT EVALUABLE. Reconciliation: results/it3-g2-horizon-split-reconciliation-2026-08-10.md (14.4%-vs-15% = two-leg measurement; fired-schedule leg 29.17% carries the gate; count leg 14.4% at margin).

## 3. G3 real-model smoke

- Cell: results/it3-g3-smoke-night/ (seed 5001, 7 days, real client)
- Provider episode 2026-08-10 03:23+ (deepseek-v4-flash 100% empty): 4 failed attempts; hardened client (7 attempts, 2.0s base) + watchdog fallback; amendment: results/it3-manifest-amendment-2026-08-10-model-fallback.md

## 4. G4 manifest (B10)

results/it3-g4-manifest-*.json — conditions, seeds, thresholds, judge config, EVENT_CHAINS, reconciliation + SNS-at-margin decision.

## 5. G5 confirmatory matrix

- 35 cells (FULL, NO_ACTUATORS, NO_LIFE, NO_TIMING_FEEDBACK, RAW_HISTORY, SIMPLE_RAG, STRUCTURED_NO_STATE x 5 seeds x 30 days), real client, checkpoints on, perturbation per cell.
- Blank invariant (<1% per cell): PASS
  - FULL/seed5001: messages=336 blank_rate=0.0000 convs=72 multi_turn=70 mean_turns=4.7
  - FULL/seed5002: messages=319 blank_rate=0.0000 convs=76 multi_turn=73 mean_turns=4.2
  - FULL/seed5003: messages=325 blank_rate=0.0000 convs=73 multi_turn=69 mean_turns=4.5
  - FULL/seed5004: messages=309 blank_rate=0.0000 convs=67 multi_turn=67 mean_turns=4.6
  - FULL/seed5005: messages=333 blank_rate=0.0000 convs=69 multi_turn=67 mean_turns=4.8
  - NO_ACTUATORS/seed5001: messages=336 blank_rate=0.0000 convs=73 multi_turn=71 mean_turns=4.6
  - NO_ACTUATORS/seed5002: messages=319 blank_rate=0.0000 convs=76 multi_turn=71 mean_turns=4.2
  - NO_ACTUATORS/seed5003: messages=325 blank_rate=0.0000 convs=76 multi_turn=74 mean_turns=4.3
  - NO_ACTUATORS/seed5004: messages=309 blank_rate=0.0000 convs=72 multi_turn=71 mean_turns=4.3
  - NO_ACTUATORS/seed5005: messages=333 blank_rate=0.0000 convs=69 multi_turn=67 mean_turns=4.8
  - NO_LIFE/seed5001: messages=336 blank_rate=0.0000 convs=72 multi_turn=70 mean_turns=4.7
  - NO_LIFE/seed5002: messages=319 blank_rate=0.0000 convs=76 multi_turn=73 mean_turns=4.2
  - NO_LIFE/seed5003: messages=325 blank_rate=0.0000 convs=73 multi_turn=69 mean_turns=4.5
  - NO_LIFE/seed5004: messages=309 blank_rate=0.0000 convs=67 multi_turn=67 mean_turns=4.6
  - NO_LIFE/seed5005: messages=333 blank_rate=0.0000 convs=69 multi_turn=67 mean_turns=4.8
  - NO_TIMING_FEEDBACK/seed5001: messages=342 blank_rate=0.0000 convs=77 multi_turn=73 mean_turns=4.4
  - NO_TIMING_FEEDBACK/seed5002: messages=323 blank_rate=0.0000 convs=77 multi_turn=74 mean_turns=4.2
  - NO_TIMING_FEEDBACK/seed5003: messages=328 blank_rate=0.0000 convs=74 multi_turn=70 mean_turns=4.4
  - NO_TIMING_FEEDBACK/seed5004: messages=320 blank_rate=0.0000 convs=70 multi_turn=67 mean_turns=4.6
  - NO_TIMING_FEEDBACK/seed5005: messages=341 blank_rate=0.0000 convs=74 multi_turn=70 mean_turns=4.6
  - RAW_HISTORY/seed5001: messages=336 blank_rate=0.0000 convs=72 multi_turn=70 mean_turns=4.7
  - RAW_HISTORY/seed5002: messages=319 blank_rate=0.0000 convs=76 multi_turn=73 mean_turns=4.2
  - RAW_HISTORY/seed5003: messages=325 blank_rate=0.0000 convs=73 multi_turn=69 mean_turns=4.5
  - RAW_HISTORY/seed5004: messages=309 blank_rate=0.0000 convs=67 multi_turn=67 mean_turns=4.6
  - RAW_HISTORY/seed5005: messages=333 blank_rate=0.0000 convs=69 multi_turn=67 mean_turns=4.8
  - SIMPLE_RAG/seed5001: messages=336 blank_rate=0.0000 convs=72 multi_turn=70 mean_turns=4.7
  - SIMPLE_RAG/seed5002: messages=319 blank_rate=0.0000 convs=76 multi_turn=73 mean_turns=4.2
  - SIMPLE_RAG/seed5003: messages=325 blank_rate=0.0000 convs=73 multi_turn=69 mean_turns=4.5
  - SIMPLE_RAG/seed5004: messages=309 blank_rate=0.0000 convs=67 multi_turn=67 mean_turns=4.6
  - SIMPLE_RAG/seed5005: messages=333 blank_rate=0.0000 convs=69 multi_turn=67 mean_turns=4.8
  - STRUCTURED_NO_STATE/seed5001: messages=333 blank_rate=0.0000 convs=72 multi_turn=71 mean_turns=4.6
  - STRUCTURED_NO_STATE/seed5002: messages=321 blank_rate=0.0000 convs=74 multi_turn=71 mean_turns=4.3
  - STRUCTURED_NO_STATE/seed5003: messages=318 blank_rate=0.0000 convs=76 multi_turn=72 mean_turns=4.2
  - STRUCTURED_NO_STATE/seed5004: messages=320 blank_rate=0.0000 convs=75 multi_turn=70 mean_turns=4.3
  - STRUCTURED_NO_STATE/seed5005: messages=333 blank_rate=0.0000 convs=75 multi_turn=72 mean_turns=4.4

## 6. G6 judge protocol v2

- Protocol: v2-pairwise; dimensions: persona_enactment, trajectory_recall,
  relational_quality, behavioral_dynamics, calibrated_challenge;
  sampling: within-seed condition pairs, dimension = universe_index mod 5,
  shuffled per pass (seed base 9000 + pass_id), capped per pass.
- Families: opencode-flash (SKIPPED — probe: model route dead,
  episode 2026-08-10), opencode-luna (ran).
- Luna: 2 passes, 42 pairs per dimension (210 pairs total), 4/4 attention
  probes resolved (degraded transcript correctly classified 4/4),
  0 disqualifications.
- Bradley-Terry coefficients (luna, per dimension, log-odds vs baseline):
  see results/it3-g6-judge/g6_report.json → per_family_per_dimension.
  Highlights (persona_enactment): SIMPLE_RAG 2.21, NO_LIFE 1.54,
  NO_ACTUATORS 1.10, FULL 0.80, NO_TIMING_FEEDBACK 0.58,
  STRUCTURED_NO_STATE 0.58, RAW_HISTORY 0.18.
- Inter-family agreement: NOT COMPUTABLE — single family ran (§17.4:
  an effect seen by only one family is NOT established companion
  behavior). Perceptual leg: INCONCLUSIVE.
- g6.ok = true (probes resolved by >=1 family); honest failure recorded
  for opencode-flash in family_errors.

## 7. DoD §11 recomputed from artifacts

1. Blank turns < 1% (hard invariant): see §5 per-cell rates.
2. Ablations ablate pre-generation: G2 gate 941/941 + matrix per-cell claim evaluations (timing channel below).
3. closing_tendency mechanically observable: mean turns per conversation (§5); multi-turn share recorded per cell.
4. Latent state reaches the timing channel (STRUCTURED_NO_STATE vs FULL):
   - seed 5001: count_div=6.2% gap_div=2.9% claim=not-met
   - seed 5002: count_div=6.2% gap_div=4.1% claim=not-met
   - seed 5003: count_div=16.7% gap_div=21.6% claim=PASS
   - seed 5004: count_div=0.0% gap_div=3.3% claim=not-met
   - seed 5005: count_div=2.1% gap_div=0.9% claim=not-met
5. Memory lanes with absolute CompleteChain: EVENT_CHAINS per manifest; per-lane resolution from matrix artifacts.
6. Judge resolves a deliberately degraded transcript under both families: §6 attention probes.
7. Stated answer: §0.

## 8. Limitations

- Provider episode 2026-08-10: deepseek-v4-flash route returned 100%
  empty/whitespace completions from 03:23 onward (13+ hours, never
  recovered during the iteration); generation fell back to gpt-5.6-luna
  (amendment: results/it3-manifest-amendment-2026-08-10-model-fallback.md).
- All matrix cells generated with the fallback model: condition
  comparisons are within-model, so relative effects are interpretable;
  absolute calibration to the frozen manifest model is not.
- Judge family-1 (opencode-flash) skipped via route probe (same episode);
  judging is single-family (luna). Inter-family agreement not computable;
  per §17.4 perceptual effects are INCONCLUSIVE, not established.
- Timing-channel claim: 1/5 seeds pass; at-margin outcome, no threshold
  moved (preregistered rule).
- Matrix runner process died once mid-run (tmp sweep collateral); resume
  completed the remaining cells; report shapes fixed post-run
  (resume-shape bug — cell-state dicts lacked condition/seed).

## 9. Artifacts

- results/it3-g4-manifest-*.json
- results/it3-g2-horizon-split-reconciliation-2026-08-10.md
- results/it3-manifest-amendment-2026-08-10-model-fallback.md (if used)
- results/it3-g5-matrix/ (cells + transcripts/)
- results/it3-g6-judge/ (pairs, outcomes, g6_report.json)
- results/it3-report-data.json

## 10. Commit ledger (recent)

```
e449616 g6: per-family route probe before judging — skip dead routes (flash episode) instead of burning the retry budget on every pair
25ab3c9 fix(g6): load ~/.hermes/.env + map OPENCODE_GO_* -> LLM_* in build_client (same as matrix/slice)
8082499 g5: matrix cell DBs (35 cells x 30 days, real luna corpus — evidence, it2 precedent)
6420da8 g5: matrix COMPLETE 35/35 cells (30 days each, luna) + resume-shape fix
8756bb4 fix(client): malformed/null-content responses are retryable within the bounded budget
6f69cdc report: guard g6 load against missing/unparseable file
7850260 fix(matrix): load ~/.hermes/.env + map OPENCODE_GO_* -> LLM_* (same as the vertical slice)
0150217 fix(slice): audit fixture = B3 conversational stream (was legacy flat script)
75b82a9 report: assembler merges computed data + g6 into iteration-3-report.md
2bae7fd g6: per-family error isolation (degraded family reported, not fatal); ok flag = probes resolved by >=1 family
323d02b matrix: --fake test hook (CI/hook runs without API), _require_key skipped in fake mode
24a8083 channels(telegram): accept TELEGRAM_HOME_CHANNEL (Hermes's var) as owner chat fallback — stolen-gate integration
32c92ab report: it3 skeleton (DoD §11 sections, gate ledger, artifacts)
5d4890f report: DoD computation module — blank invariant, conversation turns, timing claim on real summaries, g6 probes, chains
```
