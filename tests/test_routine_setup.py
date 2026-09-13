"""The routine catalog is built at setup, cached, and never trusted raw.

The catalog is an input to ``build_persona``; nothing the model says is
trusted as a number, a name, or a count, and a proposal that validates down
to too little yields the offline default.
"""

from __future__ import annotations

import json

import pytest

from harness import proposal_cache
from harness.domain import Routine
from harness.interests import build_catalog
from harness.persona import ROUTINE_CATALOG, build_persona
from harness.routine_setup import (
    MAX_CADENCE,
    MAX_DURATION_H,
    MAX_ROUTINES,
    MAX_START_FRAC,
    MIN_ROUTINES,
    MIN_START_FRAC,
    apply_proposal,
    build_routine_catalog,
    parse_proposal,
)

MINE = ("mathematics", "lifting", "anime", "history")


def _entry(name, start=0.3, hours=0.5, cadence=0.8) -> dict:
    return {"name": name, "start": start, "hours": hours, "cadence": cadence}


def _good_proposal(n: int = 6) -> list:
    names = ("morning coffee", "gym session", "manga hour", "evening cooking",
             "night reading", "problem sets", "long walk", "history podcast")
    return [_entry(names[i], start=0.25 + i * 0.08) for i in range(n)]


class StubBuilder:
    """A client that answers with a fixed payload and counts its calls."""

    def __init__(self, payload=None, raise_exc=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls = 0
        self.prompts: list[str] = []

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        if self.raise_exc is not None:
            raise self.raise_exc
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps({"routines": self.payload})


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Enable the on-disk cache, pointed at a throwaway directory."""
    monkeypatch.setenv(proposal_cache.CACHE_ENV_VAR, str(tmp_path / "cache"))
    return tmp_path / "cache"


# validation: nothing the model says is trusted


def test_numbers_are_clamped_not_believed():
    routines = apply_proposal([
        _entry("dawn swim", start=-3.0, hours=99.0, cadence=5.0),
    ])
    assert len(routines) == 1
    r = routines[0]
    assert r.start_frac == MIN_START_FRAC
    assert r.duration_h == MAX_DURATION_H
    assert r.cadence == MAX_CADENCE
    # Salience is the SAMPLER's to draw, never the model's.
    assert r.salience == 0.0


def test_day_specific_names_are_rejected():
    # The engine has no weekday, so a name that promises one cannot be kept.
    for bad in ("weekend market", "sunday roast", "weekly shop",
                "friday drinks", "monthly haircut"):
        assert apply_proposal([_entry(bad)]) == ()
    assert len(apply_proposal([_entry("saturday market"), _entry("morning tea")])) == 1


def test_bad_names_and_duplicates_are_dropped():
    kept = apply_proposal([
        _entry("morning coffee"),
        _entry("morning coffee"),      # duplicate
        _entry("Morning  Coffee"),     # normalizes onto the same name
        _entry("metal"),               # ambiguous bare word
        _entry(""),                    # empty
        _entry(None),                  # not a string
        _entry("x" * 200),             # over the length cap
        "not a dict",
    ])
    assert [r.name for r in kept] == ["morning coffee"]


def test_entries_missing_a_number_are_dropped():
    kept = apply_proposal([
        {"name": "no timing at all"},
        {"name": "half specified", "start": 0.3},
        {"name": "bad number", "start": "soon", "hours": 0.5, "cadence": 0.5},
        _entry("complete one"),
    ])
    assert [r.name for r in kept] == ["complete one"]


def test_catalog_is_capped_and_ordered_by_time_of_day():
    kept = apply_proposal([
        _entry(f"routine {i}", start=MAX_START_FRAC - i * 0.05)
        for i in range(MAX_ROUTINES + 4)
    ])
    assert len(kept) == MAX_ROUTINES
    assert [r.start_frac for r in kept] == sorted(r.start_frac for r in kept)


@pytest.mark.parametrize("raw,expected_len", [
    ('{"routines": [{"name": "a", "start": 0.3, "hours": 1, "cadence": 0.5}]}', 1),
    ('[{"name": "a", "start": 0.3, "hours": 1, "cadence": 0.5}]', 1),
    ('here you go: {"routines": []} hope that helps', 0),
])
def test_parse_tolerates_both_shapes_and_surrounding_prose(raw, expected_len):
    parsed = parse_proposal(raw)
    assert parsed is not None and len(parsed) == expected_len


@pytest.mark.parametrize("raw", ["", "no json here at all", "   "])
def test_parse_returns_none_when_there_is_no_json(raw):
    assert parse_proposal(raw) is None


# fallback: onboarding always completes


@pytest.mark.parametrize("client", [
    None,
    StubBuilder(raise_exc=RuntimeError("provider exploded")),
    StubBuilder(payload="not json at all"),
    StubBuilder(payload=[]),                      # empty proposal
    StubBuilder(payload=_good_proposal(2)),       # below MIN_ROUTINES
])
def test_setup_never_dies_and_falls_back_to_the_default(client):
    result = build_routine_catalog(MINE, default=ROUTINE_CATALOG, client=client)
    assert result.source == "default"
    assert result.routines == ROUTINE_CATALOG
    assert not result.built


def test_a_hung_provider_does_not_hang_onboarding():
    import time

    class Hangs:
        def chat(self, messages, **kwargs):
            time.sleep(30.0)
            return "{}"

    started = time.monotonic()
    result = build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=Hangs(), budget_s=0.5,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"onboarding blocked for {elapsed:.1f}s on a hung provider"
    assert result.source == "default"


def test_a_good_proposal_replaces_the_hardcoded_catalog():
    client = StubBuilder(payload=_good_proposal())
    result = build_routine_catalog(MINE, default=ROUTINE_CATALOG, client=client)
    assert result.source == "model"
    assert result.built
    assert len(result.routines) >= MIN_ROUTINES
    assert result.routines != ROUTINE_CATALOG
    # The interests reached the builder; her name did not need to.
    assert "lifting" in client.prompts[0]


# the cache: a repeated interest set costs no call


def test_the_same_interest_set_is_free_the_second_time(cache):
    first = build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=StubBuilder(payload=_good_proposal())
    )
    assert first.source == "model"

    # No client at all the second time: a cache miss would fall back to the
    # default, so resolving the same catalog proves it came from disk.
    second = build_routine_catalog(MINE, default=ROUTINE_CATALOG, client=None)
    assert second.source == "cache"
    assert second.routines == first.routines


def test_interest_order_does_not_change_the_question(cache):
    build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=StubBuilder(payload=_good_proposal())
    )
    shuffled = tuple(reversed(MINE))
    hit = build_routine_catalog(shuffled, default=ROUTINE_CATALOG, client=None)
    assert hit.source == "cache", "the same SET of interests must be one key"


def test_a_different_interest_set_is_a_different_question(cache):
    build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=StubBuilder(payload=_good_proposal())
    )
    other = build_routine_catalog(
        ("gardening", "jazz"), default=ROUTINE_CATALOG, client=None
    )
    assert other.source == "default", "a different set must not reuse the answer"


def test_a_fallback_is_never_cached(cache):
    build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=StubBuilder(payload="junk")
    )
    # Nothing was stored, so a later good client still gets asked.
    client = StubBuilder(payload=_good_proposal())
    again = build_routine_catalog(MINE, default=ROUTINE_CATALOG, client=client)
    assert again.source == "model" and client.calls == 1


def test_caching_disabled_by_an_empty_env_var(monkeypatch):
    monkeypatch.setenv(proposal_cache.CACHE_ENV_VAR, "")
    build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=StubBuilder(payload=_good_proposal())
    )
    assert build_routine_catalog(
        MINE, default=ROUTINE_CATALOG, client=None
    ).source == "default"


# the sampler is untouched


def test_the_catalog_is_an_input_and_the_draw_stays_seeded():
    graph = build_catalog()
    custom = tuple(
        Routine(f"routine {i}", 0.3 + i * 0.07, 0.5, 0.8, 0.0) for i in range(6)
    )
    a = build_persona(8001, graph=graph, user_interests=MINE, routine_catalog=custom)
    b = build_persona(8001, graph=graph, user_interests=MINE, routine_catalog=custom)

    # Same seed, same catalog -> byte-identical persona (replay determinism).
    assert a == b
    # Drawn FROM the given catalog, with salience re-sampled by the sampler.
    names = {r.name for r in custom}
    assert {r.name for r in a.routines} <= names
    assert all(r.salience > 0.0 for r in a.routines)
    # The interest portfolio does not depend on which routines exist.
    default_run = build_persona(8001, graph=graph, user_interests=MINE)
    assert a.interests == default_run.interests


def test_no_catalog_given_keeps_the_built_in_default():
    graph = build_catalog()
    persona = build_persona(8001, graph=graph, user_interests=MINE)
    assert {r.name for r in persona.routines} <= {r.name for r in ROUTINE_CATALOG}
