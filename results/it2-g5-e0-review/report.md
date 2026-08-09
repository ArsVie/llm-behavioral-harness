---
title: "G5 — E0 exploratory review: confounder ledger vs regression pins"
type: gate-review
date: "2026-08-09"
project: llm-behavioral-harness
iteration: 2
gate: 5
branch: main
commit: 43ec7c2
status: PASS
---

# Gate 5 — E0 exploratory review

Frozen archive reviewed: `eval-exploratory-2026-08-08` @ `230a9e0` (immutable;
never merge fixes into it — per orchestration rules). The E0 run was
confounded by design; its KNOWN CONFOUNDERS are listed in the archive commit
message. This review maps every confounder to the regression pin that now
guards against it, per plan G5 ("bugs → regression tests; no threshold
tuning").

## Confounder ledger

| # | E0 confounder | Evidence at E0 | Regression pin (main @ 43ec7c2) | Status |
|---|---|---|---|---|
| C1 | Clean bootstrap incomplete (companion started without full blank-DB init) | Archive driver relied on fixture-ish setup; Gate-2 checklist unverified | A1b `ensure_companion_initialized` idempotence + `test_adversarial_bootstrap::test_b1e_partial_initialization_never_regenerates_identity`; **Gate 2 PASS (5/5 seeds validated, results/it2-g2-vertical/)** | PINNED |
| C2 | 40/40/20 interest portfolio NOT user-relative | Bootstrap used independent interests only | A1b `build_persona(user_interests=, adjacency_hops=)` + `InterestGraph.distance()` + interest-composition tests (tolerance ±5 pts / ±0.5 counts); Gate 2 runs bootstrap with `GATE2_USER_INTERESTS` (mathematics/lifting/movies/metal) | PINNED |
| C3 | Driver feed/clock race → 6/6 intents spuriously `expired` at feed times | `state_events` suppression times vs intent `created_t_h`; gate passed at its own time → runtime clock was the racer | `test_r1a_fast_clock_events_near_midnight_fire_not_expire`, `test_r1c_three_events_before_midnight_all_fire_at_own_times` (A9 battery); R1-F1 quiet-deferral livelock fix (027163f) + `test_r1b`; step-wise driver advance in the A8 harness | PINNED |
| C4 | Structural checkpoint-resume FREEZES after segment 1 (47 state events, then nothing) | Re-dispatched continuation timed out; root cause never isolated at E0 | A8 `run_cell` checkpoints (restart/reopen mid-run) exercised in EVERY vertical run — 5 seeds × 5 restarts, `restart_state_loss = 0`; `test_cvs_tracks` replay-exactness tests (config reconstruction + hours/days round-trip) | PINNED |
| C5 | Judge calibration failure (rubric anchored on companion perspective → horrible month +0.04, mu never negative) | Live-run ablation v1 | Rubric v2 anchors on USER treatment with hard rule; `test_e2e_ablation.py:38` pins `horrible == -0.7` (plus perfect/flat legs); live validation: −0.71 / +0.86 / 0.00, mu −0.539 / +0.642 / 0.000 | PINNED |

## Gate-2 audit extras (beyond E0's confounders)

The G2 battery additionally re-verifies, mechanically, per seed: ungrounded
proactive = 0, wrong intent = 0, stranded opportunities = 0, cycle-state
leakage = 0, memory provenance failures = 0, duplicate turns = 0, life-dead
days = 0, fixture inserts = 0, counts consistent, restart state loss = 0.

## New regression tests added by this review

None required — every E0 confounder has a live pin (verified by test-name
grep + Gate 2's committed artifacts). No threshold tuning performed.

## Verdict

**PASS** — the E0 archive remains frozen as the immutable exploratory
baseline; all five known confounders are covered by regression pins on main.
Confirmatory evaluation (G6) may proceed against the preregistered manifest
(results/companion-vertical-slice/manifest.json @ 5a758ed).
