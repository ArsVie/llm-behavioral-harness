# Context, event, and time gaps

Status: current gap register; design decisions are settled unless marked open.
Last reconciled: 2026-09-07.

The live review exposed two related failures: virtual time was not grounded in a
usable wall-clock representation, and the model saw an under-structured context.
The latter is the central issue for internal events and Telegram messages.

## 1. Time and agenda

Observed: the live prompt could show a past morning agenda item without a current
time line, so the model treated it as present. Agenda rows also remained
`planned` after their windows passed.

Target:

- Derive real time from the authoritative anchor and expose current local time,
  day, and part of day in the state card.
- Render agenda items as past, current, or later; update their status as windows
  pass.
- Preserve virtual `t_h` semantics and replay determinism. Real timestamps may
  be additive audit fields, but the storage-vs-derive choice remains open.

Status: implementation and live wiring need verification.

## 2. Lifecycle and proactive turns

Decision: away is not close. A short silence marks presence/away; the same
conversation continues on return. A checkpoint or long-abandonment backstop may
close it, but closing is housekeeping, not character reset.

Target: a proactive event during an active conversation is handled in that
conversation; after a true close it gets an explicit new boundary. No context is
rebuilt from a summary unless the token window requires compaction.

Status: lifecycle constants and checkpoint behavior exist; integration with
proactive landing and event negotiation remains incomplete.

## 3. One context, typed internal events

Decision: use one global append-only model context with explicit conversation,
day, and lifecycle markers. Cross-conversation influence is intentional.

Target context shape:

```text
stable system/persona/tools prefix
→ day/lifecycle markers and state
→ ordinary turns and typed internal events
→ structured decision/tool results
→ current user turn or proactive hook
```

Internal events are system-level inputs. Decisions are structured tool calls with
a decision and `reason`. Internal material is visible to the model but not
necessarily to Telegram. An inactive conversation still runs and persists its
decision without creating a visible Telegram turn.

Ordering is also settled: a new event never interrupts running work; it steers
against it and waits for the next safe boundary. Agenda events resolve in order.
The stable prefix remains byte-identical and internal material belongs at the
volatile tail.

Current mismatches:

- Steer injections and pop-up blocks both render as `role="system"`
  (2026-09-07).
- Proactive fires are decided via `tool_decide_proactive` at the idle boundary,
  and past decisions are now projected back into later turns' context by
  `Session._context_turns()` — as prose blocks, not yet as provider-native
  tool calls and results.
- Day-scoped material is still re-rendered into the volatile tail every turn
  rather than appended once at rollover.
- The inactivity tick and final provider serialization are not complete.

## 4. Telegram delivery consistency

Current flow persists the assistant response, then sends it to Telegram. A
network error, timeout, or rate limit can therefore leave canonical history
claiming that Lily spoke when Telegram received nothing. Failures are logged, but
there is no durable pending-delivery state or reconciliation worker.

Required design:

1. Atomically stage the outbound intent/message and a pending outbox record.
2. Dispatch the pending record to Telegram.
3. On success, store Telegram's external message id and append delivery-confirmed
   state to the canonical context.
4. On failure, retain retryable state and append the failure outcome.
5. Treat ambiguous timeouts as reconciliation cases; do not claim exactly-once
   delivery without provider support.

Status: backlog item only; not implemented.

## 5. Event content

The event generator still uses a small template pool such as “try a small
{interest} exercise.” Keep the event as an internal hook and let the decision/
conversation turn elaborate it first; a richer activity library or daily LLM
planning pass can be evaluated later.

## Cache mechanics (checked against the provider docs, 2026-09-07)

Settled facts, so the next design argument starts from them:

- Matching is strictly prefix-based from token 0. A partial match in the middle
  of the input never hits.
- The storage unit is 64 tokens; content shorter than that is not cached at
  all. Hit rates only become reliable once the shared prefix is roughly 1024
  tokens.
- Cache units are cut at request boundaries and, for long inputs, at fixed
  token intervals. Entries clear on their own after hours to days, and hits
  are best-effort, never guaranteed.
- Hits are billed at roughly a tenth of a miss, and cut first-token latency
  substantially on long prompts.

Consequence for this harness: the ~180-token stable prefix is far below the
reliable-hit threshold on its own, so the shared prefix has to include the
transcript. That is why the context read is anchored to a compaction epoch
rather than a rolling tail (landed 2026-09-07) — slimming the prefix without
that change would have made caching worse, not better.

Two citations that did NOT survive checking, recorded so they are not repeated:
StreamingLLM's attention sinks are a serving-side KV-eviction technique and say
nothing about how an API client should order a request; and SCXML has no
automatic deferral — an event matching no transition is discarded, so deferral
must be an explicit re-queue, which is what `requeue_steer` already does.

## Open decisions

- Persist resolved real timestamps, or derive them from `t_h` and the anchor.
- Final provider wire format for system events and structured tool results.
- Whether defer means the next turn, a server-controlled boundary, or another
  explicit event boundary. Note `map_defer_n` currently derives the turn count
  by regex over the model's prose; a closed verdict enum mapping to guard sets
  would remove the guessing.
- Retention/pruning of tool output, events, and state; DeepSeek cache duration.
- Pydantic versus existing dataclasses plus explicit validators.

Engine distributions remain out of scope. Any live deployment is a separate,
explicit decision.

See [architecture-overview.md](architecture-overview.md) for the compact system
reference and [internal/BACKLOG.md](internal/BACKLOG.md) for pending work.
