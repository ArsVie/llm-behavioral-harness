# Deletion + WS-D wiring plan (2026-09-06)

> **For Hermes:** Execute with subagent-driven-development, task-by-task.
> Deletion tasks are PURE REMOVALS: each commit stages only deletions plus the
> minimal import fixes, with the `git grep` consumer-proof in the commit message.

**Goal:** Remove ~1,050 production LOC + ~1,600 test LOC + 6.7 MB of proven dead
weight, then wire the session mainline onto the WS-D `build_context_messages`
seam so the documented cache contract (stable prefix + volatile trailing state
card) is actually what runs.

**Architecture:** No new abstractions. Phase A–C are deletions with zero
behavior change (full suite green after every commit). Phase D switches the
one production call site in `harness/session.py` from
`assemble_snapshot`+`build_messages` to `build_context_messages` behind a
byte-parity check, then retires the docstring lie. Legacy-path deletion and the
typed-event/outbox work are explicitly OUT of this plan (see backlog).

**Verification baseline:** `uv run pytest -q` green before Task 1; rerun after
every commit. Live bot (`lily-telegram.service`) restarts need Ars approval —
nothing in Phases A–C touches its import graph except `sim/run_interactive.py`
removal (bot imports `run_async`; verified).

Evidence archives (full grep traces per finding):
`~/.hermes/profiles/dev/cache/delegation/subagent-summary-{0..4}-20260906_*.txt`

---

## Phase A — zero-risk repo hygiene

### Task 1: Remove stray artifacts
- `git rm sim.zip hey.md README.draft.md` ; `rm -f cov-current.json` (untracked)
- Edit `index.md`: delete the sim.zip listing line.
- Verify: `git grep -n 'sim\.zip\|hey\.md\|README.draft'` → only historical docs/results, no code.
- Commit: `chore: remove stray archive and scratch files (zero consumers)`

### Task 2: Fix the false WS-D docstring claim
- `harness/assembler.py:60-63`: change "the session mainline wires
  build_context_messages" to state the seam exists but the mainline still uses
  `assemble_snapshot` (Phase D reverses this again — keep the edit minimal).
- Trim pure narration from `assembler.py:1-81` and `runtime.py:1-69` (~100
  lines of prose); KEEP invariant statements (leakage, budget, A5 boundary).
- Commit: `docs: correct WS-D wiring claim, trim narrative docstrings`

## Phase B — dead production code (zero prod consumers, proofs in archive 0/1/4)

### Task 3: Delete behavioral_signature/
- `git rm -r behavioral_signature/ tests/test_behavioral_signature.py tests/test_signature_export.py`
- Remove its entry from `scripts/crap_report.py` module list.
- Proof: `git grep -n behavioral_signature` → nothing outside deleted paths.
- Run suite; commit.

### Task 4: Delete sim/run_interactive.py
- `git rm sim/run_interactive.py tests/test_run_interactive.py`
- Proof: only importer was its own test; `run_async` supersedes (its docstring
  says so); `experiments/live_companion.py` imports `run_async`.
- Run suite; commit.

### Task 5: Dead store queries (~45 LOC incl. fakes/tests)
- Delete `SQLiteStore.load_previous_judgement` (store.py:627-636),
  `episodes_for_turn` (:1235-1245), `list_episode_sources` (:1226-1234),
  `deterministic_hash_embedder` wrapper (memory.py:144-150, drop from `__all__`).
- Delete their fake impls (tests/helpers/fakes.py:102, test_proactive.py:105)
  and asserting tests (test_store.py:348-349,418-423,
  test_store_migrations.py:180, it2:426, it3:421, test_adversarial_restart.py:508,
  test_adversarial_memory.py:173,407 → rewrite the two embedder calls to the class).
- Trim unused `_migrate_v3/_v4/_v5` re-exports in store.py:187-206 — verify
  EACH name individually first (`git grep 'store\._migrate\|from harness.store import'`);
  keep `_SCHEMA`, `_V2_TABLES`, and anything test_spend/v6/v7 import.
- Run suite; commit.

### Task 6: Session/runtime micro-cuts (~55 LOC)
- session.py:106-111 unused `emit_negotiation_episode` import guard;
  session.py:1408-1413 `chat_with_meta` hasattr fallback;
  runtime.py:675-684 `controls is None` fallback (+ session.py comment);
  runtime.py:901-923 dead ValueError-retry leg in `_fire_exact_intent`;
  assembler.py:204-220 legacy W-E1 `build_system_prompt` entry
  (+ its tests test_assembler.py:26,77-90);
  runtime.py:86-87 empty TYPE_CHECKING block; inline `POPUP_OPENING`
  (prompts.py:110-116 → keep text, drop the duplicate constant only if
  test_prompts.py passes unchanged — prompt BYTES must not change),
  `COUNT_JITTER` (persona.py:82-83), double anchor guard (assembler.py:561-565).
- Rule: any cut that changes prompt bytes is REVERTED (parity with recorded runs).
- Run suite; commit.

### Task 7: Steering/commands test-only conveniences (~35 LOC)
- steering.py:207-210 `enqueued_t_h` alias; :299-301 `pending_count`;
  concurrency.py:108-121 `RecordingSleeper` (test_runtime_shutdown.py inlines
  or imports the cvs_common copy? No — move nothing: keep whichever single copy
  test_runtime_shutdown needs, delete the other. Simplest: keep harness copy,
  delete the duplicate in experiments/cvs_common.py:195);
  commands.py:183-184,265-266 hook-is-None fallbacks (+ matching cases in
  test_commands_core.py).
- Update test_steering.py call sites (~4 asserts) to `.t_h`/`len(...)`.
- Run suite; commit.

