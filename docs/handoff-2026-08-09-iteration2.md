# HANDOFF — llm-behavioral-harness Iteration 2 (2026-08-09, ~02:50)

Status: ORCHESTRATOR PAUSED at user request (subagent iteration cap being raised). All work below is verifiable from git; nothing is hypothetical.

## 0b. WAVE-3 COMPLETE (2026-08-09, resumed session — READ FIRST)

GATE CHAIN STATUS (07:30 UTC): G2 PASS (43ec7c2, 5/5 seeds, audit TOCTOU clamp fixed), G3 PASS (476bc84, 120d soak ×2 seeds, 5 restarts), G4 PASS (5a758ed manifest; NOTE openai-mini family needs OPENAI_API_KEY which is ABSENT — limitation), G5 PASS (b5cdd52 E0 review). G6: probe resolved (env mapping 0fcd062, hollow-run hardening, validator day-aware 2a20137, traces generator 28eca0e/9b5dc52) — 35-cell real-LLM matrix RUNNING; judges + audit runners staged at /tmp/it2-g6-{judges.sh,audit.py}.

Both Wave-3 tracks LANDED and merged in plan order. Main now at `aa3c01c` — full suite **796 green** (verified post-m10; 750 after m9 = 658 base + 92 A9, since the base 707 already included the 49 superseded vslice adversarial tests).

- **m9 = A9 adversarial battery + R1-F1 fix** (merge 6aa1a7f, commits c40fbc7 + 027163f): 92-test battery (bootstrap/interests/proactivity/runtime/prompt + rewritten restart/grounding/memory/life/actuators), 91 green at commit + 1 finding test red BY DESIGN. R1-F1 (quiet-hours deferral LIVELOCK, invariants 3/17) FIXED by the A3 owner-fix branch: `_firing_loop` advances the virtual clock to the deferral target INSIDE the lock when `now - nxt < 1e-9` (rollover-parked on-schedule case), re-evaluates at the awake instant and fires; overdue-in-quiet restarts (now > nxt) keep sleep-only pacing (R-10 semantics preserved). `_quiet_defer_until` clamps to max_virtual_hours (line 537-538). Finding test 10/10, battery 92/92, full suite 750 green, no hang.
- **m10 = A8 eval harness** (merge aa3c01c, commit 8042cb9): experiments/{companion_vertical_slice,cvs_common,cvs_manifest,validation/} + tests/test_cvs_* (4237 lines). Mock vertical VALIDATED: seed 5001, 30d, 111 msgs, 27 proactive = 27 fired, checkpoints 7/14/21/26/29, ungrounded_proactive 0, counts_consistent true, OKF validated. **A8-continuation chain capped twice** (deleg_dd003484, then deleg_6a224250 finished): T1 replay fix (run_replay now reconstructs the recorded cell config — checkpoints stored in HOURS were read back as days; hours→days reconversion is the real replay-exactness fix) + chain-event texts rewritten to match the deterministic extractor; T2 root cause = **AUDIT ARTIFACT, not harness bug** (agenda item was `planned` at fire time 205.21h, skipped afterward by life close-out; audit re-checked supersession at end-of-run → TOCTOU; clamp: only flag superseded when `src.end_t_h < intent.created_t_h` is FALSE, same for LifeArc abandoned) + counts_consistent off-by-one fixed.
- A9 gate fixes by the orchestrator (test-design, no invariants weakened): DayRecord imported from engine.types (not harness.domain) + field alignment; episode fixtures register their source session (g8b) in v1a/r1b/r10; missing `import pytest` in test_adversarial_memory.py. **Lesson: episode fixtures that expect a DELIVERY must register the source session (open_session+close_session) or the gate rejects with no_source — same class as the 39fb086 fixture fix.**
- Worktrees removed: llh-wt-it2-a9, llh-wt-it2-a3-r1f1, llh-wt-it2-a8. Branches kept (wip/it2-a9, wip/it2-a3-r1f1, wip/it2-a8). `iteration-2-integration` synced to main.
- R1-F1 fix details + code anchors: brief /tmp/llh-vslice/it2-a3-r1f1.md (may be cleaned); runtime.py `_firing_loop` ~line 382-405.

