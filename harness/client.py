"""Thin OpenAI-compatible LLM client.

Wraps ANY endpoint exposing /chat/completions; `base_url`, `api_key` and
`model` come from the environment or from `lane` resolution (see
harness.credentials) — no secrets in the repo. Transient failures (transport
errors, 429/5xx) retry with bounded exponential backoff.

`LLMClient` is the injectable protocol; tests use `FakeClient`. JSON mode and
tools are CAPABILITIES, not assumptions: clients advertise `supports_json` /
`supports_tools` and the harness gates those request keys on them.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from harness import wire
from harness.credentials import resolve_credentials

DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

#: Retry budget for transient failures and empty completions.
_MAX_RETRIES = 6
_RETRY_BASE_DELAY_S = 2.0
#: SSE stream parsing: the 2xx retry budget on the initial connection, the
#: line prefix for data events, and the end-of-stream sentinel.
_STREAM_MAX_RETRIES = 2
_SSE_DATA_PREFIX = "data:"
_STREAM_END = "[DONE]"

_logger = logging.getLogger(__name__)


@dataclass
class Usage:
    """Token usage of one chat completion.

    ``cached_tokens`` / ``cache_miss_tokens`` split the input (prompt) tokens
    between cache-served and fresh reads, from whichever variant the gateway
    returns:

    - DeepSeek: ``usage.prompt_cache_hit_tokens`` / ``usage.prompt_cache_miss_tokens``
    - OpenAI-compatible: ``usage.prompt_tokens_details.cached_tokens``
    - Anthropic (proxied): ``usage.cache_read_input_tokens`` (served from
      cache) / ``usage.cache_creation_input_tokens`` (fresh writes, folded
      into the miss bucket)

    Every field is optional: a gateway that reports no ``usage`` object — or
    no cache split — leaves the missing fields ``None`` and the totals are
    still captured when present. Never raises on malformed shapes.

    ``reasoning_tokens`` is the provider-reported completion-side reasoning
    spend (``completion_tokens_details.reasoning_tokens``); ``None`` when the
    gateway doesn't report it.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass
class ChatResult:
    """Structured result of one chat completion.

    - ``content``: the reply text (``""`` for a tool-call-only reply).
      Always a string — a reasoning-only reply round-trips as ``""``, never
      ``None`` (the serializer never puts ``content: null`` on the wire).
    - ``reasoning``: the model's reasoning, extracted from the response
      message when the provider emits it (``message.reasoning_content`` on
      DeepSeek-compatible endpoints, ``message.reasoning`` on some others).
      ``None`` for non-reasoning models.
    - ``tool_calls``: parsed function calls, each ``{"id", "name",
      "arguments_json"}`` — ``arguments_json`` stays the RAW JSON string;
      semantic parsing belongs to the runner, which fails loudly on
      invalid JSON.
    - ``finish_reason``: the provider's stop reason (``None`` when absent).
    - ``usage``: parsed token usage — ``None`` when the response carried no
      usable ``usage`` object.
    - ``raw_cost``: the gateway-reported cost in USD (a top-level ``cost``
      field) — ``None`` when absent. Gateway-side, so kept out of
      :attr:`Usage`.
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
    """Minimal client contract used by the harness.

    ``chat_stream`` is optional: the harness gates on
    ``getattr(client, "chat_stream", None)`` and never requires it, so older
    clients and test fakes without the method still conform. A client that
    offers it must keep ``chat_with_meta`` behaviour identical.
    """

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
        """Complete a chat and return the structured result."""
        ...

    def chat_stream(
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
        chunk_size: int = 64,
    ) -> Iterator[str]:
        """Yield the reply in pieces as they arrive (optional surface).

        Not required for protocol conformance: the harness gates on
        ``getattr(client, "chat_stream", None)``.
        """
        ...

    def close(self) -> None:
        """Release resources (no-op for stateless clients)."""
        ...


def _parse_tool_calls(raw: object) -> list[dict]:
    """Parse OpenAI-style ``message.tool_calls`` into ``{id, name, arguments_json}``.

    ``arguments`` is kept as the RAW JSON string. Malformed entries
    (non-dicts, missing ``function``) are skipped; the raw response stays in
    ``ChatResult.raw``.
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

    Checks ``message.reasoning_content`` (DeepSeek-compatible) then
    ``message.reasoning``; string values only — dict-shaped reasoning blocks
    stay in ``raw``. ``None`` when neither key is present; no fallback is
    attempted.
    """
    for key in ("reasoning_content", "reasoning"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """Never put ``content: null`` on the wire.

    A null or absent ``content`` normalizes to ``""``, the safe
    DeepSeek-compatible form (a null content in a replayed turn 400s the
    request); ``""`` stays ``""``; non-None content (including multimodal
    part lists) passes through untouched. Returns NEW dicts only where a
    normalization applies — caller-owned message objects are never mutated.
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


