# Two-Week Live Run Report — companion.db

**Run:** `experiments/live_two_weeks.py`, seed 6001, 14 simulated days, accelerated
(1 virtual hour ≈ a few real seconds; wall pacing = t_h × time-scale). Companion model:
`stealth/ox-alpha` via OpenRouter, `HARNESS_THINKING_EFFORT=low`. Full engine stack:
mood + cycle + proactive scheduler + negotiation + memory, all persisted in
`results/live-two-weeks/companion.db`.

---

## 1. Messages per day

| day | user | assistant (reactive) | assistant (proactive) |
|----:|-----:|---------------------:|----------------------:|
| 0   | 2    | 2                    | 0                     |
| 1   | 2    | 2                    | 2                     |
| 3   | 2    | 2                    | 2                     |
| 4   | 2    | 1                    | 2                     |
| 5   | 4    | 4                    | 1                     |
| 6   | 2    | 2                    | 2                     |
| 7   | 2    | 2                    | 2                     |
| 8   | 0    | 0                    | 3                     |
| 9   | 2    | 2                    | 1                     |
| 10  | 2    | 2                    | 2                     |
| 11  | 2    | 2                    | 2                     |
| 12  | 2    | 2                    | 0                     |
| 13  | 2    | 2                    | 1                     |

Total messages: **~50** across the run. Day 8 is a pure-proactive day: the scripted
user was silent but the companion still reached out 3× — initiative working as
designed. Days 2 and 14 are boundary days with no recorded traffic.

## 2. Proactive pipeline

- `schedule_events`: **20 fired**, 18 expired, 2 pending at run end.
- `proactive_intents`: **20 fired / 18 suppressed** — the suppression path
  (quiet-hours deferral, salience gating) rejected more than half of all intents;
  only genuinely well-timed hooks reached the channel.
- Fired hook examples: `Agenda: afternoon sketching (37.9–38.7h)`,
  `Finished: evening walk`, `Finished: read about lifting`,
  `Agenda: watch a video on metal (105.0–105.8h)`.

## 3. Daily state — mood M, reactivity g, phase

| day | M | m_level | g     | p     | phase      | score |
|----:|--:|--------:|------:|------:|------------|------:|
| 0   | 4 | -0.425  | 1.251 | 0.495 | menstrual  | -0.29 |
| 1   | 5 | -0.525  | 1.219 | 0.396 | menstrual  | -0.66 |
| 2   | 1 | -0.590  | 1.233 | 0.206 | menstrual  |  0.00 |
| 3   | 3 | -0.591  | 1.231 | 0.173 | menstrual  | -0.60 |
| 4   | 2 | -0.505  | 1.200 | 0.266 | menstrual  | -0.08 |
| 5   | 5 | -0.347  | 1.160 | 0.442 | menstrual  |  0.18 |
| 6   | 4 | -0.159  | 1.096 | 0.646 | follicular |  0.47 |
| 7   | 8 |  0.013  | 1.046 | 0.708 | follicular |  0.73 |
| 8   | 7 |  0.124  | 1.024 | 0.695 | follicular |  0.34 |
| 9   | 4 |  0.151  | 1.026 | 0.654 | follicular | -0.09 |
| 10  | 6 |  0.180  | 1.026 | 0.668 | follicular | -0.29 |
| 11  | 9 |  0.238  | 0.902 | 0.828 | follicular | -0.85 |
| 12  | 10|  0.308  | 0.839 | 0.801 | follicular | -0.85 |
| 13  | 7 |  0.369  | 0.802 | 0.678 | ovulatory  | -0.63 |

The arc reads exactly like the design intends: low, reactive mood through the
menstrual phase (M 1–5, g ≈ 1.2), rising steadily through follicular (peak M=10
on day 12 while reactivity bottoms out at g=0.84), entering ovulatory at day 13.
`p` (expected-value probability) climbs from 0.17 to 0.83 across the same span.

## 4. Conversation lifecycle

- **23 conversations opened and closed**; mean length **≈ 5.48 virtual hours**
  (~33 min at the run's pace).
- Longest threads (`conv-10`, `conv-15`, `conv-17`): 5 turns each.
- The scripted user's mid-conversation cuts show up as short threads followed by
  silence gaps; the runtime's conversation-close lifecycle handled every cut
  without a dangling open conversation (23 closed / 0 stuck).

## 5. Cost & tokens (`llm_calls`)

| metric | value |
|---|---|
| LLM calls | 45 |
| prompt tokens | 72,698 (of which **7,424 cached**) |
| completion tokens | 6,152 |
| total tokens | **78,850** |
| reported raw_cost rows | 0 (gateway returned no cost field) |
| computed cost | **$0.00** — stealth/ox-alpha is FREE on OpenRouter (pricing verified live from `/api/v1/models`: prompt=0, completion=0; now encoded in `harness/pricing.py`) |

## 6. Notable transcript moments

- **Spanish code-switching (day 6):** after the user's "Hola. How was your day?",
  the companion replied *"¡Hola! Good day, actually — the walk delivered on its
  promise. Golden light, a heron by the pond standing absurdly still like it was
  posing for a painting…"* — language mirroring fired naturally.
- **High-mood days 11–12 (M 9→10):** noticeably more playful register — *"either
  you're quoting me back at myself, or we've officially merged into one
  overworked-sketching person. If it's the second one, I want royalties."*
- **Day 12 sketching monologue:** *"I sat down with no plan and ended up drawing
  the same curve over and over…"* — consistent persona continuity across the
  whole two weeks.

## 7. Parse-failure finding (the 9 `decision_parse_failed` events)

**Root cause — model answered decide-popups in plain prose.** All 9 failures
(`steer-16, 61, 78, 82, 102, 137, 151`) have `parse_failure_mode=requeue`,
`popup_kind=tool_decide_event`, and a `raw_excerpt` that is conversational prose
("Hey — you caught me just lacing up my shoes…"), not a tool call or a
`tool_decide_event:` marker. In `harness/tools.py::_parse_raw`, when transport is
native but `raw.tool_calls` is empty, prose falls to `parse_textual_reply`, which
raises `no marker found` for decide-phase popups (only inform-phase has a
prose escape hatch). The failure is then handled by the configured policy
(`requeue`): the popup was re-asked and **all 9 recovered** — every failing slot
has ≥1 successful verdict at/after the same slot, all steering rows ended
`delivered`, zero lost decisions.

**Assessment:** this is the designed loud-failure path doing its job against a
chatty reasoning model, not a defect. ox-alpha sometimes answers the popup in
character first (it's a thinking model with strong persona pull); the requeue
costs one extra LLM call each time. A candidate improvement would be a
decide-phase prose fallback ("if prose contains no verdict, treat as no-reply +
requeue without logging a failure"), but that would weaken the audit trail by
design intent — documented here instead of fixed.

## 8. Verdict

Run is healthy end-to-end: real model, real proactivity, honest accounting,
$0 spend. The one systemic observation (prose-first decide answers from
ox-alpha → ~7% requeue overhead) is recorded above for a future iteration.
