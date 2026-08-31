"""HTTP client test seam (consolidated from test_client + test_serializer_null_hardening)."""

from __future__ import annotations

import httpx

from harness.client import OpenAICompatibleClient


def client_with(handler, **kwargs):
    """OpenAICompatibleClient wired to a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", **kwargs
    )
    client._client = httpx.Client(transport=transport)
    return client
