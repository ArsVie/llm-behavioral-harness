# Repository conventions

Current contributor rules for the behavioral harness. The living runtime contract
is in `docs/architecture-overview.md`; dated measurements and experiment records
are under `results/`. Do not add historical implementation plans to the repo.

## Environment and commands

- Work from native WSL at `/home/vruizes/.hermes/projects/llm-behavioral-harness`.
- Use the project interpreter: `.venv/bin/python`.
- Full suite: `MPLBACKEND=Agg .venv/bin/python -m pytest`.
- Run one test module with `MPLBACKEND=Agg .venv/bin/python -m pytest tests/<module> -q`.
- Run experiments as modules; write generated artifacts only to their own
  `results/<experiment>/` directory.
- Use Conventional Commits when committing. Never commit secrets or local
  environment files.

## Code

- Python 3.11+ with typed public interfaces. Avoid `Any` and `unknown` types.
- `engine/` is pure: no I/O, real-clock reads, global state, or unseeded
  production randomness. Randomness enters through the injected RNG utilities.
- Engine state is immutable from callers' perspective: transitions return new
  state values.
- Keep provider, channel, persistence, and orchestration concerns behind their
  existing interfaces.
- Internal events are system-level context, not user messages. Decisions are
  structured tools. Telegram visibility is an explicit delivery concern.
  Scope: that governs HER conversation. A call about the companion also needs
  to KNOW that conversation: the judge and the day planner FORK the mainline
  request — same stable system, same stamped stream, same card, task folded
  into the trailing system block — so the whole prefix banks on the provider
  cache; their output is engine state (judgement row, agenda text) and never
  re-enters the conversation (owner ruling 2026-09-13). With no mainline yet
  (fresh boot; the onboarding calls — interest extension, routine setup) they
  fall back to a single `user` task message — ratified 2026-09-12; a
  system-only single message also risks dialects that require a user turn.
  Her own conversation never carries harness text in a user slot: a proactive
  turn has NO user message at all, and `harness_text_in_user_roles` scans
  every request.
- Keep the stable persona/rules/tools prompt prefix byte-identical; append
  volatile internal material at the tail. Never edit, reorder, or re-render a
  message already sent to the provider — prefix caches match strictly from
  token 0, so a changed byte anywhere costs everything after it. Front-
  truncating history is the same violation: it moves the boundary every turn.
  Every request that reads the store goes through the ONE stamped-stream
  builder (`stamped_stream`): a second builder drifting (user turns stamped in
  one, raw in the other) broke the live prefix at the first user message
  (2026-09-12) — the pop-up legs banked only the system block.
- A verdict's `reason` is audit data, never dialogue. Decisions record why;
  turns produce words. Pasting a reason into the channel puts third-person
  machine rationale in the conversation.
- Any retry of a model call needs a bound. An unbounded requeue re-asks the
  same question every turn and the cost grows silently.
- Model calls outside a conversation turn (onboarding, day planning) are
  opt-in, bounded by an explicit wall-clock budget, and must fall back to a
  working offline path. The client's own retry policy is ~7 minutes and
  raises nothing while it waits, so guarding the error is not enough — bound
  the wait.
- Interest, activity and arc names must mean ONE thing standing alone. They
  reach the model verbatim — as interests, as `practice {name}`, as
  `learning {name}` — so a bare word with two senses gets read as the wrong
  one. Write `metal music`, not `metal`. `interest_extension` rejects a known
  ambiguous bare word, and `test_setup_onboarding` guards the catalog.
- Model-visible time is always a wall clock (`clock.hhmm`) or a plain duration
  (`clock.duration`). Absolute virtual hours (`t_h`) are an engine coordinate
  and never reach the prompt; raw values stay in the stored row and convert at
  the render boundary.

## Before a live run

- Run the cache gate: `MPLBACKEND=Agg .venv/bin/python -m pytest tests/test_cache_prefix_gate.py -q`.
  It drives a mixed day (reactive turns, an event pop-up decision, a steer, a
  proactive fire, a lifecycle check) and asserts that every provider call is a
  byte-prefix extension of the one before it. A new event kind, steer, tool or
  card section that writes anywhere but the tail fails here.
- Any new context-producing path must be added to `_mixed_run` in that file,
  and to the coverage assertions that keep the gate from going vacuous.

## Inspecting a run

Never read a run database with an ad-hoc query. Two read-only inspectors cover
the two questions, and a new invariant belongs in one of them rather than in a
throwaway script:

- `python -m harness.trace --db <run.db> [view]` — what happened. Views:
  `timeline` (default; every message, steer, decision, state event and call in
  one chronological stream), `checks`, `negotiations`, `decisions`, `steers`,
  `agenda`, `cache`, `memory`, `all`. Filters: `--day`, `--from`, `--to`,
  `--width`, `--out`.
- `python -m harness.audit --store <run.db> --call <id>` — what the model saw
  on one call, rendered with typed headers.

`python -m harness.trace checks --db <run.db>` exits 1 on any ERROR finding, so
it is the post-run gate: run it after every live session. The cache prefix gate
proves the stream is append-only in a synthetic day; `checks` reports what the
provider actually cached, metered and decided in the real one. A behavior we
have decided is wrong gets a check here in the same change that fixes it.

## Tests and experiments

- Tests must cover behavior, not implementation trivia. Statistical tests use
  fixed seeds and document their tolerances.
- Every experiment record states its seeds, criteria, thresholds, verdict, and
  concise interpretation. Null results remain recorded.
- Dated reports are historical evidence, not current status. Current claims
  belong in the living architecture documents or must link to a fresh result.

## Documentation

- Prefer one authoritative description over duplicated summaries.
- Keep current design, open work, and measured results separate.
- Remove superseded drafts and plans rather than indexing them as current.
- Check local Markdown links and run `git diff --check` before finishing.
