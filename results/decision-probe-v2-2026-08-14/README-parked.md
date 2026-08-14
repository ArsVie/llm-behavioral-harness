---
type: decision-probe-v2-prep
title: "A5 parked full-run agent — prep status + smoke evidence"
description: "Prep verification (imports, LLM env, 1-leg real smoke) for the 450-leg decision probe v2 grid; parked awaiting .gate GO."
timestamp: 2026-08-14T22:55:00Z
tags: [decision-probe, v2, parked, a5]
---

# A5 parked full-run agent — prep status

Branch `wip/probe-v2` (HEAD `246d60f`). This agent runs the FULL grid once the
orchestrator's pilot passes (`.gate`), then commits results. It never touches
`experiments/*.py` or `tests/` — it only RUNS the pipeline.

## Prep status (Step 1, all green)

| check | status | evidence |
|---|---|---|
| `.venv/bin/python` imports `engine` / `sim` / `harness` | PASS | import test clean |
| `experiments.probe_schema` imports | PASS | schema frozen (71dbbb4) |
| `experiments.decision_probe` imports + `--v2` CLI | PASS | landed during poll (A2), poll 11/20 |
| `experiments.probe_moods` imports | PASS | A1, committed 55f9cb6 |
| `experiments.probe_outcome` imports | PASS | A3, committed 246d60f |
| `experiments.probe_analyze` imports | PASS | A4, on disk (untracked at prep time) |
| `mood_samples.json` exists (real engine output) | PASS | 103 doses / 28 distinct briefs / 4 set_kinds (summary.json) |
| LLM env via `_load_env` | PASS | `~/.hermes/.env` present; OPENCODE_GO_API_KEY → LLM_API_KEY; base `https://opencode.ai/zen/go/v1/`; model `deepseek-v4-flash` |
| No `max_tokens` in payload (guard 3af0a5a) | PASS | payload dict has no max_tokens key (read-only check) |

## Smoke evidence (1 real leg, v2 loop)

Command (final A2 CLI; `--doses` is a Path, so the 1-leg smoke uses
`--doses-per-scenario 1 --K 1`):

```
.venv/bin/python -m experiments.decision_probe --v2 \
  --out results/decision-probe-v2-2026-08-14/smoke \
  --doses results/decision-probe-v2-2026-08-14/mood_samples.json \
  --doses-per-scenario 1 --K 1 --scenarios s01 --pool 1
```

Result: `real done: 1 legs (1 scenarios x 1 doses x K=1), 0 parse failures, 0 replayed` — exit 0.

Verified on `smoke/probe.json`:

- `leg_id: s01:native:ext-M10:k01`, `source: model`, `parse_failure: False`
- **`reasoning_content` non-empty: 815 chars, `reasoning_present: True`** (verbatim real reasoning about the decision)
- `brief` is the engine-rendered prose verbatim from `mood_samples.json` (mood brief comes ONLY from the engine chain — FLOOR)
- `raw_reply` is a real `tool_decide_event` call (`initiate: true`)
- `responded` and `choice` are separate schema fields (probe_schema; filled post-hoc by probe_outcome — FLOOR)

## Steers received (live, from orchestrator)

1. Grid: default per-scenario dose selection (2 extremes + 2 orthogonal_valence + 2
   orthogonal_energy ≈ 6 doses/scenario) — 15 scenarios x 6 doses x K=5 = **450 legs**.
   Do NOT pass all 103 doses. (Final CLI exposes `--doses-per-scenario`, default 6.)
2. Concurrency (superseded): pool 12 → ceiling 16; 429 backoff 12→8→4.
3. Concurrency (current, supersedes #2): host ceiling 35; **start pool 24, ceiling 30**;
   429 backoff **24→16→8**; raise toward 30 when a scenario completes clean.
   `--pool` is clamped to ≤ 30 by the runner itself.

## Park state (Step 2)

Awaiting `results/decision-probe-v2-2026-08-14/.gate`:
- `GO` → run the full grid (450 legs), classify, analyze, commit, verify.
- `ABORT` → write `terminal-abort.md` (state card not landing in deliberation; full grid moot), commit, exit.
- timeout (180 min) → write `terminal-timeout.md`, commit, exit.

Note: `probe.classified.json` + `report.md` + `traces/` present in this dir are A4's
FAKE pipeline smoke artifacts (`fake-scripted-v2`, `_make_fake_classified.py`) — they
will be REPLACED by the real run's outputs.
