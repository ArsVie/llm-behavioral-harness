---
type: plan
title: "Iteration 3 — perceptual validity: real conversations, real ablations, real instruments"
description: "Repairs the corpus, the ablations and the judging protocol so the perceptual question becomes answerable; multi-turn conversation is the central new capability"
tags: [plan, iteration-3, evaluation, multi-turn, orchestrator]
timestamp: 2026-08-09T00:00:00+00:00
---

# 0. Verdict on Iteration 2 — read this before anything else

Iteration 2 closes as **mechanically validated at scale, perceptually inconclusive.**

What it proved, and what stands without qualification:

- ~700 real proactive fires across 35 real-LLM cells: zero ungrounded, zero wrong-intent, zero
  stranded opportunities, zero memory-provenance failures, counts exact.
- 120-day soak, 2 seeds, 5 restarts each, all hard invariants zero.
- Deterministic replay, clean-start bootstrap, referential integrity of the grounding chain.

That is the deliverable. Do not weaken it by defending §5.

What it did not prove, and what this iteration exists to fix:

| # | Finding | Evidence |
|---|---------|----------|
| F1 | **27.7% of the corpus is blank** (579/2090 assistant turns); 18–40% in every cell; rises from ~20% early to ~40% by day 17+ | direct scan of the 35 committed DBs |
| F2 | **The judges cannot see it.** corr(blank-rate, score) = +0.10 / +0.32 / +0.07 / +0.08 across the four dimensions | 35 cells × 2 families |
| F3 | **The two judge families do not agree.** Pooled over 35 transcripts: persona r=+0.35, trajectory r=+0.24, relational r=**−0.19**, behavioral r=+0.18. Significance threshold at n=35 is r≥0.33. Severity gap up to 1.84 pts | judge_pass*.json |
| F4 | **Five of seven ablations do not ablate.** `STRUCTURED_NO_STATE` produces proactive counts *identical* to FULL in all 5 seeds; `NO_LIFE` retains arcs=3, agenda=158 unchanged; `NO_ACTUATORS` ≡ FULL in 4 of 5 seeds | matrix_audit_summary.json + per-cell DBs |
| F5 | **The memory baselines are pinned at zero by construction.** `RAW_HISTORY` has 0 episodes so any retrieval metric is 0 necessarily; `SIMPLE_RAG` scores AnyEvidence=0.0 despite holding 17 episodes byte-identical to FULL's — the metric never routes through the condition's memory lane | ec_backfill.json + DB inspection |
| F6 | **There are no conversations.** Every exchange is one user turn and one companion reply. `closing_tendency` is derived, clipped, and converted to a prompt string — it has never ended a conversation, because none last longer than one turn | `user_script()`, `harness/actuation.py:40` |

F6 is the deep one. Four dimensions of relational quality were rated against a corpus containing
no dialogue. Read together, F1–F6 say the same thing: **iteration 2 measured a system that was
never fully exercised, with instruments that could not resolve what it did measure.**

# 1. Objective

At the end of this iteration, a single command must produce a corpus in which:

```text
sustained multi-turn conversations          (not one-shot fact injections)
  × a companion who actually speaks         (blank rate < 1%, enforced)
  × ablations that measurably ablate        (asserted before generation, not after)
  × metrics that read the right lane        (each condition probed through its own memory)
  × instruments with resolution             (pairwise, severity-modelled, ≥2 families)
```

and in which the question *"is the endogenous state perceptible?"* has an answer that survives
both judge families.

# 2. The one process change that matters

Iteration 2 burned **4h12m of real API time** producing a corpus that was 28% blank, and nobody
found out until after the judging was complete. A single 7-day cell — about six minutes — would
have caught it.

**Rule for iteration 3: no expensive generation run may start until a cheap real-model smoke gate
has passed on the same code path.** This is G3 below. It is not optional and it is not a
formality.

Corollary: the mechanical audit must gain teeth it did not have. A cell where the companion is
silent 40% of the time returned `validated: true`. That must become impossible.

# 3. Non-goals — do not let these in

- Do **not** touch the Weibull timing family. Weibull-vs-lognormal remains a post-confirmatory
  experiment (it2 plan §17.5). Changing the renewal family now re-confounds the very comparison
  this iteration exists to make.
