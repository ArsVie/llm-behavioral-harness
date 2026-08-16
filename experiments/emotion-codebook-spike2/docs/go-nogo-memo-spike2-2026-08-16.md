# Go/No-Go memo — emotion-codebook SPIKE 2 (behavioral re-gate)

Date: 2026-08-16 (~07:00 local)
Status: **SHELVE** — per the pre-committed decision matrix (contract §7): "SHELVE on
any fail; G-ABS fail ⇒ SHELVE regardless." G-ABS failed on all three actors; the
PRIMARY gate G-BEH failed in the NEGATIVE direction (codebook significantly worse
than the renderer).

Pre-registration: docs/exp-affect-codebook-spike2-2026-08-16.md
Repro bundle: repro_bundle.json (revisions, seeds, dataset versions)
All commits local (no push).

---

## 1. Judge instrument integrity (read this before the numbers)

The first P6 chain (local base judges, decision 3) produced UNREADABLE verdicts —
both local base models (Gemma-3-1B, Qwen3-1.7B) are empirically non-functional as
3-way affect classifiers: with free decoding they echo the rubric
(`Low:\nMid:\nHigh`); with forced-choice decoding (decision 9) they order-follow the
label list regardless of content (bullet reversal flips every answer; demonstrated,
recorded in diagnostics/p6-machinery-notes.md). Chain-v1 output is preserved on disk
as instrument-failure evidence and is NEVER gate evidence.

