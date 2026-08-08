"""Persona builder tests (A6): 40/40/20 population means, structure, determinism."""

import re

import numpy as np
import pytest

from engine.rng import stream_rng
from harness.domain import Interest, PersonaProfile, Routine
from harness.interests import MAX_ADJACENCY_HOPS, build_catalog
from harness.persona import (
    DEFAULT_NAME,
    PERSONA_STREAM,
    ROUTINE_CATALOG,
    ROUTINE_SALIENCE_RANGE,
    SALIENCE_RANGES,
    build_persona,
)

#: Number of seeded personas used for the population-level checks.
N_SEEDS = 100

# Frozen tolerances for the 40/40/20 claim. The claim is about the POPULATION
# mean across ~100 seeds, not any single profile: bucket counts are sampled
# uniform over {target-1, target, target+1} per profile (sd ~0.82, so the
# count tolerance of +-0.5 is ~6 standard errors of the mean at N_SEEDS), and
# the fraction tolerance of +-5 percentage points is ~2.4 sd of the
# per-profile fraction around the 0.40/0.40/0.20 targets.
COUNT_TOL = 0.5
FRACTION_TOL = 0.05


@pytest.fixture(scope="module")
def graph():
    return build_catalog()


def _profiles(graph, n=N_SEEDS):
    return [build_persona(seed, graph=graph) for seed in range(n)]


def _bucket_counts(profile):
    counts = {"exact": 0, "adjacent": 0, "independent": 0}
    for interest in profile.interests:
        counts[interest.bucket] += 1
    return counts


def test_profile_shape(graph):
    p = build_persona(0, graph=graph)
    assert isinstance(p, PersonaProfile)
    assert p.name == DEFAULT_NAME
    assert p.interests and p.routines
    assert all(isinstance(i, Interest) for i in p.interests)
    assert all(isinstance(r, Routine) for r in p.routines)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", p.core.strip()) if s]
    assert len(sentences) <= 2


def test_no_duplicate_interests_across_100_profiles(graph):
    for p in _profiles(graph):
        names = [i.name for i in p.interests]
        assert len(names) == len(set(names))
        assert all(i.bucket in ("exact", "adjacent", "independent") for i in p.interests)


def test_bucket_counts_sampled_around_targets(graph):
    # Defaults n_exact=4, n_adjacent=4, n_independent=2 -> per-profile counts
    # are uniform over {3,4,5}/{3,4,5}/{1,2,3} (the frozen "sampled around the
    # target, not forced" contract).
    for p in _profiles(graph):
        c = _bucket_counts(p)
        assert c["exact"] in (3, 4, 5)
        assert c["adjacent"] in (3, 4, 5)
        assert c["independent"] in (1, 2, 3)


def test_mean_bucket_distribution_100_seeds(graph):
    profiles = _profiles(graph)
    counts = {"exact": [], "adjacent": [], "independent": []}
    fracs = {"exact": [], "adjacent": [], "independent": []}
    for p in profiles:
        c = _bucket_counts(p)
        total = sum(c.values())
        for bucket in c:
            counts[bucket].append(c[bucket])
            fracs[bucket].append(c[bucket] / total)
    targets = {"exact": 4, "adjacent": 4, "independent": 2}
    target_fracs = {"exact": 0.40, "adjacent": 0.40, "independent": 0.20}
    for bucket in targets:
        mean_count = float(np.mean(counts[bucket]))
        sem = float(np.std(counts[bucket], ddof=1)) / np.sqrt(N_SEEDS)
        assert abs(mean_count - targets[bucket]) <= COUNT_TOL, (bucket, mean_count)
        assert abs(mean_count - targets[bucket]) <= 3 * sem, (bucket, mean_count, sem)
        mean_frac = float(np.mean(fracs[bucket]))
        assert abs(mean_frac - target_fracs[bucket]) <= FRACTION_TOL, (bucket, mean_frac)


def test_adjacent_interests_have_graph_paths(graph):
    for p in _profiles(graph, n=30):
        exacts = [i.name for i in p.interests if i.bucket == "exact"]
        for i in p.interests:
            if i.bucket == "adjacent":
                assert any(
                    graph.path_exists(i.name, e, MAX_ADJACENCY_HOPS) for e in exacts
                ), (p, i)


def test_independent_interests_have_no_path_to_exacts(graph):
    for p in _profiles(graph, n=30):
        exacts = [i.name for i in p.interests if i.bucket == "exact"]
        for i in p.interests:
            if i.bucket == "independent":
                assert all(
                    not graph.path_exists(i.name, e, MAX_ADJACENCY_HOPS) for e in exacts
                ), (p, i)


def test_exact_interests_are_cluster_hubs(graph):
    hubs = set(graph.hubs())
    for p in _profiles(graph, n=30):
        for i in p.interests:
            if i.bucket == "exact":
                assert i.name in hubs


def test_salience_ranges_by_bucket(graph):
    for p in _profiles(graph, n=30):
        for i in p.interests:
            lo, hi = SALIENCE_RANGES[i.bucket]
            assert lo <= i.salience <= hi, (p, i)


def test_same_seed_reproduces_identical_profile(graph):
    a = build_persona(42, graph=graph)
    b = build_persona(42, graph=graph)
    assert a == b  # deep equality on frozen dataclasses
    # rng=None must resolve to stream_rng(seed, PERSONA_STREAM)
    c = build_persona(42, graph=graph, rng=stream_rng(42, PERSONA_STREAM))
    assert a == c


def test_different_seeds_create_variation(graph):
    profiles = [build_persona(seed, graph=graph) for seed in range(10)]
    portfolios = [tuple(i.name for i in p.interests) for p in profiles]
    assert len(set(portfolios)) >= 2
    assert len({p.core for p in profiles}) >= 2


def test_routines_from_catalog_with_sampled_salience(graph):
    catalog = {r.name: r for r in ROUTINE_CATALOG}
    for p in _profiles(graph, n=30):
        assert 2 <= len(p.routines) <= 4
        names = [r.name for r in p.routines]
        assert len(names) == len(set(names))
        for r in p.routines:
            assert r.name in catalog
            base = catalog[r.name]
            assert (r.start_frac, r.duration_h, r.cadence) == (
                base.start_frac,
                base.duration_h,
                base.cadence,
            )
            assert ROUTINE_SALIENCE_RANGE[0] <= r.salience <= ROUTINE_SALIENCE_RANGE[1]


def test_core_mentions_sampled_portfolio(graph):
    for seed in range(10):
        p = build_persona(seed, graph=graph)
        exacts = [i.name for i in p.interests if i.bucket == "exact"]
        adjacent = [i.name for i in p.interests if i.bucket == "adjacent"]
        independent = [i.name for i in p.interests if i.bucket == "independent"]
        assert exacts[0] in p.core
        assert exacts[1] in p.core
        assert adjacent[0] in p.core
        assert independent[0] in p.core