- Do **not** tune any threshold after seeing a result. Preregistration discipline from G4 of
  iteration 2 carries over unchanged.
- Do **not** re-open the cycle phenotype / per-persona latent coefficient design. It is a good
  idea and it is iteration 4.
- Do **not** add new companion features. This iteration adds exactly one capability (multi-turn)
  and otherwise repairs what exists.
- Do **not** attempt to salvage the 35 iteration-2 cells. The blanks are in the transcripts the
  judges read *and* in the context the companion conditioned on. They are archived evidence, not
  a corpus.

# 4. Ownership map (iteration 3)

The iteration-2 map is superseded. Strict file ownership, one owner per path, worktrees
`llh-wt-it3-*` on branches `wip/it3-*`.

| Agent | Workstream | Owns |
|---|---|---|
| B1 | Generation integrity **(blocking)** | `harness/client.py`, generation seam in `harness/session.py` |
| B2 | Multi-turn conversation | `harness/session.py` turn loop, `harness/runtime.py` conversation lifecycle |
| B3 | Conversational user simulator | `experiments/cvs_user.py` (new), `user_script` replacement |
| B4 | Actuator amplitude | `harness/behavior.py`, `harness/actuation.py` |
| B5 | Latent state → timing coupling | `engine/timing.py`, `harness/scheduler.py` |
| B6 | Metric lane routing + fair probes | `experiments/cvs_common.py` metrics section |
| B7 | Prompt persistence | `harness/store.py`, migrations |
| B8 | Validator teeth + ablation pre-flight | `experiments/validation/`, `experiments/cvs_preflight.py` (new) |
| B9 | Judge protocol v2 | judge section of `experiments/companion_vertical_slice.py`, `experiments/cvs_judge.py` (new) |
| B10 | Review + preregistration | read-only; `plans/`, `results/*/manifest.json` |

Nine implementation agents plus a read-only reviewer. B7 and B9 are deferrable if the batch is
too wide — see §8.

# 5. Gate 0 — contract freeze (before any parallel work)

B2, B3, B6, B8 and B9 all bind to the same two seams. Freeze them first, in one short
orchestrator-owned commit, exactly as iteration 2 did with `ContactOpportunity`.

## 5.1 The conversation seam

```python
@dataclass(frozen=True)
class ConversationTurn:
    speaker: Literal["user", "companion"]
    text: str
    t_h: float
    turn_index: int          # 0-based within the conversation
    conversation_id: str

@dataclass(frozen=True)
class Conversation:
    id: str
    opened_t_h: float
    closed_t_h: float | None
    opened_by: Literal["user", "companion"]
    close_reason: Literal["closing_tendency", "user_left", "quiet_hours", "max_turns"] | None
    turns: tuple[ConversationTurn, ...]
```

A conversation is the unit that memory sessions, judge sampling and relational metrics all key
off. `close_reason` is the field that finally makes `closing_tendency` falsifiable.

## 5.2 The ablation-effectiveness assertion

```python
@dataclass(frozen=True)
class AblationClaim:
    condition: str
    channel: str        # "timing" | "memory_store" | "generation_controls" | "life_state"
    assertion: str      # human-readable, e.g. "n_proactive differs from FULL by >= 15%"
    check: Callable[[dict, dict], bool]   # (cell_records, full_records) -> bool
```

Every non-FULL condition must declare at least one `AblationClaim`. A condition whose claim fails
the pre-flight is either fixed or removed from the matrix **before** generation. This is the gate
that would have caught F4.

Gate 0 exit: contracts committed, suite green, B10 sign-off. No agent dispatches before this.

# 6. Wave 1 — parallel implementation

All nine run concurrently in worktrees. Each brief carries: the finding it closes, its acceptance
criterion, and the explicit instruction that acceptance is mechanical, not narrative.

---

## B1 — Generation integrity (BLOCKING — everything downstream waits on this)

**Closes F1.** 27.7% of assistant turns are empty.

Investigate in this order, and report which one it actually is rather than fixing speculatively:

1. **Truncation.** `max_tokens` runs 525–594. Check whether empty replies correlate with the low
   end of the band, and whether the provider returns `finish_reason: length` with empty content.
2. **Provider empties.** Does the API return `content: ""` or `content: null`? Log the raw
   response envelope for every empty.
