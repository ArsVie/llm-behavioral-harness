# Orchestration plan — conversation lifecycle (away≠close), cache, OpenRouter

Date: 2026-08-17
Mode: orchestrator (subagents)
Scope: implement everything already DECIDED. Anything still needing a product
decision is listed at the bottom as OUT — do not implement it.

## Guiding invariants (do not violate)
1. **Lily is continuous.** A conversation close is a background checkpoint
   (promote memory), never a reset. She is never rebuilt from a summary.
2. **Never reset unless the token window forces it.** For this work it never
   forces it (a single conversation doesn't fill 160k until ~2,750 exchanges),
   so: no compaction, no summary-substitution anywhere in this plan.
3. **On user return, the SAME live conversation continues** with full raw
   context — a prior checkpoint close must not compact it or swap raw history
   for a summary.
4. **Away ≠ closed.** 15 min of silence = "user is away" (presence signal),
   not a teardown.
5. **Leverage cache.** Keep the prompt's stable prefix byte-identical across
   turns and across conversations; only the tail (volatile state) changes.
6. **never-diverge:** product == research instrument. Replay parity holds
   (same seed → byte-identical run); everything recorded/replayable.

## Already done (do NOT redo — build on it)
- `harness/tunables.py` is the single source of truth for lifecycle constants;
  `session.py` and `negotiation_contract.py` import from it (stale `12.0`
  drift bug fixed).
- `MAX_TURNS = None` (turn cap off). `CLOSING_TENDENCY_ENABLED = False`
  (closing draw off behind the flag; keyed RNG so skipping is replay-safe).

---

## WS-A — Away-as-presence, close-as-checkpoint
**Goal:** 15 min silence marks the user away (dormant, conversation stays open);
a conversation only truly closes at a natural checkpoint. Return continues the
same conversation with full continuity.

- In `tunables.py`: add `USER_AWAY_THRESHOLD_H = 0.25` (15 min, presence signal).
  Repurpose the silence backstop: `USER_LEFT_THRESHOLD_H` becomes a LONG
  abandoned-chat backstop (default ~6 h true silence; tunable) — NOT the 15-min
  value. Document both.
- `session.py` lifecycle:
  - Silence past `USER_AWAY_THRESHOLD_H` sets a presence flag `away` on the open
    conversation; it does NOT close. (Presence is derived from
    `_last_user_turn_t_h` and the current clock — no new RNG.)
  - A conversation closes (checkpoint) only on: (a) the quiet-hours/day boundary
    (existing `check_conversation_lifecycle` path), or (b) `USER_LEFT_THRESHOLD_H`
    (long backstop) of true silence. Both run the existing memory promotion.
  - **Return within the backstop continues the SAME conversation** (clears
    `away`), no new conversation row, no context rebuild.
- **Continuity guard (critical):** the memory promotion on a checkpoint close
  must not feed back as a summary that replaces raw context on the next turn.
  After a checkpoint close, the next conversation's context is assembled from
  raw recent history + memory as ADDITIVE context — never a summary substitution.
  Add a test that asserts raw prior turns are present in the next assembled
  prompt (no summary-only rebuild).
- Done when: intermittent texting with 20–40 min gaps stays ONE conversation;
  `away` flips on/off correctly; checkpoint close promotes memory; return after a
  checkpoint carries raw continuity; replay parity holds.

## WS-B — Test suite green against the new model
**Goal:** no test hardcodes a lifecycle duration; suite passes under the new
behavior.
- Rewrite the failing/again-brittle tests to read durations from `tunables.py`
  and express scenarios RELATIVE to the constants (e.g. "gap < away", "gap >
  backstop"), so a value change never breaks a test again.
- Update tests that assumed: 12 h close, `max_turns` cap, closing_tendency on.
  Replace "closes by max_turns / closing_tendency" cases with the new model
  (away flag, checkpoint close). Remove premises invalidated by the design
  (e.g. wind-down surviving a fixed gap → express relative to the grace).
- Add coverage: away→dormant→resume=same conversation; backstop close;
  day-boundary checkpoint; no-rebuild-on-return.
- Done when: `pytest tests/ -q` is green; grep shows no hardcoded lifecycle
  durations in tests.

## WS-C — Run Lily on OpenRouter (deepseek-v4-flash)
**Goal:** product lane runs on the OpenRouter free deepseek-v4-flash; judges
stay local.
- Confirm the exact free model slug on openrouter.ai/models (deepseek v4-flash,
  the `:free` variant). Do NOT guess — verify the slug resolves.
- Point the product lane at `OPENROUTER_BASE_URL` + `OPENROUTER_API_KEY` +
  the confirmed `LLM_MODEL` slug. The launcher lives OUTSIDE the repo
  (`~/.hermes/scripts/live_telegram.sh`); update it there. Judges/research lane
  (`JUDGE_GENERATOR_TOKEN`, local) unchanged.
- Validate: token check (`getMe`) + one real round-trip reply end-to-end.
- Secrets discipline: never print/commit token values; `.env` stays 600 +
  gitignored. (Reminder for the human: rotate the pasted key.)
- Done when: Lily answers a real message through OpenRouter; no secret leaked.

## WS-D — Prompt-cache friendliness + usage capture on the new lane
**Goal:** structural prompt caching (the DeepSeek-read finding) + real token
numbers on OpenRouter.
- Reorder the assembled prompt so the stable prefix (persona / instructions /
  fixed system) is FIRST and byte-identical every turn and across conversations;
  put volatile state (state-card, current-time line, mood-derived brief) at the
  TAIL. No `cache_control` needed — caching is structural.
- Probe: does OpenRouter/deepseek return cached-token counts (e.g.
  `usage.prompt_tokens_details.cached_tokens`)? Record the passthrough result.
- Capture usage (prompt/completion/cached) per call on the OpenRouter product
  lane into the v8 `llm_calls` ledger; pricing config updated for the new lane.
- Verify: reorder changes token layout only, not behavior (replay parity).
- Done when: cache-hit tokens observed (or definitively reported absent through
  the gateway), and per-turn usage lands in the ledger.

## WS-E — Serializer null-hardening (reasoning-only turns)
**Goal:** never send `content: null`.
- Guard the request/response serialization so `content` is `""` never `null`
  when the model answers entirely in the reasoning channel (the v4-flash quirk;
  verify whether it reproduces through OpenRouter and guard regardless).
- Add a regression test with a reasoning-only fixture.
- Done when: a reasoning-only turn serializes without a 400; test pins it.

---

## Sequencing
- **Parallel now:** WS-A (+WS-B follows it closely), WS-C, WS-E — independent.
- **After WS-C:** WS-D (needs the OpenRouter lane live to probe cached tokens).
- WS-B is the gate for WS-A landing; each WS keeps the suite green on its branch.

## OUT — needs a product decision, do NOT implement here
- closing_tendency redesign (flat vs fatigue curve) — flag stays OFF until decided.
- Compaction / memory-continuity spec — deferred (not needed near-term).
- Affection / closeness score — needs its own design; gates the AFK double-text.
- Wiring the `away` signal into proactive-landing / double-text / skip-inform —
  the signal is produced here (WS-A); consuming it in those behaviors is a
  follow-up (AFK design + negotiation inform/decide split).
