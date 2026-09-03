---
type: design-note
title: AFK / double-message / no-goodbye — spike-4 design pass (armed inactivity presence)
description: "Design for spike 4: an armed post-proactive inactivity event fires at most one gentle double-text when the user goes quiet; a deterministic farewell detector tags conversations abandoned without a goodbye. Distinct from the negotiation AFK bomb and from the away/presence signal — under the away≠close lifecycle model a quiet user goes dormant, not closed. Engine owns timing; Lily owns the message. Pre-registration follows this note."
tags: [design, afk, double-message, proactive, spike-4, s3]
timestamp: 2026-08-16
---

# AFK / double-message / no-goodbye — spike-4 design pass

## Why this pass exists

`results/spikes-registry.md` row 4: candidate, needs design before pre-registration.
The spike inherits scope deferred twice already:

- `availability-negotiation-contract.md:176-178` — "Explicitly deferred: General
  user-response monitoring / AFK reactions — this build uses 'user silent >
  SHORT_AFK' only as a Decide trigger."
- `design-note-cognition-principle-2026-08-15.md:67-69` — the AFK/double-message
  experiment is listed as wave 2, out of scope for wave 1.

This note fixes: what the three features mean, where they live, how they stay
deterministic, and how they interact with the machinery they must not collide
with. It ends with the draft pre-registration for review.

## The three features

1. **Armed inactivity event** — after a proactive companion message in an open
   conversation, the engine arms ONE inactivity deadline; if the user has not
   spoken by then, the event is due.
2. **Gentle double-text** — when the armed event is due (and a deterministic
   draw says so), Lily sends at most one short follow-up, generated as an
   ordinary companion turn. Never a second nudge after that (idempotent).
3. **No-goodbye detection** — at any conversation close, a deterministic
   farewell detector classifies whether the user said goodbye; a no-goodbye
   close is tagged and (optionally) emitted as a COMPANION_EPISODE. Purely
   observational in the spike.

The cognition principle applies unchanged (`design-note-cognition-principle`
§The causal claim): engine owns timing (when the arm arms/fires/expires),
Lily owns the decision (whether the double-text is the right move, and its
words). The engine never invents message text.

## Never-collapse contract — three silence signals stay distinct

| Signal | Constant | Where | Meaning |
|---|---|---|---|
| Negotiation Decide bomb | `SHORT_AFK_MIN = 10.0` | `negotiation_contract.py:79-82` | 10 min of silence resolves go/skip/delay inside an unresolved event negotiation |
| User away / abandoned-chat backstop | `USER_AWAY_THRESHOLD_H` (= `USER_LEFT_THRESHOLD_H = 0.25` vh / 15 min) | `harness/tunables.py` (single source; imported by `session.py`) | Silence past this marks the user *away* (dormant, not closed — away≠close) and, at the long end, backstops an abandoned chat. Closing itself is a checkpoint decision (day/quiet-hours boundary or the long backstop), not this signal alone |
| Double-text trigger (NEW) | `DOUBLE_TEXT_SILENCE_MIN = 8.0` (default, tunable) | new `harness/inactivity.py` | silence past this makes the armed post-proactive event due |

**Fact check that reframes the registry:** the old "10-min idle-close"
proposal is SUPERSEDED by the away≠close decision — silence makes the user
*away* (dormant), and closing is a checkpoint, not an idle timer. See
[`../plans/plan-lifecycle-away-checkpoint-2026-08-17.md`](../plans/plan-lifecycle-away-checkpoint-2026-08-17.md)
and [`../BACKLOG.md`](../BACKLOG.md) for the current model; do not treat the
idle-close as the plan. The 10-min figure in code belongs to the negotiation's
Decide bomb, not to any close. Keep the three signals distinct.

**Ordering invariant:** the double-text must fire BEFORE the away/backstop
threshold would disarm the arm, or the feature never fires. Rule:
`DOUBLE_TEXT_SILENCE_MIN < USER_AWAY_THRESHOLD_H` (0.25 vh / 15 min). Default
~8 min keeps that margin.

## Where it lives

Mirror the negotiation seam exactly (proven pattern: pure mechanics + session
wake hook + runtime parking):

