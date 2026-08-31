"""Typed interest graph and the hand-built companion catalog (A6).

The graph is an undirected, weighted adjacency structure over interest names.
Cluster hubs (e.g. ``mathematics``) are the *exact* interests a companion can
be absorbed in; every other node of a cluster is tied to its hub by an edge.

Bucket semantics (structural, never LLM-decided)
------------------------------------------------
* exact       — a cluster hub (``sample_exact``).
* adjacent    — within ``MAX_ADJACENCY_HOPS`` (3) edges of at least one hub
                (``sample_adjacent``).
* independent — no path of <= ``MAX_ADJACENCY_HOPS`` edges to any hub
                (``sample_independent``).

The default catalog (``build_catalog``) contains the six required clusters, a
handful of sparse leaf-to-leaf cross edges (strength <= 0.25) that never touch
a hub, and a hubless island (``ISLAND``) whose nodes have no path to any hub —
they guarantee the independent bucket is always fillable, for both
profile-relative sampling and the graph-level ``sample_independent``.

Distance queries: ``distance(a, b)`` returns the shortest path length (or
``None``) and ``reachable_within(name, hops)`` the adjacency region — the
primitives behind the user-relative 40/40/20 sampler (``harness.persona``).

All randomness enters through an injected ``numpy.random.Generator`` (see
``engine.rng``); there is no global RNG state and no real-clock read here.
"""

from __future__ import annotations

#: Maximum number of edges that still counts as "adjacent" to an exact interest.
MAX_ADJACENCY_HOPS = 3

#: Cluster hubs (exact-interest candidates) -> their members; each
#: member connects to its hub (strength 0.6).
CLUSTERS: dict[str, tuple[str, ...]] = {
    "mathematics": ("physics", "statistics", "puzzles", "programming"),
    "metal": ("rock", "live music", "guitar", "alternative music"),
    "literature": ("poetry", "fantasy", "book clubs"),
    "outdoors": ("hiking", "running", "camping"),
    "food": ("cooking", "baking", "coffee"),
    "art": ("drawing", "photography", "pottery"),
}

#: Sparse leaf-to-leaf cross edges (from, to, strength); never hub-to-hub
#: and at most one per leaf.
CROSS_EDGES: tuple[tuple[str, str, float], ...] = (
    ("programming", "guitar", 0.20),  # mathematics x metal
    ("puzzles", "poetry", 0.15),  # mathematics x literature
    ("hiking", "photography", 0.25),  # outdoors x art
    ("coffee", "book clubs", 0.20),  # food x literature
    ("running", "cooking", 0.20),  # outdoors x food
)

#: Hubless island: nodes with no path to any hub — always independent
#: candidates. Guarantees ``sample_independent`` is never empty.
ISLAND: tuple[str, ...] = ("gardening", "birdwatching", "woodworking")

#: Internal island edges (from, to, strength); keep the island connected to
#: itself but never bridge it to a cluster.
ISLAND_EDGES: tuple[tuple[str, str, float], ...] = (
    ("gardening", "birdwatching", 0.40),
    ("gardening", "woodworking", 0.35),
)


