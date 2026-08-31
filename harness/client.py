"""Thin OpenAI-compatible LLM client (W-E1).

The harness wraps ANY endpoint exposing /chat/completions; `base_url`,
`api_key` and `model` come from the environment (no secrets in the repo):

    LLM_BASE_URL  (default https://api.commandcode.ai/provider/v1)
    LLM_API_KEY   (required at runtime for live calls)
    LLM_MODEL     (default deepseek/deepseek-v4-flash)
    JUDGE_MODEL   (default deepseek/deepseek-v4-flash — judges may use a cheaper model)

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

WS-D additions (spend accounting, 2026-08-16): every response's
OpenAI-compatible `usage` object is parsed into `ChatResult.usage`
(prompt/completion/total + the cached/miss split across the DeepSeek,
OpenAI-`prompt_tokens_details.cached_tokens` and Anthropic variants — the
opencode gateway uses the OpenAI variant, surfacing `cached_tokens` only
when a prompt prefix is actually cached); the gateway's top-level `cost`
is captured as `ChatResult.raw_cost`. `FakeClient` scripts both. Absent
usage degrades to `None` — nothing new is required of any caller.
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

DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

#: Retry budget: 7 attempts with exponential backoff for transient
#: failures and empty completions; raises when the budget is exhausted.
_MAX_RETRIES = 6
_RETRY_BASE_DELAY_S = 2.0

_logger = logging.getLogger(__name__)


@dataclass
class Usage:
    """Token usage of one chat completion (WS-D spend accounting).

    ``cached_tokens`` / ``cache_miss_tokens`` are the split of the input
    (prompt) tokens between cache-served and fresh reads, derived from
    whichever cache-field variant the gateway returns:

    - DeepSeek: ``usage.prompt_cache_hit_tokens`` / ``usage.prompt_cache_miss_tokens``
    - OpenAI-compatible: ``usage.prompt_tokens_details.cached_tokens``
    - Anthropic (proxied): ``usage.cache_read_input_tokens`` (served from
      cache) / ``usage.cache_creation_input_tokens`` (fresh writes — full
      price, folded into the miss bucket)

    Every field is optional: a gateway that reports no ``usage`` object —
    or no cache split — leaves the missing fields ``None`` and the totals
    are still captured when present. Never raises on malformed shapes.

    ``reasoning_tokens`` is the provider-reported completion-side reasoning
    spend (OpenAI ``completion_tokens_details.reasoning_tokens``; the
    commandcode provider surfaces it there, 2026-08-28). It stays separate
    from ``completion_tokens`` because pricing differs for reasoning output
    on some providers; ``None`` when the gateway doesn't report it.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass
class ChatResult:
    """Structured result of one chat completion (WS3).

    - ``content``: the reply text (``""`` for a tool-call-only reply).
      Always a string — a reasoning-only reply (v4-flash quirk: the model
      answers entirely in ``reasoning_content`` and returns ``content: null``)
      round-trips as ``""``, never ``None`` (WS-E serializer hardening).
    - ``reasoning``: the model's reasoning, extracted from the response
      message when the provider emits it (e.g. DeepSeek-compatible endpoints
      put it in ``message.reasoning_content``; some others in
      ``message.reasoning``). ``None`` for non-reasoning models.
    - ``tool_calls``: parsed function calls, each ``{"id", "name",
      "arguments_json"}`` — ``arguments_json`` stays the RAW JSON string;
      semantic parsing belongs to the runner (WS2), which fails loudly on
      invalid JSON.
    - ``finish_reason``: the provider's stop reason (``None`` when absent).
    - ``usage``: parsed token usage (WS-D) — ``None`` when the response
      carried no usable ``usage`` object (graceful degradation).
    - ``raw_cost``: the gateway-reported cost in USD (a top-level ``cost``
      field the opencode gateway returns alongside ``usage``; discovery
      2026-08-16) — ``None`` when absent. Kept separately from
      :attr:`Usage` because it is gateway-side, not part of ``usage``.
    - ``raw``: the full parsed response body (audit/replay fidelity).
    """

    content: str
    reasoning: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None
    raw_cost: float | None = None
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


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """WS-E serializer guard: never put ``content: null`` on the wire.

    DeepSeek-compatible v4-flash can answer ENTIRELY in the reasoning
    channel: ``message.reasoning_content`` is populated while ``content``
    comes back null/empty. If such a turn is serialized verbatim into the
    next request (``{"role": "assistant", "content": null, ...}``) the
    gateway 400s the request — the null-content brick (alpha finding
    2026-08-16). A null or absent ``content`` normalizes to ``""``, the
    safe DeepSeek-compatible form; ``""`` stays ``""``; non-None content
    (including multimodal part lists) passes through untouched. Returns
    NEW dicts only where a normalization applies — caller-owned message
    objects are never mutated, so repro/audit records keep the original
    list while the wire carries the hardened shape.
    """
    out: list[dict] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("content") is None:
            msg = {**msg, "content": ""}
        out.append(msg)
    return out