def _parse_cache_split(raw: dict,
                       prompt: int | None) -> tuple[int | None, int | None]:
    """The (cached, miss) prompt-token split, across gateway dialects.

    Three shapes in the wild, tried in order: DeepSeek's explicit hit/miss
    pair, OpenAI's ``prompt_tokens_details.cached_tokens``, and Anthropic's
    read/creation counters — where a cache READ is a hit but a cache CREATION
    is fresh input and belongs in the miss bucket.

    Whatever the dialect, the two numbers are reconciled against the prompt
    total so the ledger always adds up: a missing miss count is the
    remainder, and a response with no cache information at all is treated as
    fully uncached rather than unknown.
    """
    cached = _int_or_none(raw.get("prompt_cache_hit_tokens"))
    miss = _int_or_none(raw.get("prompt_cache_miss_tokens"))
    if cached is None and miss is None:
        details = raw.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _int_or_none(details.get("cached_tokens"))
        if cached is None:
            cached = _int_or_none(raw.get("cache_read_input_tokens"))
        creation = _int_or_none(raw.get("cache_creation_input_tokens"))
        if creation is not None:
            miss = creation if miss is None else miss + creation
    if prompt is not None and cached is not None and 0 <= cached <= prompt:
        # The split MUST sum to the prompt total, so an explicit miss count
        # is only trusted when no prompt total came with it.
        return cached, prompt - cached
    if prompt is not None:
        if cached is not None and miss is None:
            miss = max(prompt - cached, 0)
        elif cached is None and miss is None:
            miss = prompt
    return cached, miss


