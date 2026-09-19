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

1. **Only two forks.** The judge and the day planner are the only things
   allowed a model call of their own. Everything else rides the turn's request.
   If you are about to add a leg, you are about to be wrong.
2. **A decision is not a call and not a tool.** It rides the turn's own request
   as the last system block, ending in a question plus
   `[DECIDE]{...}[/DECIDE]`, and she answers it inside the reply she was
   writing anyway. One pop-up per turn; the rest defer.
3. **The stream is append-only.** Request N+1 is request N plus what happened.
   Never edit, reorder, re-render or front-truncate a message already sent —
   prefix caches match from token 0. `tests/test_cache_prefix_gate.py` is the
   pre-run gate.
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
- `harness/session.py` — the turn. All three entry points (`on_message`,
  `fire_proactive`, `self_turn`) go through `_chat`.
- `harness/steering.py` — the safe-boundary queue: what is asked, what defers.
- `harness/tools.py` — the `[DECIDE]` protocol and the (empty) actuation menu.
- `harness/runtime.py` — inbound messages, proactive scheduling, the life loop,
  locking, real-time anchoring.
- `harness/store.py`, `store_schema.py` — SQLite, migrations, replay records.
- `observability/` — the run viewer (`python -m observability --port 8788`).
- `experiments/`, `tests/` — probes and the suite.

## Working style

- Fix the cause, not the symptom. This repo's tests pin behaviour, so a failing
  test is a claim about the system — read it before changing it.
- Dated reports are history. Current claims belong in the living documents.
- Prefer one authoritative description over duplicated summaries.
