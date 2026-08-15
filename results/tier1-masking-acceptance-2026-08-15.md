# Tier-1 masking-fix build — acceptance report (2026-08-15)

Branch: wip/tier1-masking (base 653de09 = main, 6 commits, NO pushes)
Build: close two proven masking defects in the llm-behavioral-harness experiment
harness (WS-A day-0 neutral plan; WS-B renderer state collapse) + G0 reversal
record for the lost Q1 scratch. Capability/plumbing only — NO judged run.

## Commits

- cc6863a  G0: Q1 reversal record — re-derived actuator (original scratch lost),
            all brief-cited figures REPRODUCED at 500 seeds.
- 49a5d07  A4: anti-collapse property test (RED against 9-state renderer).
- 772b0f0  A1: state-aware day-0 plan in both experiment paths + fired-event
            integrity tests.
- 39206c9  A3: renderer widened — 8 valence x 6 energy bands, 48 states,
            anti-collapse green. (Orchestrator takeover commit: leaf timed out
            after the change was verified but before committing.)
- b51b302  A2: re-measure driver + results (model-free, n=500, CIs) + pre-fix
            A/B. (Orchestrator takeover commit: leaf timed out after both runs
            completed but before processing/committing.)
- (this)   docs: acceptance report.

## Gates

G0 — Q1 record. PASS. results/q1-actuator-rerun-2026-08-14.md plainly labeled
RE-DERIVATION ("That file is LOST"); brief's numbers treated as hypotheses;
elasticity +62.8%, counts 31.2→58.2, stored S_d mean 1.3123 [0.5636, 2.0],
envelope 15–90 analytic — all reproduced. No fabricated originals.

G1 — suite green. PASS. Full suite: 1067 passed in 641.81s, PYTEST_EXIT=0
(1057 base + 7 test_experiment_planning + 3 test_renderer_anticollapse).
A5 additionally re-ran the changed test files live: 39 passed (7+3+29).

G2 — re-measure verdict + attribution correction. PASS.
FULL 47.654 [47.31, 48.00] vs SNS 41.524 [41.25, 41.80], delta_abs +6.13
[5.75, 6.51], paired rel +15.25% [14.26, 16.24] (n=500, seeds 5001–5500,
30 days, fake client — model-free, real engine.timing.next_event path).
Pre-fix A/B (653de09 worktree): +14.92% [13.92, 15.91] — the ~+15% state
effect is PRE-EXISTING. The +10% point prediction is REFUTED (10% outside
the CI). Attribution correction recorded: the brief's "−1.35% was the
day-0-neutral-plan artifact; fix flips to +10%" story is falsified — the
−1.35% (n=5, real model) was measurement noise, and the day-0 fix
contributes ~+0.3pp (FULL +0.146/30d, not significant; SNS +0.006). The
fix stands on correctness grounds (production _replan parity + fired-event
integrity), not on flipping a number.

G3 — renderer anti-collapse. PASS. valence 0.6 vs 1.0 render distinct briefs
(bright vs radiant); energy probe 0.4 vs 0.6 distinct (calm vs lively — the
sharper probe, since 0.4 vs 0.8 already survived the old bands); 8x4 grid
(32 vectors) → 32 distinct briefs. 8 valence bands x 6 energy bands = 48
states; lexicon monotonic natural prose, no numbers, no phase labels, no
state-naming; invariant closing line intact; continuity/texture/care
channels unchanged; BehaviorTrace never enters the prompt.

## Floor compliance (A5 verified, file:line evidence)

- engine/ byte-identical: git diff 653de09..HEAD -- engine/ EMPTY.
- git diff --name-only 653de09..HEAD = exactly the 12 permitted files.
- No new tools; no independent-judge/import work; no judged runs.
- Re-measure model-free: run_cell(fake=True), DeterministicClient/Judge,
  real engine.timing.next_event path.
- Deterministic replay verified live by A5: run_cell(seed 5001) → FULL=48,
  SNS=45, matching the committed JSON.
- Fired-event integrity: test_experiment_planning.py asserts fired rows stay
  fired with unchanged t_h/reason, never resurrected (INSERT OR IGNORE).
- Honest labeling throughout (G0 re-derived; G2 attribution correction).

## A5 verdict

APPROVE (read-only review, no violations). Three minor notes, none affecting
correctness or honesty: (1) A4's energy probe pair is 0.4 vs 0.6 (sharper than
the briefed 0.4 vs 0.8, documented in the test); (2) pre-fix raw data lives in
the MD only (temp worktree, no committed JSON — documented); (3) the MD's
"Δse ≈ 0.247" is a per-arm-se heuristic, not paired se — the "not significant"
conclusion holds either way.

## Findings for the record

1. FULL > SNS proactive counts by ~+15% (paired rel [14.3%, 16.2%]) — real,
   stable, model-free.
2. The state-aware day-0 plan fix is correct but its measured contribution to
   the delta is ~+0.3pp (one day of thirty).
3. The it3 −1.35% was n=5 measurement noise, not the day-0 artifact — G0's
   commit message attribution is corrected in the re-measure record.