def _parse_usage(raw: object) -> Usage | None:
    """Parse the OpenAI-compatible ``usage`` object, or ``None``.

    Tolerates every documented cache-field variant and any missing field —
    only what the gateway actually returns is captured, and a bare usage
    dict without cache details still yields the three totals. ``None`` only
    when the response carries no usable usage object at all (callers then
    persist nothing).
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
    cached, miss = _parse_cache_split(raw, prompt)
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

    def _post(self, payload: dict, stream: bool = False) -> httpx.Response:
        wire.save(payload)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if stream:
                    # post() buffers the body; a streamed request goes through
                    # send(build_request(...), stream=True).
                    resp = self._client.send(
                        self._client.build_request(
                            "POST",
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json=payload,
                        ),
                        stream=True,
                    )
                else:
                    resp = self._client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                    if stream:
                        resp.close()
                    continue
                if stream and resp.status_code != 200:
                    # Release the streamed non-2xx body before the raise, or
                    # it stays unread on the connection.
                    resp.close()
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
        """Complete a chat and return the reply text (thin wrapper).

        Returns ``chat_with_meta(...).content``; every pre-existing call site
        keeps working unchanged.
        """
        return self.chat_with_meta(
            messages,
            system=system,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
        ).content

    def _build_payload(self, messages: list[dict], *, system: str | None,
                       temperature: float, json_mode: bool,
                       max_tokens: int | None, tools: list[dict] | None,
                       tool_choice: dict | str | None,
                       reasoning_effort: str | None) -> dict:
        """The request body for one completion.

        Every optional field is omitted rather than sent as null, and the
        capability flags gate their own fields. A system prompt with no user
        turns yet is sent alone.
        """
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY is not set — the harness never stores credentials. "
                "Export it before running live."
            )
        if system is None:
            payload_messages = _normalize_messages(messages)
        elif messages:
            # Normalize messages: null content is sent as "" on the wire.
            payload_messages = [
                {"role": "system", "content": system},
                *_normalize_messages(messages),
            ]
        else:
            payload_messages = [{"role": "system", "content": system}]
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
        return payload

    def _empty_reply_verdict(self, content, text: str, tool_calls,
                             reasoning) -> tuple[str | None,
                                                 RuntimeError | None]:
        """Whether this response is worth retrying, and what to raise if not.

        A tool call or a reasoning-only turn legitimately carries no content
        and must never be retried; only a reply empty in EVERY channel is a
        real empty completion.
        """
        if content is None and not tool_calls and reasoning is None:
            return "null content", RuntimeError("LLM response had null content")
        if not text.strip() and not tool_calls and reasoning is None:
            return "empty content", RuntimeError(
                f"LLM returned empty/whitespace-only content after "
                f"{self.max_retries + 1} attempts"
            )
        return None, None

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

        ``tools``/``tool_choice`` are sent only when the endpoint advertises
        ``supports_tools``; response ``tool_calls`` are parsed to ``{"id",
        "name", "arguments_json"}`` with ``arguments`` kept as the raw JSON
        string.

        ``reasoning_effort`` is sent only when provided; extracted reasoning
        is stored in ``ChatResult.reasoning`` — it never contaminates
        ``content``.

        GUARD: never combine ``max_tokens`` caps with reasoning models — a
        capped budget starves the thinking pass and yields truncated junk.
        Use ``reasoning_effort`` to control length; pass ``max_tokens=None``.
        """
        payload = self._build_payload(
            messages, system=system, temperature=temperature,
            json_mode=json_mode, max_tokens=max_tokens, tools=tools,
            tool_choice=tool_choice, reasoning_effort=reasoning_effort,
        )
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
            text = "" if content is None else str(content)
            retry_reason, terminal = self._empty_reply_verdict(
                content, text, tool_calls, reasoning
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
            assert terminal is None  # set only together with retry_reason
            if finish_reason == "length":
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

    def chat_stream(
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
        chunk_size: int = 64,
    ) -> Iterator[str]:
        """Yield the reply text in pieces as they arrive (streaming).

        The request body is the non-streaming payload plus ``stream: true``,
        and the response is parsed as Server-Sent Events: each ``data:``
        line's ``choices[0].delta.content`` is yielded as it arrives (empty
        deltas, role-only deltas and the ``[DONE]`` sentinel are skipped).
        Pieces are the raw wire deltas — NOT padded or grouped to
        ``chunk_size``, which exists so FakeClient and the real client share
        one signature (FakeClient slices its queued reply with it).

        Retry/backoff applies to the INITIAL connection only; once the first
        SSE event arrives the response is streamed to completion. This is a
        lazy generator: the request fires on the first ``next()`` and the
        connection stays open while the caller consumes the iterator, so it
        must be drained (or dropped) before the client makes another call.

        Fallback: an endpoint that does not speak SSE — a non-2xx status
        after the retry budget, or a 2xx body with no content deltas —
        degrades to ONE non-streaming :meth:`chat_with_meta` call whose
        content is yielded once, so a caller can always drain the iterator to
        the full reply. Transport errors are not caught.
        """
        payload = self._build_payload(
            messages, system=system, temperature=temperature,
            json_mode=json_mode, max_tokens=max_tokens, tools=tools,
            tool_choice=tool_choice, reasoning_effort=reasoning_effort,
        )
        payload["stream"] = True
        try:
            resp = self._post(payload, stream=True)
        except httpx.HTTPStatusError as exc:
            _logger.warning(
                "LLM stream failed (HTTP %s) — falling back to a single "
                "non-streaming completion",
                exc.response.status_code,
            )
            yield self.chat_with_meta(
                messages, system=system, temperature=temperature,
                json_mode=json_mode, max_tokens=max_tokens, tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=reasoning_effort,
            ).content
            return
        saw_content = False
        for piece in self._iter_stream_events(resp):
            saw_content = True
            yield piece
        if not saw_content:
            # 2xx but no content deltas: the endpoint may not speak SSE.
            _logger.warning(
                "LLM stream carried no content deltas — falling back to "
                "a single non-streaming completion"
            )
            yield self.chat_with_meta(
                messages, system=system, temperature=temperature,
                json_mode=json_mode, max_tokens=max_tokens, tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=reasoning_effort,
            ).content

    def _iter_stream_events(self, resp: httpx.Response) -> Iterator[str]:
        """Yield non-empty content deltas from an SSE stream response.

        Each ``data:`` line is parsed as JSON and its
        ``choices[0].delta.content`` is yielded when present; the ``[DONE]``
        sentinel, empty/whitespace deltas and role-only deltas are skipped.
        Malformed lines are skipped with the line logged at debug level.
        """
        try:
            for line in resp.iter_lines():
                if not line.startswith(_SSE_DATA_PREFIX):
                    continue
                data = line[len(_SSE_DATA_PREFIX):].strip()
                if not data or data == _STREAM_END:
                    continue
                try:
                    event = json.loads(data)
                    delta = event["choices"][0]["delta"]
                    piece = delta.get("content")
                except (ValueError, KeyError, IndexError, TypeError,
                        AttributeError) as exc:
                    _logger.debug("skipping malformed SSE line: %s", exc)
                    continue
                if isinstance(piece, str) and piece:
                    yield piece
        finally:
            resp.close()

    def _retry_post(self, payload: dict, attempt: int) -> httpx.Response:
        """Backoff + repost for one retryable failure."""
        time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
        return self._post(payload)


def _chunk_text(text: str, chunk_size: int) -> Iterator[str]:
    """Yield ``text`` in pieces of at most ``chunk_size`` characters.

    A partial trailing chunk is still delivered (a short reply with a large
    ``chunk_size`` yields the whole reply once, never nothing).
    """
    if not text:
        return
    start = 0
    end = chunk_size
    while start < len(text):
        yield text[start:end]
        start = end
        end += chunk_size


class FakeClient:
    """Scripted client for tests and offline runs.

    `responses` is a queue consumed in order; once exhausted, a default reply
    is returned (no cycling). Entries are either plain strings (reply text)
    or dicts scripting a full response: ``{"content", "reasoning",
    "tool_calls", "finish_reason", "usage", "cost"}`` — ``usage`` scripts
    the RAW usage object (parsed through the same ``_parse_usage`` as the
    real client) and ``cost`` the gateway-reported cost. `echo` mode returns
    the last user message wrapped. Records every call (including
    tools/tool_choice/reasoning_effort) for assertions. Mirrors the LLMClient
    protocol and offers the optional ``chat_stream`` surface.
    """

    supports_json: bool = True
    supports_tools: bool = True

    def __init__(self, responses: list[str | dict] | None = None, echo: bool = False,
                 lane: str | None = None):
        self.responses = deque(responses or [])
        self.calls: list[dict] = []
        self.echo = echo
        #: Lane name for spend attribution.
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

    def chat_stream(
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
        chunk_size: int = 64,
    ) -> Iterator[str]:
        """Yield the next queued response's content in ``chunk_size`` slices.

        Records ONE call entry with the same wire shape as
        :meth:`chat_with_meta` and consumes one queued response; dict
        responses yield their ``content`` field, plain strings yield the
        string, the default reply yields ``FakeClient reply.``, and echo mode
        chunks ``echo: <last content>``. A reply shorter than ``chunk_size``
        is yielded whole — the caller always receives the full text.
        """
        result = self.chat_with_meta(
            messages, system=system, temperature=temperature,
            json_mode=json_mode, max_tokens=max_tokens, tools=tools,
            tool_choice=tool_choice, reasoning_effort=reasoning_effort,
        )
        yield from _chunk_text(result.content, chunk_size)

    def close(self) -> None:
        pass