## Phase C — test-suite dedup (proofs in archive 3)

### Task 8: Delete un-migrated inline fake copies (~497 LOC)
- test_channel_telegram.py L25-78 → import from tests/helpers/channel_fakes;
  test_memory.py L104 FakeStore → helpers.fakes.FakeStore;
  test_proactive.py L49 SeamStore → helpers copy;
  test_telegram_helpers.py ManualClock → helpers.clocks.ManualClock.
- Also delete the triplicated `_LEGACY_PREFIX_CATEGORIES` in test_memory.py:65-100
  (import from harness.store_schema; keep fakes.py copy only if fakes must not
  import harness — check, prefer the store_schema import everywhere).
- Run suite; commit per file if diffs are large.

### Task 9: Migration-test consolidation (~700 LOC of 2,119)
- Keep: one v1→v8 chain test, one idempotence test, one fresh-db test
  (the v7 file's), the two live-DB tests (v6:195, v7:459), the v5→v6
  seeded-data survival test, and every verbatim legacy DDL fixture the chain
  test needs (keep `_build_v1`; drop `_build_v2/_v4/_v5/_v6` only if the chain
  starts at v1).
- Delete: duplicate `test_migration_runs_twice*` (keep migrations.py:197,
  drop it2:462, it3:447), duplicate `test_fresh_db_reaches_vN` for v2/v3/v5/v6,
  per-file `_table_counts` duplicates, test_store.py `test_schema_version_recorded`
  (≡ it2's), it2 privacy asserts subsumed by it3.
- DO NOT delete any whole file — suites are layered, not generational.
- Run suite; commit.

### Task 10: Helper adoption + small dedup (~215 LOC)
- Replace local `_session` builders with existing `helpers.store.make_session`
  (14 files, list in archive 3 finding 2) and `_store()` one-liners with
  `make_store` (12 files) — only where the helper is a documented superset.
- Delete test_cvs_common.py::test_user_script_deterministic_and_consistent
  (strict assert-subset of test_cvs_user's legacy-projection test).
- Trim test_proactive_it2 re-asserts of invariant 7 already in test_session.py
  (keep the adversarial runtime-seam variant).
- Run suite; commit.

## Phase D — WS-D mainline wiring (the only behavior-adjacent phase)

### Task 11: Parity test first (RED)
- New test in tests/test_prompt_cache_order.py: for a fixed snapshot/turn,
  assert the JOINED content of `build_context_messages(...)` equals the JOINED
  content of the legacy `assemble_snapshot`+`build_messages` request
  (same sections, same bytes, different placement), AND that the stable system
  string is byte-identical across two consecutive turns.
- Expected initially: placement parity holds by construction of the seam; if
  the seam has drifted from mainline sections, the test FAILS — fix the seam
  (not the mainline) until green. This is the gate for the switch.

### Task 12: Switch the call site
- `harness/session.py:1537` (`_generate` request build): replace
  `assemble_snapshot`+`build_messages` with `build_context_messages`, passing
  the session-cached persona `day_block`. One call site; do not touch the
  popup/`_popup_request_call` path in this task unless it shares the site.
- Run: full suite + `tests/test_session_close_parity.py` (byte-identity
  merge-blocker pin) — if parity pin breaks, the pinned bytes change is the
  POINT of WS-D; update the pin only with recorded before/after and Ars sign-off.
- Sim smoke: `uv run python -m sim.run_daily --seed 5001` (or the documented
  smoke invocation) completes; transcript diff reviewed.
- Commit: `feat: wire session mainline onto WS-D build_context_messages seam`

### Task 13: Live verification gate (Ars-operated)
- Ars runs: `sqlite3 results/live-companion/companion.db 'select max(version) from schema_meta'`
  (records schema version — unblocks future migration pruning, out of scope here).
- Ars checks `lily-telegram.service` env for `TELEGRAM_HOME_CHANNEL` vs
  `TELEGRAM_CHAT_ID` (decides the telegram.py:299 alias's fate — out of scope
  to delete until answered).
- Live bot restart onto the new prompt path: requires Ars approval; observe
  one real conversation + provider cache-hit metrics before calling WS-D done.

### Task 14: Docstring truth pass
- Re-edit assembler.py header: the wiring claim from Task 2 is now true again;
  state it plainly. Note `assemble_snapshot` is now aux/experiment-only.
- Update docs/architecture-overview.md "implementation gaps" if it lists the
  state-card-in-system gap; update verbatim-design gap register: mainline
  retrieval item remains open (typed events NOT done here).
- Commit.

---

## Explicitly OUT of this plan (stay on backlog)
- Typed internal events / decision lane (popup steers are still synthetic
  user messages after this plan).
- Telegram durable outbox.
- Legacy `assemble_snapshot` path deletion (aux/experiment callers remain).
- `reason=` proactive contract retirement; hasattr store-seam shim removal
  (needs test-fixture standardization decision).
- Migration pruning (needs Task 13 schema reading + policy on old results DBs).
- negotiation_scenarios G0 runs (acceptance gate for real-time event work).
- Owner-call deletions: harness/audit.py, engine/validation.py,
  scheduler structured_no_state_* (dies with CVS suite), render_popup_block,
  assert_cell_valid, hourly_histogram.

## Risks
- Prompt-byte drift in Phase B Task 6 or Phase D breaks replay parity with
  recorded runs — the parity tests and the revert rule are the guards.
- test_prompt_cache_order currently pins the UNWIRED seam; Task 11 may reveal
  seam/mainline section drift — fix direction is seam→mainline, never the
  reverse.
- Deleting fakes' methods (Task 5) can silently un-exercise hasattr branches
  in session.py; that's acceptable (those branches are slated for the future
  fixture-standardization work), but note it in the commit.
