# Thermo-Nuclear Code Quality Review — Consolidated Findings

Date: 2026-08-28
Repo: llm-behavioral-harness @ e928ccb (provider switch landed same day)
Method: 3 parallel read-only audit subagents (tests / system / feature-seams),
thermo-nuclear mandate, "seams that support future features must be preserved."
Full reports: /tmp/thermo-review-tests.md (146 lines), /tmp/thermo-review-system.md
(173 lines), /tmp/thermo-review-seams.md (132 lines).

---

## Executive Summary

The codebase is disciplined where it matters most — replay parity, RNG stream
discipline, additive migrations, credential no-fallback, and the seam-guarding
test pins are all genuinely intact, and the provider switch broke none of them.
The structural debt is accretion: every feature wave landed as additive flag-gated
layers, leaving two 2,000+-line god objects (session.py, store.py), a 103-file
test suite with no shared helper layer (~2,000 lines of copy-paste), and three
unfinished seams the BACKLOG already declares open.

Verdict: NO critical correctness defect. High-leverage fixes are all
behavior-preserving. Do them in this order.

---

## TIER 1 — Integrity fixes (small, additive, unblock future work)

1. **[T2/System-C7] IntentResolver default RNG is bare `stream_rng(0)`**
   - proactive.py:149 — `self._rng = rng if rng is not None else stream_rng(0)`
   - Key 0 is DAILY_STREAM space. Any future draw on key 0 collides.
   - Fix: default to EXPERIMENT_STREAM (2) or raise if unset. 1-line.

2. **[T3] Judge calls ride the PRODUCT-lane client in the sim entries**
   - sim/run_async.py:363 + run_interactive.py:140 build ONE product-lane client
     for both actor AND judge; Session.finalize_day calls `self.judge(transcript,
     self.client, ...)` (session.py:669). A live `--feedback` run attributes judge
     spend to the product lane and uses the product token for judge calls.
   - The affection-score judgment at conversation close (G1) inherits this site.
   - Fix: build the judge client on the research lane; wire finalize_day to it.

3. **[T1/D1] RNG stream register lives only in prose outside the frozen file**
   - engine/rng.py documents 0-3; 4-7 live as scattered constants in
     life.py/persona.py/session.py; stream 8 is claimed by the AFK design note
     but enforced by nothing. Future features (compaction, inactivity, affection
     draws) MUST reserve keys deliberately.
   - Fix: additive comment block (or STREAM_KEYS tuple) in engine/rng.py —
     comments only, byte-identical behavior, replay untouched.

## TIER 2 — Code-judo (behavior-preserving simplification, ~2-4h each)

4. **[Tests-C1] One `tests/helpers` module** — deletes ~2,000 lines of duplicate
   `_store` (13×), `_session` (13×), `_rows`/`_ground_agenda`/`_agenda_item`,
   FakeApplication, SeamStore/FakeStore; resolves the inter-test import tangle
   (10 files importing from test_proactive/test_channel_telegram) and the
   `abs()` semantic fork in `_rows`. Fix the frozen-conftest artifact.

5. **[System-C1] Split session.py (2578 lines)** — negotiation state machine
   (2033-2501) → own module; steering/decision execution (1396-2027) → own
   module; conversation lifecycle (873-1244) → conversation.py. 46 hasattr
   guards + 3 inspect.signature probes collapse into construction-time
   feature detection.

6. **[System-C2] Split store.py (2178 lines)** — migrations+DDL (165-688) →
   store_schema.py; semantic projections (load_user_model, resolve_intent_source,
   _category_from_key) → domain.py. Target ~1200.

7. **[System-C3] Delete or wire the second prompt-assembly path**
   (assembler.build_context_messages 723-774 has ZERO callers outside
   assembler.py; session mainline hand-builds {role,content} lists at
   1509-1512 + 1971-1973). One of the two must go — the WS-D cache-ordered
   path is the compaction foundation, so wiring the mainline to it is the
   right move (see seams G-seam-11 gap).

8. **[System-C4] Delete tools.py:632 `map_defer_turns`** — line-for-line copy of
   negotiation_state.py:184 `map_defer_n`; no import cycle blocks the fix.

## TIER 3 — Cleanup (fast, low-risk)

9. **[System-Judo-7] Delete dead code**: negotiation_state INFORM/DECIDE_DECISION_PREFIX
   (zero importers), negotiation_contract USER_LEFT_THRESHOLD_H_REF alias,
   concurrency.shutdown_executor (no callers), commands._COMMAND_NAMES,
   interests.sample_adjacent/indepdendent (test-only).
10. **[System-Judo-2] Collapse 3 retry loops in client.chat_with_meta** into one
    classify-reply helper.
11. **[System-Judo-5] One judgement-score parse** — 4 copy-pasted float parses →
    store.load_judgement returns float.
12. **[Tests-C4] De-brittle pins**: delete `SCHEMA_VERSION == 8` literal pin
    (test_store_it2.py:232, migration tests own it); delete test_life.py:595
    source-text forbidden-scan (linter work; determinism already pinned
    behaviorally); verify test_validation message pins are codes not cosmetics.
13. **[Tests-C3] Slow-suite operational wins**: @pytest.mark.slow split; 0.3s
    sleep sites → ManualClock pattern (test_runtime_anchor.py:86 already proves it).
14. **[Seams-T5] Clean stale opencode strings**: it3_manifest.py:111 labels
    research lane "via opencode-go"; cvs_g6.py:137 probe text. Keep
    JUDGE_FAMILIES indirection (multi-provider seam).
15. **[Seams-T6] Add commandcode reasoning variant to `_extract_reasoning`**
    (completion_tokens_details.reasoning_tokens) before the internal-thoughts
    spike depends on reasoning capture.

## Gaps for planned future work (BACKLOG-mapped, NOT blockers)

- G1: Affection score — no storage home, no update path, no gate. v9 additive
  migration is the proven framework; close-checkpoint (session.py:1188-1211)
  is the natural update site.
- G2: away/presence signal exists (is_user_away, session.py:972-990) but wired
  into NONE of its three targets (by plan): skip-inform plug = session.py:2171-2175;
  proactive gate = runtime.py:880-897; double-text = new harness/inactivity.py
  + INACTIVITY_STREAM=8.
- G3: closing tendency — flag OFF, draw seam replay-safe, redesign is a design
  decision not code.
- G4: compaction — no trigger; raw-recent tail capped at RECENT_TURNS=12
  (assembler.py:113) must lift to budget-based when spec'd.
- G5: real token numbers — capture exists (llm_calls v8 usage); analysis
  consumer absent.
- G6: /setup wiring lives in run_async only; live entry lacks it.
- G7: command expansion seam present (CommandContext + hooks).

## Preserved (do NOT touch)

Two-lane credential resolver, RNG stream discipline (after T1/T2), tunables
env-gating, migration additivity + SCHEMA_VERSION, replay-parity pins
(PARITY_PIN sha256, unanchored-v7 byte parity), forbidden-token battery
(consolidate location only), fork-able client protocol (supports_json/tools),
judge shadow mode, L4 canonical taxonomy, engine/ frozen surface,
behavioral_signature 8-metric contract, WS-D stable-prefix/volatile-tail cache
order, anchor/clock-skew loud-failure contract.