3. **Swallowed retries.** `_post` retries on exception. Does it retry on a 200-with-empty-body?
   Almost certainly not — that is the likeliest culprit.
4. **Actuator interaction.** `NO_ACTUATORS` (flat `max_tokens=600`) has the *highest* blank rate
   (31–40%). If a larger budget produces more blanks, the cause is upstream of truncation.

Acceptance:
- Root cause named, with the evidence that identifies it.
- Empty replies retried with bounded backoff; a persistent empty raises rather than persisting a
  blank turn.
- A 7-day real-client probe on one cell reports **blank rate < 1%**.
- Regression test: a client stub returning empty content twice then real content produces one
  non-empty persisted turn.

Note for the brief: the day-26 and day-29 transcripts show the companion *narrating around its own
silence* ("you've caught me twice now", "since you asked twice now"). The blanks are in its
context. This is not cosmetic.

---

## B2 — Multi-turn conversation

**Closes F6.** This is the largest workstream and the one that changes the product, not just the
measurement.

Today the runtime handles one user message and produces one reply. It must handle a conversation:

- Open a conversation on the first message (either party).
- Continue while the user replies and `closing_tendency` has not fired.
- Close on: a `closing_tendency` draw, user silence past a threshold, quiet hours, or `max_turns`.
- Persist `Conversation` + `ConversationTurn` rows; record `close_reason`.
- One memory session per conversation, not per day — L1→L2 episode formation keys off the
  conversation boundary.

`closing_tendency` becomes a **real actuator**: at each companion turn, draw against it to decide
whether to taper. It currently only produces one of two prompt strings across 62 messages. After
this, its effect is mechanically observable in the turn-count distribution.

Acceptance:
- Turn-count-per-conversation is a non-degenerate distribution (not all 1).
- `close_reason == "closing_tendency"` accounts for a preregistered share of closures.
- High vs low `closing_tendency` produces a measurable difference in mean turns per conversation
  under the fake client, asserted by a test.
- Restart mid-conversation resumes without rewind (extends the existing resume-no-rewind
  invariant to conversation granularity).

---

## B3 — Conversational user simulator

**Closes F6 on the eval side.** Depends on Gate 0's seam; may start immediately against it.

Replace `user_script()` — a list of `(t_h, text)` one-shots — with a seeded conversational agent
that holds a multi-turn exchange. It must remain **deterministic per seed** and **identical across
conditions**; that is non-negotiable and it is why this is a scripted-agent, not a free LLM.

Required conversational repertoire, because these are what the relational dimensions actually
probe (CompanionBench finds *holding ambiguity* and *calibrated challenge* the most discriminating
dimensions, and "surface warmth substituting for substantive support" the dominant failure):

| Behavior | Why |
|---|---|
| Follow-up questions within a conversation | the only way persona consistency is stressed |
| Disagreement / pushback | tests calibrated challenge vs sycophancy |
| Ambiguous, unresolved disclosure | tests holding ambiguity |
| Rupture and repair | the relational arc that one-shots cannot contain |
| Topic abandonment mid-conversation | tests whether she notices |
| The existing fact/chain/probe injections | preserved, now embedded in dialogue |

The perturbation block (days 11–14) moves inside conversations rather than sitting as isolated
negative one-liners.

Acceptance:
- Mean turns per conversation ≥ 4 under the fake client.
- Byte-identical user turns across all conditions for a given seed (assert this — it is the
  foundation of every between-condition comparison).
- The recall probes and event chains still fire on their preregistered days.

---

## B4 — Actuator amplitude

**Closes F4 (partially).** The actuators are wired and inert.

Measured range over FULL/seed5001, 62 messages:

```
max_tokens          525 – 594     (±6% around 551)
response_delay_s    3.27 – 5.80   virtual seconds
closing_tendency    0.24 – 0.48
closing_guidance    2 distinct strings, total
```

A ±6% token budget and a 2.5-virtual-second delay spread cannot be perceived by any observer. This
is why `NO_ACTUATORS ≡ FULL`: not a null result, arithmetic.

Widen the state→control mapping so that a low-energy day and a high-energy day are *visibly*
different artifacts, then preregister the target ranges **before** the matrix runs. Suggested
starting points, to be justified and frozen by B4 and reviewed by B10:

