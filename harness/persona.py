"""Persona builder: structural 40/40/20 portfolio sampling (A6 / Iteration-2 A1b).

``build_persona`` assembles a ``PersonaProfile`` from the interest graph
purely structurally — bucket membership (exact / adjacent / independent) is
decided by graph distance, never by an LLM.

Sampling contract
-----------------
* Bucket counts are sampled AROUND the target counts ``n_exact``,
  ``n_adjacent``, ``n_independent``: each count is uniform over
  ``{target - 1, target, target + 1}`` (clamped to the available pool), so an
  individual companion deviates from 40/40/20 while the population mean
  converges to the targets across seeds (defaults 4/4/2 on a ~10-interest
  portfolio).
* Two modes, selected by ``user_interests``:

  * Hub-relative (``user_interests`` None/empty — legacy): exact interests
    are cluster hubs; adjacent interests are nodes within
    ``MAX_ADJACENCY_HOPS`` (3) edges of at least one sampled exact;
    independent interests have no such path.
  * User-relative (``user_interests`` given — Iteration-2 A1b): EXACT =
    interest in the user's interests; ADJACENT = graph-distance within
    ``adjacency_hops`` of at least one USER interest but not exact;
    INDEPENDENT = outside exact + adjacency region. The user-relative
    definition of 40/40/20 is "40% of the portfolio shares the user's
    interests, 40% sits adjacent to them in the interest graph, 20% is
    independent" — the ratios stay distribution targets, not per-companion
    hard ratios.

* All sampling is without replacement, so a profile never repeats an interest.
* Salience is bucket-conditioned (seeded): exact ~ U(0.60, 1.00),
  adjacent ~ U(0.35, 0.80), independent ~ U(0.10, 0.50).
* Routines are drawn from ``ROUTINE_CATALOG`` (2-4 of them) with seeded
  salience.
* ``core`` is deterministic <= 2-sentence prose derived from the portfolio
  (no LLM, no store), in the voice of ``DEFAULT_PERSONA_CORE``.
* RNG: when ``rng`` is None the module uses ``stream_rng(seed, PERSONA_STREAM)``
  (engine.rng stream key 5, reserved for persona construction). There is no
  global RNG state, so repeated calls with the same seed reproduce the
  identical profile.
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

#: Counts are uniform over ``target +- COUNT_JITTER`` (clamped to the pool).
COUNT_JITTER = 1


def _sample_count(rng: np.random.Generator, target: int, pool: int) -> int:
    """Count around ``target``: uniform over {target-1, target, target+1}.

    Clamped to ``[1, pool]``; returns 0 when the pool is empty. With a large
    enough pool the mean equals ``target`` exactly.
    """
    if pool <= 0:
        return 0
    k = target + int(rng.integers(-COUNT_JITTER, COUNT_JITTER + 1))
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


def _build_core(
    exacts: list[str], adjacent: list[str], independent: list[str]
) -> str:
    """Deterministic <= 2-sentence core prose over the sampled portfolio.

    Pure function of the portfolio (no LLM, no store): the same seed always
    yields the same portfolio and therefore the same core.
    """
    absorbed = _join_names(exacts[:2]) if exacts else "many small things"
    soft = _join_names(adjacent[:1]) if adjacent else "a few familiar comforts"
    curious = _join_names(independent[:1]) if independent else "things you have not tried yet"
    second = (
        f"These days you are absorbed in {absorbed}, with a soft spot for {soft} "
        f"and a quiet curiosity about {curious}."
    )
    return (
        "You are Nova, a warm and attentive companion with your own days and rhythms. "
        + second
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
) -> PersonaProfile:
    """Build a deterministic ``PersonaProfile`` around the 40/40/20 target.

    ``seed`` seeds the persona stream (``stream_rng(seed, PERSONA_STREAM)``)
    unless an explicit ``rng`` is given. The graph is only read, never
    mutated.

    ``user_interests`` selects the bucket semantics: ``None``/empty keeps the
    legacy hub-relative sampling (exact = cluster hubs); a non-empty sequence
    switches to USER-relative sampling (EXACT = interest in the user's
    interests, ADJACENT = within ``adjacency_hops`` — default
    ``MAX_ADJACENCY_HOPS`` — of at least one user interest but not exact,
    INDEPENDENT = outside that region). User interests that are not in the
    graph are still valid exact candidates (the companion shares them); their
    adjacency region is just themselves.
    """
    rng = rng if rng is not None else stream_rng(seed, PERSONA_STREAM)

    if user_interests:
        exacts, adjacent, independent = _sample_user_relative(
            rng, graph, user_interests,
            n_exact, n_adjacent, n_independent, adjacency_hops,
        )
    else:
        # 1. Bucket counts around the targets (legacy hub-relative path).
        k_exact = _sample_count(rng, n_exact, len(graph.hubs()))
        exacts: list[str] = []
        while len(exacts) < k_exact:
            node = graph.sample_exact(rng)
            if node not in exacts:
                exacts.append(node)
        exact_set = set(exacts)

        # 2. Adjacent / independent pools relative to this profile's exacts;
        #    membership is computed structurally here.
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
    k_routines = _sample_count(rng, 3, len(ROUTINE_CATALOG))
    chosen = set(_sample_distinct(rng, [r.name for r in ROUTINE_CATALOG], k_routines))
    routines = tuple(
        dataclasses.replace(r, salience=float(rng.uniform(*ROUTINE_SALIENCE_RANGE)))
        for r in ROUTINE_CATALOG
        if r.name in chosen
    )

    # 5. Deterministic prose over the portfolio.
    core = _build_core(exacts, adjacent, independent)
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
    """User-relative bucket sampling (Iteration-2 A1b, plan §5-A1 task 2).

    EXACT pool = the user's interests (deduplicated, order kept); ADJACENT
    pool = every node within ``adjacency_hops`` of at least one user interest,
    minus the user interests themselves; INDEPENDENT pool = every remaining
    graph node. All three pools are sampled with the usual around-target
    counts, so the population means converge to the 40/40/20 targets while
    each companion deviates.
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
