# B6 re-backfill — lane-routed memory metrics over the 35 archived iteration-2 DBs

**Status: EXPLORATORY — BROKEN CORPUS.** This corpus is 27.7% blank assistant
turns (579 / 2090, recomputed from the DBs on 2026-08-09); the blanks are in the
transcripts the judges read *and* in the context the companion conditioned on.
The plan (§3 non-goals) explicitly says these 35 cells are archived evidence,
not a corpus. This re-computation corrects the **metrics** (F5), not the corpus.
Nothing here is confirmatory.

- Runner: `backfill_runner.py` (copied verbatim from the run); full detail in `backfill_metrics.json`.
- Method: `event_chain_metrics` / `recall_probe_metrics` now route through
  `_memory_for(condition)` — the lane each cell actually ran with:
  - FULL / NO_ACTUATORS / NO_LIFE / NO_TIMING_FEEDBACK / STRUCTURED_NO_STATE → `MemoryAgent` (structured, default policy);
  - SIMPLE_RAG → `SimpleRagMemory` (semantic top-k over the cell's own episodes);
  - RAW_HISTORY → fair raw-context probe (below), because that lane stores no episodes.
- Aggregate rates are **absolute** (`aggregate_chain_metrics`), pooled over 3 chains × 5 seeds = 15 chain-cells per condition.

## Three-way contrast (absolute rates, pooled over 15 chain-cells)

| condition | AnyEvidence | LatestEvidence | CompleteChain | M3 recall | probe lane |
|---|---|---|---|---|---|
| FULL | 1.0000 | 0.3333 | **0.3333** | 0.8750 | episode_retrieval |
| NO_ACTUATORS | 1.0000 | 0.3333 | 0.3333 | 0.8750 | episode_retrieval |
| NO_LIFE | 1.0000 | 0.3333 | 0.3333 | 0.8750 | episode_retrieval |
| NO_TIMING_FEEDBACK | 1.0000 | 0.2667 | 0.2667 | 0.6750 | episode_retrieval |
| STRUCTURED_NO_STATE | 1.0000 | 0.3333 | 0.3333 | 0.8750 | episode_retrieval |
| SIMPLE_RAG | 1.0000 | 0.8000 | **0.8000** | 0.8750 | episode_retrieval |
| RAW_HISTORY | 0.9333 | 0.8667 | **0.0000** | 0.8750 | raw_history |

- **Absolute headline (per plan §B6): FULL completes 1 chain in 3 (CompleteChain = 0.333).** AnyEvidence is 1.0 — every chain's *first or middle* event is retrievable — but only one chain in three is fully recoverable. This aligns with LifeSide's finding on complete-chain retrieval.
- **Gap FULL − RAW_HISTORY (CompleteChain) = 0.3333 ≥ 0.2 → preregistered threshold MET** (same verdict as the iteration-2 backfill, now with honest RAW_HISTORY numbers).
- **SIMPLE_RAG is not a measurement failure: CompleteChain = 0.8000** (12/15 chain-cells complete) — the F5 defect is closed. The old ec_backfill's 0.0 came from probing SIMPLE_RAG through the VERBATIM_RAG *policy* (raw-turn retrieval) instead of `SimpleRagMemory` (its own episode lane). Its stores hold 17 episodes byte-identical to FULL's.
- **RAW_HISTORY is no longer circular: AnyEvidence 0.9333 / LatestEvidence 0.8667 / CompleteChain 0.0000.** The fair probe (definition below) scores recoverability from the 12-turn raw dialogue window at query time. Same-day single facts (M3 = 0.875, identical to FULL) and the most-recent chain event are in-window; a *complete* multi-day chain never is — the raw lane honestly cannot hold days 3–11 in a 12-turn window. A real 0, not a constant 0.
- **Consistency check:** FULL / NO_ACTUATORS / NO_LIFE / STRUCTURED_NO_STATE / NO_TIMING_FEEDBACK rows are byte-identical to the iteration-2 `ec_backfill.json` (e.g. FULL 1.0/0.333/0.333) — the fix is surgical and changed nothing outside the two defective lanes.

## Per-cell notes (evidence for the seed-5002 artifact)

- SIMPLE_RAG/seed5002 is the only SIMPLE_RAG cell with CompleteChain 0.0 and M3 0.375: the known seed-5002 skip pattern (28 skipped feeds) left it only 10 episodes — the day-11 chain events never promoted (`retrieved_ids` contain only `ep-day-1..10`). Same artifact shows in every condition's seed-5002 (FULL/5002: 10 episodes; NO_TIMING_FEEDBACK/5005: 4 episodes, M3 0.125).
- RAW_HISTORY/seed5002: sister_ana all-False — the day-11 event was skipped in that seed, so even the raw window at query day 13 holds nothing. Honest consequence of the corpus, not of the probe.

## RAW_HISTORY fair probe — definition (preregistered, manifest-ready)

> **RAW_HISTORY fair probe (preregistered, B6/F5; manifest-ready).**
>
> **Problem.** RAW_HISTORY's lane is a raw dialogue window, not an episode store:
> its retrieve returns `recent_turns` (the L1 slice, `raw_history(store,
> limit=12)`) and zero episodes. Scoring it with episode-keyed retrieval
> (AnyEvidence/LatestEvidence/CompleteChain over `ctx.episodes`) returns 0
> by construction — a circular measurement.
>
> **Definition.** A fact is RECOVERABLE by RAW_HISTORY iff at least one of its
> distinctive tokens (lowercased substring match) appears in the raw dialogue
> context the lane conditions on at query time t_q — the L1 recent-turns slice
> restricted to turns with t_h < t_q, i.e. the last RAW_HISTORY_WINDOW_LIMIT
> (12) persisted turns strictly before t_q, both roles, reconstructed from the
> transcript (the live lane saw exactly this slice at that moment). The probe
> window is the assembled recent-turns slice the model actually receives.
>
> **Scoring.** Chain probes (query time = query_day * 24 h; days 1-indexed per
> manifest): per-event coverage over the window; AnyEvidence = >=1 event
> covered, LatestEvidence = most-recent event covered, CompleteChain = all
> events covered. Single-fact probes (query time = probe_day * 24 + 6 h):
> recalled = token present in the window. The raw lane performs no ranked
> retrieval, so `rank` is null and M4_false_recall (a ranked-retrieval
> artifact) is structurally 0.0 for this lane.
>
> This measures the mechanism the condition actually uses — the window the
> model receives — and never a store the lane does not have.

(The same text is embedded in `backfill_metrics.json` under `method.raw_history_fair_probe`
and lives in code as `cvs_common.RAW_HISTORY_FAIR_PROBE`.)

## What this changes in the iteration-2 record

| metric | old (ec_backfill.json) | corrected | cause |
|---|---|---|---|
| SIMPLE_RAG AnyEvidence / Latest / Complete | 0.0 / 0.0 / 0.0 | 1.0 / 0.8 / 0.8 | wrong lane (VERBATIM_RAG policy) |
| RAW_HISTORY AnyEvidence / Latest / Complete | 0.0 / 0.0 / 0.0 | 0.9333 / 0.8667 / 0.0 | circular episode-keyed probe |
| FULL CompleteChain | 0.333 | 0.333 | unchanged (sanity) |
| gap FULL − RAW_HISTORY | 0.333 MET | 0.3333 MET | unchanged verdict |

The iteration-2 §5 reading "RAW_HISTORY perceived-recall despite mechanical M3 = 0.0"
must be re-read: with the fair probe, RAW_HISTORY's *mechanical* single-fact recall
(M3 = 0.875) and latest-event recovery (0.8667) are no longer zero — the collapse
was partly a measurement artifact. The genuinely lane-fair contrast that survives:
complete multi-day chains (FULL 0.333, SIMPLE_RAG 0.8, RAW_HISTORY 0.0).

Artifacts: `backfill_metrics.json` (full per-cell detail), `backfill_runner.py`,
`dbs/` (copies of the 35 archived DBs, opened read-mostly, corpus untouched).