- `max_tokens`: a range wide enough that terse days are genuinely terse (order 150 → 700).
- `response_delay_s`: must map to real inter-turn latency inside a conversation, now that
  conversations exist — this is where delay finally becomes observable.
- `closing_tendency`: consumed mechanically by B2, so its amplitude now shows up in turn counts.
- `closing_guidance`: more than two strings, or drop it in favour of the mechanical draw.

Acceptance: a fake-client A/B at fixed extreme states produces distributions of reply length,
turn count and latency that are separated by a preregistered margin.

---

## B5 — Latent state → timing coupling

**Closes F4 (the important half).**

`STRUCTURED_NO_STATE` produced proactive counts identical to FULL in all five seeds. The hazard
responds to the score-feedback term and to nothing else — `NO_TIMING_FEEDBACK` moves counts
22→46 at seed 5005, so the plumbing works; the state simply is not connected to it.

**This is the single change that makes the central thesis testable.** Until latent state reaches
the timing channel, "endogenous stochastic state drives mechanical behavior" is unfalsifiable in
the timing dimension.

Couple energy / social-drive / mood into the hazard multiplicatively, in the spirit of:

```
h(τ, t) = h₀(τ) · C(t) · exp(w_E·E_t + w_S·S_t + w_R·R_t + w_A·A_t)
```

with the weights preregistered and the Weibull base `h₀` untouched.

Acceptance:
- `STRUCTURED_NO_STATE` vs `FULL` produces a preregistered minimum divergence in proactive count
  and inter-contact distribution under the fake client, across ≥5 seeds.
- Existing timing invariants (min gap, daily cap, max silence, quiet hours) all still hold.
- The `AblationClaim` for `STRUCTURED_NO_STATE` passes pre-flight.

---

## B6 — Metric lane routing + fair probes

**Closes F5.**

Two defects, both in the metrics rather than the system under test:

1. `event_chain_metrics()` and `recall_probe_metrics()` construct a `MemoryAgent`
   unconditionally. They never route through `_memory_for(condition)`. Consequence: `SIMPLE_RAG`
   scores `AnyEvidence = 0.0` while its store holds 17 episodes byte-identical to FULL's. The
   preregistered hypothesis is *"STRUCTURED_MEMORY > both baselines"* and one baseline is a
   measurement failure.
2. `RAW_HISTORY` has zero episodes, so any episode-keyed metric returns 0 necessarily. Reporting
   that as a memory finding is circular. The probe must be **fair to the lane**: score whether the
   fact is recoverable *by the mechanism the condition actually uses* — for raw history, that means
   scoring against the dialogue context the model receives.

Acceptance:
- Every condition is probed through its own memory lane; assert `SIMPLE_RAG` returns non-zero
  `AnyEvidence` given a populated store.
- Re-backfill over the 35 archived iteration-2 DBs and publish the corrected three-way contrast
  as an **exploratory** result (it remains a broken corpus; label it as such).
- Report absolute CompleteChain, not only the gap. FULL at 0.333 — one chain in three — is the
  honest headline and it aligns with LifeSide's finding on complete-chain retrieval.

---

## B7 — Prompt persistence

**Closes the iteration-2 §6 limitation.**

`llm_calls` stores `prompt_hash` (16 hex chars), empty `response`, and `system_len: null`. The
prompt-side leak scan is therefore unverifiable for those runs, and replay cannot reconstruct what
was sent.

Persist the full system prompt and message payload (a run-scoped, opt-in eval-mode column is fine;
it need not be on by default in production). Then the invariant-16 leak scan covers what it claims
to cover.

Acceptance: a prompt containing a forbidden cycle token is caught by the scan in a test, and
`repro_json` alone suffices to reconstruct a call.

---

## B8 — Validator teeth + ablation pre-flight

**Closes F1's audit blindness and F4's discovery-after-the-fact.** Two deliverables.

**(a) Hard invariants gain:**

- `empty_assistant_turns == 0` (hard zero).
- blank-rate ceiling as a run-level assertion.
- truncated-reply detection (the iteration-2 corpus ends a run on `Nova: Hey`).
- conversation coherence: no conversation with zero companion turns.

A cell that is 40% blank must fail, loudly, with the count in the failure message.

**(b) `cvs_preflight` — the cheap ablation gate:**

