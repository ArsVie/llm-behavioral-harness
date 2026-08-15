# Q1 Actuator — Re-derived reversal record (G0)

Date: 2026-08-14 · Branch: wip/tier1-masking (base 653de09)

## Provenance — READ FIRST

The orchestrator brief referenced `scratchpad/q1_actuator.py` as the "elasticity
harness" and asked to move it into experiments/ as the G0 reversal record.
**That file is LOST**: verified absent from the repo, all worktrees, /tmp,
/mnt/c, git history (no commit ever added it), and session history. The only
surviving copy of the finding was the brief's own prose.

This record is therefore a **RE-DERIVATION**, not the original file:
`experiments/q1_actuator.py` was re-authored from the brief's design spec and
re-run. The brief's cited numbers were treated as HYPOTHESES to confirm, not
ground truth. Results below.

## Finding (confirmed by re-run)

**The −1.35% it3 artifact was an up-front-neutral-plan defect.** The it3 matrix
planned day-0 contacts at neutral state (`plan_and_persist(scores=None)` in
`experiments/cvs_common.py` ~1070) before the day-0 mood row existed, and
INSERT OR IGNORE never revised them — so FULL cells' first day ran at state
factor exactly 1.0, making FULL ≈ SNS on the channel that carries the coupling.
Production `_replan` (harness/runtime.py:219) is ALREADY state-aware; the
defect lives only in the experiment paths (cvs_common.py and
live_companion.py — fixed in A1, same branch).

**The hazard is genuinely elastic: +62.8% across the S_d sweep** (500 seeds,
30 days, guards on): mean 31.21 messages at S_d=0.5 → 58.16 at S_d=2.0
(count@1.0 = 42.9; elasticity = (58.16 − 31.21)/42.9 = 0.628). Guards on ≈
guards off (elasticity 0.649 off) — the queue guards do not absorb the effect.

**Real per-day S_d from the committed FULL runs** (it3-g5-matrix full/seed5001..5005
@ 653de09, 155 day-samples): mean 1.312, sd 0.429, range [0.564, 2.0]
(seed means: 1.278, 1.280, 1.343, 1.146, 1.516). The +10% FULL-vs-SNS prediction
rests on this stored-run mean (≈1.31).

**Fresh-engine S_d** (500 seeds × 90 days = 45,000 samples): mean 1.644,
range [0.540, 2.0] — hotter than the stored-run mean by +0.33, as expected:
the fresh sample has no conditioning on the specific matrix trajectory
(momentum, previous-day state, score history). The stored-run value is the
load-bearing one for the re-measure.

**Envelope 15–90: analytic, not simulated** (per brief): max_gap 48h forces
≥1 contact per 2 days (floor ~15/30d); daily_cap 3 caps at 3/day (ceiling
90/30d). Observed sweep range 24–75 sits inside it.

## Claim verification (brief's cited numbers vs re-run)

| Claim | Cited | Re-run | Verdict |
|---|---|---|---|
| elasticity | ≈ +63% | +62.8% (guards on) / +64.9% (off) | REPRODUCED |
| counts | ≈31 → ≈58 msgs | 31.2 → 58.2 (mean, n=500) | REPRODUCED |
| guards on ≈ off | on ≈ off | Δ elasticity 0.021 | REPRODUCED |
| stored S_d mean | ≈1.28 | 1.312 | REPRODUCED |
| stored S_d range | 0.56–2.0 | [0.564, 2.0] | REPRODUCED |
| fresh > stored | fresh ≈1.53, hotter | fresh 1.644, +0.33 | REPRODUCED (direction; magnitude larger) |
| envelope | 15–90 | 15–90 (analytic) | REPRODUCED |

Minor correction: fresh-engine mean measured 1.644 (not the in-session ~1.53);
the stored-run mean 1.312 (not 1.28 exactly). Both within the claims'
stated intent; the record stands on the re-run numbers.

## Artifacts

- `experiments/q1_actuator.py` — re-derived experiment (Exp-1 sweep, Exp-2a
  guards-off, Exp-3 fresh+stored, analytic envelope). Deterministic; drives
  the frozen engine.timing.next_event through sim/run_events.run with
  phase/adj pinned to 1.0 (isolates S_d), mod_ub = S_d.
- `results/q1-actuator-rerun-2026-08-14.json` — full machine-readable record
  (per-arm stats + CI, per-seed stored factors, verification block).
- `results/q1-actuator-rerun-2026-08-14.md` — this document.

## Reproduce

    .venv/bin/python experiments/q1_actuator.py --seeds 500   # full (≈75 s)
    .venv/bin/python experiments/q1_actuator.py --smoke       # 5-seed sanity