class InterestGraph:
    """Undirected weighted graph of interest names.

    Nodes are created implicitly by ``add_relation``/``add_hub``; edges are
    symmetric (``add_relation(a, b, s)`` connects both directions).
    """

    def __init__(self) -> None:
        self._adj: dict[str, dict[str, float]] = {}
        self._hubs: set[str] = set()

    # -- construction ----------------------------------------------------

    def add_relation(self, from_: str, to: str, strength: float) -> None:
        """Add an undirected edge with ``strength`` in [0, 1]."""
        if not 0.0 <= strength <= 1.0:
            raise ValueError(f"strength must be in [0, 1], got {strength!r}")
        self._adj.setdefault(from_, {})[to] = strength
        self._adj.setdefault(to, {})[from_] = strength

    def add_hub(self, name: str) -> None:
        """Mark ``name`` as a cluster hub (an exact-interest candidate)."""
        self._adj.setdefault(name, {})
        self._hubs.add(name)

    # -- queries ---------------------------------------------------------

    def nodes(self) -> list[str]:
        """All node names, sorted (deterministic order)."""
        return sorted(self._adj)

    def hubs(self) -> list[str]:
        """Cluster hub names, sorted."""
        return sorted(self._hubs)

    def neighbors(self, name: str) -> list[str]:
        """Neighbor names of ``name``, sorted; empty list for unknown nodes."""
        return sorted(self._adj.get(name, {}))

    def path_exists(self, a: str, b: str, max_hops: int = 3) -> bool:
        """True if ``b`` is reachable from ``a`` in <= ``max_hops`` edges.

        ``a == b`` counts as a path of zero hops (returns True). Unknown
        nodes simply have no edges, so they are unreachable unless equal.
        """
        if a == b:
            return True
        if max_hops < 1:
            return False
        seen = {a}
        frontier = [a]
        for _ in range(max_hops):
            nxt: list[str] = []
            for node in frontier:
                for nb in self._adj.get(node, ()):
                    if nb == b:
                        return True
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
        return False

    def _reachable_within(self, start: str, max_hops: int) -> set[str]:
        """All nodes reachable from ``start`` in <= ``max_hops`` edges."""
        seen = {start}
        frontier = [start]
        for _ in range(max_hops):
            nxt: list[str] = []
            for node in frontier:
                for nb in self._adj.get(node, ()):
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
        return seen

    def reachable_within(self, name: str, max_hops: int) -> set[str]:
        """Public distance query: every node within ``max_hops`` edges of ``name``.

        The returned set always includes ``name`` itself (zero hops). Unknown
        nodes have no edges, so the set is ``{name}`` for them. This is the
        adjacency-region primitive the user-relative 40/40/20 sampler uses
        (Iteration-2 A1b).
        """
        return self._reachable_within(name, max_hops)

    def distance(self, a: str, b: str) -> int | None:
        """Shortest path length between ``a`` and ``b``; ``None`` if unreachable.

        ``distance(a, a) == 0``. Unknown nodes are only reachable from
        themselves. The graph is undirected, so the result is symmetric.
        """
        if a == b:
            return 0
        if a not in self._adj or b not in self._adj:
            return None
        seen = {a}
        frontier = [a]
        hops = 0
        while frontier:
            hops += 1
            nxt: list[str] = []
            for node in frontier:
                for nb in self._adj.get(node, ()):
                    if nb == b:
                        return hops
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
        return None

    # -- sampling --------------------------------------------------------

    def sample_exact(self, rng) -> str:
        """Uniform random cluster hub (an exact interest)."""
        hubs = self.hubs()
        if not hubs:
            raise ValueError("graph has no hub nodes; call add_hub() first")
        return hubs[int(rng.integers(0, len(hubs)))]

    def sample_adjacent(self, rng) -> str:
        """Uniform random node within ``MAX_ADJACENCY_HOPS`` of a hub.

        The returned node is never a hub itself and always has a path of
        <= ``MAX_ADJACENCY_HOPS`` edges to at least one hub.
        """
        reachable: set[str] = set()
        for hub in self.hubs():
            reachable |= self._reachable_within(hub, MAX_ADJACENCY_HOPS)
        candidates = sorted(reachable - self._hubs)
        if not candidates:
            raise ValueError("no node is within adjacency range of any hub")
        return candidates[int(rng.integers(0, len(candidates)))]

    def sample_independent(self, rng) -> str:
        """Uniform random node with NO path of <= ``MAX_ADJACENCY_HOPS`` to any hub."""
        reachable: set[str] = set()
        for hub in self.hubs():
            reachable |= self._reachable_within(hub, MAX_ADJACENCY_HOPS)
        candidates = sorted(set(self._adj) - reachable)
        if not candidates:
            raise ValueError("no node is independent of every hub")
        return candidates[int(rng.integers(0, len(candidates)))]


def build_catalog() -> InterestGraph:
    """Hand-built default catalog: six clusters + sparse cross edges + island.

    Cluster members connect to their hub with strength 0.6; cross-cluster
    edges and island edges use the strengths in ``CROSS_EDGES``/``ISLAND_EDGES``.
    Returns a fresh graph on every call (the caller owns it).
    """
    graph = InterestGraph()
    for hub, members in CLUSTERS.items():
        graph.add_hub(hub)
        for member in members:
            graph.add_relation(hub, member, 0.6)
    for from_, to, strength in CROSS_EDGES:
        graph.add_relation(from_, to, strength)
    for from_, to, strength in ISLAND_EDGES:
        graph.add_relation(from_, to, strength)
    return graph
