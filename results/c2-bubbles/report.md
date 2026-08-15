# C2 — Mood-paced multi-bubble replies: offline splitter experiment

- **Plan ref:** `plans/advisor-orchestration-2026-08-15.md`, Part 3, section C2
- **Worktree:** `llh-wt-c-bubbles` (branch `wip/c-bubbles`, HEAD `79c9b30`)
- **Date:** 2026-08-15
- **Script:** `experiments/c2_bubbles.py` (offline splitter; adds files only under `experiments/` and `results/`)
- **Data files:** `results/c2-bubbles/c2_bubbles_results.json`, `results/c2-bubbles/judge_scores.json`
- **Seed:** 5001 (splitter jitter, gap draws, judge-order shuffle, bootstrap CIs)

---

## 1. Methodology

### 1.1 Corpus

| Source | Planned (plan §C2) | Actual | Note |
|---|---|---|---|
| Live (`results/live-companion/companion.db`, **read-only**, URI `mode=ro`) | 84 exchanges | **6 assistant replies** | see §5 blocker |
| Sim (`FakeClient` + `RecordingSession` via `experiments.cvs_common.make_session`, seed 5001) | 30 replies | 30 replies | scripted pool of 30 multi-sentence Ana-style replies, 5 virtual days × hours {9,12,15,18,21,23} |
| **Total** | **114** | **36** | mechanical check runs on n=36 |

The live DB is opened exclusively in read-only mode; the experiment never writes to it (SQLite WAL supports concurrent readers — the live bot process kept running throughout).

### 1.2 Expressiveness channel (read-only)

The gap/burst split is driven by the harness's own expressiveness directive:

- **Live:** recomputed deterministically with `harness.behavior.derive_behavior(DayRecord ← daily_state row, TimingParams(), hour = t_h % 24)` → `BehaviorDirective.expressiveness` (same function the live session uses; no harness code was modified).
- **Sim:** read from `TurnResult.directive.expressiveness` produced by the sim machinery.

Observed ranges: live e = 0.418 (all six at hour 0.0, midnight); sim e ∈ [0.338, 0.809].

### 1.3 Splitter under test (offline)

- Bubble count `k = min(1 + floor((e − 0.15)/0.22), 4, n_sentences)`; with probability 0.30 a jittered +1 bubble (seeded RNG) when room remains. Low expressiveness → single message; high expressiveness → burst of up to 4.
- Split points fall **only at sentence boundaries** (custom segmenter with abbreviation/decimal/ellipsis protection; `\n\n` paragraph breaks only count when the boundary rule holds).
- Inter-bubble gaps: `gap_s ~ lognormal(μ = ln(0.5 + 2.2·e), σ = 0.35)`, clipped to [0.3, 6.0] s — the gap magnitude is the expressiveness-driven pacing signal (mean ≈ 0.76 s at e=0.12 → 2.59 s at e=0.95).

### 1.4 Legs and success criteria (plan §C2)

