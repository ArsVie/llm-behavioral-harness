# G2 Horizon-Split Reconciliation — 2026-08-10

Decision record for the G2 gate fix: three flags, two causes; the gate
threshold split from the hypothesis threshold; measured margins recorded
for the B10 manifest freeze (G4). DoD input for the confirmatory matrix.

## The three flags and their causes

| condition | 3-day flag | cause | resolution |
|---|---|---|---|
| NO_LIFE | flagged as null | BROKEN ABLATION — `_ensure_life()` no-op while life is created upstream; byte-identical to FULL | goldfish fix (commit c24d880): per-day arc wipe + epoch counting, claim asserts arc-identity discontinuity. A FIX, not a horizon change |
| STRUCTURED_NO_STATE | flagged as null | HORIZON ARTIFACT — score feedback cannot land before day 2-3 (adj=1 by construction through day 1-2); at 3 days the channel has not been exercised | `min_days=4`; preflight reports NOT EVALUABLE below horizon, never FAIL |
| SIMPLE_RAG | flagged as null | HORIZON ARTIFACT — episode store below the retrieval surface (limit=8) until ~day 5-10; retrieved set identical to FULL's at 3-5 days, diverges by 10 | `min_days=10` (measured crossing: identical at 5d, divergent at 10d, seed 5001) |

## The 14.4%-vs-15% discrepancy — resolved

My earlier report quoted "222 vs 254 proactives = 14.4%, verdict PASSES"
as a contradiction. It was not: the positive-control check has TWO legs
(n_proactive OR n_fired_schedule), and my probe only measured one.

Which leg actually fired (30 days, fake client):

| leg | FULL | NO_TIMING_FEEDBACK | divergence |
|---|---|---|---|
| n_proactive (seed 5001) | 48 | 54 | 12.5% — BELOW COUNT_DIVERGENCE_MIN |
| n_proactive (5-seed pooled) | 222 | 254 | 14.4% — BELOW COUNT_DIVERGENCE_MIN |
| n_fired_schedule (seed 5001) | 48 | 62 | **29.2% — the leg that fired** |
| mean inter-event gap (seed 5001) | 3.2 h | 2.8 h | 12.5% ≥ GAP_DIVERGENCE_MIN |

The positive control passes via the fired-schedule leg — the timing
channel's direct realized artifact. Not a threshold move; a measurement.

## STRUCTURED_NO_STATE at margin (manifest claim, G5)

The count leg of `structured_no_state_claim()` is binding:
|254−222|/222 = 14.4% < 0.15 on the pooled fake runs (single-seed 6.25%).
The preregistered margin is NOT met on the fake client at 30 days. The
legitimate responses (per user, §3 discipline — the constant does not
move):

1. B5 strengthens the coupling weights so the mechanism produces a
   larger effect (code change; preflight re-evidence before G4), or
2. report the claim as NOT MET at margin on the real matrix, with the
   gap leg (12.5%) noted.

Both are acceptable; moving COUNT_DIVERGENCE_MIN is not. The B10
manifest freeze records this reconciliation verbatim.

## The split (wired in cvs_preflight.py)

- `GATE_MIN_DIVERGENCE = 0.05` — preflight gate: "channel not dormant",
  a null-detector; nothing rides on its exact value.
- `COUNT_DIVERGENCE_MIN = 0.15` / `GAP_DIVERGENCE_MIN = 0.10`
  (harness/scheduler.py) — hypothesis thresholds, UNCHANGED, tested on
  the real matrix at G5.
- `min_days` on AblationClaim (harness/domain.py, default 1):
  below-horizon claims report NOT EVALUABLE (passed=None), never FAIL.
- Preflight default horizon 3 → 30 days (mirrors the confirmatory
  matrix; fake client makes it cheap — ~1 min for 5 seeds × 30 days per
  condition pair).
- 3-day leg demoted to `--smoke`: fast structural check (conditions
  construct, tables exist, determinism, zero blanks). Not the gate.
- Verdicts now carry `measured` (count_div/fired_div/gap_div/
  times_identical) so which leg fired is recorded in the report JSON.

## Measured margins for the B10 manifest

Preflight report JSON (results of `run_preflight(days=30)` on current
main) is the evidence source; the reconciliation above is the reading of
it. The B10 manifest at G4 cites this record and the report path.
