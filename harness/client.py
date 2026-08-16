"""Thin OpenAI-compatible LLM client (W-E1).

The harness wraps ANY endpoint exposing /chat/completions; `base_url`,
`api_key` and `model` come from the environment (no secrets in the repo):

    LLM_BASE_URL  (default https://opencode.ai/zen/go/v1/)
    LLM_API_KEY   (required at runtime for live calls)
    LLM_MODEL     (default deepseek-v4-flash)
    JUDGE_MODEL   (default deepseek-v4-flash — judges may use a cheaper model)

Two-lane credential split (WS-C, 2026-08-16): pass `lane="product"` (the
live companion actor — token LILY_TOKEN / optional LILY_BASE_URL) or
`lane="research"` (judges + all experiment-generated replies — token
JUDGE_GENERATOR_TOKEN / optional JUDGE_GENERATOR_BASE_URL). Lane resolution
goes through harness.credentials: it fails loudly at construction when the
lane token is missing (never a silent fallback to LLM_API_KEY /
OPENCODE_GO_API_KEY), never logs the value, and stamps the client with
`client.lane` for spend attribution. Explicit `api_key`/`base_url` arguments
always win over the resolver; omitting `lane` keeps the legacy env behavior.

`LLMClient` is the injectable protocol; tests use `FakeClient`. JSON mode is
a CAPABILITY, not an assumption: clients advertise `supports_json` and the
harness gates `response_format` on it. Same for tools: `supports_tools`
advertises that the ENDPOINT accepts the `tools` parameter (the MODEL may
still fail to call tools correctly — that failure is the runner's loud
parse-failure path, WS2). Transient failures (transport errors, 429/5xx)
retry with bounded exponential backoff (review fix #3).

WS3 additions (runtime redesign): `ChatResult` carries content, extracted
reasoning, tool calls, finish_reason and the raw response; `chat_with_meta`
exposes tools/tool_choice/reasoning_effort; `chat()` remains a thin wrapper
returning plain content so every pre-existing call site is untouched.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from harness.credentials import resolve_credentials

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1/"
DEFAULT_MODEL = "deepseek-v4-flash"

#: Bounded retry for transient failures (review fix #3) and for
#: empty/whitespace-only completions (iteration-3 B1: generation integrity).
#: G3 evidence (2026-08-10): opencode-go intermittently returns empty
#: completions in multi-minute windows (3 consecutive G3 smokes died with
#: 4-consecutive empties while direct probes between windows succeeded).
#: 7 attempts × 2/4/8/16/32/64s backoff (~2 min worst case) rides through
#: short windows; the budget is still bounded and still raises loudly.
#: Robustness hardening, not threshold tuning — blanks are never accepted.
_MAX_RETRIES = 6
_RETRY_BASE_DELAY_S = 2.0

_logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    """Structured result of one chat completion (WS3).

    - ``content``: the reply text (``""`` for a tool-call-only reply).
    - ``reasoning``: the model's reasoning, extracted from the response
      message when the provider emits it (e.g. DeepSeek-compatible endpoints
      put it in ``message.reasoning_content``; some others in
      ``message.reasoning``). ``None`` for non-reasoning models.
    - ``tool_calls``: parsed function calls, each ``{"id", "name",
      "arguments_json"}`` — ``arguments_json`` stays the RAW JSON string;
      semantic parsing belongs to the runner (WS2), which fails loudly on
      invalid JSON.
    - ``finish_reason``: the provider's stop reason (``None`` when absent).
    - ``raw``: the full parsed response body (audit/replay fidelity).
    """

    content: str
    reasoning: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    raw: dict = field(default_factory=dict)


class LLMClient(Protocol):
    """Minimal client contract used by the harness."""

    supports_json: bool
    supports_tools: bool

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a chat. `system` is prepended when given."""
        ...

    def chat_with_meta(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """Complete a chat and return the structured result (WS3)."""
        ...

    def close(self) -> None:
        """Release resources (no-op for stateless clients)."""
        ...


def _parse_tool_calls(raw: object) -> list[dict]:
    """Parse OpenAI-style ``message.tool_calls`` into ``{id, name, arguments_json}``.

    ``arguments`` is kept as the RAW JSON string — semantic parsing belongs
    to the runner (WS2), which fails loudly on invalid JSON. Malformed
    entries (non-dicts, missing ``function``) are skipped with the raw
    response preserved in ``ChatResult.raw``.
    """
    if not isinstance(raw, list):
        return []
    calls: list[dict] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        calls.append(
            {
                "id": tc.get("id"),
                "name": fn.get("name"),
                "arguments_json": fn.get("arguments"),
            }
        )
    return calls


def _extract_reasoning(msg: dict) -> str | None:
    """Pull the model's reasoning out of a response message.

    Provider placement varies: DeepSeek-compatible endpoints put it in
    ``message.reasoning_content``; some others use ``message.reasoning``.
    Both are checked (string values only — dict-shaped reasoning blocks stay
    in ``raw`` for the audit path). Non-reasoning models have neither key →
    ``None``, and no fallback is attempted (Hermes behavior, D2).
    """
    for key in ("reasoning_content", "reasoning"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


class OpenAICompatibleClient:
    """httpx-based client for any OpenAI-compatible /chat/completions."""

    supports_json: bool = True
    #: ENDPOINT capability, not model capability: the endpoint accepts the
    #: `tools` parameter, but the MODEL may still fail to call tools
    #: correctly — that failure is the runner's loud parse-failure path
    #: (WS2), not a client concern.
    supports_tools: bool = True

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = _MAX_RETRIES,
        lane: str | None = None,
    ):
        # Lane resolution (WS-C): explicit args win over the resolver; the
        # resolver fails loudly at construction when the lane token is
        # missing. lane=None keeps the legacy LLM_* env behavior and stamps
        # nothing (client.lane stays None).
        if lane is not None:
            lane_key, lane_base = resolve_credentials(lane)
            self.api_key = api_key if api_key is not None else lane_key
            self.base_url = (
                base_url or lane_base or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
            ).rstrip("/")
        else:
            self.api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY", "")
            self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.lane = lane
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def _post(self, payload: dict) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                    continue
                raise
        raise RuntimeError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a chat and return the reply text.

        Thin wrapper over :meth:`chat_with_meta` — every pre-existing call
        site keeps working unchanged.
        """
        return self.chat_with_meta(
            messages,
            system=system,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
        ).content

    def chat_with_meta(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """Complete a chat and return the structured :class:`ChatResult`.

        ``tools``/``tool_choice`` are passed through to the request body when
        the endpoint supports them (``supports_tools``); ``tool_calls`` in
        the response are parsed to ``{"id", "name", "arguments_json"}`` with
        ``arguments`` kept as the raw JSON string.

        ``reasoning_effort`` is sent only when provided; the model's
        reasoning is extracted from the response message when present
        (``message.reasoning_content`` on DeepSeek-compatible endpoints,
        ``message.reasoning`` on some others) and stored separately in
        ``ChatResult.reasoning`` — it never contaminates ``content``.

        GUARD (repo pitfall 3af0a5a): never combine ``max_tokens`` caps with
        reasoning models — a capped budget starves the thinking pass and
        yields truncated junk. ``reasoning_effort`` is the sanctioned
        control; callers configuring a reasoning model must pass
        ``max_tokens=None``.
        """
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY is not set — the harness never stores credentials. "
                "Export it before running live."
            )
        if system is not None:
            if messages:
                payload_messages = [{"role": "system", "content": system}, *messages]
            else:
                # Proactive opener on a fresh transcript: a system-only request
                # is the honest context (no user turns exist yet).
                payload_messages = [{"role": "system", "content": system}]
        else:
            payload_messages = messages
        payload: dict = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
        }
        if json_mode and self.supports_json:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools is not None and self.supports_tools:
            payload["tools"] = tools
        if tool_choice is not None and self.supports_tools:
            payload["tool_choice"] = tool_choice
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        resp = self._post(payload)
        for attempt in range(self.max_retries + 1):
            try:
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                finish_reason = choice.get("finish_reason")
                content = msg.get("content")
                tool_calls = _parse_tool_calls(msg.get("tool_calls"))
                reasoning = _extract_reasoning(msg)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                # Malformed (p.ej. 200 sin 'content'): reintentable con el
                # MISMO presupuesto acotado que los vacíos (it3 G5:
                # opencode-go/luna devuelve ocasionalmente 200 sin content).
                if attempt < self.max_retries:
                    _logger.warning(
                        "malformed LLM response (%s, attempt %d/%d) — retrying",
                        exc, attempt + 1, self.max_retries + 1,
                    )
                    time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                    resp = self._post(payload)
                    continue
                raise RuntimeError(f"malformed LLM response: {exc}") from exc
            if content is None and not tool_calls:
                # Null content with no tool calls is a broken reply; a
                # tool-call-only response (content=None + tool_calls) is
                # legitimate and must NOT be retried.
                if attempt < self.max_retries:
                    _logger.warning(
                        "LLM response had null content (attempt %d/%d) — retrying",
                        attempt + 1, self.max_retries + 1,
                    )
                    time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                    resp = self._post(payload)
                    continue
                raise RuntimeError("LLM response had null content")
            text = "" if content is None else str(content)
            if not text.strip() and not tool_calls:
                # 200-with-empty-body / empty completion: retry with the
                # same bounded backoff as transport failures (it2 F1: blanks
                # were persisted because empties were returned silently).
                if attempt < self.max_retries:
                    _logger.warning(
                        "LLM returned empty content (attempt %d/%d) — retrying",
                        attempt + 1,
                        self.max_retries + 1,
                    )
                    time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                    resp = self._post(payload)
                    continue
                raise RuntimeError(
                    f"LLM returned empty/whitespace-only content after "
                    f"{self.max_retries + 1} attempts"
                )
            if finish_reason == "length":
                # Truncation is NOT an empty reply: content is persisted, the
                # truncation is recorded as a marker log line (it2 corpus ends
                # on "Nova: Hey" — truncated runs must be visible).
                _logger.warning(
                    "LLM reply truncated (finish_reason=length, %d chars) — "
                    "content persisted, truncation recorded",
                    len(text),
                )
            return ChatResult(
                content=text,
                reasoning=reasoning,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                raw=data,
            )
        raise RuntimeError(f"LLM call failed after {self.max_retries + 1} attempts")


class FakeClient:
    """Scripted client for tests and offline runs.

    `responses` is a queue consumed in order; once exhausted, a default reply
    is returned (no cycling). Entries are either plain strings (reply text)
    or dicts scripting a full response: ``{"content", "reasoning",
    "tool_calls", "finish_reason"}``. `echo` mode returns the last user
    message wrapped. Records every call (including tools/tool_choice/
    reasoning_effort) for assertions. Faithful to the LLMClient protocol:
    system-only payload on empty transcripts, `supports_json`,
    `supports_tools`, no-op `close`.
    """

    supports_json: bool = True
    supports_tools: bool = True

    def __init__(self, responses: list[str | dict] | None = None, echo: bool = False,
                 lane: str | None = None):
        self.responses = deque(responses or [])
        self.calls: list[dict] = []
        self.echo = echo
        #: Lane stamp (WS-C attribution): mirrors OpenAICompatibleClient —
        #: None for un-laned fakes.
        self.lane = lane

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a chat and return the reply text (thin wrapper)."""
        return self.chat_with_meta(
            messages,
            system=system,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
        ).content

    def chat_with_meta(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        # Mirror OpenAICompatibleClient semantics (system-only payload when the
        # transcript is empty) so fakes are faithful protocol stand-ins.
        if system is not None and not messages:
            messages = [{"role": "system", "content": system}]
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "temperature": temperature,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "tools": tools,
                "tool_choice": tool_choice,
                "reasoning_effort": reasoning_effort,
            }
        )
        if self.echo:
            return ChatResult(content=f"echo: {messages[-1]['content']}")
        scripted = self.responses.popleft() if self.responses else None
        if isinstance(scripted, dict):
            return ChatResult(
                content=scripted.get("content") or "",
                reasoning=scripted.get("reasoning"),
                tool_calls=list(scripted.get("tool_calls") or []),
                finish_reason=scripted.get("finish_reason"),
            )
        if scripted is not None:
            return ChatResult(content=scripted)
        return ChatResult(content="FakeClient reply.")

    def close(self) -> None:
        pass
