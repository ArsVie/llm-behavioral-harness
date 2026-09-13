"""Persona builder: structural 40/40/20 portfolio sampling.

``build_persona`` assembles a ``PersonaProfile`` from the interest graph
purely structurally — bucket membership (exact / adjacent / independent) is
decided by graph distance, never by an LLM.
"""

from __future__ import annotations

import dataclasses
from typing import Optional, Sequence

import numpy as np

from engine.rng import stream_rng

from harness.domain import Interest, PersonaProfile, Routine
from harness.interests import InterestGraph, MAX_ADJACENCY_HOPS

#: Reserved engine.rng stream key for persona construction (5 = PERSONA).
PERSONA_STREAM = 5

#: Companion display name used in PersonaProfile.name and the core prose.
DEFAULT_NAME = "Nova"

#: Routine catalog: fixed name/start/duration/cadence; salience is re-sampled
#: per profile. start_frac is the fraction of the day (0..1) the routine starts.
ROUTINE_CATALOG: tuple[Routine, ...] = (
    Routine("morning coffee", 0.29, 0.5, 0.95, 0.90),
    Routine("evening walk", 0.71, 0.75, 0.80, 0.70),
    Routine("weekend market", 0.42, 1.5, 0.30, 0.60),
    Routine("night reading", 0.90, 1.0, 0.70, 0.80),
    Routine("afternoon sketching", 0.58, 0.75, 0.45, 0.50),
    Routine("evening cooking", 0.79, 1.0, 0.60, 0.65),
)

#: Salience sampling ranges per bucket (exact > adjacent > independent).
SALIENCE_RANGES: dict[str, tuple[float, float]] = {
    "exact": (0.60, 1.00),
    "adjacent": (0.35, 0.80),
    "independent": (0.10, 0.50),
}

#: Salience sampling range for routines.
ROUTINE_SALIENCE_RANGE: tuple[float, float] = (0.30, 1.00)

#: Counts are uniform over ``target +- 1`` (clamped to the pool).


def _sample_count(rng: np.random.Generator, target: int, pool: int) -> int:
    """Count around ``target``: uniform over {target-1, target, target+1}.

    Clamped to ``[1, pool]``; returns 0 when the pool is empty.
    """
    if pool <= 0:
        return 0
    k = target + int(rng.integers(-1, 2))
    return max(1, min(k, pool))


def _sample_distinct(rng: np.random.Generator, candidates: list[str], k: int) -> list[str]:
    """``k`` distinct candidates without replacement (selection order kept)."""
    idx = rng.permutation(len(candidates))
    return [candidates[int(i)] for i in idx[:k]]


