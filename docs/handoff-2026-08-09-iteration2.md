# HANDOFF — llm-behavioral-harness Iteration 2 (2026-08-09, ~02:50)

Status: ORCHESTRATOR PAUSED at user request (subagent iteration cap being raised). All work below is verifiable from git; nothing is hypothetical.

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
