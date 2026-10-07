# AGENTS.md

Entry point for any agent working in this repository. Read this first, then the
one or two documents it points you at — not the whole `docs/` tree.

## What this is

A harness that wraps an OpenAI-compatible LLM and delivers a companion (Lily)
through Telegram. A seeded stochastic engine supplies the latent variation —
mood, energy, a ~28-day cycle, daily agenda, contact timing. The model renders
that state into language and never sees the raw numbers. Every stochastic and
model step is recorded so a run replays.

## Before you touch anything

- Work from `/home/vruizes/.hermes/projects/llm-behavioral-harness`.
- `python` is **not** on PATH. Use `.venv/bin/python`.
- Full suite: `MPLBACKEND=Agg .venv/bin/python -m pytest`
- Correctness lint: `.venv/bin/ruff check --select F harness/ experiments/ observability/`
  (the full `ruff check` reports pre-existing style noise; `--select F` is the gate).
- Never commit secrets, `.env`, or live-trial databases.

## The owner's own words

[VERBATIM.md](VERBATIM.md) keeps the owner's decisive wording on how this system
is supposed to work, unedited. Read it. Where it and the code disagree, the
quote is the intent: the difference is either a later ruling someone recorded or
a defect, and you say which — you do not quietly follow the code.

## The five rules that are easy to break

1. **Only three forks.** The judge, the day planner and the end-of-day memory
   (`harness/memory_fork.py`, added 2026-09-26) are the only things allowed a
   model call of their own. Everything else rides the turn's request. If you
   are about to add a leg, you are about to be wrong. The memory fork reads
   only the day it remembers (`memory_fork.day_view`, ruling 2026-10-03) and
   writes its summary of the day over that day's session summaries.
2. **A decision is a tool call on her own stream, not a fork.** The pop-up
   rides as a system block at the tail of the mainline request, and she
   answers with a decision tool (VERBATIM, "Event and decision roles"); the
   exchange is appended to the stream. `reason` is the decision's audit field
   and never her words; a decision outside a conversation is Telegram-silent.
   Prose instead of a tool call is a parse failure, never a message. One
   pop-up per turn, the turn's own first; the rest wait untouched. The block
   is saved with its decision and dropped only by the cold-point prune
   (VERBATIM, "Live-run audit rulings").
3. **The stream is append-only.** Request N+1 is request N plus what happened.
   Never edit, reorder, re-render or front-truncate a message already sent —
   prefix caches match from token 0. `tests/test_cache_prefix_gate.py` is the
   pre-run gate. Two breaks are sanctioned, both logged: compaction at day
   rollover (`context_compacted`) and the cold-point prune after 2 h with no
   model call, when the provider cache is gone anyway (`context_pruned`,
   `Session._cold_prune`).
4. **A verdict gates DELIVERY, not generation.** She always writes. A no-reply
   verdict keeps her words in the stream and sends nothing.
5. **Her conversation never carries harness text in a user slot.** Events are
   system blocks. A proactive or clock-started turn has no user message at all.

## Where to look

| you need | read |
|---|---|
| how a turn is assembled, who starts one, what the prompt looks like | [docs/architecture-overview.md](docs/architecture-overview.md) |
| the rules for writing code and tests here | [CONVENTIONS.md](CONVENTIONS.md) |
| the engine's mathematics and parameters | [DESIGN.md](DESIGN.md) |
| why something is the way it is, in the owner's words | [VERBATIM.md](VERBATIM.md) |
| what the project is, with evidence | [README.md](README.md) |
| unresolved work | [docs/BACKLOG.md](docs/BACKLOG.md) |
| work that is planned but not built | [docs/plans/](docs/plans/) |

Everything under [docs/reports/](docs/reports/) is **dated evidence, not current
status** — postmortems, parity checks, superseded specs. Read one when you need
to know why something is the way it is; never cite one as the way things are.
Experiment outputs are under `results/`, prior art under `research/`.

## Code map

- `engine/` — seeded mood, cycle, circadian, contact timing. Pure: no I/O, no
  real clock, no unseeded randomness.
- `harness/session.py` — the turn. The entry points `on_message`,
  `fire_proactive` and `incoming_call` go through `_chat`; a voice-call turn
  (`call_turn`) builds the same request without the pop-up drain.
- `harness/steering.py` — the safe-boundary queue: what is asked, what defers.
- `harness/tools.py` — the decision tools, their parsing and the (empty) actuation menu.
- `harness/runtime.py` — inbound messages, proactive scheduling, the life loop,
  locking, real-time anchoring, and the voice call (`/call`, `/hangup`, the
  pauses while a call is on).
- `harness/call.py` — the voice-call HTTP API live-talk talks to
  (127.0.0.1:8790, `HARNESS_CALL_PORT`) and the live-talk launch command.
- `harness/store.py`, `store_schema.py` — SQLite, migrations, replay records.
- `harness/images.py` — photos he sends: stored by hash next to the database,
  referenced in the stream, inlined only on the posted body.
- `observability/` — the web app (`python -m observability --port 8788`): `/chat` (the live conversation, her sprite, the call button; writes are forwarded to the bot's web channel), `/debug` (also `/`, the run viewer) and `/setup` (the owner commands).
- `experiments/`, `tests/` — probes and the suite.

## Working style

- Fix the cause, not the symptom. This repo's tests pin behaviour, so a failing
  test is a claim about the system — read it before changing it.
- Dated reports are history. Current claims belong in the living documents.
- Prefer one authoritative description over duplicated summaries.
