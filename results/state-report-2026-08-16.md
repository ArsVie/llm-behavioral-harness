# State report — spikes, waves, decisions (2026-08-16)

Consolidated snapshot across the session's parallel work. Facts verified at
source where load-bearing; unreported items marked as such, not guessed.

## TL;DR
- **Wave 1**: CLOSED, gate green (1322 tests, exit 0). Real-time substrate live.
- **Codebook (spike-2)**: SHELVED — decisive, honest null. Not an instrument
  artifact (renderer control passed the same bar the codebook failed).
- **DeepSeek alpha (spike-5)**: DONE — 30-row memo, top-3 actionable.
- **AFK/double-text (spike-4)**: DESIGNED — draft 6-gate pre-reg, pending 2 tiny
  decisions from you.
- **Bubbling (WS-B)**: result in — *direction* confirmed, *magnitude* is
  instrument-bound, *delimiter* still undecided. Uncommitted on a branch.
- **Spend capture (WS-D)**: BLOCKED — 429 weekly limit on deepseek-v4-flash.
- **HARD BLOCKER**: all API work on deepseek-v4-flash is rate-limited (~7 h reset
  or enable balance). Local-judge work is unaffected.

---

## Results (what we now know)

| Work | Verdict | Evidence | Trust |
|---|---|---|---|
| Codebook value→words | **SHELVE** | pooled acc 0.31 (qwen) / 0.28 (gemma) / 0.41 (qwen8b) ≈ chance 0.33; on primary actor codebook Δ **−0.300** vs renderer, CI excludes 0 (worse, wrong sign); renderer control passed G-ABS at 0.711 | High — control discriminates, so the codebook surface is the broken link, not the judge |
| DeepSeek harness alpha | **Mine, don't ship** | 30 ranked patterns, top-3 below; zero `cache_control` (caching is structural); v4-flash answers entirely in the reasoning channel | High — top-12 citations spot-checked at `file:line` |
| Bubbling naturalness | **Direction yes, size no** | newline Δ +5.85, blank +5.73, enter +5.40, all CIs exclude 0; but unsplit scoring ~2-3/10 is not a human effect; 3 delimiters overlap in CI | Medium — rubric punishes bad splits (calib 8/10) but over-penalizes single-message delivery |

### Codebook rethink (for the next pre-registration, not now)
Surface fidelity first (the token-soup renderer is the suspect), or bigger actor,
or ground the geometry on judge-classified behavior instead of human VAD.

### DeepSeek alpha — top 3 to act on
1. **Static-prefix / frozen-header cache discipline** + a `cached_tokens > 0`
   assertion — biggest lever on spend *and* latency; match the model's native shape.
2. **Serializer null-hardening for reasoning-only turns** — v4-flash can answer
   entirely in the reasoning channel; `content:""` never `null` or it 400s.
   Must land before the internal-thoughts spike ships.
3. **Purpose-tagged cheap-call policy**, thinking OFF for all side-work.
   - *Gateway watch*: `thinking` toggle, `reasoning_effort`, `reasoning_content`
     passback, `usage.prompt_tokens_details` all need opencode passthrough probes.

### Bubbling — what's actually decided
- Ship bubbling as a **direction** (splits ≥ no-split, robustly; bad splits penalized).
- **Do not** let +5.85 become a claim outside this rubric.
- **Do not** pick the delimiter on these overlapping numbers — decide it on
  `followed%` / `stray` / leak-rate + *which token Telegram renders as a real
  bubble* + *which survives the opencode gateway* (likely `\n`, not `<enter>`).
- Confound to close before ship: split = bubbles **+** `[pause]` markers, bundled;
  re-judge with pauses stripped, or eyeball N=10 yourself.

---

## Status board

### Waves
| Item | State |
|---|---|
| Wave 1 (real-time substrate, S1/S2, state-card, W4 harness) | **CLOSED**, gate green |
| WS-A (enable commands / debounce / sent_at) | not yet reported run |
| WS-B (delimiter/bubbling) | **result in**, uncommitted on `wip/wsb-delimiter`, machinery ready |
| WS-C (LILY_TOKEN / JUDGE_GENERATOR_TOKEN split) | not yet reported run |
| WS-D (usage + cache capture, v7→v8, spend report) | **BLOCKED on 429** mid-dispatch |

### Spikes registry
| # | Spike | State |
|---|---|---|
| 1 | Internal-thoughts marker | **READY** — un-run; needs API (blocked by 429) + serializer hardening first |
| 2 | Behavioral-signature detectability | candidate — near-free (reuses W4), no API dependency |
| 3 | Prompt-cache structure | candidate — pairs with WS-D; informed by alpha top-1 |
| 4 | AFK / double-message / no-goodbye | **DESIGNED** — draft pre-reg, pending 2 decisions |
| 5 | DeepSeek alpha extraction | **DONE** — memo delivered |
| — | Codebook re-gate (spike-2) | **SHELVED** |

---

## Decisions pending from you

1. **WS-D unblock** — wait for the ~7 h weekly-limit reset on deepseek-v4-flash,
   or enable balance to continue now. Everything API-dependent (internal-thoughts,
   WS-D) waits on this. *Local-judge and sim-only work does not.*
2. **AFK (spike-4)** — two cheap calls before I write the full pre-registration:
   (a) accept that an armed double-text can itself trip `closing_tendency` and end
   the conversation it was sustaining (I recommend accept — it's a natural close);
   (b) confirm `P_DOUBLE_TEXT=0.5` is spike-only and must not become the ship
   default (product likely wants mood-conditioned firing).
3. **Bubbling delimiter** — pick on operational metrics + Telegram/gateway reality,
   not naturalness. I can pull `followed%`/`stray`/leak from `wsb_results.json` to
   rank the candidates.
4. **WS-B ledger** — confirm whether WS-B's DeepSeek generations were counted by
   WS-D (WS-D hadn't merged when they ran); if not, the spend ledger has a small gap.
5. **Still-open inputs for WS-C/WS-D** — per-model pricing incl. the cached-input
   rate; whether the two token lanes hit the same gateway or different providers.

## Suggested order (given the 429)
While API is rate-limited, the unblocked-now work is: **spike-2 behavioral-signature**
(no API), **finalize the AFK pre-reg** (needs only your 2 decisions), and the
**bubbling operational-metric readout** (local files). Hold internal-thoughts and
WS-D until the limit resets or balance is enabled — and land the serializer
null-hardening (alpha top-2) before internal-thoughts runs regardless.

## Discipline held
No pushes; no merges to main. Third-party repos mined as patterns-not-code. Token
values never printed/committed; `.env`/`*.env` gitignored + 600. Never-diverge
intact (production model, replay-pinned). Registry corrected for the phantom
idle-close (only silence-close in code is `USER_LEFT_THRESHOLD_H=12.0`,
[session.py:168](../harness/session.py:168)).
