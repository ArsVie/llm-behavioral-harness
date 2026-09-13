"""Graph extension for user interests the built-in catalog does not contain.

Places unknown interests into the graph at onboarding (one bounded model call)
so the structural sampler has neighbours to draw from; any failure falls back
to :func:`heuristic_extension`.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
from dataclasses import dataclass

from harness import proposal_cache
from harness.interests import InterestGraph

#: Edge strength for a new interest to one of its own proposed neighbours.
NEIGHBOUR_STRENGTH = 0.6

#: Edge strength for a proposed cross-edge into the existing catalog, at the
#: catalog's own cross-edge ceiling.
CROSS_STRENGTH = 0.25

#: Neighbours proposed per unknown interest (fewer accepted, more truncated).
NEIGHBOURS_PER_INTEREST = 4

#: Bridges asked for per missing interest: links back into the EXISTING
#: catalog, as an exact count (a bridge landing elsewhere is dropped).
BRIDGES_PER_INTEREST = 2

#: Hard cap on nodes one extension may add, whatever the model returns.
MAX_NEW_NODES = 40

#: Longest accepted interest name, in characters.
MAX_NAME_CHARS = 40

#: Reasoning effort for the onboarding call: a naming task in front of a
#: waiting user, not one that rewards deliberation.
EXTENSION_REASONING_EFFORT = "low"

#: Wall-clock budget for the onboarding call, in seconds. The client retries
#: for minutes without raising, so the wait itself must be bounded.
EXTENSION_BUDGET_S = 90.0

# proposal cache

#: Cache namespace and schema marker for this builder's proposals. Bump the
#: schema whenever the prompt or the accepted shape changes.
CACHE_NAMESPACE = "interest-extension"
CACHE_SCHEMA = "v2-exact-counts"


def _cache_payload(known, unknown) -> dict:
    """The inputs that define this question, for the cache key."""
    return {
        "known": list(known),
        "unknown": list(unknown),
        "n": NEIGHBOURS_PER_INTEREST,
        "b": BRIDGES_PER_INTEREST,
    }


def load_cached_proposal(known, unknown) -> dict | None:
    """A previously accepted proposal for this exact question, or None."""
    cached = proposal_cache.load(
        CACHE_NAMESPACE, CACHE_SCHEMA, _cache_payload(known, unknown)
    )
    return cached if isinstance(cached, dict) else None


def store_cached_proposal(known, unknown, proposal: dict) -> None:
    """Cache an accepted proposal. Best-effort: failures are swallowed."""
    if not isinstance(proposal, dict) or not proposal:
        return
    proposal_cache.store(
        CACHE_NAMESPACE, CACHE_SCHEMA, _cache_payload(known, unknown), proposal
    )


_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9 \-'&/]*$")

#: Bare words that mean two different things: a proposal naming one of these
#: alone is rejected (the qualified form passes -- "metal music", not "metal").
AMBIGUOUS_BARE_NAMES: frozenset[str] = frozenset({
    "metal",      # music, or metalworking
    "rock",       # music, climbing, or geology
    "fantasy",    # fiction, or fantasy sports
    "coffee",     # drinking, brewing, or cafe culture
    "puzzles",    # logic, jigsaw, or crossword
    "drama",      # theatre, or the genre
    "classical",  # music, or antiquity
    "pop",        # music, or pop culture
    "board",      # board games, or surfing/skating
    "cards",      # card games, or collecting
    "shooting",   # photography, sport, or firearms
    "swings",     # dance, or playground
    "bass",       # instrument, or fish
})

EXTENSION_PROMPT = """\
Existing interests: {known}

For each of these: {unknown}

give exactly {n} "neighbours" (related pursuits a person into it would also
be into) and exactly {b} "bridges" chosen FROM the existing list above.

Names: lowercase, one to three words, each meaning one thing on its own —
"metal music" not "metal", "oil painting" not "art". Something a person does.

