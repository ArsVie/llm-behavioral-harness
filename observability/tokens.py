"""Heuristic token pricing for the model-visible surface.

The provider's own usage numbers are the only exact token counts in the
store; the *composition* of a request is always an estimate. This module
prices one request's parts with a fixed character heuristic and then anchors
the parts to the provider's reported prompt size, so the composition bar sums
to a number the provider actually billed while each slice keeps its share.

Baseline semantics mirror DeepSeek Harness' token meter: ``usage`` when a
provider anchor covers the priced surface, ``estimated`` when none does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Characters per token for the fixed heuristic (English prose + JSON mix).
CHARS_PER_TOKEN: float = 3.6

#: Per-message role framing the chat template adds around the content.
MESSAGE_OVERHEAD_TOKENS: int = 4

#: Per-schema JSON framing for a tool definition.
TOOL_OVERHEAD_TOKENS: int = 6


def estimate_text(text: str | None) -> int:
    """Heuristic tokens for one text body (0 for empty)."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _render(content: Any) -> str:
    """Message content as text: strings verbatim, block lists serialized."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    import json

    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def estimate_message(message: dict[str, Any]) -> int:
    """Heuristic tokens for one chat message, role framing included."""
    body = _render(message.get("content"))
    calls = message.get("tool_calls")
    extra = "" if not calls else _render(calls)
    return estimate_text(body) + estimate_text(extra) + MESSAGE_OVERHEAD_TOKENS


def estimate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Per-schema prices: ``[{name, tokens, chars}]`` in declared order."""
    import json

    priced: list[dict[str, Any]] = []
    for tool in tools or []:
        blob = json.dumps(tool, ensure_ascii=False, sort_keys=True)
        name = str(tool.get("function", {}).get("name", tool.get("name", "?")))
        priced.append({
            "name": name,
            "tokens": estimate_text(blob) + TOOL_OVERHEAD_TOKENS,
            "chars": len(blob),
        })
    return priced


@dataclass(frozen=True)
class Anchor:
    """How the displayed prices were reconciled with the provider.

    ``kind`` is ``usage`` when the provider reported prompt tokens for this
    request and they cover the heuristic total, ``estimated`` when a provider
    total exists but reads *below* the heuristic (the estimate overshoots, so
    the prices are scaled down to the billed total and stay proportional),
    and ``none`` when the provider reported nothing at all.
    """

    kind: str
    provider_tokens: int | None
    heuristic_tokens: int
    scale: float

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "provider_tokens": self.provider_tokens,
            "heuristic_tokens": self.heuristic_tokens,
            "scale": round(self.scale, 4),
        }


def reconcile(heuristic_total: int, provider_tokens: int | None) -> Anchor:
    """The anchor for a request priced at ``heuristic_total`` tokens.

    A known provider total always wins as the budget: the parts are scaled to
    it, up when the estimate undershoots (``usage``) and down when it
    overshoots (``estimated``). Without one the raw heuristic stands.
    """
    if not provider_tokens or heuristic_total <= 0:
        return Anchor("none", provider_tokens, heuristic_total, 1.0)
    scale = provider_tokens / heuristic_total
    kind = "usage" if provider_tokens >= heuristic_total else "estimated"
    return Anchor(kind, provider_tokens, heuristic_total, scale)


def price(parts: list[int], anchor: Anchor) -> list[int]:
    """Apply the anchor scale to every part price (integer tokens)."""
    return [int(round(part * anchor.scale)) for part in parts]
