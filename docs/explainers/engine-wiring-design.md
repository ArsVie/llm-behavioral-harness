# Engine → control-vector wiring: design + A/B plan

Status: DRAFT (2026-10-04). Owner: Lily. Related: `docs/explainers/` (engine internals),
emotion-geometry repo (`reports/geometry-so-far.md`, `reports/psych-axes-crosscheck.md`),
replay results (`results/replay-live-31b-2026-10-03/report.md`).

Goal: replace (or augment) the prose "AFFECTIVE BEARING" block with **control-vector steering
whose intensities are computed by the stochastic engine** (cycle + circadian + mood), so the
model's affect state is injected in activation space instead of text.

---

## 1. What exists today

- Engine chain: cycle (m mood-offset, g reactivity, phase) + circadian (energy h) + mood
  (sampled M, momentum) → `BehaviorBrief` 11 float channels → prose block in the prompt.
- Only ONE number (energy) currently carries cycle+circadian into behavior; the rest of the
  cycle influence rides the daily mood sample. `c(h)` (circadian valence, logit) is defined
  but unwired (zero consumers).
- Replay evidence (31B, harness repo): valence steering is dose-gated and temperature-stable;
  constant application is strongest; stacked directions compound super-additively and the
  coherence cliff follows the COMBINED delta norm (pairs ~60 at the edge, triple ~73 collapses).

## 2. The mapping (proposal)

| engine quantity | direction (31B) | layer | scale source |
|---|---|---|---|
| valence (from M) | `p2-directions-chat-gemma-4-31b.npz:L36` row valence | layers[35] | dose window s ∈ 21–56 (disp=14 null; 42.4 proven) |
| energy (arousal) | corrected PC2 (hs35, Amd.1-suppressed) | layers[34] | same window; **weak validation — see §5** |
| warmth/love | `L36` row love | layers[35] | same window |
| dominance (NEW) | `p1f-dirs-gemma-4-31b-w500.npz:d` (ridge) | layers[35] | validation in flight (steering battery) |
| intensity (NEW) | `p1f-dirs-...:int` (ridge) | layers[35] | validation in flight |

**Scale rule (from the compose cliff):** budget the TOTAL delta norm, not per-direction.
For k active directions: `s_i = s_total / sqrt(k)`, with `s_total` conservative ≤ 45
(single-direction safe range 21–56; pairs ~60 = edge; 73 = collapse). Never stack at full
per-direction dose.

**Application style:** constant per forward (strongest, simplest) or decay; prefill-only and
last-position variants are strictly weaker. No per-token shocks needed.

## 3. Intensity locus (where the engine number becomes a scale)

Preferred: **(A) trace-level** `{m, g, phase, μ, η, mood_delta}` — the audit record built to
carry non-text causes; cleanest causality for experiments.
Acceptable: **(B) directive-level** `{valence, energy, warmth, playfulness, reactivity}` — the
shared hub the scheduler already consumes.
Rejected: **(C) parsing the rendered prose** — lossy, un-auditable.

## 4. A/B experiment design (replay-based, offline)

Scenarios: the 5 recorded live calls (same as all replay runs). Conditions:

1. `brief` — current system (prose BEARING block, no vectors) [what the live loop does now]
2. `vec` — vectors only (engine intensities → steering), no BEARING text
3. `both` — BEARING text + vectors (double channel)
4. `m-vs-M` — vectors driven by raw `m` vs sampled `M` (intensity locus sub-test)

Readouts: NRC valence/arousal/dominance of replies, persona markers (sprite tag, name-calls,
hearts), coherence (rep3), plus the dominance/assertiveness marker set from `analyze_steer.py`.
Success = vector arms match or beat the brief arm on affect carry while preserving persona
and coherence, and `both` shows no degradation.

## 5. Open items / blockers

- **Arousal direction**: steering-validated only once; the NRC-arousal text readout has a
  compressed dynamic range (~0.06 span vs valence 0.44) — readout-limited, not necessarily
  direction-limited. Battery run in flight (A±).
- **Dominance/intensity**: readable + transfer-validated; steering validation in flight (D±, I±).
- **Calibration**: dose window measured on 31B only; per-model calibration needed for others.
- **Latency/ops**: steering hook overhead is negligible (one add per forward); live path would
  be a local steer server (ModelServer + hook) behind the existing model backend abstraction;
  Modal is for experiments only.
- **Engine-side follow-ups** (separate): wire `c(h)` with its own peak (17–19h), fix late-luteal
  energy sign, add post-prandial dip, calibrate OU noise — see emotion-geometry
  `reports/psych-axes-crosscheck.md` §3.

## 6. Non-goals (this phase)

- No changes to the live loop yet (offline replay first).
- No new engine constants without the literature-verification pass (done, directional only —
  constants need sign-off).