def _int_or_none(value: object) -> int | None:
    """Coerce a token count to int, tolerating bools/strs/None (never raises)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _cost_or_none(value: object) -> float | None:
    """Coerce a gateway-reported cost to float, tolerating junk (never raises)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_usage(raw: object) -> Usage | None:
    """Parse the OpenAI-compatible ``usage`` object (WS-D), or ``None``.

    Tolerates every documented cache-field variant and any missing field —
    only what the gateway actually returns is captured, and a bare usage
    dict without cache details (observed on the real gateway, 2026-08-16)
    still yields the three totals. ``None`` only when the response carries
    no usable usage object at all (every field absent → graceful
    degradation: callers persist nothing).
    """
    if not isinstance(raw, dict):
        return None
    prompt = _int_or_none(raw.get("prompt_tokens"))
    completion = _int_or_none(raw.get("completion_tokens"))
    total = _int_or_none(raw.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    # Reasoning tokens, read from completion_tokens_details when present.
    reasoning: int | None = None
    ctd = raw.get("completion_tokens_details")
    if isinstance(ctd, dict):
        reasoning = _int_or_none(ctd.get("reasoning_tokens"))
    # DeepSeek variant: explicit hit/miss split on the usage object.
    cached = _int_or_none(raw.get("prompt_cache_hit_tokens"))
    miss = _int_or_none(raw.get("prompt_cache_miss_tokens"))
    if cached is None and miss is None:
        # OpenAI variant: cached_tokens from prompt_tokens_details.
        details = raw.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _int_or_none(details.get("cached_tokens"))
        # Anthropic variant: cache reads count as hits; cache creation
        # writes are fresh input, folded into the miss bucket.
        read = _int_or_none(raw.get("cache_read_input_tokens"))
        if cached is None:
            cached = read
        creation = _int_or_none(raw.get("cache_creation_input_tokens"))
        if creation is not None:
            miss = creation if miss is None else miss + creation
    # Miss count absent: estimate it as prompt minus cached, minimum 0.
    if cached is not None and miss is None and prompt is not None:
        miss = max(prompt - cached, 0)
    # No cache split at all: all tokens are fresh (uncached) reads.
    if cached is None and miss is None and prompt is not None:
        miss = prompt
    if (
        prompt is None and completion is None and total is None
        and cached is None and miss is None and reasoning is None
    ):
        return None
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
        cache_miss_tokens=miss,
        reasoning_tokens=reasoning,
    )


class OpenAICompatibleClient:
    """httpx-based client for any OpenAI-compatible /chat/completions."""

    supports_json: bool = True
    #: The endpoint accepts the `tools` parameter.
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
        # Credentials: explicit args win, then lane resolver, then env vars.
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
                # Normalize messages: null content is sent as "" on the wire.
                payload_messages = [
                    {"role": "system", "content": system},
                    *_normalize_messages(messages),
                ]
            else:
                # No user turns yet: send the system prompt alone.
                payload_messages = [{"role": "system", "content": system}]
        else:
            payload_messages = _normalize_messages(messages)
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
            retry_reason: str | None = None
            terminal: RuntimeError | None = None
            try:
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                finish_reason = choice.get("finish_reason")
                content = msg.get("content")
                tool_calls = _parse_tool_calls(msg.get("tool_calls"))
                reasoning = _extract_reasoning(msg)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                # Malformed response (e.g. 200 without 'content'): retry with
                # the same bounded budget as empty completions.
                if attempt < self.max_retries:
                    _logger.warning(
                        "malformed LLM response (%s, attempt %d/%d) — retrying",
                        exc, attempt + 1, self.max_retries + 1,
                    )
                    resp = self._retry_post(payload, attempt)
                    continue
                raise RuntimeError(f"malformed LLM response: {exc}") from exc
            if content is None and not tool_calls and reasoning is None:
                # Retry null content only when no tool calls or reasoning
                # are present; those round-trip as content "".
                retry_reason = "null content"
                terminal = RuntimeError("LLM response had null content")
            text = "" if content is None else str(content)
            if retry_reason is None and not text.strip() and not tool_calls and reasoning is None:
                # Empty/whitespace content is retried; reasoning-only and
                # null-content replies are exempt (handled above).
                retry_reason = "empty content"
                terminal = RuntimeError(
                    f"LLM returned empty/whitespace-only content after "
                    f"{self.max_retries + 1} attempts"
                )
            if retry_reason is not None and terminal is not None:
                if attempt < self.max_retries:
                    _logger.warning(
                        "LLM returned %s (attempt %d/%d) — retrying",
                        retry_reason, attempt + 1, self.max_retries + 1,
                    )
                    resp = self._retry_post(payload, attempt)
                    continue
                raise terminal
            assert terminal is None  # terminal is None unless retry_reason was set
            if finish_reason == "length":
                # Truncation is logged as a warning; content is persisted.
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
                usage=_parse_usage(data.get("usage")),
                raw_cost=_cost_or_none(data.get("cost")),
                raw=data,
            )
        raise RuntimeError(f"LLM call failed after {self.max_retries + 1} attempts")

    def _retry_post(self, payload: dict, attempt: int) -> httpx.Response:
        """Backoff + repost for one retryable failure (shared by the
        malformed/null/empty retry branches)."""
        time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
        return self._post(payload)


class FakeClient:
    """Scripted client for tests and offline runs.

    `responses` is a queue consumed in order; once exhausted, a default reply
    is returned (no cycling). Entries are either plain strings (reply text)
    or dicts scripting a full response: ``{"content", "reasoning",
    "tool_calls", "finish_reason", "usage", "cost"}`` — ``usage`` scripts
    the RAW usage object (parsed through the same ``_parse_usage`` as the
    real client, so every cache-field variant is exercisable) and ``cost``
    scripts the gateway-reported cost. `echo` mode returns the last user
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
        #: Lane name for spend attribution, like OpenAICompatibleClient.
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
        # Mirror OpenAICompatibleClient: system-only payload on empty transcripts.
        if system is not None and not messages:
            messages = [{"role": "system", "content": system}]
        # Record the wire shape: null content is normalized to "".
        messages = _normalize_messages(messages)
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
                usage=_parse_usage(scripted.get("usage")),
                raw_cost=_cost_or_none(scripted.get("cost")),
            )
        if scripted is not None:
            return ChatResult(content=scripted)
        return ChatResult(content="FakeClient reply.")

    def close(self) -> None:
        pass
