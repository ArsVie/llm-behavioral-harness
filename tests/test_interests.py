"""Interest graph tests (A6): catalog structure, paths, samplers, reproducibility."""

import numpy as np
import pytest

from engine.rng import stream_rng
from harness.interests import (
    CLUSTERS,
    CROSS_EDGES,
    ISLAND,
    ISLAND_EDGES,
    MAX_ADJACENCY_HOPS,
    InterestGraph,
    build_catalog,
)


def test_catalog_contains_required_clusters():
    g = build_catalog()
    for hub, members in CLUSTERS.items():
        assert hub in g.hubs()
        assert hub in g.nodes()
        for member in members:
            assert member in g.nodes()


def test_every_cluster_member_connected_to_its_hub():
    g = build_catalog()
    for hub, members in CLUSTERS.items():
        neighbors = g.neighbors(hub)
        for member in members:
            assert member in neighbors
            assert g.path_exists(hub, member, 1)
            assert g.path_exists(member, hub, 1)


def test_cross_edges_are_sparse_leaf_to_leaf():
    g = build_catalog()
    hubs = set(g.hubs())
    endpoints = []
    assert len(CROSS_EDGES) <= 6  # frozen: sparse by construction
    for a, b, strength in CROSS_EDGES:
        assert a not in hubs and b not in hubs  # never hub-to-hub
        assert 0.0 < strength <= 0.25
        assert a in g.neighbors(b) and b in g.neighbors(a)
        endpoints.extend((a, b))
    # no leaf participates in more than one cross edge
    assert len(endpoints) == len(set(endpoints))


def test_island_has_no_path_to_any_hub():
    g = build_catalog()
    for node in ISLAND:
        for hub in g.hubs():
            assert not g.path_exists(node, hub, MAX_ADJACENCY_HOPS)
            assert not g.path_exists(hub, node, MAX_ADJACENCY_HOPS)
        assert not g.path_exists(node, "mathematics", 20)  # no path at any depth
    # island stays internally connected
    for a, b, _ in ISLAND_EDGES:
        assert g.path_exists(a, b, 1)


def test_neighbors_symmetric_and_sorted():
    g = build_catalog()
    for node in g.nodes():
        neighbors = g.neighbors(node)
        assert neighbors == sorted(neighbors)
        for nb in neighbors:
            assert node in g.neighbors(nb)


def test_path_exists_respects_max_hops():
    g = build_catalog()
    # physics -> guitar is exactly 3 hops: physics-mathematics-programming-guitar
    assert g.path_exists("physics", "guitar", 3)
    assert not g.path_exists("physics", "guitar", 2)
    # baking -> pottery exists but only at 8 hops; bounded at 3 it must not
    assert not g.path_exists("baking", "pottery", MAX_ADJACENCY_HOPS)
    assert g.path_exists("baking", "pottery", 8)


def test_path_exists_self_unknown_and_zero_hops():
    g = build_catalog()
    assert g.path_exists("mathematics", "mathematics", 0)
    assert g.path_exists("unknown", "unknown", 0)  # a == b always holds
    assert not g.path_exists("unknown", "mathematics", 3)
    assert not g.path_exists("mathematics", "physics", 0)
    assert g.neighbors("unknown") == []


def test_add_relation_validates_strength():
    g = InterestGraph()
    g.add_relation("a", "b", 0.5)
    with pytest.raises(ValueError):
        g.add_relation("a", "c", 1.5)
    with pytest.raises(ValueError):
        g.add_relation("a", "c", -0.1)
    assert "c" not in g.nodes()


def test_sample_exact_only_hubs():
    g = build_catalog()
    rng = stream_rng(123, 5)
    draws = [g.sample_exact(rng) for _ in range(200)]
    assert all(d in g.hubs() for d in draws)
    assert len(set(draws)) >= 5  # the sampler covers the hub pool


def test_sample_adjacent_within_hops_of_a_hub():
    g = build_catalog()
    rng = stream_rng(124, 5)
    draws = [g.sample_adjacent(rng) for _ in range(200)]
    for d in draws:
        assert d not in g.hubs()
        assert any(g.path_exists(d, h, MAX_ADJACENCY_HOPS) for h in g.hubs())


def test_sample_independent_no_path_to_any_hub():
    g = build_catalog()
    rng = stream_rng(125, 5)
    draws = [g.sample_independent(rng) for _ in range(200)]
    for d in draws:
        assert all(not g.path_exists(d, h, MAX_ADJACENCY_HOPS) for h in g.hubs())
    assert set(draws) <= set(ISLAND)


def test_samplers_reproducible_per_seed():
    g = build_catalog()
    exact1 = [g.sample_exact(stream_rng(7, 5)) for _ in range(20)]
    exact2 = [g.sample_exact(stream_rng(7, 5)) for _ in range(20)]
    assert exact1 == exact2
    adj1 = [g.sample_adjacent(stream_rng(7, 5)) for _ in range(20)]
    adj2 = [g.sample_adjacent(stream_rng(7, 5)) for _ in range(20)]
    assert adj1 == adj2
    ind1 = [g.sample_independent(stream_rng(7, 5)) for _ in range(20)]
    ind2 = [g.sample_independent(stream_rng(7, 5)) for _ in range(20)]
    assert ind1 == ind2


def test_sample_exact_requires_hubs():
    g = InterestGraph()
    g.add_relation("a", "b", 0.5)
    with pytest.raises(ValueError):
        g.sample_exact(np.random.default_rng(1))