- NEW `harness/inactivity.py` — pure, deterministic, importable, frozen
  contract (like `negotiation_state.py`). No I/O. Holds the armed-event state
  machine and the farewell detector in one small module.
- `harness/session.py` — a `self._inactivity_arms` map (sibling of
  `self._negotiations`); arm at the end of a proactive outbound; a
  `check_inactivity(now)` wake hook (sibling of `check_negotiation`,
  `session.py:2023`); disarm on user turn / conversation close / negotiation
  arm.
- `harness/runtime.py` — call the hook wherever `check_negotiation` is called
  (rollover `runtime.py:583-585`/`692-694`, firing loop `813-815`); park the
  rollover at the next inactivity deadline (negotiation `next_trigger_t_h`
  pattern, `negotiation_state.py:159`).
- Both entries (`sim/run_async.py`, `experiments/live_companion.py`) share
  AsyncRuntime — one wire, both paths. The spike runs SIM/fake; no launcher
  change (`~/.hermes/scripts/live_telegram.sh` is outside the repo and stays
  untouched).

No schema migration for the spike: arms live in session memory (a bounded sim
run; persistence is a productization decision, see Decisions).

## Armed event mechanics

Per-arm state: `{id, source_proactive_id, armed_at_t_h, deadline_t_h,
fired(bool)}`.

**ARM** — at the end of any proactive outbound (`proactive=True`) in an open
conversation: create one arm; `deadline = sent_t_h + DOUBLE_TEXT_SILENCE_MIN`.
Also armed when a proactive opens a NEW conversation (`opened_by="companion"`,
`session.py:1418-1420`) — same rule. Arming is deterministic: every proactive
arms; there is no arm-probability draw (the draw is on firing, below).

**TRIGGER** — due when `now >= deadline_t_h` and ALL eligibility conditions hold:
- **affection/closeness score for this user ≥ `AFFECTION_MIN`** (new relationship
  state, see below) — a new or cold relationship never double-texts;
- conversation still open (a close disarms, below);
- NO pending negotiation;
- not quiet hours;
- user has not turned since the source proactive (same `_last_user_turn_t_h`
  anchor the negotiation uses).

**FIRE — model decides, not a coin flip.** When the arm comes due and eligible,
the engine does NOT auto-send. It hands the model a decision: "you messaged them
N minutes ago and they've gone quiet — send a gentle follow-up, or leave it?"
The model returns send/stay-quiet and, if send, writes the message. Engine owns
the timing and the eligibility check; the model owns the choice and the words
(the cognition principle — engine times, model decides). At most once per arm
(`fired` bool idempotency, never key presence). The decision + outcome are
recorded (`replay_id`) so replay reproduces exactly. Masked-clean (no engine
numbers), sent proactive outbound, production model. This replaces the earlier
flat `P_DOUBLE_TEXT=0.5` draw entirely.

**DISARM** — a user turn arrives; the conversation closes (any reason); a
negotiation arms (its Decide bomb supersedes ours); quiet-hours deferral
outlives the conversation; the arm fired.

**NO-NAG** — after the double-text the arm is consumed; no second follow-up
ever for the same source proactive. The double-text does NOT arm a new event
(it is not a proactive source).

## Distribution & determinism

- Reserve `INACTIVITY_STREAM = 8` (engine `rng.py:12-15` uses 0-3; `life.py:74`
  LIFE=4; persona=5; `session.py:162` CONVERSATION=6; `session.py:206`
  DECISION=7). Draw ONLY `stream_rng(seed, 8, ...)`, NEVER `day_rng` — the
  replay generator is untouchable (life.py:15-20 discipline; any added draw on
  it desyncs end-of-day updates and `test_replay_matches_run_daily`).
- The **only** RNG draw is the silence-window delay: draw `X` from a
  distribution (e.g. lognormal centered ~8 min) instead of a fixed threshold, so
  the window opens at an organic time. `deadline = sent_t_h + X`.
- **Whether to actually send is a MODEL decision, not a draw** (see FIRE above) —
  this replaces the old flat `P_DOUBLE_TEXT` coin flip. The arm always exists and
  is visible in traces/replay; the model's send/stay-quiet decision is recorded.
