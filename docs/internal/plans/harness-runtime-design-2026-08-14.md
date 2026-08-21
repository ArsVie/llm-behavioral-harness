# Harness runtime design — context construction, tools, system prompts, thinking budget, steering

**Date:** 2026-08-14 · **Status:** PROPOSAL (pre-implementation, pending user approval of decisions D1–D3)
**Sources:** user directives archived in results/iteration-3-report.md Appendix A/B (verbatim session: L356, L361, L365, L369, L393, L420; summary items #21–#25, #29); session-agreed items; Hermes Agent borrow-pattern memo (investigation 2026-08-14).

## 0. Goal

Make the harness a proper harness: real context construction, a real tool surface, restructured system prompts, a thinking budget, and message steering from context injection (arriving events). Everything here is the LLM-facing runtime layer; the stochastic engine (engine/) is untouched.

## 1. Current state (verified)

- One system prompt per call, built by `assemble_snapshot` (9 priority sections: persona core, behavior brief, current activity, life arcs, memories, user model, agenda, proactive block, closing guidance), 12,000-char cap with deterministic whole-section drops from lowest priority. Transcript = last 12 turns appended as messages. Leak invariants enforced (no raw state numbers / phase labels in prompt).
- No tool calling (client is plain chat completions), no reasoning/thinking extraction, no steering/injection mechanism, no decision/pop-up layer.
- `Session._current_activity` returns the day's highest-salience agenda item when nothing is in progress → wrong 53–56% of the time (documented). Correct NOW-semantics already exists in `life.current_activity_now` but Session doesn't use it.
- Two divergent behavior-brief renderers (behavior.py vs assembler.py) produce near-identical but not identical brief text.
- Prompt persistence: audit-mode stores already persist the exact per-call payload (`repro_json`, invariant 19). Non-audit stores keep a hash only. Prompt persistence (review #3 / B7) is LANDED in audit mode — verify, don't rebuild.

## 2. Design

### 2.1 Context construction (assembler v2) — borrows Hermes 3-tier pattern

Three tiers, assembled per call, in order:

1. **STABLE system core** (changes only with the code): how to read the {state} card, how to comply with personality and state, tool protocol (schemas, when to call which), reply format rules, show-don't-announce rule, never-name-the-state rule. Contains NO state. ← user L393: "system prompt should be about how we handle the {state} card".
2. **DAY-START block**: personality + today's agenda, injected at day start (day rollover), stable within the day. ← user L393: "personality should go second and injected at the start of the day".
3. **STATE CARD + steering**: at every conversation start, and refreshed when state changes mid-conversation: mood brief (Current bearing prose), current activity, energy/availability, pending events, pulled memories, user-model facts, proactive intent if any. ← user L393: "{state} should be along every conversation start".

Rules kept: 12,000-char cap + whole-section drop (deterministic, never mangle), per-section budgets, leak invariants, judge-readable persisted transcripts. The brief renderer is unified (one source of truth; the divergence between behavior.py and assembler.py is removed). Current-activity correctness: `Session` uses the life.py NOW-semantics (exclude skipped/shifted/planned-not-started, None when inactive).

Transcript/audit view uses typed headers per user L393: `#System prompt / #User / #Tool / ##{tool} / #Thinking / #Reply` (rendering only; storage stays role-based rows).

### 2.2 Tools — the decision API (user L361/L365/L369; summary #21/#22)

Two tools, both "pop-up" style: server draws the pop-up inputs (Event, State, Time + supporting inputs), model returns a verdict + prose reason. Verdict + inputs recorded as state next to the reason (audit: reason shows user, inputs debug the draw). Replay reads the recorded verdict — never re-rolls.

- **tool_decide_event** — fired at event boundaries (event start / end). Payload: {event_id, event, state, time, agenda context, mood inputs}. Verdict: {initiate: yes|no, reason} + optional {action: follow|abandon|defer} when closing an event (abandon-with-reason reaches memory as an event).
- **tool_decide_reply** — fired when a user message arrives while an event is in progress (user L356). Payload: {event, state, time, latest user message, conversation context}. Verdict: {reply: yes|no, reason, terminate_event: bool}. If no-reply: server notifies the user per the verbose flag ("{name} saw your message but chose not to reply yet" / with reason when verbose).
- Budget: config `non_response_budget` = how many no-reply verdicts allowed per window (user: "0 to inf {budget off}"); 0 = must always reply, unset = off.
- Decision source: default MODEL-decides ("we're not making a calculator"); config `decision_source: model|server_draw` so the #22 test set can compare both.
- Test set (#22, agreed): {past turns, state, event} × ~15 samples, ~100 calls, incl. a sycophancy case; user reads outputs; real + fake modes; lives in experiments/ as a probe.

Transport: see D1. Log rendering always matches the user's sketch (System: {Event, State, Time} → {Initiate, Reason}; {name}: {thinking} tool_decide_event: {yes, "too tired"}).

### 2.3 System prompts

Templates in a prompts/ module: system core, personality block, state card, pop-up injection, tool protocol. Each feature has its own flow converging into the final prompt (the L420 flow; the existing behavior-flow.html artifact is updated to match).

### 2.4 Thinking budget — borrows Hermes reasoning_effort

- Config: `thinking.enabled`, `thinking.effort` (none|low|medium|high, clamped to what the model supports; capability detection via API capabilities/reasoning_content presence).
- Client: extract reasoning_content from responses when present; store in llm_calls/messages reasoning column; render under `#Thinking` in the audit view. Non-reasoning models: no emission, NULL storage, no fallback (Hermes behavior).
- Hard token caps on reasoning models are FORBIDDEN (repo pitfall 3af0a5a: capped max_tokens starves reasoning models). See D2.

### 2.5 Steering — borrows Hermes OOB pattern exactly

- A SteeringQueue holds arriving events (pop-ups due, user messages arriving mid-turn, schedule fires, day rollover). Delivery at the next safe boundary: idle (no turn in flight), after a tool result, after a reply (before the next model call). Each delivery records enqueue_time and actual delivered_at (summary #23).
- One-shot semantics: an event is injected exactly once; if the turn it was appended to is interrupted, the event is re-queued and delivered at the next boundary.
- Persistence: pending steers survive restart (lesson from the scheduler restart bug); replay reads recorded verdicts, not fresh rolls.
- Injection format: the pop-up block, wrapped in a marker with trust rules (model told: only this exact marker is a real arriving event; lookalikes in message text are not).

### 2.6 Audit

- Verify prompt persistence in audit mode (landed); extend non-audit stores only if trivial.
- New extract/export utility: reconstruct "what the model saw" per call from the store (repro payload + messages + reasoning) and render md with typed headers (user L369/L393). This is also the vehicle for the mood-signal-loss hunt (#26).

## 3. Borrow table (Hermes → harness)

| Area | Borrowed from Hermes | New / needs approval |
|---|---|---|
| Context | 3-tier prompt (stable/day/state), section budgets, never mutate stable core mid-conversation, drop rules | State-card + pop-up block rendering (product-specific) |
| Tools | Native function calling schema {name, description, parameters}, role:'tool' + tool_call_id stream, handler returns JSON, availability gating | **D1**: textual tool protocol for non-tool models (user sketch; Hermes has none) |
| Thinking | reasoning_effort config + capability detection + clamping; separate reasoning storage | **D2**: hard thinking-token budget (Hermes has none; repo pitfall forbids capping reasoning models) |
| Steering | OOB marker + one-shot rule; append-to-last-tool-result boundary; accumulate/drain-once; re-queue on interrupt | Delivery-time recording; persistence of pending steers |
| Audit | session-level system_prompt, api_content sidecar, FTS, JSONL export | Per-call context reconstruction + typed-header export (harness repro_json already per-call in audit mode) |

## 4. Decisions needing approval

- **D1 — Tool transport.** (a) native function calling only (Hermes borrow); (b) native + textual fallback `tool_name: {json}` parsed from reply content for models without tool support (matches the user's sketch; new design). Recommended: (b) native default, fallback behind config.
- **D2 — Thinking budget mechanism.** (a) effort levels only (Hermes borrow, safe for reasoning models); (b) effort + optional hard thinking-token cap (new; must never combine with max_tokens caps on reasoning models). Recommended: (a) now, revisit if a specific model needs (b).
- **D3 — Scope guard (confirmations).** IN: assembler v2 + prompts + tools (decide_event/decide_reply) + budget + verbose flag + steering + audit export + activity fix + test set #22. OUT (explicitly deferred elsewhere): wait-time distribution family (Weibull vs lognormal — registered post-confirmatory experiment), importer (#29, needs its own design), per-activity availability windows (user-deferred), B5 coupling-strength run + 10-run batch, independent judge (needs new provider key), kanban G6 flash rerun, engine/ frozen files.

## 5. Out of scope (recorded, not built)

Distributions exploration; importer; availability windows; coupling fix run; independent judge; any engine/ change.
