# Backlog

Unresolved implementation work only. Experiment records and completed work live
under `results/`; this file is not a project history.

## Open

### Code reduction and WS-D mainline wiring
Planned in [plan-2026-09-06-deletion-and-wsd-wiring.md](plan-2026-09-06-deletion-and-wsd-wiring.md):
pure-removal deletion phases (stray artifacts, dead modules/queries, duplicated
test fakes and migration scaffolding), then switching the session mainline onto
the `build_context_messages` seam behind a byte-parity gate. The plan's
"explicitly out" list enumerates what stays on this backlog.

### Complete the global typed context
The canonical context is intended to be one append-only stream, with lifecycle
markers, system-level events, structured decisions, and provider-native tool
results.

Landed 2026-09-07:

- **Append-only context read.** `Session._context_turns()` reads from a stored
  compaction epoch (`kv_store` key `context.epoch_id`) instead of
  `recent_messages`' 12-row tail, so between boundaries the message list only
  grows. The epoch moves at day rollover only (`_maybe_compact_context`) and
  records a `context_compacted` event. `tests/test_cache_prefix_gate.py`
  asserts the extension property over every provider call in a mixed run and
  is verified against three sabotage cases.
- The model-visible decision lane. `Session._context_turns()` merges
  `messages` with `decision_records` by `(t_h, lane, id)` and renders each
  past verdict as a `role="system"` block, so a decision the model made is
  readable by the model on later turns instead of only by the auditor.
- Pop-up aux calls extend the mainline request instead of carrying their own
  prefix, and their pop-up block rides as `role="system"`.

Still open:

- Lifecycle and day markers as typed events rather than re-rendered card
  sections; day-scoped material (agenda plan, arcs) should be appended once at
  rollover, not rebuilt into the volatile tail every turn.
- Provider-native tool-result serialization for the decision lane (the
  projection currently renders prose, not `tool_calls` + `tool` results).
- Drain outstanding steers before day finalize and conversation close. There
  is no equivalent of Temporal's `all_handlers_finished`: a day can currently
  be finalized with steers still pending.

### Telegram delivery consistency
Add a durable outbox for model-generated Telegram messages. Stage the outbound
intent and message before dispatch, track pending/sent/failed/ambiguous states,
append delivery confirmation to canonical context only after Telegram success,
and support retry/reconciliation without assuming exactly-once delivery.

### Real-time event integration
Finish the wall-clock/agenda integration and verify that event negotiation,
away presence, and proactive landing use the same authoritative time and safe
boundary rules. Preserve virtual-clock replay determinism.

### Partial availability — an event is not a wall
Every event is currently all-or-nothing: the window is open, so she is *in* it,
and the only question a user message raises is `tool_decide_reply`'s
reply/no-reply. But activities differ in how much of her they take. The gym is
close to unreachable; drawing at home is barely an interruption; a class is
unreachable for a fixed block and fine either side of it. The decision lane has
no vocabulary for that, so the model has to infer reachability from the activity
name alone every time it is asked.

This is the same gap as the fine-grained availability windows the negotiation
contract deferred out of v1 — "Fine per-activity availability windows (gym
gaps, class blocks) — the AgendaItem window is the only window in v1" — so the
two want designing together rather than separately. Prior art to read first:
the G0 contract's Explicitly-deferred section, and the spike-4 AFK/presence
design note, which already separates the negotiation's decide bomb from the
away/presence signal and warns against collapsing thresholds that measure the
same silence. Note both documents are currently deleted in the working tree and
live only in git history.

Open questions: does reachability belong on the AgendaItem (a per-activity
attribute the generator sets), on the interest/arc it came from, or is it a
judgement the model makes from the pop-up each time? Does it change what
`tool_decide_reply` is asked, or only what the state card says she is doing?

### Context budget and compaction
Define a token-budget trigger and a compaction format that keeps the stable
prefix and recent raw continuity while summarizing only older material. Record
what was compacted so replay and audit remain explainable.

### Relationship state for inactivity behavior
Define the per-user affection/closeness score and its recorded update rule before
using it to gate double-text or related presence behavior. Keep the score out of
the frozen engine and mask it from prompts like other internal state.

