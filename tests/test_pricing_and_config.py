"""Pricing-table lookup and channel selection.

The pricing table turns the token ledger into dollars; channel selection
keeps ``import harness.config`` free of python-telegram-bot.
"""

from __future__ import annotations

import pytest

from harness.config import select_channel
from harness.pricing import (
    DEFAULT_MODEL_PRICING,
    MODELS,
    price_for,
    rates_from_json,
)

_RATE_KEYS = ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok")


# --- price_for -----------------------------------------------------------


def test_known_model_uses_its_own_rates():
    model = next(iter(MODELS))
    assert price_for(model) == MODELS[model]


def test_unknown_but_named_model_falls_back_to_the_default_tier():
    """A model we have never priced still renders dollars, at the fallback
    tier — an unpriced-but-named row would otherwise silently read as free."""
    assert price_for("some/never-seen-model") == DEFAULT_MODEL_PRICING


def test_absent_or_blank_model_is_unpriced():
    """None/blank means the ledger row has no model at all: the report
    counts the call but shows no dollars rather than inventing a rate."""
    assert price_for(None) is None
    assert price_for("") is None
    assert price_for("   ") is None


def test_pricing_override_is_per_call_only():
    override = {"m": {k: 1.0 for k in _RATE_KEYS}}
    assert price_for("m", override) == override["m"]
    assert "m" not in MODELS  # no global side effect


# --- rates_from_json -----------------------------------------------------


def test_rates_from_json_normalizes_to_floats():
    payload = {"m": {"input_per_mtok": 1, "cached_input_per_mtok": "0.5",
                     "output_per_mtok": 2}}
    out = rates_from_json(payload)
    assert out == {"m": {"input_per_mtok": 1.0, "cached_input_per_mtok": 0.5,
                         "output_per_mtok": 2.0}}
    assert all(isinstance(v, float) for v in out["m"].values())


def test_rates_from_json_drops_extra_keys():
    payload = {"m": {**{k: 1.0 for k in _RATE_KEYS}, "note": "ignored"}}
    assert set(rates_from_json(payload)["m"]) == set(_RATE_KEYS)


def test_rates_from_json_rejects_a_missing_rate_key():
    """Loudly, by name: a typo in --pricing-json must not render $0 quietly."""
    payload = {"m": {"input_per_mtok": 1.0}}
    with pytest.raises(ValueError, match="missing rate keys"):
        rates_from_json(payload)


def test_rates_from_json_rejects_a_non_dict_entry():
    with pytest.raises(ValueError, match="not a dict"):
        rates_from_json({"m": 1.0})


def test_rates_from_json_of_an_empty_payload():
    assert rates_from_json({}) == {}


# --- select_channel ------------------------------------------------------


def test_select_cli_channel():
    from harness.channels.cli import CLIChannel

    assert isinstance(select_channel("cli"), CLIChannel)


def test_select_fake_channel_passes_inbound():
    from harness.channels.base import FakeChannel

    channel = select_channel("fake", inbound=[(1.0, "hi")])
    assert isinstance(channel, FakeChannel)


def test_unknown_channel_lists_the_valid_names():
    with pytest.raises(ValueError, match="'cli', 'telegram', 'fake'"):
        select_channel("carrier-pigeon")


def test_importing_config_does_not_require_telegram():
    """The telegram import is lazy and inside its branch, so a machine
    without python-telegram-bot can still run the cli/fake channels."""
    import importlib
    import sys

    assert "harness.config" in sys.modules
    importlib.reload(sys.modules["harness.config"])
    select_channel("cli")  # still works after a reload
