# Companion harness — architecture overview

Living reference for the system's purpose, boundaries, and current context contract.
Point-in-time measurements and experiment results belong in `results/`.
Last reconciled: 2026-09-07.

## Purpose

The harness wraps an OpenAI-compatible LLM and delivers Lily through Telegram. A
seeded stochastic engine supplies latent variation—mood, energy, cycle, daily
activity, and contact timing. The LLM renders that state into language; it never
receives the raw engine values. Every stochastic and LLM step is recorded for
replay and evaluation.

## Invariants

- The engine is frozen, seeded, and deterministic on replay.
- Persona is configuration, not engine code.
- The model receives behavioral guidance, not raw state labels or numbers.
- Product and research runs use the same recorded/replayable behavior.
- Running work is never interrupted. New events wait for a safe boundary and
  agenda events resolve in order.

## Context contract

The model's canonical context is one global, append-only stream. It contains
ordinary conversation data plus explicit markers for conversations, days, and
other lifecycle divisions. Cross-conversation influence is intentional.

The internal lane is separate by responsibility and Telegram visibility, not by
context:

- Events are system-level inputs, never user messages.
- Decisions are structured tool calls containing the decision and its `reason`.
- Steers, decisions, reasoning, and tool results are saved in the same main
  context the conversation model reads. Past decisions are projected back into
  later turns as system-role blocks, so the model can read what it decided.
- Internal decisions during inactivity remain in context but create no visible
  Telegram turn.
- The stable system/persona/tools prefix stays byte-identical; new internal
  material is appended to the volatile tail for structural prompt caching.

A normal turn is therefore:

```text
engine/state → behavior guidance → context assembly → model/tool call
                                      ↑                    ↓
                         memory, events, timing ← persisted result
```

Events arriving during a call are queued. At the next safe boundary—idle start,
after a tool result, or after a reply—the event is delivered against the running
context. It must be serialized as system/event input, not as synthetic user text.

## Onboarding

A cold start resolves the owner's identity (stored profile > supplied >
config > product defaults), places any interests the built-in catalog lacks
into the interest graph with one model call, samples the companion's 40/40/20
portfolio structurally against that graph, seeds life arcs, and generates the
first agenda. Identity and graph are both persisted, so a resumed run samples
against the same graph the persona was built on and never calls the model
again.

Bucket membership is always computed from graph distance. The model proposes
interest NAMES and edges; it never assigns a bucket, which is what keeps the
40/40/20 mix true by construction. With no client — or on any provider error
or unparseable reply — onboarding falls back to a heuristic extension and
completes offline.

`/setup` runs this same chain from the channel on a blank database. Start the
launcher with `--defer-bootstrap` (and `--enable-commands`) to leave a fresh
DB uninitialized for it; otherwise identity is created at startup and `/setup`
correctly refuses.

## Main components

- `engine/`: seeded mood, cycle, circadian, and contact-timing processes.
- `behavior.py`: maps engine output to continuous behavioral channels and prose
  guidance.
- `assembler.py`: builds the stable prefix, day block, state card, and transcript.
- `bootstrap.py`, `interest_extension.py`, `persona.py`: onboarding identity,
  interest graph, and the structural 40/40/20 sampler.
- `life.py`, `day_planner.py` and memory tables: agenda, activities, arcs,
  and memory promotion. The engine owns selection and scheduling; the
  planner names what she is actually doing, once per day.
- `session.py`, `steering.py`, `tools.py`: event decisions, structured tools,
  safe-boundary steering, and replay records.
- `runtime.py`: inbound messages, proactive scheduling, lifecycle, locking, and
  real-time anchoring.
- `channels/` and `client.py`: Telegram/CLI/web delivery and LLM transport. `channels/multi.py` puts the web chat (`channels/web.py`, 127.0.0.1:8791, on with `HARNESS_WEB_CHAT=1`) next to Telegram: one session and one history; a reply goes back where the message came from, proactive messages go to Telegram.

## External delivery contract

Telegram is an external side effect and cannot share a transaction with SQLite.
The current implementation still persists the assistant response before sending
it and logs failures; it does not yet provide a durable outbox, confirmation
entry, retry state, or reconciliation for ambiguous requests.

Target behavior is durable at-least-once delivery:

```text
model decision → durable pending outbox row → Telegram dispatch
             → success: external id + delivery-confirmed context event
             → failure/timeout: retained retryable state + reconciliation
```

Exactly-once delivery is not assumed. See the backlog entry for the outbox work.

## Prompt layout

The stable prefix leads with the persona, then the card-handling rules and the
tool protocol. Everything per-turn is a row in the stream after it: the state
card is stored as a system row when it changes (a smaller update row when it
can be), so it sits in the transcript at the point it changed rather than
trailing it. Model-visible time is always a 24-hour wall clock (`clock.hhmm`);
absolute virtual hours are an engine coordinate and never reach the prompt.

```text
system:  persona → card rules + tool protocol      (stable, cached)
messages: transcript turns, state-card rows and past decisions (append-only)
          → pop-up block, when one is being decided (saved with its
            decision, replayed ahead of the tool call)
```

Deliberately absent from the prefix, each recorded in the backlog: the closing
guidance, the steer trust prose, and the mood-brief label.

The transcript is read from a stored compaction epoch, not a rolling tail, so
between boundaries request N+1 is request N plus what was appended. The epoch
moves at day rollover only, and records a `context_compacted` event. The
first request after 2 h or more with no model call prunes once: state cards
older than the newest full card and decision tool pairs from before his last
message, with the pop-up blocks they were asked with (`Session._cold_prune`,
`context_pruned`; the provider's cache is gone by then). Idle is measured
from when each call actually went out (`llm_calls.called_at`). `tests/test_cache_prefix_gate.py` is the pre-run check.

Her replies open with a face tag, `[smug] …` (`harness/faces.py`). Her stored
row keeps it, and every channel strips it: `TurnResult.face`, a `{"face"}`
event ahead of the call's first sentence, and `face` on the `/chat` history
rows.

## Current implementation gaps

- Day-scoped material (agenda plan, life arcs) is rebuilt into the volatile
  tail every turn instead of being appended once at rollover.
- Outstanding steers are not drained before day finalize or conversation
  close. Since 2026-10-03 the day's unanswered pop-ups and proactive fires are
  abandoned when it finalizes (`steers_expired`), so they no longer pop up the
  next day; asking them before finalize is still open.
- A pop-up that never parsed leaves no decision, so its block is not saved
  and the next request does not extend that one (owner ruling 2026-10-03
  saves blocks with their decisions).
- Idle decisions and their relationship to visible conversation turns need the
  final runtime wiring.

## Pointers

- Earlier spec (dated): [reports/spec-context-events-time-2026-08-15.md](reports/spec-context-events-time-2026-08-15.md)
- Verbatim design record: [../VERBATIM.md](../VERBATIM.md)
- Operational flow (dated): [reports/context-flow-2026-08-14.md](reports/context-flow-2026-08-14.md)
- Lifecycle policy: see the lifecycle section above.
- Backlog: [BACKLOG.md](BACKLOG.md)
