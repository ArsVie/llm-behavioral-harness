"""Pricing config for spend accounting — user-editable.

Rates are $ per 1M tokens, tiered::

    cost = cached_tokens/1e6 * cached_input_per_mtok
         + cache_miss_tokens/1e6 * input_per_mtok
         + completion_tokens/1e6 * output_per_mtok

Cache savings = cached_tokens * (input_per_mtok -
cached_input_per_mtok) / 1e6. ``models`` may also be overridden at report
time via ``harness/spend.py --pricing-json <file>``.
"""

from __future__ import annotations

#: Set False once real rates are confirmed (edit me).
PRICING_PENDING: bool = False

#: \$ per 1M tokens, keyed by model name (both wire ids key to same rates).
MODELS: dict[str, dict[str, float]] = {
    # DeepSeek V4 Flash — OpenRouter deepseek/deepseek-v4-flash
    "deepseek-v4-flash": {
        "input_per_mtok": 0.0784,
        "cached_input_per_mtok": 0.01568,
        "output_per_mtok": 0.1568,
    },
    # Same rates under the commandcode org-qualified id (the wire id).
    "deepseek/deepseek-v4-flash": {
        "input_per_mtok": 0.0784,
        "cached_input_per_mtok": 0.01568,
        "output_per_mtok": 0.1568,
    },
    # ox-alpha — stealth route, FREE (no cached tier).
    "stealth/ox-alpha": {
        "input_per_mtok": 0.0,
        "cached_input_per_mtok": 0.0,
        "output_per_mtok": 0.0,
    },
}

#: Fallback tier for models not listed in ``MODELS``.
DEFAULT_MODEL_PRICING: dict[str, float] = {
    "input_per_mtok": 0.50,
    "cached_input_per_mtok": 0.05,
    "output_per_mtok": 1.50,
}

_RATE_KEYS = ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok")


def price_for(
    model: str | None,
    pricing: dict[str, dict[str, float]] | None = None,
) -> dict[str, float] | None:
    """Rates for ``model``: the named entry, the fallback tier for an
    unknown-but-named model, or ``None`` when the model is absent/blank
    (unpriced row — the report counts it but shows no dollars).

    ``pricing`` overrides the module-level ``MODELS`` table for this call
    only (no global side effect); ``None`` uses the built-in rates.
    """
    table = pricing if pricing is not None else MODELS
    if model and model in table:
        return table[model]
    if model and model.strip():
        return dict(DEFAULT_MODEL_PRICING)
    return None


def rates_from_json(payload: dict) -> dict[str, dict[str, float]]:
    """Normalize an externally-supplied pricing dict (e.g. the CLI's
    ``--pricing-json``) into the ``MODELS`` shape; entries missing rate keys
    raise."""
    out: dict[str, dict[str, float]] = {}
    for model, rates in payload.items():
        if not isinstance(rates, dict):
            raise ValueError(f"pricing entry for {model!r} is not a dict")
        bad = [k for k in _RATE_KEYS if k not in rates]
        if bad:
            raise ValueError(
                f"pricing entry for {model!r} missing rate keys: {bad}"
            )
        out[model] = {k: float(rates[k]) for k in _RATE_KEYS}
    return out