Run all matrix conditions for 3 days with the **fake** client. For each, evaluate its declared
`AblationClaim` against FULL. Any condition whose claim fails is reported as a **null ablation**
and blocks the matrix until fixed or dropped.

Cost: seconds. It would have caught `NO_LIFE` (arcs=3, agenda=158, unchanged),
`STRUCTURED_NO_STATE` (identical proactive counts) and `NO_ACTUATORS` (identical in 4/5 seeds)
before 4h12m of API spend.

---

## B9 — Judge protocol v2

**Closes F2 and F3.**

The current instrument does not work, and adding families to it does not help:

- Pooled cross-family agreement: +0.35 / +0.24 / **−0.19** / +0.18. Three of four dimensions are
  not significant at n=35; one is negative.
- Severity gap up to 1.84 points on a 9-point scale.
- Ceiling: flash rates 97% of `relational_quality` at ≥8; luna uses **two** values for
  `persona_enactment` across 70 ratings.
- Both families rated a 28%-blank corpus at 7.5–8.7 on persona.

Replace absolute 1–9 rating with **forced pairwise comparison**: same seed, two conditions, blind,
order-randomised, "which of these two is more X, and why" with a required one-sentence
justification. Pairwise is dramatically more robust than absolute scoring on long transcripts, it
removes the severity term by construction, and the justification text is auditable — a judge that
cannot see 40% blanks will produce justifications that visibly fail to mention it.

Additionally:

- Aggregate with Bradley–Terry (or Elo) to recover a per-condition scale from pairwise outcomes.
- If any absolute scoring is retained, model judge severity explicitly (β_j) rather than averaging
  across families.
- Keep judge identity attached to every score. Keep the §17.4 rule: an effect seen by one family
  is not established.
- **Add an attention probe.** Include a deliberately corrupted transcript in each pass. A judge
  that rates it highly is disqualified for that pass. This directly instruments F2.
- Retain the four dimensions, and add a fifth now that dialogue exists: **calibrated challenge /
  anti-sycophancy**, which one-shot transcripts could not support.

Acceptance: on a held-out pair where one transcript is deliberately degraded, the protocol
identifies the degraded one at high rate under both families.

---

## B10 — Review + preregistration (read-only)

Runs throughout. Two hard outputs:

1. Gate-0 contract review, and a review of every B1–B9 acceptance claim against evidence rather
   than narrative. Iteration 2's lesson: *the report said "2 instances"; the corpus had 579.*
   Every quantitative claim in the final report must be independently recomputed from artifacts by
   B10 before it ships.
2. The iteration-3 preregistration manifest, frozen before any generation, carrying: matrix
   design, the `AblationClaim` set, actuator amplitude targets, timing-coupling weights, judge
   protocol, and every threshold.

# 7. Wave 2 — gates

| Gate | What | Blocks |
|---|---|---|
| **G0** | Contracts frozen, suite green | all of Wave 1 |
| **G1** | B1–B9 merged in plan order, full suite green after each | G2 |
| **G2** | `cvs_preflight` — every ablation claim passes on the fake client | G3 |
| **G3** | **Real-model smoke: 1 condition × 1 seed × 7 days.** Blank rate < 1%, conversations non-degenerate, no truncation. ~6 minutes | G4 |
| **G4** | Preregistration manifest frozen and committed | G5 |
| **G5** | Confirmatory matrix, real LLM, **with checkpoints and perturbation enabled** | G6 |
| **G6** | Judge protocol v2, ≥2 families, attention probes passed, Bradley–Terry aggregation | report |

G3 is the gate that iteration 2 lacked. Do not skip it because the fake client passed.

# 8. Matrix redesign

Iteration 2 ran 7 conditions × 5 seeds. Given that judge resolution — not condition count — is the
binding constraint, **more seeds beat more conditions.**

| Condition | Keep? | Rationale |
|---|---|---|
| FULL | yes | reference |
| NO_ACTUATORS | yes | meaningful once B4 lands |
| STRUCTURED_NO_STATE | yes | meaningful once B5 lands; carries the central thesis |
| RAW_HISTORY | yes | memory floor, now with a fair probe (B6) |
| SIMPLE_RAG | yes | proper RAG baseline, now actually measured (B6) |
| NO_TIMING_FEEDBACK | yes | **positive control** — the one ablation with a proven large effect. If the pipeline cannot detect it, the pipeline is broken |
| NO_LIFE | **only if fixed** | currently a null ablation; B2's owner fixes it or B8's pre-flight drops it |

