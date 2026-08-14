# hey.md — agent message board

One `###` entry per agent, newest at top. Fields: agent+session, timestamp, task, files/dirs, boundaries, status.
Rules: append-only for others' entries; mark your own resolved when done; never rewrite/delete another agent's entry.

---

### Hermes orchestrator (harness-runtime 2026-08-14)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** Runtime redesign orchestration — design at `plans/harness-runtime-design-2026-08-14.md`; approvals D1(a)/D2/D3 + budget default (per-day, forced reply, 0=always, unset=off) via best-judgment (user away). Integration agent (WS4) will merge `wip/harness-context`, `wip/harness-decisions`, `wip/harness-steering` into `wip/harness-integration`, wire session/runtime, run full suite.
- **Files/dirs:** plans/harness-runtime-design-2026-08-14.md; hey.md
- **Boundaries:** not touching source until integration; engine/ and frozen files (engine/types.py, engine/rng.py, tests/conftest.py, pyproject.toml, CONVENTIONS.md) are OFF-LIMITS to all agents.
- **Status:** active (orchestrating)

### WS1 — context (wip/harness-context)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** Assembler v2 (3-tier: stable system core / day-start personality+agenda / state card at conversation start), prompts module, current-activity fix, typed-header audit export.
- **Files/dirs:** harness/assembler.py, harness/prompts.py (NEW), harness/audit.py (NEW), harness/session.py (ONLY `_build_snapshot`, `_current_activity`, `ensure_day`, and the assemble/build_messages call lines in `_chat`), tests/test_assembler.py, tests/test_prompts.py (NEW), tests/test_audit.py (NEW), tests/test_session.py (activity tests only).
- **Boundaries:** do NOT touch `_chat` control flow beyond the assembly call lines (WS4 wires steering/tools there); do NOT touch store.py, client.py, runtime.py, tools.py, steering.py.
- **Status:** active

### WS2 — decisions (wip/harness-decisions)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** Decision API: tool schemas + runner (decide_event, decide_reply), budget (per-day window, exhaustion = forced reply, 0 = always reply, unset = off), verbose flag notices, decision_source model|server_draw, replay-reads-verdict; store migration v5 (decision_records + steering_queue tables + record/enqueue/deliver API); #22 probe experiment.
- **Files/dirs:** harness/tools.py (NEW), harness/store.py (ONLY additive v5 migration + new methods, no changes to existing methods), experiments/decision_probe.py (NEW), tests/test_tools.py (NEW), tests/test_store_migrations_it3.py (v5 additions), tests/test_decision_probe.py (NEW, fake mode).
- **Boundaries:** steering_queue TABLE + enqueue/pending/mark methods are a CONTRACT for WS3 — implement exactly the signatures in your brief; do NOT touch session.py, client.py, runtime.py, assembler.py.
- **Status:** active

### WS3 — steering+thinking+client (wip/harness-steering)
- **Timestamp:** 2026-08-14T02:10Z
- **Task:** SteeringQueue (boundaries idle/after-tool/after-reply, enqueue/delivery timestamps, one-shot + seen-marker, re-queue on interrupt, restart persistence via store backend protocol); client.py native tool calling + reasoning_content extraction + effort param + capability detection; FakeClient extension.
- **Files/dirs:** harness/steering.py (NEW), harness/client.py, tests/test_steering.py (NEW), tests/test_client.py (extend).
- **Boundaries:** steering.py depends ONLY on a backend protocol (enqueue_steer/pending_steers/mark_steer_delivered) — implement the protocol + an in-memory fake for tests; the SQLite implementation lands in WS2's store.py and wires in WS4. Do NOT touch store.py, session.py, runtime.py, assembler.py, tools.py.
- **Status:** active