Decision 10 (contract line 80 escape hatch): judge = the hosted API model
(deepseek-v4-flash via zen gateway, lane="research", temperature 0). Instrument fix:
max_tokens 64 → 512 after a probe showed the reasoning model starves a tight budget
(finish_reason='length', empty content; 512 leaves the one-word verdict inside the
budget). Paired G-BEH matching fixed to the id index suffix (variant is baked into
ids; chain v1's n_pairs=0). Tee-masked crashes fixed (set -euo pipefail) after a
silent-failure incident. No gate threshold moved; no generation was regenerated —
chain v1's K=30 replies are byte-identical inputs to the new judge.

## 2. P6 gate results (valid measurement, hosted judge, K=30/band/actor)

G-ABS (H3): codebook generations judge-classified >= 0.60 (3-way), 95% CI excludes
chance (0.33), K >= 30/band. Per band: codebook acc / CI / verdict | renderer acc.

| actor   | band | codebook acc | codebook 95% CI    | G-ABS | renderer acc |
|---------|------|--------------|--------------------|-------|--------------|
| qwen    | low  | 0.200        | [0.067, 0.333]     | FAIL  | 0.533        |
| qwen    | mid  | 0.633        | [0.467, 0.800]     | PASS  | 0.633        |
| qwen    | high | 0.100        | [0.000, 0.200]     | FAIL  | 0.567        |
| qwen    | ALL  | 0.311        | [0.222, 0.411]     | FAIL  | 0.578        |
| gemma   | low  | 0.200        | [0.067, 0.367]     | FAIL  | 0.367        |
| gemma   | mid  | 0.400        | [0.233, 0.567]     | FAIL  | 0.467        |
| gemma   | high | 0.233        | [0.100, 0.400]     | FAIL  | 0.467        |
| gemma   | ALL  | 0.278        | [0.189, 0.378]     | FAIL  | 0.433        |
| qwen8b  | low  | 0.267        | [0.133, 0.433]     | FAIL  | 0.600        |
| qwen8b  | mid  | 0.733        | [0.567, 0.867]     | PASS  | 0.767        |
| qwen8b  | high | 0.233        | [0.100, 0.400]     | FAIL  | 0.767        |
| qwen8b  | ALL  | 0.411        | [0.311, 0.511]     | FAIL  | **0.711**    |

G-BEH (H4, PRIMARY — qwen8b, largest local actor): DeltaAcc >= +0.10, 95% CI on the
paired difference excludes 0, paired same contexts/levels.

| band | delta_acc | 95% CI           | excludes 0 |
|------|-----------|------------------|-----------|
| low  | **-0.333**| [-0.533, -0.133] | YES       |
| mid  | -0.033    | [-0.267, 0.200]  | no        |
| high | **-0.533**| [-0.733, -0.300] | YES       |
| ALL  | **-0.300**| [-0.433, -0.167] | YES       |

**G-ABS: FAIL (all actors). G-BEH: FAIL — the codebook is significantly WORSE than
the renderer, not better.** The mid band is the only one either actor's codebook
passes, and it passes by being *uninformative*: codebook replies collapse toward
'mid' regardless of the target level (qwen8b: 22/30 low-band and 23/30 high-band
replies judged 'mid' vs renderer 12/6). The renderer control on qwen8b (pooled
0.711, CI [0.622, 0.800]) clears the same G-ABS bar the codebook fails — proof the
judge discriminates and the failure is the codebook surface, not the instrument.

## 3. What the spike DID validate (machinery + measurement)

- Full P0→P6 pipeline runs end-to-end locally in 8 GB: two families + an 8B scale
  point (NF4 + dq), geometry extraction (C1/C2/C3 confound fixes), ev_bins
  readout, codebook derivation, P5 quality gates, K=30 behavioral generations,
  hosted judging, paired stats with seeded bootstrap CIs.
- Determinism held: master seed 20260815, identical seeds/bins across re-runs.
- Confound fixes worked: C1 (J-lens polarity w = ȳ − y) verified at full n on both
  families; C2 (one pre-registered middle-third layer rule, signed selection on
  TRAIN) applied identically; C3 (orthogonalization) moved Gemma arousal EV
  0.294 → 0.532. Corpus confound refuted (G-DATA 0.4122, ~7.6x GoEmotions' 0.054).
- P5 quality gates (committed 2026-08-16 d86793c): G-MASK 6/6 PASS; G-DEGEN 5/6
  (qwen/arousal FAIL); G-SMOOTH 3/6 (small-model codebooks marginal, 8B-valence
  book clean). Failures were honest and mild — surface quality was already
  borderline, and the behavioral result matches that picture.
- SCALE diagnostic (within-family Qwen): valence EV r 0.181 (1.7B) → 0.631 (8B),
  arousal 0.201 → 0.568 — geometry measurably improves with size (caveats: NF4 vs
  bf16, n=3 points, 8B C2 layer at band edge). But larger geometry did NOT rescue
  the behavioral gate: 8B codebook pooled G-ABS 0.411 still fails and G-BEH is
  significantly negative. SCALE was not flat, so even the SELF-HOST branch's
  "G-BEH passes + SCALE flat" condition was not met — the gate failed first.

## 4. Interpretation

1. **Value→words transfer is the broken link, twice demonstrated.** Spike-1 failed
   on VAD alignment (extracted geometry); spike-2 fixed the confounds, improved the
   geometry (esp. 8B), rebuilt the reference corpus, and still fails at the point
   where the codebook's affect tokens must survive into generated prose. The
   renderer proves the scaffold CAN carry affect the judge can read (0.711); the
   codebook's surface does not.
2. **Codebook replies are mid-collapsed.** Judgment distributions concentrate on
   'mid' (qwen8b 22-23/30 at both extremes) — the codebook text either fails to
   reach the extremes or flattens them; the affect-bearing reading the judge needs
   is absent from the surface. This matches P5's thin small-model separation.
3. **The judge instrument is sound.** Local base judges are non-functional
   classifiers (documented defect, decision 9 evidence); hosted deepseek-v4-flash
   at temp 0 discriminates (probe + renderer control). This cleanly separates
   "measurement broken" (chain v1) from "hypothesis refuted" (this run).

## 5. Rental / self-host decision

- **Do not rent V4-Flash; do not self-host.** Per the contract's own branch logic:
  G-ABS fail ⇒ SHELVE regardless; and G-BEH did not merely fail to pass, it was
  significantly negative on the primary actor. A bigger model is the obvious next
  probe (SCALE says geometry improves with size), but that is a NEW pre-registration
  decision, not a license to keep this branch alive.
- SHELVE = pipeline + machinery parked, results committed, reproducible. Reopening
  requires a new brief (rethink options below), NOT re-running this one.

## 6. Rethink options (proposals for the next pre-registration, not gate changes)

A. **Surface fidelity first.** The codebook→prose renderer is the suspected weak
   link (token-soup surface, P5 marginal smoothness). Test a fluent prose renderer
   (LLM polish of the SAME tokens, provenance-preserving) before touching anything
   else — it isolates surface from geometry.
B. **Bigger local model as the actor** (SCALE is the only direction that moved):
   re-run P6 on a 8B-class codebook vs renderer with the now-valid instrument.
C. **Behavioral grounding instead of VAD grounding**: fit the geometry
   directions directly on judge-classified behavior (the brief's own H4 spirit)
   rather than human VAD ratings — VAD alignment may be the wrong objective for
   model-native affect (spike-1's original suspicion, now supported twice).
D. **Keep the hosted judge as the standing instrument** for any future P6; the
   local base judges are documented as unusable for this task.

## 7. Where things stand

- P0 (contract) → P1 (corpus + G-DATA PASS) → P2 (extraction, 3 models) → P3
  (geometry + SCALE) → P4 (codebooks) → P5 (quality gates) → P6 (behavioral eval,
  VALID verdicts) — COMPLETE, committed. P7 = this memo.
- Gate ledger: G-DATA PASS; G-MASK 6/6; G-DEGEN 5/6; G-SMOOTH 3/6; G-ABS FAIL (3/3);
  G-BEH FAIL (significantly negative). Verdict: **SHELVE**.
- Key artifacts: diagnostics/p6-eval.json (gate record), diagnostics/gates-p5.json,
  diagnostics/geometry-{qwen,gemma,qwen8b}.json, data/codebooks/<model>/*.json,
  data/extractions/*/eval/*_judged_hosted_*.jsonl (K=30), p6-chain + judge logs,
  scripts/{p6_*,run_p6_hosted_judge,probe_hosted_judge,verify_hosted_judgings}.py.
- Commits (spike-2): 7cec79e → 05c1982 → 6212f3f → d65fac5 → c7d48ef → 41058a2 →
  6d70208 → f276dc0 → 9373d3f → c16cade → d86793c → 9890a38 → 8e4f18e → ab597d7 →
  81cd7d7. All local.