"""Client tests (W-E1): FakeClient + OpenAICompatibleClient payload shape."""

import json

import httpx
import pytest

from harness.client import FakeClient, OpenAICompatibleClient


def test_fake_client_cycles_responses():
    client = FakeClient(responses=["a", "b"])
    assert client.chat([{"role": "user", "content": "x"}]) == "a"
    assert client.chat([{"role": "user", "content": "x"}]) == "b"
    assert client.chat([{"role": "user", "content": "x"}]) == "FakeClient reply."
    assert len(client.calls) == 3


def test_fake_client_echo():
    client = FakeClient(echo=True)
    out = client.chat([{"role": "user", "content": "hi there"}])
    assert out == "echo: hi there"


def test_openai_client_payload_and_system_prepend():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        payload = json.loads(body)
        assert "/chat/completions" in request.url.path
        assert "Bearer sekrit" in request.headers["authorization"]
        assert payload["model"] == "m1"
        assert payload["messages"][0]["role"] == "system"  # system prepended
        assert "response_format" not in payload  # json_mode off
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="sekrit",
        model="m1",
    )
    client._client = httpx.Client(transport=transport)
    out = client.chat(
        [{"role": "user", "content": "q"}], system="SYS", temperature=0.0
    )
    assert out == "ok"


def test_openai_client_json_mode_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        payload = json.loads(body)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"s": 1}'}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m"
    )
    client._client = httpx.Client(transport=transport)
    assert client.chat([{"role": "user", "content": "q"}], json_mode=True) == '{"s": 1}'


def test_openai_client_requires_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = OpenAICompatibleClient(base_url="https://x.test", api_key=None, model="m")
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        client.chat([{"role": "user", "content": "q"}])
