"""Thin OpenAI-compatible LLM client (W-E1).

The harness wraps ANY endpoint exposing /chat/completions; `base_url`,
`api_key` and `model` come from the environment (no secrets in the repo):

    LLM_BASE_URL  (default https://opencode.ai/zen/go/v1/)
    LLM_API_KEY   (required at runtime for live calls)
    LLM_MODEL     (default deepseek-v4-flash)
    JUDGE_MODEL   (default deepseek-v4-flash — judges may use a cheaper model)

`LLMClient` is the injectable protocol; tests use `FakeClient`. JSON mode is
a CAPABILITY, not an assumption: clients advertise `supports_json` and the
harness gates `response_format` on it. Transient failures (transport errors,
429/5xx) retry with bounded exponential backoff (review fix #3).
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Protocol

import httpx

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1/"
DEFAULT_MODEL = "deepseek-v4-flash"

#: Bounded retry for transient failures (review fix #3) and for
#: empty/whitespace-only completions (iteration-3 B1: generation integrity).
_MAX_RETRIES = 3
_RETRY_BASE_DELAY_S = 0.5

_logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Minimal client contract used by the harness."""

    supports_json: bool

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

    def close(self) -> None:
        """Release resources (no-op for stateless clients)."""
        ...


class OpenAICompatibleClient:
    """httpx-based client for any OpenAI-compatible /chat/completions."""

    supports_json: bool = True

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = _MAX_RETRIES,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY", "")
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
        resp = self._post(payload)
        for attempt in range(self.max_retries + 1):
            try:
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"malformed LLM response: {exc}") from exc
            if content is None:
                raise RuntimeError("LLM response had null content")
            text = str(content)
            if not text.strip():
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
            return text
        raise RuntimeError(f"LLM call failed after {self.max_retries + 1} attempts")


class FakeClient:
    """Scripted client for tests and offline runs.

    `responses` is a queue consumed in order; once exhausted, a default reply
    is returned (no cycling). `echo` mode returns the last user message
    wrapped. Records every call for assertions. Faithful to the LLMClient
    protocol: system-only payload on empty transcripts, `supports_json`,
    no-op `close`.
    """

    supports_json: bool = True

    def __init__(self, responses: list[str] | None = None, echo: bool = False):
        self.responses = deque(responses or [])
        self.calls: list[dict] = []
        self.echo = echo

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
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
            }
        )
        if self.echo:
            return f"echo: {messages[-1]['content']}"
        if self.responses:
            return self.responses.popleft()
        return "FakeClient reply."

    def close(self) -> None:
        pass