- The delay draw consumes no `day_rng`; the parked rollover reproduces exactly
  (park-at-the-event-hour discipline, `runtime.py:546-557`/`622-654`).

## Interaction matrix (each row = a pre-registered expectation)

| Situation | Behavior |
|---|---|
| Negotiation pending (INFORM/DECIDE) | No double-text while the negotiation owns the conversation end (contract; `session.py:1102-1109`). If a negotiation arms after our arm, ours disarms — the Decide bomb supersedes. |
| `closing_tendency`/`max_turns` close before deadline | Conversation closed → disarm; nothing fires. |
| Quiet hours at deadline | Defer like proactive quiet handling (`runtime.py:915` `_quiet_defer_until` pattern); resolve at the first wake after quiet hours, still within the conversation lifetime; skip if the conversation closed meanwhile. |
| User goes away (15 min) / abandoned-chat backstop | The post-proactive window (~8 min) fires before the away threshold. An away user leaves the conversation dormant, not closed (away≠close); any arm still live when a checkpoint or the long backstop finally closes the conversation is disarmed by that close. |
| Proactive opened a new conversation | Armed there too (same rule). No cross-conversation arms. |
| User replies just before deadline | Disarm on the user turn (re-checked at fire time from `_last_user_turn_t_h`). |
| Double-text itself (a companion turn) | NOT a proactive source: it creates no new arm. It MAY trigger the normal `closing_tendency` draw like any companion turn (fact-sheet open question #4) — accepted for the spike, pre-registered. |

## No-goodbye detection

- NEW deterministic pure function `detect_no_goodbye(text) -> bool` in
  `harness/inactivity.py`. English farewell set (product language): bye,
  goodbye, see you, goodnight, night, talk to you later, later, take care,
  bye-bye, have a good … Word-boundary regex, case-insensitive. Evaluated on
  the LAST user turn (last 2 if the final is one-word).
- Applied at every close (`session.py:_close_conversation`,
  `session.py:1158-1181`): close_reason + `no_goodbye` flag. A no-goodbye
  close (`user_left`, `max_turns`, quiet-hours boundary, `closing_tendency`)
  with `farewell=False` → tagged; option (default ON in the spike) to also
  emit a COMPANION_EPISODE (category COMPANION_EPISODE, tag `no_goodbye`) via
  the existing `store.insert_episode` seam (negotiation-episodes pattern,
  contract:133-141) so a later conversation can reference "she left without
  saying goodbye."
- Purely observational in the spike: nothing sends a message about it. (A
  "she noticed you left" close-message is a possible productization — flagged,
  not built.)

## New dependency: an affection / closeness score

The double-text should only be considered once the relationship is warm enough.
We don't currently store this. Today the DB holds *qualitative* relationship
memory only — `relationship_events_json` on session summaries
([store.py:320](../harness/store.py:320)) and `relationship_patterns` facts in
the user model ([store.py:433](../harness/store.py:433)) — but no scalar level.

Proposed (needs its own short design + sign-off):
- **Shape:** one number 0-1 per user, stored in the DB alongside the existing
  relationship memory. NOT in the frozen engine — the engine is her user-agnostic
  internal life; affection is per-user and interaction-driven.
- **Update:** once at conversation close, a recorded judgment (the model, or a
  cheap local judge) rates how the conversation went; the score moves via a slow
  running average (EMA) so it drifts gradually, not per-message.
- **Determinism:** the update is logged (`replay_id`) so replay reproduces it —
  no hidden drifting number.
- **Masking:** never shown as a number; it gates features (this one) and can
  color the behavior brief in buckets, exactly like mood.
- **Open:** what pushes it up/down (length, reciprocity, self-disclosure,
  time-since-last?), decay when ignored, and `AFFECTION_MIN` for this feature.

This is a small separate design; the AFK experiment can run its first pass with
the gate stubbed always-true and adopt the real score once it lands.

## Draft pre-registration (for review → becomes `docs/exp-…` once approved)

**Question:** does an armed post-proactive double-text make Lily's presence
more natural without breaking determinism, and can the system reliably tell an
abandoned conversation from a farewell?

**Conditions:** BASE (current build, no arm) vs ARMED (arm + draw + double-
text). Scripted-user sim runs (fake client) for the deterministic gates;
real-model runs (production model, JUDGE_GENERATOR_TOKEN lane) for the judged
axis. Headline numbers: armed events per N-day sim, double-texts sent,
fired-vs-suppressed counts, $/turn on the research lane.

**Axes:**
1. **Correctness (deterministic):** arm/fire/disarm/no-nag/negotiation-
   supersede/quiet-defer expectations all hold on the scripted fixture set —
   the interaction matrix above, as tests. Replay parity: same-seed runs
   byte-identical with the armed lane active.
2. **Naturalness:** the double-text turn judged vs BASE's post-proactive
   silence (K ≥ 30, independent cross-family judge, bootstrap CI) — the
   presence claim.
3. **Detection:** farewell fixture set classified with zero errors
   (deterministic).

**Gates (pre-committed; reported against, never moved):**
- G-arm: every proactive in an open conversation arms exactly one event; no
  cross-conversation arms; deterministic.
- G-fire: at most one double-text per arm; never during negotiation; never in
  quiet hours; never after close; disarm on user turn; no re-arm from the
  double-text itself.
- G-no-nag: no second follow-up for the same source; `fired` idempotency
  marker holds.
- G-detect: zero misclassifications on the scripted farewell/abandon fixture
  set.
- G-natural: ARMED double-text naturalness Δ ≥ 0 vs BASE, 95% CI excludes 0
  (same bar as WS-B's primary gate).
- G-replay: replay parity holds with the armed lane active.

| Outcome | Decision |
|---|---|
| G-arm+G-fire+G-no-nag pass, G-natural passes | Port the armed double-text as an env-gated product feature (`HARNESS_INACTIVITY=1`); no-goodbye episode emission ON |
| Determinism gates pass, G-natural fails | Do not ship the double-text (presence claim unproven); keep detection + episodes, record |
| Any determinism gate fails | Do not ship the armed event; detection-only remains |
| G-detect fails | Reexamine the fixture set BEFORE any product decision (pre-commit) |

**Cost:** ~1 LLM turn per armed event + judged pairs (K≥30); cheap, research
lane; feeds WS-D spend capture when it lands.

## Decisions to surface

- **Firing is a model decision gated on affection, NOT a probability.** The old
  `P_DOUBLE_TEXT=0.5` flat draw is removed. New dependency: an affection/closeness
  score must exist (see below) before this experiment can gate on it. Until it
  does, the fallback is affection-gate-always-true (every eligible arm offers the
  model the choice) — usable for a first pass, but the real feature waits on the score.
- `DOUBLE_TEXT_SILENCE_MIN` becomes the *center* of the delay distribution
  (~8 min), not a fixed threshold — named/tunable; re-check against
  `USER_AWAY_THRESHOLD_H` (the away/backstop threshold in `harness/tunables.py`).
- Persistence: session-memory arms only for the spike; a new table + migration
  (avoiding WS-D's v8 claim) only if productized.
- Double-text generated by the production model as a normal companion turn
  (research-lane token), not a separate artifact.
- Episode emission on no-goodbye: default ON in the spike.
- Tag-only vs a distinct `close_reason` ("abandoned"): tag-only — no new close
  reason pollutes the lifecycle contract; the flag at close time suffices.

## Open questions

- `closing_tendency` keys off the last COMPANION turn index — a double-text
  companion turn can itself trigger a closing draw. Accepted for the spike
  (its own turn is a natural close opportunity and the arm is consumed), but
  confirm no exemption is wanted.
- Sim path still plans with `scores=None` (`run_async.py:142-150` legacy,
  a7:116-117): the spike inherits it — confirm the armed lane doesn't need
  `day_scores`.

## Out of scope

the away≠close lifecycle work (separate; see
[`../plans/plan-lifecycle-away-checkpoint-2026-08-17.md`](../plans/plan-lifecycle-away-checkpoint-2026-08-17.md)
— this spike only respects the away/backstop threshold); availability-negotiation
changes; the affect codebook; WS-B
bubbling; arm persistence/productization (decision above); the "she noticed
you left" close message (flagged).