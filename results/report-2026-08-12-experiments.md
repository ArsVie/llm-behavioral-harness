# Experiments report — 2026-08-12 (orchestrator away)

> **CORRECTION ADDENDUM — 2026-08-13 (probe artifact):** the "flash still
> dead (HTTP 200, empty content)" conclusion in §1 E0a was a MEASUREMENT
> ARTIFACT, not a provider outage. The route probes capped `max_tokens` at
> 10; deepseek-v4-flash is a REASONING model that consumes its token budget
> on reasoning before emitting content, so capped probes return 200 with
> empty content (`finish_reason='length'`) even when the route is healthy.
> Verified 2026-08-13: real judge-shaped calls (no `max_tokens`) on flash
> returned valid pairwise JSON (2/3, third hit the 60s read timeout);
> `max_tokens=512` 3/3 OK; no cap 4/4 OK; the `max_tokens=10` probe shape
> 3/3 empty. Rate limits ruled out: zero 429/5xx, 10 parallel calls fail at
> the same rate as sequential, luna unaffected under identical concurrency.
> Fix: `experiments/cvs_g6.py::_family_route_ok` now probes WITHOUT
> `max_tokens`; judge client timeout raised 60s → 120s (judge calls run
> 20-47s on 13K-char prompts). G6 flash-family rerun is queued as backlog.
> The 2026-08-10 G3 episode (4-consecutive empties on actuated caps) may
> still have been real — caps in the 290-669 range can be exhausted by
> reasoning — but the "dead route" status since then was a probe artifact.

Run by the coding agent while the orchestrator is away. **No existing code or
committed artifacts were changed.** All new artifacts are untracked:
`results/it3-independent-audit-2026-08-12/`,
`results/it3-mood-decision-probe-2026-08-12/`,
`results/it3-mood-decision-probe-v2-2026-08-12/`. Scripts in `~/llh-exp-20260812/`.

Repo state at start: `main @ 07c7184` (manifest amendment, *awaiting user
approval*); iteration 3 complete through G5; G6 judged single-family (luna)
because the flash route was dead → **perceptual leg INCONCLUSIVE** per §17.4.

---

## 1. What was run