### Decision lane — fixed 2026-09-08
A live evening discarded 20 of 25 aux model calls and left a user's goodbye
unanswered. Four causes, all fixed, all with guards in
`tests/test_decision_transport_2026_09_08.py` (six sabotage cases verified):

- **Only the requested schema is offered.** All three went out before, so an
  event pop-up could come back as `tool_decide_reply`. Measured against the
  live gateway: 1-in-3 wrong tool with three offered, 0-in-3 with one.
- **`tool_choice` stays "auto" — it cannot be narrowed on this model.**
  `deepseek-v4-flash` is always in thinking mode (`reasoning_effort` accepts
  only low..max; omitting it still reports thinking) and thinking mode 400s
  both `"required"` and a named function: *"Thinking mode does not support
  this tool_choice"*. A named choice would break every decision call. Do not
  reintroduce one without re-testing the gateway.
- **Retry budget** (`steering.MAX_ATTEMPTS = 3`, schema v11
  `steering_queue.attempts`). A failed decision was requeued unbounded and
  re-asked every turn, one call each, growing as more accumulated (4, 4, 5,
  7 across four consecutive turns). Exhausted steers are `abandoned`, a
  terminal status so the gap stays explainable.
- **Decisions replay as a native tool exchange** (assistant `tool_calls` +
  matching `role="tool"` result) instead of a prose summary. The prose form
  recorded the decision but left no evidence in history that tool calls
  happen here, so every pop-up was a first-ever ask appended after real
  dialogue. `assembler.wire_message` carries the tool keys through — copying
  role/content only stranded the pair, which providers reject.
- **A go/initiate verdict drives a real generated turn.** It used to paste
  the verdict's `reason` into the channel as her words (third-person machine
  rationale: *"...I'll send a warm in-character send-off and keep cooking"*)
  and close the conversation BEFORE generating, so the reply was suppressed
  and a goodbye got silence. The turn now speaks (`GO_NOTE`/`START_NOTE` on
  the tail) and the close is deferred until after the reply is persisted.
  **`reason` is unchanged and still recorded** in `decision_records` — it is
  the engine's audit trail; it just stopped being dialogue.

Verified end to end on the real provider: 0 parse failures, 0 pending steers,
a generated goodbye plus the inform mention.

Still open:

- A prose reply with no tool call is BOUNDED, not prevented — the provider
  will not let us require a tool. If it recurs often, the next lever is
  putting the aux pop-up back as the last `user` message (0 failures in 27
  decisions before the role change) rather than a trailing system block.
