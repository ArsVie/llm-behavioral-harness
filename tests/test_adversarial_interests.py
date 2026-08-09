"""A9 Iteration-2 adversarial wave — INTEREST-GRAPH attack class (plan §5-A9 B2).

Attacks on the interest graph and the user-relative 40/40/20 sampler: the
distance function must be symmetric and consistent with path_exists, there is
no self-distance, and the adjacency boundary cannot be gamed into 'exact' by
duplicating edges or duplicate user interests.

Every test is deterministic: no RNG draws except the seeded samplers, no real
clock, no LLM.
"""

from __future__ import annotations

import numpy as np

from harness.interests import (
    ISLAND,
    MAX_ADJACENCY_HOPS,
    InterestGraph,
    build_catalog,
)
from harness.persona import build_persona

SEED = 4242


def _all_pairs(nodes: list[str]) -> list[tuple[str, str]]:
    return [(a, b) for a in nodes for b in nodes]


# --------------------------------------------------------------------------- #
# B2-a: distance symmetry + consistency with path_exists
# --------------------------------------------------------------------------- #


def test_b2_distance_symmetric_and_consistent_with_path_exists():
    """For EVERY node pair of the default catalog: distance(a,b) ==
    distance(b,a); distance(a,a) == 0; and the hop-bounded reachability
    primitive agrees with the exact distance for every hop bound
    (distance(a,b) <= h  <=>  path_exists(a,b,h))."""
    graph = build_catalog()
    nodes = graph.nodes()
    assert len(nodes) >= 20, "precondition: a catalog worth attacking"
    for a, b in _all_pairs(nodes):
        dab = graph.distance(a, b)
        dba = graph.distance(b, a)
        assert dab == dba, f"asymmetric distance: {a!r}->{b!r}={dab}, reverse={dba}"
        if a == b:
            assert dab == 0, f"self-distance must be 0, got {dab}"
        for h in (1, 2, 3, 4, 5, MAX_ADJACENCY_HOPS):
            reachable = graph.path_exists(a, b, h)
            consistent = (dab is not None and dab <= h)
            assert reachable == consistent, (
                f"path_exists({a!r},{b!r},{h})={reachable} disagrees with "
                f"distance={dab}"
            )


def test_b2_no_self_distance_and_unknown_nodes():
    """distance(x, x) == 0 for every node INCLUDING unknown names;
    reachable_within always includes the start node itself; an unknown node
    is unreachable from anything else and reachable only from itself."""
    graph = build_catalog()
    for name in graph.nodes() + ["ghost-interest", ""]:
        assert graph.distance(name, name) == 0
        assert name in graph.reachable_within(name, 0)
        assert name in graph.reachable_within(name, MAX_ADJACENCY_HOPS)
    assert graph.distance("ghost-interest", "mathematics") is None
    assert graph.distance("mathematics", "ghost-interest") is None
    assert not graph.path_exists("ghost-interest", "mathematics", MAX_ADJACENCY_HOPS)
    assert not graph.path_exists("mathematics", "ghost-interest", MAX_ADJACENCY_HOPS)


def test_b2_adjacency_boundary_not_gameable_via_duplicate_edges():
    """Duplicate relations cannot shorten distances: re-adding an existing
    edge (even many times) never reduces a distance below its true shortest
    path, and re-marking a hub never adds duplicate hubs to the exact pool."""
    graph = build_catalog()
    before = {
        (a, b): graph.distance(a, b)
        for a in graph.nodes()
        for b in graph.nodes()
        if a != b
    }
    # hammer the graph with duplicate edges (same pair, same strength, many
    # times) and duplicate hub markings
    for a in graph.nodes():
        for b in graph.neighbors(a):
            for _ in range(5):
                graph.add_relation(a, b, 0.6)
    for hub in graph.hubs():
        for _ in range(5):
            graph.add_hub(hub)
    after = {
        (a, b): graph.distance(a, b)
        for a in graph.nodes()
        for b in graph.nodes()
        if a != b
    }
    assert before == after, "duplicate edges changed graph distances"
    assert len(graph.hubs()) == len(set(graph.hubs())), "duplicate hubs in the pool"
    # a 4-hop pair stays OUTSIDE the 3-hop adjacency boundary no matter how
    # many times its path edges are duplicated
    assert graph.distance("rock", "mathematics") == 4  # metal->guitar->programming
    assert not graph.path_exists("rock", "mathematics", MAX_ADJACENCY_HOPS)
    for _ in range(5):
        graph.add_relation("guitar", "programming", 0.2)
    assert graph.distance("rock", "mathematics") == 4, (
        "duplicating a path edge shortened the adjacency distance"
    )


def test_b2_duplicate_user_interests_cannot_game_exact():
    """The adjacency boundary cannot be gamed into 'exact' via duplicates: a
    user listing the SAME interest many times still gets ONE exact slot (the
    dedup'd pool), the portfolio never contains duplicate names, and the
    exact bucket never exceeds the dedup'd user set."""
    graph = build_catalog()
    rng = np.random.default_rng(SEED)
    # "pottery" repeated 20 times + one real graph-adjacent neighbour
    inflated = tuple(["pottery"] * 20 + ["art", "hiking"])
    profile = build_persona(
        SEED, graph=graph, user_interests=inflated,
        n_exact=10, n_adjacent=10, n_independent=10,
        rng=rng,
    )
    names = [i.name for i in profile.interests]
    assert len(names) == len(set(names)), "portfolio contains duplicate interest names"
    exacts = [i.name for i in profile.interests if i.bucket == "exact"]
    # the dedup'd user set is {pottery, art, hiking} — exact can never exceed it
    assert set(exacts) <= {"pottery", "art", "hiking"}
    assert len(exacts) == len(set(exacts)) == 3, (
        "duplicate user interests inflated the exact bucket"
    )
    # adjacency region of the dedup'd set is unaffected by the duplicates:
    # 'rock' is 4 hops from pottery/art/hiking — still independent, never
    # promoted to adjacent by repetition
    independent = {i.name for i in profile.interests if i.bucket == "independent"}
    reachable = set()
    for u in ("pottery", "art", "hiking"):
        reachable |= graph.reachable_within(u, MAX_ADJACENCY_HOPS)
    for name in independent:
        assert name not in reachable, f"{name!r} became adjacent via duplication"


def test_b2_island_always_independent_pool():
    """The hubless island can never be gamed into exact or adjacent: every
    island node stays > MAX_ADJACENCY_HOPS from every hub of the default
    catalog (the structural guarantee that the independent bucket is always
    fillable)."""
    graph = build_catalog()
    for island_node in ISLAND:
        for hub in graph.hubs():
            d = graph.distance(island_node, hub)
            assert d is None or d > MAX_ADJACENCY_HOPS, (
                f"island node {island_node!r} is within {d} hops of hub {hub!r}"
            )
            assert not graph.path_exists(island_node, hub, MAX_ADJACENCY_HOPS)
