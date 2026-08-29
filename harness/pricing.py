"""Pricing config for WS-D spend accounting — user-editable, PENDING-USER.

Every rate below is a PENDING-USER placeholder: a sane order-of-magnitude
guess (cached input is ~10x cheaper than fresh input), NOT a real price.
The user must fill the actual \$/1M-token rates per model before the spend
report's dollar figures are trustworthy, then flip ``PRICING_PENDING`` to
``False``. Until then the report prints a "PRICING PENDING" banner and
labels the dollar columns as placeholder math.

Cost formula (plan §WS-D, tiered)::

    cost = cached_tokens/1e6 * cached_input_per_mtok
         + cache_miss_tokens/1e6 * input_per_mtok
         + completion_tokens/1e6 * output_per_mtok

i.e. cached input is billed at the cheap cached rate, the uncached
(cache-miss) input at the fresh rate, completion at the output rate.
Cache savings = what the same call would have cost fully-uncached minus
the actual tiered cost = cached_tokens * (input_per_mtok - cached_input_per_mtok) / 1e6.

Edit this file to set real numbers. ``models`` may also be overridden at
report time via ``harness/spend.py --pricing-json <file>``.
"""

from __future__ import annotations

#: False once the user has confirmed real rates (edit me).
PRICING_PENDING: bool = False

#: \\$ per 1M tokens, keyed by model name. Verified 2026-08-20 via
#: openrouter.ai/api/v1/models id deepseek/deepseek-v4-flash (pre-commandcode)
#: and 2026-08-28 via the commandcode provider models list.
#: NOTE: the wire model id on the commandcode provider is org-qualified
#: ``deepseek/deepseek-v4-flash`` (the bare ``deepseek-v4-flash`` 400s with
#: ``unsupported_model``). The pricing table keys both ids to the SAME rates —
#: the org-qualified id is what the ledger records; the bare id is kept so
#: legacy/other-provider rows still price out.
MODELS: dict[str, dict[str, float]] = {
    # DeepSeek V4 Flash — OpenRouter deepseek/deepseek-v4-flash
    # prompt $0.0784 / cached $0.01568 / completion $0.1568 per 1M
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
    # ox-alpha — OpenRouter stealth route (stealth/ox-alpha)
    # FREE: pricing {prompt: "0", completion: "0"} verified live via
    # openrouter.ai/api/v1/models on 2026-08-22. The route exposes no
    # cached tier; cached input is billed at the same $0 rate.
    "stealth/ox-alpha": {
        "input_per_mtok": 0.0,
        "cached_input_per_mtok": 0.0,
        "output_per_mtok": 0.0,
    },
}

#: Fallback tier for models not listed in ``MODELS`` (still placeholder).
DEFAULT_MODEL_PRICING: dict[str, float] = {
    "input_per_mtok": 0.50,
    "cached_input_per_mtok": 0.05,
    "output_per_mtok": 1.50,
}

_RATE_KEYS = ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok")


def price_for(model: str | None) -> dict[str, float] | None:
    """Rates for ``model``: the named entry, the fallback tier for an
    unknown-but-named model, or ``None`` when the model is absent/blank
    (unpriced row — the report counts it but shows no dollars)."""
    if model and model in MODELS:
        return MODELS[model]
    if model and model.strip():
        return dict(DEFAULT_MODEL_PRICING)
    return None


def rates_from_json(payload: dict) -> dict[str, dict[str, float]]:
    """Normalize an externally-supplied pricing dict (e.g. the CLI's
    ``--pricing-json``) into the ``MODELS`` shape; unknown-rate entries are
    skipped loudly so a typo renders no dollars silently."""
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
