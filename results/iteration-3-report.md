# Iteration-3 — perceptual validity: confirmatory report

> **CORRECTION ADDENDUM — 2026-08-13 (probe artifact):** "the flash route
> died (episode 2026-08-10)" (headline below and §17.4) was a MEASUREMENT
> ARTIFACT from 2026-08-11 onward. The route probe in
> `experiments/cvs_g6.py::_family_route_ok` capped `max_tokens` at 10;
> deepseek-v4-flash is a REASONING model whose reasoning consumes the token
> budget before content is emitted, so the probe always saw HTTP 200 with
> empty content and declared the route dead. Verified 2026-08-13: judge-
> shaped calls (no cap) on flash return valid pairwise JSON; luna works
> under identical concurrency; zero 429/5xx anywhere (not rate limits).
> The probe is fixed (no `max_tokens`) and the judge client timeout raised
> to 120s; re-running G6 with the flash family (true second family, closing
> the §17.4 INCONCLUSIVE leg) is queued as kanban backlog. The original
> 08-10 G3 empties may still have been real (actuated caps 290-669 can be
> exhausted by reasoning); the "dead since then" status was the artifact.

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

## Appendix — User directives (Iteration 3)

Verbatim user directives extracted from the Claude Code session transcript for this
iteration (42 user messages). [PROPIO] = passage dictated by the user; [PEGADO] = text the
user pasted into a message (reviewer/agent output or attached quote). Line indices (L..)
refer to the source JSONL transcript. Source: /tmp/arnes-user-messages.txt (copy on the
user's Desktop: `C:\Users\vruizes\Desktop\arnes-user-messages.txt`).

### A. PROMPT-CONSTRUCTION / CONTEXT-FILLING / DISTRIBUTION

- [PROPIO] **L5** — original spec (ES): the day's schedule is generated from the system prompt and INJECTED into context; the motive of each message comes from what was injected:

  > Cronograma. Basado en el system prompt y la configuración anterior, se generara un cronograma de actividades a realizar durante el dia, inyectado en el contexto de la conversacion al inicio de este. Este cronograma no necesariamente tiene que ser seguido al pie de la letra y tampoco impide al acompañante responder al usuario.
  >
  > Iniciativa de conversación. Al inicio de cada conversación, ya sea iniciado por el acompañante o el usuario, tanto el cronograma como los gustos deben de ser inyectados en contexto para que el acompañante elija el motivo de su mensaje (porque se comunica con el usuario) o sus actividades cuando el usuario se comunica.

- [PEGADO en L5] — reviewer text the user attached to audit the code against it (assembler / minimal projection / prompt as ONE actuator among several / personality distributions):

  > The LLM doesn't need all raw state. The assembler decides the minimal projection necessary for the current call.
  > ...
  > Then the prompt becomes one actuator among several, rather than the entire system.
  > I would, however, make those ratios distribution targets rather than an immutable law. For example, different generated personalities might draw p i ∼ Dirichlet(α) around a population mean near 40/40/20... That preserves the product rule while preventing companions from having suspiciously identical psychological construction.

- [PROPIO] **L334** — message-count distribution must depend on mood:

  > Also do any of the fixes needed to make her mood affect how many messages she sends you take another run?

- [PROPIO] **L369** — wants to see the EXACT EXTRACT of what the model sees before its replies (system prompt → context injection → tools → thinking → reply) before believing state moves nothing; suspects the system prompt is stale or absent:

  > I want to see the exact extract of what the model is seeing before its replies before deciding if state doens't do anything. Do we have full logs from the models? from initial system prompt to context injection to tools to thinking to reply? Also, we might need to tweak the system prompt, I have yet to read it but I'll assume it's stale or theres none

- [PROPIO] **L369** — pop-up decision structure to register in logs; injections act as immediate steering (as soon as the agent is free — idle, finished tool call or reply):

  > The pop up should end up registered in the logs as something like
  > System: {Event: gym, State: start, Time: {time}}
  > {Initiate:{yes, no} , Reason: " " }
  > {name}: {thinking} tool_decide_event: {yes, "too tired"}
  > Not verbatism, but the idea is there. Also, I should not mention it, but injections like these should function like steering, they go inmediately as soon as the agent is free e.g. if idle, finished tool call or reply

- [PROPIO] **L393** — the {state} card: system prompt must NOT contain the state; it should be about HOW WE HANDLE the {state} card; personality injected at the start of the day; {state} at every conversation start:

  > Is this sytem prompt or injection? If it's system prompt that's bad, system prompt should be about how we handle the {state} card and how to comply with personality and state, personality should go second and injected at the start of the day, {state} should be along every conversation start. or something like this.

- [PROPIO] **L393** — do NOT send everything in one block of text; arrange the extract like a harness, with headers collapsing each part by type (#System prompt / #User / #Tool / #Thinking / #Reply):

  > Are all conversations like this? Extract a couple of full conversation in a md. Use headers to properly collapse parts of the reply by type
  > #System prompt . . . #User #Tool ##{Tool related stuff} #Thinking #Reply
  > Something like that

- [PROPIO] **L420** — detailed flow of how the system prompt is populated; every feature with its own flow converging into the system prompt (base of the assembler diagram); html artifact, pastel, editable, hover pop-ups per variable, appendix:

  > I want a detailed flow of how system prompt is populated, every feature it's own flow that converges into the system prompt same for generators. A text based version and a big mermaid version one. ... add an apendix (hidden from main view) with every variable you mention explained and linked to the code, variables should have a small pop up that explains them once hovered. This should be an html, make it aesthetic (clear pastel), easy to read and mainly to modify...

- [PROPIO] **L682** — correction on which graphics to review:

  > Not the diagrams, the distributions

### B. AVAILABILITY WINDOWS

- [PROPIO] **L5** — daily activity schedule (base of the availability system); does not block replying to the user (cronograma quote above, section A).

- [PROPIO] **L356** — events firing mid-conversation: depends on conversation state; flags to record whether the agent engaged with the activity; server-side resolution (not LLM-induced); reply in context ("I'm in class, what do you want") or not reply (with a server-side notice); option to terminate the event:

  > ...if the user messages it mid event it should know and reply in context, e.g. "I'm in class, what do you want" if mood bad-neutral or directly not replying (though served should notify the user that the agent decided to not reply), maybe even the option to terminate the event and follow through with user intent if context requires it.

- [PROPIO] **L361** — {Event, State} {Initiate, Reason} pop-up; configurable verbose flag (server tells the user she saw the message but chose not to reply, optionally with the reason); punishment budget 0..inf (budget off); test set {past turns}{state}{event}:

  > This should basically be a pop up for the agent, something like {Event: gym, State: start} {Initiate:{yes, no} , Reason: " " }
  > We can have a setting for the harness that is basically a verbose flag and with it off, server sends user, "{name} saw your message but choose not to reply yet}" or something like that and with it on it says "{name} not replying, reason: {Reason}"
  > This can be a setting, user can set how much punishement they can handle, from 0 to inf {budget off}
  > Agree, but we can test how it behaves based on what we already have, we can set make a test set with {past turns} {state} {event} or something like that and see what it replies in 15 or so attempts.

- [PROPIO] **L365** — decision NOT server-side: the model decides from feelings, "we're not making a calculator" (but test both); budget = how many times she may not respond; AVAILABILITY WINDOWS ARE A LATER ITERATION — per-activity-type windows (gym 3–5 min for sets, class 0.5–1h, work set by the model at day start per personality from full to none granularly, home full, transport none/full depending on driving); for now full availability during activities is fine as long as the model knows it should have started and decided:

  > I don't want the decision to be server side tuning, I want the model to decide based on its feelings, we're not making a calculator. But testing does't cost anything, we should test both.
  > For the budget I meant buget of "how many times can she not respond a message", ... a later iteration could add "availability windows" during activities depending on the type, e.g. Gym has 3-5 minutes windows for sets, class/school is either 0.5 or 1 hour windows, work is set by the model at the start of the day depending on its personality, from full to none, granularly (maybe even a stocastic window for some) , relaxing at home is full availability and transportation is either none or full depending of driving or not, things like that, for now I think full availability during activities is fine as long as the model knows it should have started and decided.

- [PEGADO en L361] — attached review text the user responded to ("Is it really a defect?", "Agree..."); frames availability as a state machine with persistence, budget, and reason recorded as state:

  > Don't let the model decide availability. Draw it, pass the verdict in.
  > Tune the scarcity. A companion who's constantly unavailable is worse than one who's always there. This needs a budget, not just a rule.

- [PROPIO] **L710** — slider/math/visualization demands tied to the availability/behavior artifact: sliders for every variable that affects her, latex + common math notation, epsilon instead of "a small random nudge", hover pop-ups per variable, appendix in both artifacts:

  > Also, add sliders for every variable that affects her, I want you to use latex and common math notation, what do you mean "a small random nudge" use epsilon, for both artifacts, an appendix, and it should have popups above the variable inside the main tabs that show when you hover the term or variable. That was the original idea for the original artifact, use the variable names but add a pop up that explains what that variable outputs and its meaning

### C. Archive note

Full verbatim extraction of all 42 user messages (1,473 lines, with JSONL line indices and
[PROPIO]/[PEGADO] curation) archived at `/tmp/arnes-user-messages.txt`; copy saved to the
user's Desktop as `llm-harness-user-messages-2026-08-11.md`.