**NEXT: Gates 2→6** (clean-start vertical → 120-day soak → preregistration manifest → E0 review → confirmatory real-LLM matrix as background processes + 5 causal traces, per plan §13/§17 and the A8 manifest command).

## 0. WAVE-3 BATCH COMPLETED AFTER PAUSE (deleg_d33773f8, both agents capped UNCOMMITTED — READ THIS FIRST)
Both Wave-3 agents hit the iteration cap BEFORE committing. Working-tree state is on disk — do not clean worktrees without committing first.

**A9 (wip/it2-a9, worktree llh-wt-it2-a9):** 5 modified + 5 NEW adversarial test files, ALL UNCOMMITTED (`git add tests/test_adversarial_* && git commit` needed — 38 new tests). Full inventory: modified {actuators,grounding,life,memory,restart}, new {bootstrap,interests,proactivity,prompt,runtime}. Baseline 70/70 intact (5 original files + test_proactive_it2).
- **R1-F1 (HIGH, routes to A3/A6): LIVELOCK — quiet-hours deferral + rollover park never terminates.** Test `test_r1b_quiet_deferral_of_parked_event_terminates_and_delivers` (deliberately fails fast). Repro: still-valid event at 23:30 (12h validity, outlives quiet) gets PARKED by _rollover_loop, then DEFERRED by _quiet_defer_until; the deferral sleep (`_firing_loop` ~line 441: `await asyncio.sleep((defer_until - now) * scale)`) sleeps WALL time but NEVER advances the virtual clock → park → gate → defer → sleep → re-poll forever; run never reaches max_virtual_hours. Violates invariant 17 + r4b semantics (deferred event must eventually fire or expire by policy). Fix direction: deferral must advance the virtual clock when the rollover is parked, OR the rollover must not park at events that will be deferred. Do NOT weaken the finding test.
- A9's r1a/r1c were test-design artifacts (check-in grounding + replanned day-0 leftover → spurious 'expired'); fixed by retargeting to agenda-grounded REASON_SCHEDULE events — applied but NOT re-run before cap; verify before merge.
- l9*/m9*/r10/v1*/v1a/b/a7/a8 additions written but NEVER EXECUTED — must run before the A9 gate claim.
- No other confirmed harness defects (probed: bootstrap identity, hostile interests, canonical-only categories, policy-switch ordering, determinism, expired-intent ValueError, exact-id firing, suppression-without-message, clean shutdown).

**A8 (wip/it2-a8, worktree llh-wt-it2-a8):** ALL FILES UNCOMMITTED (untracked): experiments/{companion_vertical_slice.py, cvs_common.py, cvs_manifest.py, validation/}, tests/test_cvs_{common,manifest,tracks,validate}.py.
- ⚠️ A8's LAST write to cvs_common.py FAILED (temp-file error: `.hermes-tmp.1303930` no such file) — the file on disk may be STALE/partial. VERIFY cvs_common.py integrity before resume (it exists, but may lack the final version).
- Mock vertical ran to completion (111 msgs, 27 proactive, 5 checkpoints) but validation red: ungrounded_proactive=1 + 1 more error; grounding_detail never printed → root cause TBD (audit artifact vs A3 harness bug — ROUTING RULE in §4.1).
- Track tests red at cap: test_replay_mini_exact, test_chain_events_promotable, test_deterministic_judge_perturbation_dip (partially patched).
- Continuation brief staged: /tmp/llh-vslice/it2-a8c.md (NOT dispatched; update it with the R1-F1 cross-dependency before dispatch).