Recommended: **6 conditions × 6 seeds × 30 days = 36 cells.**

Budget warning for the orchestrator: iteration 2 ran ~5–6 min/cell at ~100 one-shot messages.
Multi-turn conversations will multiply message volume roughly 3–4×. Expect **12–20 min/cell**, so
36 cells is **8–12 hours** of real API time. Run it as a background batch after G3, checkpoint per
cell, and do not start it until G2 and G3 are both green. If the budget is unacceptable, cut days
to 21 before cutting seeds — statistical power matters more than horizon here, and 21 days still
covers a full cycle plus recovery.

Every cell runs with **checkpoints enabled** (so M7 is real, not vacuous) and **perturbation
enabled** (so §17.3 is finally exercised under a real model).

# 9. Dispatch order

```
G0  orchestrator: contract freeze                    (serial, short)
     │
     ├── B1  generation integrity      ← BLOCKING, dispatch first, highest priority
     ├── B2  multi-turn conversation   ← largest; dispatch immediately after B1's brief
     ├── B3  user simulator            ← binds to G0 seam, parallel with B2
     ├── B4  actuator amplitude
     ├── B5  state → timing coupling
     ├── B6  metric lane routing
     ├── B7  prompt persistence        ← deferrable if batch too wide
     ├── B8  validator + pre-flight
     ├── B9  judge protocol v2         ← deferrable to a second batch
     └── B10 review                    ← read-only, runs throughout
     │
G1  merge B1 → B8 → B6 → B4 → B5 → B2 → B3 → B7 → B9   (suite green after each)
G2  cvs_preflight                                     (seconds)
G3  real smoke, 1 cell, 7 days                        (~6 min)
G4  freeze manifest
G5  confirmatory matrix                               (8–12h background)
G6  judge v2
```

Merge order rationale: B1 first because everything is measured through it; B8 second so the
teeth exist before anything claims validation; B2/B3 late because they are the widest diffs.

If the batch cap forces a split: **batch 1 = B1, B2, B3, B4, B5, B8** (the corpus and the
ablations), **batch 2 = B6, B7, B9** (the instruments). B10 spans both.

# 10. Risk register

| Risk | Mitigation |
|---|---|
| B1 finds the blanks are a provider-side quality issue, not a harness bug | Then the model is unfit for a 30-day corpus. Escalate to a model change before G5 — do not run the matrix on a generator that returns 28% empties |
| Multi-turn (B2) destabilises the clock / rollover / quiet-hours logic | B2 must extend, not replace, the resume-no-rewind and quiet-hours invariants; A9-class adversarial tests before merge |
| Multi-turn blows the API budget past what is affordable | Cut days 30→21 before cutting seeds; decide at G4, on the G3 measurement, not by guessing |
| Pairwise judging (B9) is slower than absolute rating | It is: 36 cells is ~630 pairs at full crossing. Sample pairs within seed rather than crossing all — preregister the sampling |
| Wide parallel diffs collide | Strict file ownership per §4; worktrees; merge in the §9 order with a green suite after each |
| The perceptual answer comes back null again | That is a legitimate result **only if** G2 and G3 passed — i.e. only if the ablations ablated and the companion spoke. That is precisely the difference between this iteration and the last one |

# 11. Definition of done

The iteration ships when the report can state, with artifacts a reviewer can recompute:

1. Blank rate < 1%, enforced by a hard invariant.
2. Every matrix condition demonstrably ablates its target channel, asserted before generation.
3. Conversations are multi-turn, with `closing_tendency` mechanically observable in turn counts.
4. Latent state measurably reaches the timing channel.
5. Each memory condition probed through its own lane; absolute CompleteChain reported, not only gaps.
6. Judge protocol resolves a deliberately degraded transcript under both families.
7. A stated answer — positive **or** negative — to: *is the endogenous stochastic state perceptible
   to an independent observer?*

A clean negative on (7), on a corpus that passed (1)–(6), is a publishable result and a good
outcome. A positive on (7) without (1)–(6) is what iteration 2 produced, and it is worth nothing.