JSON only:
{{"anime": {{"neighbours": ["manga", "..."], "bridges": ["literature", "..."]}}}}
"""


@dataclass(frozen=True)
class ExtensionResult:
    """What an extension actually did, for logging and for the /setup reply."""

    graph: InterestGraph
    added_nodes: tuple[str, ...]
    added_edges: int
    source: str  # 'model' | 'cache' | 'heuristic' | 'none'

    @property
    def extended(self) -> bool:
        return bool(self.added_nodes) or self.added_edges > 0


def _clean_name(raw: object) -> str | None:
    """Normalize a proposed name, or None when it is not usable."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.strip().lower().split())
    if not name or len(name) > MAX_NAME_CHARS:
        return None
    if not _NAME_OK.match(name):
        return None
    if name in AMBIGUOUS_BARE_NAMES:
        return None
    return name


def unknown_interests(graph: InterestGraph, interests) -> tuple[str, ...]:
    """User interests with no node in ``graph`` (order preserved, de-duped)."""
    known = set(graph.nodes())
    out: list[str] = []
    for raw in interests:
        name = _clean_name(raw)
        if name is None or name in known or name in out:
            continue
        out.append(name)
    return tuple(out)


def heuristic_extension(graph: InterestGraph, unknown: tuple[str, ...]) -> ExtensionResult:
    """Offline fallback: each unknown interest becomes its own small hub.

    No invented neighbours; a later model-backed extension can attach them.
    """
    added: list[str] = []
    for name in unknown:
        graph.add_hub(name)
        added.append(name)
    return ExtensionResult(
        graph=graph, added_nodes=tuple(added), added_edges=0,
        source="heuristic" if added else "none",
    )


def apply_proposal(
    graph: InterestGraph,
    unknown: tuple[str, ...],
    proposal: dict,
) -> ExtensionResult:
    """Validate a proposal and merge it into ``graph`` (mutated in place).

    Only the interests we ASKED about are accepted as roots; names are
    normalized and length-capped; an edge must touch at least one NEW node (a
    proposal can never rewire pre-existing catalog adjacency); bridges must name
    a node that already exists; the node budget is enforced regardless of
    response size. Roots are walked in the order they were asked about, never
    the proposal's own key order (the budget cuts off by iteration order).
    """
    known_before = set(graph.nodes())
    body_by_root: dict[str, dict] = {}
    for raw_root, raw_body in (proposal or {}).items():
        name = _clean_name(raw_root)
        if name is not None and name not in body_by_root:
            body_by_root[name] = raw_body if isinstance(raw_body, dict) else {}

    added_nodes: list[str] = []
    added_edges = 0

    def _register(name: str) -> None:
        if name not in known_before and name not in added_nodes:
            added_nodes.append(name)

    for asked_root in unknown:
        root = _clean_name(asked_root)
        if root is None or root not in body_by_root:
            continue
        body = body_by_root[root]
        graph.add_hub(root)
        _register(root)

        neighbours = body.get("neighbours")
        neighbours = neighbours if isinstance(neighbours, list) else []
        for raw in neighbours[:NEIGHBOURS_PER_INTEREST]:
            name = _clean_name(raw)
            if name is None or name == root:
                continue
            if len(added_nodes) >= MAX_NEW_NODES and name not in known_before:
                continue
            _register(name)
            graph.add_relation(root, name, NEIGHBOUR_STRENGTH)
            added_edges += 1

        bridges = body.get("bridges")
        bridges = bridges if isinstance(bridges, list) else []
        for raw in bridges[:BRIDGES_PER_INTEREST]:
            name = _clean_name(raw)
            # A bridge must land on a pre-existing node and the root is new, so
            # the edge always touches a new node.
            if name is None or name == root or name not in known_before:
                continue
            graph.add_relation(root, name, CROSS_STRENGTH)
            added_edges += 1

    # Anything the response ignored still has to land, or it keeps an empty region.
    for name in unknown:
        if name not in graph.nodes():
            graph.add_hub(name)
            _register(name)

    return ExtensionResult(
        graph=graph, added_nodes=tuple(added_nodes), added_edges=added_edges,
        source="model" if added_edges else "heuristic",
    )


