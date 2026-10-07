# Provider switch: back to OpenCode Go (2026-09-26)

Supersedes `provider-commandcode-2026-08-28.md`. The commandcode research lane
ran out of credits mid-experiment on 2026-09-26.

## What changed

- Endpoint: `https://opencode.ai/zen/go/v1` (`/chat/completions`, `/models`).
- Model id: the bare `deepseek-v4-flash` (also listed: `deepseek-v4.1-flash`,
  `deepseek-v4-pro`). Wire dumps from the commandcode period carry the
  org-qualified `deepseek/deepseek-v4-flash`; replays strip the prefix.
- Every chat request now carries `User-Agent: llm-behavioral-harness/1.0` and
  `x-opencode-session`. Go returns 400 `MissingSessionID` without the session
  header. One session id per process (`harness.client.default_session_id`,
  overridable with `LLM_SESSION_ID`), so the judge fork and the mainline share
  routing and prompt cache. Raw callers use `harness.client.request_headers`.
- Repo `.env`: both lanes use the `OPENCODE_GO_API_KEY` value and the Go base
  URL; `LLM_MODEL` / `JUDGE_MODEL` are the bare id. The commandcode lines are
  kept commented for rollback.

## Verified (2026-09-26)

- Both lanes through `OpenAICompatibleClient`: chat, tools (`tool_calls` with
  JSON args) and `json_mode` all 200.
- Prompt cache reported in `usage.prompt_tokens_details.cached_tokens`
  (5888 of 6086 on a repeated 6k prompt) and parsed by `_parse_usage`.
- Reasoning comes back as `message.reasoning_content`; `usage` has no
  `reasoning_tokens`.
- `reasoning_effort: "low"` accepted.

## Notes

- Go is a subscription; `pricing.py` token math is an estimate, not the bill.
- Go's docs describe it as for coding agents and say traffic is monitored for
  abuse (https://opencode.ai/docs/go/).
