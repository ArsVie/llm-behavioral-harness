"""Thin OpenAI-compatible LLM client (W-E1).

The harness wraps ANY endpoint exposing /chat/completions; `base_url`,
`api_key` and `model` come from the environment (no secrets in the repo):

    LLM_BASE_URL  (default https://opencode.ai/zen/go/v1/)
    LLM_API_KEY   (required at runtime for live calls)
    LLM_MODEL     (default deepseek-v4-flash)
    JUDGE_MODEL   (default deepseek-v4-flash — judges may use a cheaper model)

`LLMClient` is the injectable protocol; tests use `FakeClient`. The judge
path supports `response_format={"type": "json_object"}` when the endpoint
advertises it (JSON-mode is a capability flag, not an assumption).
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1/"
DEFAULT_MODEL = "deepseek-v4-flash"


class LLMClient(Protocol):
    """Minimal client contract used by the harness."""

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
    ) -> str:
        """Complete a chat. `system` is prepended when given."""
        ...


class OpenAICompatibleClient:
    """httpx-based client for any OpenAI-compatible /chat/completions."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.timeout_s = timeout_s
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
    ) -> str:
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY is not set — the harness never stores credentials. "
                "Export it before running live."
            )
        payload_messages = messages
        if system is not None:
            payload_messages = [{"role": "system", "content": system}, *messages]
        payload: dict = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["choices"][0]["message"]["content"])


class FakeClient:
    """Scripted client for tests and offline runs.

    `responses` is a list of replies cycled in order; `echo` mode returns the
    last user message wrapped. Records every call for assertions.
    """

    def __init__(self, responses: list[str] | None = None, echo: bool = False):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.echo = echo

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
    ) -> str:
        self.calls.append(
            {"messages": messages, "system": system, "temperature": temperature, "json_mode": json_mode}
        )
        if self.echo:
            return f"echo: {messages[-1]['content']}"
        if self.responses:
            return self.responses.pop(0)
        return "FakeClient reply."
