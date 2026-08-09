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


def test_fake_client_records_max_tokens_per_call():
    client = FakeClient()
    client.chat([{"role": "user", "content": "x"}], max_tokens=123)
    client.chat([{"role": "user", "content": "y"}])

    assert client.calls[0]["max_tokens"] == 123
    assert client.calls[1]["max_tokens"] is None
    assert set(client.calls[0]) == {"messages", "system", "temperature", "json_mode", "max_tokens"}


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


def test_openai_client_max_tokens_payload():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m"
    )
    client._client = httpx.Client(transport=transport)

    client.chat([{"role": "user", "content": "q"}], max_tokens=777)
    client.chat([{"role": "user", "content": "q"}])

    assert seen[0]["max_tokens"] == 777
    assert "max_tokens" not in seen[1]


def test_openai_client_requires_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = OpenAICompatibleClient(base_url="https://x.test", api_key=None, model="m")
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        client.chat([{"role": "user", "content": "q"}])


# -- iteration-3 B1: generation integrity ---------------------------------- #
# Empty/whitespace completions are retried with bounded backoff; a persistent
# empty raises; truncation (finish_reason=length) is recorded, not treated as
# an empty reply (it2 F1: 579/2090 blank assistant turns were persisted).


def _empty_then_real_client(empties: int = 2, max_retries: int = 2):
    """OpenAICompatibleClient whose transport returns empty content `empties`
    times before a real reply."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= empties:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": ""}}]}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "real content"}}]}
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m",
        max_retries=max_retries,
    )
    client._client = httpx.Client(transport=transport)
    return client, calls


def test_openai_client_retries_empty_content_then_succeeds(monkeypatch, caplog):
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    client, calls = _empty_then_real_client(empties=2, max_retries=2)

    out = client.chat([{"role": "user", "content": "q"}])

    assert out == "real content"
    assert calls["n"] == 3  # two empties, one real
    assert sleeps == [0.5, 1.0]  # bounded exponential backoff (0.5 * 2**i)
    assert "empty content" in caplog.text


def test_openai_client_retries_whitespace_only_content(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "   \n\t"}}]}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", max_retries=1
    )
    client._client = httpx.Client(transport=transport)

    assert client.chat([{"role": "user", "content": "q"}]) == "ok"
    assert calls["n"] == 2


def test_openai_client_persistent_empty_raises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}}]}
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", max_retries=1
    )
    client._client = httpx.Client(transport=transport)

    with pytest.raises(RuntimeError, match="empty/whitespace-only content"):
        client.chat([{"role": "user", "content": "q"}])
    assert calls["n"] == 2  # capped at max_retries + 1 — never hangs


def test_openai_client_truncation_finish_reason_records_marker(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "partial reply"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", max_retries=1
    )
    client._client = httpx.Client(transport=transport)

    # Truncated-but-non-empty content is returned (not retried, not raised)
    # and the truncation is recorded as a marker log line.
    assert client.chat([{"role": "user", "content": "q"}]) == "partial reply"
    assert "truncated (finish_reason=length" in caplog.text