| ID | Experiment | Cost | Result |
|---|---|---|---|
| E0a | Route probes (flash / luna / openrouter / dashscope / deepseek / zen) | ~20 calls | **flash still dead** (HTTP 200, empty content). luna alive. No other provider reachable (401/403/402). zen = same service as go |
| E0b | Full pytest suite at HEAD | free | **green** (~941 tests) |
| E0c | `cvs_preflight` re-run (fake client, 3 days, seed 5001) | free | **GATE OPEN** — all ablation claims pass at HEAD |
| E1 | Independent audit of all 35 matrix cells (from DBs + records + persisted prompts) | free | see §2 — several claims verified, **one report bug found** |
| E2 | Mood-decision probe (the artifact's pending experiment), 2 frames × 90 generations on luna | ~180 calls | see §3 — **clean negative on decisions, positive on tone** |
| E3 | Judge self-consistency (luna pass1 vs pass2 from existing artifacts) | free | see §4 |
| E4 | Memory-lane verification (F5 fix) on 3 cells | free | **F5 confirmed fixed** |

Not run: G6 second family. Flash is still returning empty completions
(episode started 2026-08-10 03:23, still ongoing at 2026-08-12 02:26 UTC);
OpenRouter/DashScope/DeepSeek keys are invalid or unfunded (401/403/402); the
zen key is the same endpoint as go. The preregistered fallback (re-evaluate
family 1 with luna) is available but adds only test-retest evidence, which E3
already quantifies — see recommendation in §5.

---

## 2. E1 — Independent audit of the 35 cells (B10-style)

Full tables: `results/it3-independent-audit-2026-08-12/report.md` + `audit.json`.

### Verified claims (artifact "how-she-decides" + iteration-3 report)

| Claim | Artifact/report | Recomputed | Verdict |
|---|---|---|---|
| Empty replies | 0 / 6,489 (35 runs) | **0 / 6,489** llm_calls | ✅ exact |
| "What she's doing now" wrong | 108/192 = 56% (one run) | FULL 494/922 = **53.6%** (5 seeds); sample: "morning coffee" at t_h=19:00 | ✅ real bug, reproducible |
| Hooks grounded | 222/222 | **222/222** FULL; 1,589/1,589 pooled (7 conditions) | ✅ exact |
| Hooks *mentioned* in reply | ~27% | **~21%** keyword overlap (undercount by design, both sides) | ✅ consistent |
| Proactive fires mid-conversation | 32–36% | **34%** FULL (76/222), **36%** NTF (92/254) | ✅ exact |
| Mood→timing effect | 1.35% (it2) | matrix: SNS vs FULL 0–30% by seed — see timing bug below | ⚠️ see below |
| NO_LIFE goldfish | arcs don't survive midnight | active-arc day-overlap **0.000** vs FULL **1.000** | ✅ ablation works |
| Actuator amplitude | FULL varied vs flat | FULL max_tokens 290–669 (129 distinct), delay 7.4–30.3 s, closing 0.22–0.85; NO_ACTUATORS and STRUCTURED_NO_STATE flat 600/5.0/0.5 | ✅ B4 landed |
| Perturbation block | days 11–14 | **4 negative turns in every cell, days 11–14** (20/condition) | ✅ present |
| Blank invariant < 1% | report §5 | 0 blanks in all 35 cells | ✅ |

### Bug found in the iteration-3 report (§7.4 timing table)

`it3-report-data.json` records `"full_seed": 5001` and every
STRUCTURED_NO_STATE seed was compared against **FULL/seed5001** instead of the
paired same-seed FULL cell. Recomputed paired:

| seed | SNS n_pro | FULL n_pro (paired) | count_div | gap_div | two-leg ≥15%+≥10% |
|---|---|---|---|---|---|
| 5001 | 45 | 48 | 6.2% | 2.9% | no |
| 5002 | 45 | 43 | 4.7% | 3.6% | no |
| 5003 | 40 | 47 | 14.9% | 17.9% | **no (count 0.1pt under)** |
| 5004 | 48 | 37 | **29.7%** | **23.7%** | **yes** |
| 5005 | 47 | 47 | 0.0% | 3.7% | no |

Same verdict ("1/5 pass, at margin") but **the passing seed is 5004, not 5003**
(the report's table is wrong on 5001/5002/5004/5005; 5003 fails the count leg
at 14.9% paired). NO_TIMING_FEEDBACK (positive control): two-leg pass 2/5
(5004, 5005), direction 5/5 positive (+6.4%…+29.7%) — the control is much
weaker at the preregistered bars than the G2 split suggested (29.17% fired /
14.4% count were fake-client numbers).

### Judge reliability (from the existing two luna passes)

- Same 105 pairs judged in both passes (full within-seed crossing).
- **Winner agreement across passes: 59.1%** (chance ≈ 14%).
- BT-scale Spearman per dimension: persona 0.49, trajectory 0.58, relational
  0.78, behavioral **−0.68**, calibrated 0.59.
- Attention probes: 4/4 correctly classified (degraded transcript), 0
  disqualifications — the protocol's guard works.
- Reading: the instrument is only moderately self-consistent; per-dimension
  BT scales flip substantially between passes (e.g. trajectory FULL 0.46 →
  3.01). The single-family perceptual leg is **not just incomplete, it is
  noisy** — a second family would have had a hard time agreeing with it.

### Memory lanes (F5) — verified fixed on seed 5001

| lane | episodes | AnyEvidence | LatestEvidence | CompleteChain |
|---|---|---|---|---|
| FULL | 17 | 1.0 | 0.333 | **0.333** |
| SIMPLE_RAG | 17 | 1.0 | 1.0 | **1.0** |
| RAW_HISTORY | 0 (by design) | 0.333 (fair raw probe) | 0.333 | **0.0** |

SIMPLE_RAG's keyword lane completes all three chains where FULL's semantic
lane completes one in three — the it2 "FULL 0.333" headline reproduced, and
the simple baseline now measures honestly (F5 closed).

---

## 3. E2 — Mood-decision probe (the artifact's pending experiment)

> "Same conversation, same event, run her mood from awful to great, 15 tries
> at each level. If she skips the gym more often when she's in a bad mood,
> she's genuinely using her mood."

Implementation: real DayRecords from the matrix (M = 1/3/5/7/9, real cycle
phases, momentum, mu), real Nova persona, agenda item "practice lifting
19:00–20:30", real assembler prompt, actuated max_tokens per level, 15 reps ×
temperature 0.8 on gpt-5.6-luna; all 180 replies read **blind** (shuffled).
Frame v1: "What are you up to tonight?" Frame v2: "…are you still going to
lifting?" (choice pressure). Full report:
`results/it3-mood-decision-probe-2026-08-12/report.md`.

**Decision channel: completely inert.** 90/90 GO in v1, 90/90 GO in v2 —
zero skips at any mood, control included. The doc's test answers *no*: she
does not skip the gym more when low.

**Tone channel: mood clearly reaches the text (open frame only).**
Humor markers per level, v1: M=1 0/15 · M=3 0/15 · M=5 2/15 · M=7 5/15 ·
M=9 **10/15** (perfectly monotonic; Fisher M9-vs-M1 p = 0.0002, trend
p = 1.9e-5). Length 163→177 chars. Harness-off control: flat, templated,
0/15 humor. Under choice pressure (v2) every level collapses to the same
scripted template — the state's signature vanishes.

**Implication for the availability feature:** the design's backup path
("decided by the system from her mood and how important it is") is
empirically justified — an LLM asked a yes/no availability question falls
back to a scripted template and ignores the state, so the decision must come
from the state mechanically or the mood will never be perceptible in it.

---

## 4. E3 — G2 preflight + suite at HEAD

`cvs_preflight` (fake client, 3 days, seed 5001): **GATE OPEN**, ok=true, all
seven conditions' claims evaluate; STRUCTURED_NO_STATE/NO_ACTUATORS realize
flat controls, all others varied — identical shape to the G2 gate close.
Full pytest: green.

Minor reproducibility finding: re-running `experiments.behavior_showcase`
regenerates byte-different `behavior-trace.json`/PNG/examples.md (aggregate
`phase-summary.json` identical) — the showcase is not byte-reproducible
across runs; committed files were restored untouched.

---

## 5. State of the iteration and recommendation

1. **DoD 1–5 verified independently** (blank invariant, ablations, turn
   counts, timing at margin, memory lanes) — the corpus is clean and the
   mechanical claims hold, with the §7.4 table corrected (paired-seed).
2. **DoD 6 (two judge families) cannot be completed right now**: flash is
   still dead, no other provider credential is valid. The perceptual leg
   stays INCONCLUSIVE. Options when you're back: (a) wait for flash to
   recover and run G6 family 1; (b) preregistered fallback — two more luna
   passes as family 1 (adds test-retest, not independence; expected
   agreement ~60% given E3); (c) fund/point a genuinely different provider
   (OpenRouter/DashScope/DeepSeek keys exist but are rejected) for a true
   second family.
3. **The mood-decision probe answers the artifact's open question** with a
   clean, cheap negative on decisions and a strong positive on tone — and it
   hands the availability feature a concrete design constraint (mechanical
   decision, not LLM choice).
4. Nothing was committed. New artifacts are untracked and ready for review;
   the manifest amendment commit is still awaiting your approval.
