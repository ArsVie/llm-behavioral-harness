"""Typed interest graph and the hand-built companion catalog.

Buckets are structural: ``exact`` = a cluster hub, ``adjacent`` = within
``MAX_ADJACENCY_HOPS`` edges of one, ``independent`` = beyond that.
"""

from __future__ import annotations

#: Maximum number of edges that still counts as "adjacent" to an exact interest.
MAX_ADJACENCY_HOPS = 3

#: Cluster hubs (exact-interest candidates) -> their members (strength 0.6).
#: A node name must mean ONE thing alone: every consumer renders it verbatim.
CLUSTERS: dict[str, tuple[str, ...]] = {
    "mathematics": ("physics", "statistics", "logic puzzles", "programming"),
    "metal music": ("rock music", "live music", "guitar", "alternative music"),
    "literature": ("poetry", "fantasy novels", "book clubs"),
    "outdoors": ("hiking", "running", "camping"),
    "food": ("cooking", "baking", "coffee brewing"),
    "art": ("drawing", "photography", "pottery"),
}

#: Sparse leaf-to-leaf cross edges (from, to, strength); never hub-to-hub
#: and at most one per leaf.
CROSS_EDGES: tuple[tuple[str, str, float], ...] = (
    ("programming", "guitar", 0.20),  # mathematics x metal music
    ("logic puzzles", "poetry", 0.15),  # mathematics x literature
    ("hiking", "photography", 0.25),  # outdoors x art
    ("coffee brewing", "book clubs", 0.20),  # food x literature
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

    Nodes are created implicitly by ``add_relation``/``add_hub``/``add_node``;
    edges are symmetric (``add_relation(a, b, s)`` connects both directions).
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

    def add_node(self, name: str) -> None:
        """Register a node with no edges (an isolated interest)."""
        self._adj.setdefault(name, {})

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

    def edges(self) -> list[tuple[str, str, float]]:
        """Every edge as ``(from, to, strength)``, each pair once (``from < to``),
        sorted.
        """
        out: list[tuple[str, str, float]] = []
        for src in sorted(self._adj):
            for dst, strength in sorted(self._adj[src].items()):
                if src < dst:
                    out.append((src, dst, float(strength)))
        return out

    def isolated(self) -> list[str]:
        """Nodes with no edges, sorted (they still belong to the graph)."""
        return sorted(n for n, adj in self._adj.items() if not adj)

    def path_exists(self, a: str, b: str, max_hops: int = 3) -> bool:
        """True if ``b`` is reachable from ``a`` in <= ``max_hops`` edges.

        ``a == b`` counts as zero hops; unknown nodes are unreachable unless equal.
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
        """Every node within ``max_hops`` edges of ``name``, ``name`` included.

        Unknown nodes have no edges, so the set is ``{name}`` for them.
        """
        return self._reachable_within(name, max_hops)

    def distance(self, a: str, b: str) -> int | None:
        """Shortest path length between ``a`` and ``b``; ``None`` if unreachable.

        ``distance(a, a) == 0``; unknown nodes are only reachable from themselves.
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
        """Uniform random node within ``MAX_ADJACENCY_HOPS`` of a hub (never a hub)."""
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
    """Hand-built default catalog: six clusters, sparse cross edges, an island.

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