- **Outcome capture is polluted.** Any end-boundary verdict reason is stored
  as `agenda_items.outcome`, so a decline rationale ("Nothing to initiate;
  I'm mid-storyboard") became what came of morning coffee — and that feeds
  the day planner. Only `abandon`/`follow` reasons are actually about the
  item.
- **A fresh evening start replays the whole day.** A DB born at 18:40
  enqueued pop-ups for 06:58, 11:00 and 17:00 at once. Restart recovery and
  first-boot need different rules.
- **Aux calls are absent from `llm_calls`**, so spend accounting missed ~25
  calls and could not show the leak.

### Onboarding and event content
Landed 2026-09-07 (the setup side):

- `/setup` works on the live runtime. `AsyncRuntime` now wires
  `request_setup`, so the command has a reachable success path; before this
  it refused either as "already initialized" or with a `--defer-bootstrap`
  message naming a flag only `sim/run_async.py` had. `live_companion.py`
  gained `--defer-bootstrap` so a blank DB can be onboarded from the channel.
- Identity is persisted. Schema v9 added `user_profile` and
  `interest_relations`; `BootstrapStore` had declared
  `load_user_profile`/`save_user_profile` for months with no table behind
  them, so the profile was re-derived from the environment every start.
- The product default identity is no longer the ablation fixture.
  `live_companion.owner_profile()` fell back to
  `cvs_common.GATE2_USER_INTERESTS` with the owner env vars unset, so a real
  trial built its whole 40/40/20 portfolio around an experiment's example
  user. `bootstrap.DEFAULT_USER_INTERESTS` is now a separate product list.
- `harness/interest_extension.py` places off-catalog user interests into the
  graph with one model call at onboarding, persisted and never repeated on a
  warm start. Buckets stay structural — the model proposes names, distances
  decide membership. Measured on the real provider: adjacency pool 8 -> 33.
  Offline and error paths fall back to a heuristic extension.

Landed 2026-09-07 (the event-content side):

- **`harness/day_planner.py`** — one model call at day rollover turns arcs,
  interests and recorded outcomes into concrete activities with real objects.
  The engine keeps selection, windows, salience and ids; the planner supplies
  activity TEXT only, applied after every RNG draw, so the seeded schedule is
  byte-identical with and without it. Opt-in (`HARNESS_DAY_PLANNER`,
  `--day-planner`) because it shares the conversation client. Never called on
  replay: a day is only generated when the store has no agenda for it, and
  the planned text persists with the items.
- **`agenda_items.outcome`** (schema v10) — what actually came of an item,
  written only from a decide_event verdict's reason. A window merely elapsing
  records nothing: the planner's continuity has to be true or the next day
  follows on from a fiction.
- **`life.BUCKET_WEIGHT`** — interest draws are now
  `salience * bucket_weight`, so the portfolio's independent slice reaches
  the day. Measured over 300 days on a low-salience independent interest:
  12.5% of interest items unweighted, 18.3% weighted. The planner also
  receives HERS/SHARED per interest, so a thing she does alone reads
  differently from a thing they share.

Verified on the real provider across two consecutive days: day 1 followed on
from day 0's thread and answered a recorded outcome
("throw the fourth stoneware bottle, widening its foot to stop collapse").
Six sabotage cases confirmed the guards catch their regressions.

Still open:

- **Long generations are slow on this gateway, and the budget's tail is not
  covered.** Measured 2026-09-07: a short prompt returns in 1.2-3.4s (three
  consecutive raw calls, all HTTP 200 — it is not rate limiting), while the
  extension's ~400-character JSON took 20s, 41s and twice overran 90s. The
  fallback fired correctly both times, but a cold start then samples the
  persona against the bare catalog, which is the exact collapse the extension
  exists to prevent. Fix by splitting the request per interest — three ~130
  character outputs instead of one ~400 — so each call sits well inside
  budget and partial success still helps. Same applies to the day planner.
- Start times are still whole hours (`rng.integers(9, 21)`), so the minute
  field takes three values across a fortnight while END times are random
  floats — precise endings, robotic beginnings.
- Outcomes are only captured at decide_event boundaries, so most items still
  resolve to a status with no outcome. Broadening that (without inventing
  anything) is what would make arcs accumulate properly.
- `art`, `food`, `outdoors` and `literature` are category hubs rather than
  pursuits, so the FALLBACK templates still render "practice food". Harmless
  while the planner runs; it is the fallback that reads wrong.

### Prompt channels disabled 2026-09-07 (re-enable or delete)
Three prompt channels were turned off in the content pass. Each is still wired
end to end so re-enabling is a one-line change; each needs a decision before it
comes back or gets deleted outright.

- **Closing guidance** (`actuation.CLOSING_GUIDANCE_ENABLED`). The per-turn
  continuation policy told the model how to end every reply, from a per-turn
  draw, and the behavior it actuates is itself unspecified (see below). The
  band prose is retained in `actuation._CLOSING_BANDS` and its mapping still
  has a test behind the flag. `closing_tendency` is untouched: it still drives
  the conversation-close draw, it just no longer speaks.
- **Steer trust rule** (`prompts.STEER_TRUST_RULE`, now `""`). It rode the
  stable prefix on every turn to explain a marker that already names itself,
  for an event that arrives a few times a day. Trust now comes from the
  channel: steer blocks render as `role="system"`. If a future transport
  drops role fidelity, this needs a replacement rather than a revert.
- **Mood-brief label** (`prompts.MOOD_BRIEF_HEADER`, no longer rendered). It
  prefixed a brief that opens with "Current bearing:", inside a section headed
  AFFECTIVE BEARING. Kept as a constant so audit tooling reading old rows
  still resolves it.

Also removed, with no flag: the per-turn brief's trailing "Do not name or
explain the internal state" sentence, which restated a stable-core rule on
every turn in a second voice.

### Closing behavior
Keep `closing_tendency` disabled until its behavior is specified and tested as a
natural conversation boundary rather than an arbitrary per-turn cutoff. Any
replacement must preserve replay parity and the away-is-not-close lifecycle.