def _parse_proposal(text: str) -> dict | None:
    """Pull the JSON object out of a model reply; None when unusable."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _make_call(client):
    """The zero-argument model call for this client, or None.

    Prefers ``chat_with_meta`` (the only surface taking ``json_mode`` and
    ``reasoning_effort``; ``max_tokens`` stays unset). Falls back to the plain
    ``chat`` surface for minimal clients and fakes.
    """
    rich = getattr(client, "chat_with_meta", None) if client is not None else None
    if rich is not None:
        def _call(prompt: str):
            result = rich(
                # Aux task call, not an event in her conversation.
                [{"role": "user", "content": prompt}],
                system="You return JSON only.",
                temperature=0.3,
                json_mode=True,
                max_tokens=None,
                reasoning_effort=EXTENSION_REASONING_EFFORT,
            )
            return getattr(result, "content", result)

        return _call

    plain = getattr(client, "chat", None) if client is not None else None
    if plain is None:
        return None

    def _call(prompt: str):
        return plain(
            [{"role": "user", "content": prompt}],
            system="You return JSON only.",
            temperature=0.3,
        )

    return _call


def _call_within_budget(call, prompt: str, budget_s: float):
    """Run the model call with a hard wall-clock deadline.

    A worker thread makes the deadline enforceable without touching the shared
    client's configuration; it raises nothing while it waits, so the wait itself
    is what gets bounded. Returns the reply text, or raises TimeoutError /
    whatever the call raised; on timeout the thread is left to finish on its own.
    """
    # not a `with` block: its __exit__ blocks on shutdown(wait=True).
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="interest-extension"
    )
    try:
        future = pool.submit(call, prompt)
        return future.result(timeout=budget_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def extend_graph_for_user(
    graph: InterestGraph,
    interests,
    *,
    client=None,
    logger=None,
    budget_s: float = EXTENSION_BUDGET_S,
) -> ExtensionResult:
    """Place a user's off-catalog interests into ``graph``.

    At most one model call, at onboarding, bounded by ``budget_s``; None, any
    failure, or an overrun falls back to :func:`heuristic_extension`, so
    onboarding always completes. A previously accepted proposal for the same
    interest set is reused from the on-disk cache (``source='cache'``).
    """
    unknown = unknown_interests(graph, interests)
    if not unknown:
        return ExtensionResult(graph=graph, added_nodes=(), added_edges=0, source="none")

    known = tuple(graph.nodes())
    cached = load_cached_proposal(known, unknown)
    if cached is not None:
        if logger is not None:
            logger("interest extension: reusing the cached proposal (no call)")
        result = apply_proposal(graph, unknown, cached)
        return ExtensionResult(
            graph=result.graph,
            added_nodes=result.added_nodes,
            added_edges=result.added_edges,
            source="cache",
        )

    call = _make_call(client)
    if call is None:
        return heuristic_extension(graph, unknown)

    prompt = EXTENSION_PROMPT.format(
        known=", ".join(graph.nodes()),
        unknown=", ".join(unknown),
        n=NEIGHBOURS_PER_INTEREST,
        b=BRIDGES_PER_INTEREST,
    )
    try:
        reply = _call_within_budget(call, prompt, budget_s)
    except concurrent.futures.TimeoutError:
        if logger is not None:
            logger(
                f"interest extension: no reply within {budget_s:.0f}s; "
                "using fallback"
            )
        return heuristic_extension(graph, unknown)
    except Exception as exc:  # noqa: BLE001 - intentional: onboarding must survive provider errors.
        if logger is not None:
            logger(f"interest extension: model call failed ({exc}); using fallback")
        return heuristic_extension(graph, unknown)

    proposal = _parse_proposal(reply if isinstance(reply, str) else "")
    if proposal is None:
        if logger is not None:
            logger("interest extension: unparseable reply; using fallback")
        return heuristic_extension(graph, unknown)
    # Cached only once it parsed: a fallback reply is not worth replaying.
    store_cached_proposal(known, unknown, proposal)
    return apply_proposal(graph, unknown, proposal)
