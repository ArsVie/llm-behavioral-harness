# hey.md — agent message board

One `###` entry per agent, newest at top. Fields: agent+session, timestamp, task, files/dirs, boundaries, status.
Rules: append-only for others' entries; mark your own resolved when done; never rewrite/delete another agent's entry.

---

### Hermes orchestrator (harness-runtime 2026-08-14)
- **Timestamp:** 2026-08-14T03:25Z
- **Task:** Runtime redesign orchestration — design at `plans/harness-runtime-design-2026-08-14.md`; approvals D1(a)/D2/D3 + budget default (per-day, forced reply, 0=always, unset=off) via best-judgment (user away). All four streams merged into `wip/harness-integration`; finalization (suite, smokes, docs/context-flow-2026-08-14.md, commit) done by orchestrator.
- **Files/dirs:** plans/harness-runtime-design-2026-08-14.md; docs/context-flow-2026-08-14.md; hey.md
- **Boundaries:** engine/ and frozen files (engine/types.py, engine/rng.py, tests/conftest.py, pyproject.toml, CONVENTIONS.md) are OFF-LIMITS.
- **Status:** resolved — all streams merged into `wip/harness-integration`, full suite green, probe runs committed, branch merged to main (2026-08-14)

### WS4 — integration (wip/harness-integration)
- **Timestamp:** 2026-08-14T02:33Z
- **Task:** Merge all three streams; wire session/runtime (3-tier context, day-block cache, steering drains, DecisionRunner dispatch, thinking effort, single reply-path); fix r7 + forbidden-token failures; new tests/test_harness_wiring.py (11 tests).
- **Files/dirs:** harness/session.py, harness/runtime.py, harness/prompts.py (STEER_TRUST_RULE), harness/behavior.py (texture reword), tests/test_harness_wiring.py (NEW), tests/test_adversarial_restart.py, tests/test_snapshot.py, tests/test_prompts.py.
- **Boundaries:** respected — engine/ + frozen files untouched.
- **Status:** resolved (hit iteration cap before full suite/docs/commit; orchestrator finalizing)

### WS1 — context (wip/harness-context)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** Assembler v2 (3-tier: stable system core / day-start personality+agenda / state card at conversation start), prompts module, current-activity fix, typed-header audit export.
- **Files/dirs:** harness/assembler.py, harness/prompts.py (NEW), harness/audit.py (NEW), harness/session.py (ONLY `_build_snapshot`, `_current_activity`, `ensure_day`, and the assemble/build_messages call lines in `_chat`), tests/test_assembler.py, tests/test_prompts.py (NEW), tests/test_audit.py (NEW), tests/test_session.py (activity tests only).
- **Boundaries:** do NOT touch `_chat` control flow beyond the assembly call lines (WS4 wires steering/tools there); do NOT touch store.py, client.py, runtime.py, tools.py, steering.py.
- **Status:** resolved (merged 5b11edd..2561732 into wip/harness-integration)

### WS2 — decisions (wip/harness-decisions)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** Decision API: tool schemas + runner (decide_event, decide_reply), budget (per-day window, exhaustion = forced reply, 0 = always reply, unset = off), verbose flag notices, decision_source model|server_draw, replay-reads-verdict; store migration v5 (decision_records + steering_queue tables + record/enqueue/deliver API); #22 probe experiment.
- **Files/dirs:** harness/tools.py (NEW), harness/store.py (ONLY additive v5 migration + new methods, no changes to existing methods), experiments/decision_probe.py (NEW), tests/test_tools.py (NEW), tests/test_store_migrations_it3.py (v5 additions), tests/test_decision_probe.py (NEW, fake mode).
- **Boundaries:** steering_queue TABLE + enqueue/pending/mark methods are a CONTRACT for WS3 — implement exactly the signatures in your brief; do NOT touch session.py, client.py, runtime.py, assembler.py.
- **Status:** resolved (merged 2a073e0..c58ea66 into wip/harness-integration)

### WS3 — steering+thinking+client (wip/harness-steering)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** SteeringQueue (boundaries idle/after-tool/after-reply, enqueue/delivery timestamps, one-shot + seen-marker, re-queue on interrupt, restart persistence via store backend protocol); client.py native tool calling + reasoning_content extraction + effort param + capability detection; FakeClient extension.
- **Files/dirs:** harness/steering.py (NEW), harness/client.py, tests/test_steering.py (NEW), tests/test_client.py (extend).
- **Boundaries:** steering.py depends ONLY on a backend protocol (enqueue_steer/pending_steers/mark_steer_delivered) — implement the protocol + an in-memory fake for tests; the SQLite implementation lands in WS2's store.py and wires in WS4. Do NOT touch store.py, session.py, runtime.py, assembler.py, tools.py.
- **Status:** resolved (merged 7fd4f44..8638eaa into wip/harness-integration)
### Hermes orchestrator (wave 1 — unblockers & foundations 2026-08-15)
- **Timestamp:** 2026-08-15T22:50Z
- **Task:** Execute `docs/plan-unblockers-foundations-2026-08-15.md` (contract committed 4cdd6dd, spec `docs/spec-context-events-time-2026-08-15.md`). Track A: W1 (real-time substrate, v6→v7 migration, `anchor.real_at`) then W2+W3 (time-aware assembler + state-card sectioning, same lane). Track B: W4 (`behavioral_signature/` extractor, shared with the codebook H4 evaluator). W5 after gate close (archive live DB, fresh trial restart). Gates G1–G5 pre-registered; no pushes; engine/ + frozen files off-limits.
- **Files/dirs:** harness/anchor.py, harness/store.py (additive v7 migration + write paths), harness/assembler.py, harness/life.py, harness/session.py (status transitions only), behavioral_signature/ (NEW), tests/*, docs/design-note-cognition-principle-2026-08-15.md (NEW).
- **Boundaries:** codebook experiment's uncommitted files (docs/exp-affect-codebook-pipeline-2026-08-15.md, experiments/emotion-codebook-spike/, results/it3-g5-matrix/*, results/iteration-3-report.md) are NOT mine — do not commit/touch. Live DB `results/live-companion/companion.db` is read-only until W5.
- **Status:** in progress — lanes dispatched (wip/w1-real-time, wip/w4-behavioral-signature)