def _join_names(names: list[str]) -> str:
    """'a, b and c' style join; '' for an empty list."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


#: The built-in voice, used when no persona file is configured.
DEFAULT_VOICE = (
    "You are Nova, a warm and attentive companion with your own days and "
    "rhythms."
)


#: Opening words of the generated interest sentence; also the split point for
#: swapping an authored voice into a stored core (:func:`split_core`).
INTEREST_SENTENCE_PREFIX = "These days you are absorbed in"


def interest_sentence(
    exacts: list[str], adjacent: list[str], independent: list[str]
) -> str:
    """The one sentence describing what this cycle's portfolio made her care
    about. Pure function of the portfolio — no LLM, no store."""
    absorbed = _join_names(exacts[:2]) if exacts else "many small things"
    soft = _join_names(adjacent[:1]) if adjacent else "a few familiar comforts"
    curious = _join_names(independent[:1]) if independent else "things you have not tried yet"
    return (
        f"{INTEREST_SENTENCE_PREFIX} {absorbed}, with a soft spot for {soft} "
        f"and a quiet curiosity about {curious}."
    )


def split_core(core: str) -> tuple[str, str] | None:
    """Split a stored core into ``(voice, interest_sentence)``.

    Returns None when the core has no recognizable interest sentence — an
    unexpected shape is left strictly alone rather than rewritten on a guess.
    """
    if not core:
        return None
    index = core.find(INTEREST_SENTENCE_PREFIX)
    if index == -1:
        return None
    return core[:index].strip(), core[index:].strip()


def compose_core(voice: str, interests: str) -> str:
    """Authored voice first, then the drawn interests.

    The two are COMPOSED, never substituted: dropping the interest sentence
    would leave the seeded portfolio with no voice in the prompt. Authored
    prose gets its own paragraph.
    """
    voice = (voice or "").strip()
    interests = (interests or "").strip()
    if not voice:
        return interests
    if not interests:
        return voice
    # The default voice runs straight into the interest sentence; authored
    # prose gets its own paragraph.
    joiner = " " if voice == DEFAULT_VOICE else "\n\n"
    return voice + joiner + interests


def _build_core(
    exacts: list[str], adjacent: list[str], independent: list[str],
    *, voice: str | None = None,
) -> str:
    """Deterministic core prose: the voice plus the sampled portfolio.

    Pure function of (voice, portfolio) — no LLM, no store. ``voice`` is the
    authored persona from the configured persona file; ``None`` uses
    :data:`DEFAULT_VOICE`.
    """
    return compose_core(
        voice if voice is not None else DEFAULT_VOICE,
        interest_sentence(exacts, adjacent, independent),
    )


def build_persona(
    seed: int,
    *,
    graph: InterestGraph,
    user_interests: Optional[Sequence[str]] = None,
    n_exact: int = 4,
    n_adjacent: int = 4,
    n_independent: int = 2,
    adjacency_hops: Optional[int] = None,
    rng: Optional[np.random.Generator] = None,
    routine_catalog: Optional[Sequence[Routine]] = None,
    voice: Optional[str] = None,
) -> PersonaProfile:
    """Build a deterministic ``PersonaProfile`` around the 40/40/20 target.

    ``seed`` seeds the persona stream (``stream_rng(seed, PERSONA_STREAM)``)
    unless an explicit ``rng`` is given. The graph is only read, never mutated.

    ``voice`` is the authored persona prose that opens the core; ``None`` uses
    :data:`DEFAULT_VOICE`. It is composed with the drawn interest sentence,
    never substituted for it.

    ``routine_catalog`` is the pool the daily routines are drawn from; ``None``
    uses :data:`ROUTINE_CATALOG`. It is an INPUT to the sampler, never
    something the sampler decides.

    ``user_interests`` selects the bucket semantics: ``None``/empty keeps the
    hub-relative sampling (exact = cluster hubs); a non-empty sequence switches
    to user-relative sampling (EXACT = interest in the user's interests,
    ADJACENT = within ``adjacency_hops`` of at least one user interest but not
    exact, INDEPENDENT = outside that region).
    """
    rng = rng if rng is not None else stream_rng(seed, PERSONA_STREAM)

    if user_interests:
        exacts, adjacent, independent = _sample_user_relative(
            rng, graph, user_interests,
            n_exact, n_adjacent, n_independent, adjacency_hops,
        )
    else:
        # 1. Bucket counts around the targets (hub-relative path).
        k_exact = _sample_count(rng, n_exact, len(graph.hubs()))
        exacts: list[str] = []
        while len(exacts) < k_exact:
            node = graph.sample_exact(rng)
            if node not in exacts:
                exacts.append(node)
        exact_set = set(exacts)

        # 2. Adjacent / independent pools relative to this profile's exacts.
        all_nodes = graph.nodes()
        adjacent_candidates = [
            n
            for n in all_nodes
            if n not in exact_set
            and any(graph.path_exists(n, e, MAX_ADJACENCY_HOPS) for e in exacts)
        ]
        independent_candidates = [
            n
            for n in all_nodes
            if n not in exact_set
            and not any(graph.path_exists(n, e, MAX_ADJACENCY_HOPS) for e in exacts)
        ]
        k_adjacent = _sample_count(rng, n_adjacent, len(adjacent_candidates))
        k_independent = _sample_count(rng, n_independent, len(independent_candidates))
        adjacent = _sample_distinct(rng, adjacent_candidates, k_adjacent)
        independent = _sample_distinct(rng, independent_candidates, k_independent)

    # 3. Interests with bucket-conditioned, seeded salience.
    interests = tuple(
        Interest(name, bucket, float(rng.uniform(*SALIENCE_RANGES[bucket])))
        for bucket, names in (
            ("exact", exacts),
            ("adjacent", adjacent),
            ("independent", independent),
        )
        for name in names
    )

    # 4. Routines: 2-4 from the catalog (catalog order preserved), seeded salience.
    catalog = tuple(routine_catalog) if routine_catalog else ROUTINE_CATALOG
    k_routines = _sample_count(rng, 3, len(catalog))
    chosen = set(_sample_distinct(rng, [r.name for r in catalog], k_routines))
    routines = tuple(
        dataclasses.replace(r, salience=float(rng.uniform(*ROUTINE_SALIENCE_RANGE)))
        for r in catalog
        if r.name in chosen
    )

    # 5. Deterministic prose over the portfolio.
    core = _build_core(exacts, adjacent, independent, voice=voice)
    return PersonaProfile(
        name=DEFAULT_NAME, core=core, interests=interests, routines=routines
    )


def _sample_user_relative(
    rng: np.random.Generator,
    graph: InterestGraph,
    user_interests: Sequence[str],
    n_exact: int,
    n_adjacent: int,
    n_independent: int,
    adjacency_hops: Optional[int],
) -> tuple[list[str], list[str], list[str]]:
    """User-relative bucket sampling.

    EXACT pool = the user's interests (deduplicated, order kept); ADJACENT
    pool = every node within ``adjacency_hops`` of at least one user interest,
    minus the user interests themselves; INDEPENDENT pool = every remaining
    graph node.
    """
    hops = MAX_ADJACENCY_HOPS if adjacency_hops is None else adjacency_hops
    if hops < 1:
        raise ValueError(f"adjacency_hops must be >= 1, got {hops!r}")
    user_set = list(dict.fromkeys(user_interests))

    reachable: set[str] = set()
    for name in user_set:
        reachable |= graph.reachable_within(name, hops)

    exact_candidates = user_set
    adjacent_candidates = sorted(reachable - set(user_set))
    independent_candidates = sorted(set(graph.nodes()) - set(user_set) - reachable)

    k_exact = _sample_count(rng, n_exact, len(exact_candidates))
    exacts = _sample_distinct(rng, exact_candidates, k_exact)
    k_adjacent = _sample_count(rng, n_adjacent, len(adjacent_candidates))
    k_independent = _sample_count(rng, n_independent, len(independent_candidates))
    adjacent = _sample_distinct(rng, adjacent_candidates, k_adjacent)
    independent = _sample_distinct(rng, independent_candidates, k_independent)
    return exacts, adjacent, independent