1. **Mechanical check — zero mid-sentence splits.** Independent validator over the rendered bubble strings (not the splitter's bookkeeping): every boundary must have (a) sentence-terminal punctuation `. ! ? …` at the left tail (after stripping closing quotes/brackets/stage-direction asterisks) and (b) a right head starting with uppercase/digit/quote/dash/asterisk. **Planned n=114 (84 live + 30 sim); run on n=36 available.**
2. **Judge-scored naturalness, paired n=30 (sim replies).** Each reply rendered twice: unsplit (single message) and split (bubbles with explicit pauses), 2 blind LLM-judge calls per pair (order shuffled, judge never told which is split), temperature 0, JSON score 1–10 on *delivery* naturalness. Paired Δ = score_split − score_unsplit; success: **mean Δ ≥ 0 and bootstrap 95% CI lower bound ≥ −0.05** (10 000 resamples, percentile).
3. **gap-vs-expressiveness Spearman ρ ≥ 0.5** over the full corpus (gap = mean inter-bubble gap; 0.0 for unsplit replies), bootstrap 95% CI. Split-only subset reported as secondary.

Judge client: harness `OpenAICompatibleClient`, `LLM_BASE_URL=https://opencode.ai/zen/go/v1/`, model `deepseek-v4-flash` (harness default), `LLM_API_KEY` resolved from the session env (recipe `set -a; . ~/.hermes/.env; set +a` per orchestration context; `.env` itself carries no LLM_* keys — they resolve from the running session). 60 calls, 0 failures, scores cached in `judge_scores.json`.

---

## 2. Results

### 2.1 Criterion 1 — zero mid-sentence splits (mechanical)

| Metric | Value |
|---|---|
| Replies checked (n) | 36 (6 live + 30 sim; planned 114) |
| Total split boundaries | 49 |
| Mid-sentence violations | **0** |
| **Verdict** | **PASS** (on available corpus) |

Every one of the 49 boundaries validates as sentence-terminal-left / sentence-start-right. Sample: `"Hey. *pauses…* Good to see you."` → `"How's your day treating you so far?"`.

### 2.2 Criterion 2 — judge-scored naturalness, split ≥ unsplit (n=30 paired)

| Metric | Value |
|---|---|
| Pairs scored | 30 (all sim replies; 0 judge failures) |
| Mean score, unsplit | 9.53 / 10 |
| Mean score, split | 9.03 / 10 |
| **Mean paired Δ** | **−0.500** |
| Bootstrap 95% CI (percentile, 10k) | **[−0.783, −0.217]** |
| **Verdict** | **FAIL** |

The real LLM judge rates both renderings as highly natural in absolute terms, but the multi-bubble renderings score consistently *below* the single-message baseline (diff distribution: 13 negatives, 12 zeros, 5 positives; worst −2.0, best +1.0). The plan bar — mean Δ ≥ 0 **and** CI lower bound ≥ −0.05 — is not met on either condition (mean is negative and the CI lies entirely below −0.05).

**Interpretation (hypothesis, not claim):** the offline rendering with explicit pauses reads as *more* deliberate/annotated than a single message; the splitter itself never splits mid-sentence (criterion 1) and is absolutely "natural" (9.03/10), but on this judge+rubric it does not beat the unsplit baseline. This is the experiment's finding: **the feature as specced fails the plan's naturalness bar on the sim corpus.**

### 2.3 Criterion 3 — gap-vs-expressiveness Spearman ρ

| Metric | Value |
|---|---|
| ρ over full corpus (n=36; gap=0 for unsplit) | **0.544** |
| Bootstrap 95% CI | [0.266, 0.736] |
| ρ split-only (n=35) | 0.504 (CI [0.218, 0.705]) |
| **Verdict** | **PASS** (ρ = 0.544 ≥ 0.5) |

The realized gaps track the expressiveness directive as designed (monotone mean with lognormal noise); the correlation survives the zero-gap unsplit cases. Note the CI lower bound sits below 0.5 — the point estimate passes the plan bar, the CI does not exclude weaker correlations.

---

## 3. Verdict summary

| # | Criterion (plan) | Result | Verdict |
|---|---|---|---|
| 1 | Zero mid-sentence splits (mechanical, n=114) | 0 violations / 49 boundaries, **n=36 available** | **PASS** (with corpus caveat) |
| 2 | Judge naturalness split ≥ unsplit (paired n=30) | mean Δ = −0.500, 95% CI [−0.783, −0.217] | **FAIL** |
| 3 | ρ(gap, expressiveness) ≥ 0.5 | ρ = 0.544, 95% CI [0.266, 0.736] | **PASS** |

**Plan consequence (C2):** success requires **all** criteria; criterion 2 fails → the experiment does **not** clear the gate. Per plan §C2, the feature must **not** move to channel-side actuation ("Pass ⇒ channel-side actuation only") on this evidence.

---

## 4. Blockers & deviations

1. **Live-corpus shortfall (main blocker):** the plan assumes 84 live exchanges in `results/live-companion/companion.db`. At run time the DB contains **14 messages / 6 assistant replies** — it was recreated 2026-08-15 03:30 (during the live round-trip fix, commit `79c9b30`) and currently holds only the round-trip test script (all at day 0 / t_h 0.0). The mechanical check therefore ran on **n=36 (6+30) instead of the planned n=114**. No substitute data was used — criterion 1's sample size is honestly reported as short. Re-run when the live corpus has ≥84 exchanges (the splitter, checks, and judge cache are all deterministic and idempotent).
2. **Background-shell env:** LLM judge calls must run in a foreground session — the background wrapper resets `LLM_API_KEY`/`LLM_BASE_URL`. No impact on results (judge leg ran successfully in foreground).
3. **Criterion-1 verdict wording:** PASS on the available 36-reply corpus; the planned n=114 was unattainable (blocker 1), so the verdict is provisional on corpus size.

## 5. Reproducibility

```
cd llh-wt-c-bubbles
.venv/bin/python -m experiments.c2_bubbles            # full run (judge leg needs session env)
.venv/bin/python -m experiments.c2_bubbles --skip-judge   # offline legs only
```

Deterministic: seed 5001 (splitter jitter, lognormal gaps, judge order, bootstrap); judge scores cached in `judge_scores.json` (keyed by SHA-1 of rendering); sim replies verified aligned with the scripted pool (assertion in script); live DB read-only (`mode=ro`).