**RESUME ORDER UPDATE:** R1-F1 must be fixed (A3/A6 owner) BEFORE m9 (A9 suite can't pass with the finding test red by design) and ideally before A8's mock validation (the ungrounded_proactive=1 may share the deferral/park clock path).

## 1. Authoritative contract
- `plans/iteration-2-integration-2026-08-09.md` (2042-line plan, INCLUDES §17 eval-protocol addendum: 4-dim scoring, event-chain metrics, perturbation+recovery blocks, >=2 judge families, Weibull timing FROZEN).
- Legacy slice contract: `plans/companion-vertical-slice-2026-08.md` (superseded; do not resume its A10 matrix — E0 is archived, see §5).

## 2. Repo state
- MAIN at `71ff50b` — **707 tests green** (MPLBACKEND=Agg .venv/bin/python -m pytest -o addopts="" -q, ~2 min).
- `iteration-2-integration` branch == main tip (sync with: `git branch -f iteration-2-integration main`).
- Untracked `sim.zip` on main — LEAVE ALONE (pre-existing, not ours).

## 3. Merges 1-8 (all done, full suite green after each)
| # | What | Commit |
|---|------|--------|
| m1 | A1 contracts (ContactOpportunity, canonical L4 taxonomy, MemoryPolicy) — A10 APPROVED | d71df14 (merge) / df862d7 |
| m2 | A7 persistence: messages.intent_id, canonical L4 storage, eval-mode call audit | a189692 |
| m3 | A6 concurrency: subprocess-exit regression, sleeper/executor ownership | 32647a1 |
| m4 | A4 memory: faithful 0.35/0.30/0.35 reranker, embedder/summarizer interfaces, revision | 1141d9e |
| -- | gate-fix: M-3 adversarial test re-scoped to Iteration-2 contract (topicality = experimental variant only) | c2613ab |
| m5 | A2 life: arc start-time, CurrentActivity=now, replenishment (fixes "life permanently dies"), 30/60/120d | a257e98 |
| m6 | A1b bootstrap: idempotent clean-start, user-relative 40/40/20, onboarding fallback | a6bdaf3 |
| m7 | A5 session/assembler: no dup turns, memory-as-data quoting, fire_proactive(intent_id), resume-no-rewind | 47b1a57 / 305caae |
| m8 | A3 proactivity/runtime: ContactOpportunity scheduler, exact intent identity, A6 integration, rollover clock discipline | e8ea2e5 / 231fd73 |
| -- | gate-fix: A3 fixture aligned to A5 seam (_chat(intent=...)) | 71ff50b |

Suite progression: 578 → 583 → 596 → 624 → 637 → 666 → 680 → 686 → 701 → 707.

## 4. Wave 3 — IN FLIGHT / INTERRUPTED
Delegation `deleg_d33773f8` (2 leaves, deepseek-v4-flash):
- **A9 (task-0)** — adversarial extension: was mid-matrix at last check (M9 memory attacks appended; B1/B2/P1/R1/L9/PR1/V1 files partially written). Batch did not complete before the pause. Live transcript: `/home/vruizes/.hermes/cache/delegation/live/deleg_d33773f8/task-0.log`.
- **A8 (task-1)** — eval harness: STOPPED at iteration cap (22:47:48, 1334s) with the branch UNFINISHED and mid-work. Live transcript: `/home/vruizes/.hermes/cache/delegation/live/deleg_d33773f8/task-1.log`.

### A8 known failures (verified by orchestrator)
1. `vertical --seed 5001 --fake --days 30` RUNS fully now (111 messages, 27 proactive, 5 checkpoints 7/14/21/26/29) but VALIDATION FAILED: `ungrounded_proactive: 1` + 1 more validation error. Root cause NOT determined — grounding_detail not printed. ROUTING RULE: if detail shows the runtime fired an intent with absent/superseded source at firing time → HARNESS BUG, route to A3 (owner fix); if audit artifact → A8 fixes the audit. Do NOT paper over.
2. Track tests red at cap: `tests/test_cvs_tracks.py::test_replay_mini_exact` (assert False), `test_chain_events_promotable`, `test_deterministic_judge_perturbation_dip` (A8 patched test_cvs_common.py for it — verify).
3. Earlier executor crash (`executor 'runtime' not running` — feed after run() shut the executor down) — FIXED by A8 (full run now completes).

### Ready to dispatch: A8-CONTINUATION
Brief staged at `/tmp/llh-vslice/it2-a8c.md` — NOT dispatched. Scope: repair track tests, root-cause ungrounded_proactive=1 (evidence-based: audit artifact vs A3 bug), re-validate mock vertical (validated:true), full suite green, commit experiments/* + tests/test_cvs_*.py on wip/it2-a8. NO real-LLM runs.

## 5. E0 exploratory eval
- FROZEN on branch `eval-exploratory-2026-08-08` @ `230a9e0` (broken driver + confounded partial results archived as exploratory baseline). NEVER merge fixes into it. Gate 5 = analyze it → bugs become regression tests; no threshold tuning.

## 6. Worktrees / branches inventory
- Worktrees: llh-wt-it2-a{1,7,6,4,2,1b,5,3,9,8,10} (wip/it2-*), llh-wt-a9 + llh-wt-a10 (OLD vslice, wip/vslice-* — archived, do not resume), llh-wt-a1b (old vslice branch wip/vslice-a1b).
- Merged branches (keep or delete): wip/it2-a1..a5,a1b (merged), wip/it2-a6..a3 (merged), wip/it2-a9/a8 (IN FLIGHT).
- Prior: wip/vslice-* all merged/archived; wip/vslice-a10 + eval-exploratory-2026-08-08 = E0.

## 7. Resume sequence (exact)
1. Let A9's batch tail finish or re-dispatch A9 with remaining matrix (brief /tmp/llh-vslice/it2-a9.md; findings → owner routing; commit only tests/test_adversarial_*).
2. Dispatch A8-CONTINUATION (brief /tmp/llh-vslice/it2-a8c.md).
3. Merge m9 (A9) then m10 (A8) in plan order, full suite after each, `git branch -f iteration-2-integration main` after each.
4. A9 findings with harness-owner routes → owner-fix agents (pattern: branch from failing commit, may NOT weaken spec; orchestrator gate fixes for superseded-contract tests only).
5. Gate 2: clean-start vertical slice (blank DB → coherent companion; mechanical checks; NO fixture inserts). Gate 3: 120-day deterministic soak with restarts. Gate 4: eval preregistration manifest (A8's manifest command; incl. §17 requirements). Gate 5: E0 review. Gate 6: confirmatory eval (real LLM, OPENCODE_GO_API_KEY sourced from ~/.hermes/.env, never printed). Deliverables: 10-item package incl. 5 machine-generated causal traces.

## 8. Environment / conventions
- VENV: /home/vruizes/.hermes/projects/llm-behavioral-harness/.venv/bin/python (MAIN venv — worktree tests run with this, from the worktree root).
- Canonical: MPLBACKEND=Agg .venv/bin/python -m pytest -o addopts="" -q. Matplotlib only in sim/plots.py + experiments, always Agg.
- Delegation: batch <= 10; leaves ~2000s cap (USER IS RAISING IT — A8's cap stop is the reason for this handoff); briefs in /tmp/llh-vslice/, short pointers; denials HALT leaves.
- Strict ownership: A1 domain/bootstrap/persona/interests; A2 life; A3 proactive/scheduler/runtime/gates; A4 memory/embeddings/summarization; A5 session/assembler; A6 concurrency; A7 store/migrations; A8 experiments/*; A9 tests/test_adversarial_*; A10 reviews (read-only).
- Real-store contract: rowid order, most-recent-any-status get_assertion; gates: source session must exist (g8b); no cycle labels (phase_label/cycle_day/mu/eta/g); deterministic seeded RNG; no real-clock reads below runtime.
- EOL: repo is native WSL (LF fine). /mnt/c repos need git.exe/gh.exe (Windows) — not applicable here.
- Docs in EN + OKF convention; experiment code: Spanish docstrings, English identifiers, fixed seeds in titles.

## 9. Open findings ledger (do not lose)
- ungrounded_proactive=1 (A8 mock vertical) — TBD: audit artifact vs A3 harness bug (see §4.1).
- A8 track tests (replay/chain/judge) — A8-continuation.
- A1b-surfaced restart rewind defect — FIXED by A5 (resume-no-rewind; regression test landed).
- M-3 test re-scope — done (c2613ab), contract-aligned, do not revert.
- A7 note: store's load_user_model vs memory.py key conventions historically disagreed — canonical storage landed; full alignment = A4's (done) + verify in Gate 2.
- Weibull timing: FROZEN this sprint (plan §17.5) — do not let anyone "improve" it.
