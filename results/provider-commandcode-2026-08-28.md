# Provider switch: commandcode.ai (2026-08-28)

Status: DONE — both lanes now resolve to the commandcode provider.

## What changed

- Endpoint: `https://api.commandcode.ai/provider/v1` (OpenAI-compatible
  `/chat/completions` + `/models`).
- Model id: the provider REQUIRES the org-qualified id
  `deepseek/deepseek-v4-flash`. The bare `deepseek-v4-flash` 400s with
  `unsupported_model` (`param=model`).
- Repo `.env` (gitignored, values never shown): both lanes now use the same
  commandcode key, lane-specific base URLs, and the org-qualified model:
  - `LILY_TOKEN` / `LILY_BASE_URL=https://api.commandcode.ai/provider/v1`
  - `JUDGE_GENERATOR_TOKEN` / `JUDGE_GENERATOR_BASE_URL=https://api.commandcode.ai/provider/v1`
  - `LLM_MODEL=deepseek/deepseek-v4-flash` / `JUDGE_MODEL=deepseek/deepseek-v4-flash`
  - (OPENROUTER_API_KEY/OPENROUTER_BASE_URL preserved for `live_two_weeks.py`
    which explicitly uses ox-alpha via OpenRouter — untouched.)

## Code changes (main)

- `harness/client.py`: `DEFAULT_BASE_URL` → commandcode, `DEFAULT_MODEL` →
  `deepseek/deepseek-v4-flash`; docstring updated.
- `tests/test_client.py`: two lane-precedence pins updated to the new default.
- `harness/pricing.py`: added `deepseek/deepseek-v4-flash` entry with the SAME
  OpenRouter-verified rates (wire id is org-qualified; bare id kept for legacy
  rows).
- `experiments/cvs_manifest.py`: JUDGE_FAMILIES base_url + model updated
  (opencode-flash → commandcode; luna stays gpt-5.6-luna which the endpoint
  also lists).
- `experiments/cvs_common.py`, `experiments/decision_probe.py`: MODEL default →
  org-qualified id.
- Launcher `~/.hermes/scripts/live_telegram.sh`: dropped the stale
  `LLM_BASE_URL="${OPENCODE_GO_BASE_URL:-}"` export; lanes resolve base URLs
  from the repo `.env`.

## Verified (live probes 2026-08-28)

- `GET /models` → 200, lists `deepseek/deepseek-v4-flash`, `gpt-5.6-luna`,
  claude/gpt/kimi families.
- Plain chat: 200, `finish=stop`, content correct.
- `response_format={"type":"json_object"}`: 200 — the judge path works.
- Tools: 200, proper `tool_calls` (name + JSON args) — decision-probe path works.
- Usage object: OpenAI-compatible shape with `prompt_tokens_details.cached_tokens`
  — parsed by `_parse_usage` (cache split captured). `cost` field absent (None).
- Harness integration: `probe_lane` auth OK on both lanes; real
  `OpenAICompatibleClient(lane="research")` call returned `HARNESS-OK`,
  finish=stop, usage parsed, reasoning content present.

## Notes / gotchas

- reasoning: this provider surfaces reasoning via `completion_tokens_details.reasoning_tokens`
  (not `reasoning_content` on the message) — the client's `_extract_reasoning`
  won't find it on the MESSAGE, but `ChatResult.raw` retains the full body, so
  reasoning traces stay audit-able. No behavior impact for normal replies.
- max_tokens: use ≥512 for reasoning models (repo pitfall #71). JSON-mode
  smoke showed the model consumed ~283 reasoning tokens under a 512 cap —
  plenty of headroom, but keep the cap generous.
- `raw_cost` is None on this provider (no top-level `cost` field) — the spend
  ledger's dollar figures rely on pricing.py token math (which is why the
  org-qualified pricing entry matters).
