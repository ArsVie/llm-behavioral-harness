# Thermo-Nuclear Review — Follow-up Plan (parked refactors)

Date: 2026-08-28
Status: PARKED — landed waves are green; these are the deliberate, high-risk
decompositions that each need a clean dedicated pass + the full-suite gate
(~13-14 min per iteration). Do NOT rush them; do NOT run them parallel to
another gate.

## What landed (committed, behavior-preserving, tests green)

- e928ccb — provider switch to commandcode.ai (org-qualified deepseek/deepseek-v4-flash)
- 2d99a74 — Wave 1: RNG stream register doc, IntentResolver default clarified,
  map_defer_turns -> re-export of map_defer_n, dead code removal, probe_lane
  single resolve, spend CLI pricing override, Usage.reasoning_tokens, stale
  provider strings, de-brittle test pins, pricing docstring escape fix
- 7f0b7d7 — Wave 2: judge calls use RESEARCH-lane client (T3)
- 6a45df4 (9595322) — Wave 5: client retry-loop collapse + store
  load_judgement_score canonical float-parse
- 102ab58 — Wave 3 (partial): tests/helpers/ package (9 modules) + 10 test
  files converted; inter-test imports resolved in those files; `rows = rows`
  collision + missing SOURCE_AGENDA import fixed (real bugs the extraction
  exposed)

## Parked — Wave 4 (big decompositions)

### 4a. session.py (2578 lines) — split the negotiation state machine

Target: extract the availability-negotiation block (session.py:2041-2501,
~470 lines) into a `NegotiationCoordinator` class in a new module
(harness/negotiation_coordinator.py) or fold into harness/negotiation_state.py.

The block (methods): _restore_negotiations, _persist_negotiation,
_find_agenda_item, _afk_anchor, next_negotiation_trigger_t_h,
check_negotiation, _maybe_start_negotiation, _run_inform, _run_turn_decides,
_run_decide_leg, _resolve_go, _resolve_skip, _resolve_forced, _resolve_delay,
_log_resolution, _emit_episode.

Dependencies to pass in (the entanglement):
- store (list_agenda_items, log_event, events_since)
- _conversation (current open Conversation; _afk_anchor needs its
  last_user_turn_t_h / opened_t_h)
- _steering + _decision (the pop-up plumbing: _run_inform/_run_decide_leg
  call _execute_decision/_apply_steer/_popup_request_call)
- _negotiations dict
- _emit_episode's memory/logging side effects
- client + judge_model for the decide-leg LLM call
- day / t_h resolution helpers

SEAM CONSTRAINT (non-negotiable): every decision id stays byte-identical
(neg-<item_id>-inform / neg-<item_id>-decide-<n>); the state snapshot
persistence (negotiation_state JSON via state_to_dict/state_from_dict) is the
replay contract — the coordinator must persist EXACTLY the same snapshots at
the same instants. Replay-parity tests (test_availability_negotiation.py,
test_negotiation_state.py, test_session_close_parity.py) are the gate.

Suggested cut: a NegotiationCoordinator constructed with (store, session)
that reads session state via narrow accessors (or a small dependency bundle
dataclass); Session delegates the 4 public entry points
(next_negotiation_trigger_t_h, check_negotiation, _maybe_start_negotiation,
the _resolve_* internals stay private to the coordinator). Keep Session's
public method names as thin delegates so nothing outside session.py changes.

Verification: run test_negotiation_state.py + test_availability_negotiation.py
+ test_session_close_parity.py + test_adversarial_restart.py, then the full
suite.

### 4b. store.py (2193 lines) — split schema/migrations from the access layer

Target: extract the DDL + migration chain (store.py:165-688: _SCHEMA_META,
the v1 base tables DDL, schema_meta(), _current_version(), _ensure_column(),
_migrate_v2..v8, _migrate(), _category_from_key) into harness/store_schema.py;
store.py imports them back (re-export) so external imports stay unchanged.

The migration functions reference UserModelCategory (from harness.domain) and
raw sqlite3 — store_schema.py needs those imports. `_migrate(conn)` is called
from SQLiteStore.__init__ (store.py:~770) — keep the call, move the def.

SEAM CONSTRAINT: SCHEMA_VERSION stays in store.py (or store_schema.py with
store.py re-exporting); the migration chain order and DDL strings MUST be
byte-identical (migration tests: test_store_migrations*.py, 2,133 lines).
Additive-only contract preserved.

Verification: test_store.py + test_store_it2.py + test_store_migrations*.py
+ test_store_migrations_it3.py, then full suite.

### 4c. Wave 3 follow-up — convert the remaining ~15 test files to tests/helpers

**STATUS: DONE (2026-08-30).** Converted test_life.py, test_life_long_horizon.py,
test_adversarial_runtime.py, test_adversarial_proactivity.py,
test_adversarial_restart.py, test_runtime.py, test_proactive_it2.py,
test_assembler.py, test_prompts.py, test_prompt_cache_order.py,
test_serializer_null_hardening.py, test_client.py, test_persona.py,
test_bootstrap.py, test_tools.py, test_negotiation_schema.py,
test_store_migrations_v6.py, test_store_migrations_v7.py,
test_channel_telegram.py, test_snapshot.py, test_w2w3_time_aware.py,
test_conversations.py, test_cvs_preflight.py, test_validation.py,
test_memory.py, test_commands_channel.py, test_channels_base.py.

New helper modules: life.py, runtime.py, domain_builders.py, client.py,
misc.py, tools.py, migrations.py, conversations.py, memory.py (extended),
clocks.py (extended). ~1,400 tracked deletions vs HEAD; net across tests/
including new helper lines ≈ −735.

Kept local (deliberate): test_lifecycle_away._session (SEED=4242 ≠ helper
12345), migration _build_v*_db builders (version-specific schemas, seam-guard
invariants), test_conversations thin wrapper (store.py make_session has
`clock` keyword-only + no `replies`), test_adversarial_runtime._run
(IntentResolver default rng = stream_rng(0) ≠ helper's stream_rng(SEED)).

Full suite verified green: **1371 passed, 0 failed (657s)** — including the
make_session canonical-signature restore (store.py superset re-exported by
runtime.py, not duplicated) and rows(store, seed) arity fix.

### 4d. runtime.py _rollover_loop decomposition (C5)

Extract `park_target(now)` + `_wake_hooks(now)` helpers; collapse the
triplicated wake-hook sequences (runtime.py:601-615, 716-730, 836-849) into
one helper. Purely mechanical, but the loop is timing-sensitive (rollover
races) — dedicated pass with test_runtime.py + test_runtime_anchor.py +
test_adversarial_runtime.py + full suite.

## Remaining cleanup items (low priority, fast)

- memory.py facade (19-name re-export __all__) — move baselines to test-support
- tools.py per-kind verdict spec table (data-drive EVENT_VERDICT_KEYS/
  REPLY_VERDICT_KEYS/_normalize_verdict)
- `_record_from_row` duplicate (session.py:558-572 vs scheduler.py:282-296) —
  break the session->scheduler import cycle by moving REASON_* constants to
  domain.py
- `_env_bool` 5 copies -> harness/tunables.get_flag
- three time-renderers -> one renderer
- DeterministicSummaryExtractor identity wrapper -> protocol check
- negotiation_episodes re-declared tables -> consume contract constants
- session.py:1480-1486/1692-1700 bare `except Exception: pass` bubbles
