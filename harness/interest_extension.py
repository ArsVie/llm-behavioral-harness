"""Graph extension for user interests the built-in catalog does not contain.

The problem this solves
-----------------------
``harness.persona.build_persona`` samples the 40/40/20 portfolio STRUCTURALLY:
exact = a user interest, adjacent = within ``MAX_ADJACENCY_HOPS`` edges of one,
independent = outside that region. That is a deliberate invariant -- the mix is
true by construction rather than asserted after the fact, which is why
``harness.interests`` says "bucket semantics (structural, never LLM-decided)".

But the catalog is 29 hand-written nodes, and a real user's interests mostly
are not in it. ``build_persona`` accepts an off-catalog interest as exact and
notes that "their adjacency region is just themselves" -- so it contributes
ZERO adjacent candidates. With ``("mathematics", "lifting", "anime",
"history")`` only ``mathematics`` is anchored, the whole adjacent bucket is
drawn 4-from-8 out of one cluster, and the companion ends up a mathematician
who merely lists anime. That is the mechanism behind an agenda of statistics
exercises.

What this module does
---------------------
Uses the model ONCE, at onboarding, to place unknown interests INTO the graph
-- proposing neighbour nodes and cross-edges to existing clusters -- and then
gets out of the way. The structural sampler runs over the extended graph
unchanged, so:

* buckets stay computed, never LLM-assigned (the frozen invariant holds);
* the run stays deterministic -- the extension is persisted and reloaded, so
  the same store always samples against the same graph;
* there is no per-day or per-turn cost, and no model call on a warm start.

Failure is never fatal: no client, a bad response, or a refusal all fall back
to :func:`heuristic_extension`, which attaches each unknown interest to the
graph as its own small hub. That is strictly better than the status quo (an
isolated node with no adjacency region at all) and keeps onboarding offline.

Safety of the proposal
----------------------
The model proposes NAMES ONLY. Every edge is validated before it lands:
strengths are clamped, self-edges and duplicates dropped, names normalized and
length-capped, the node budget enforced, and -- critically -- a proposal may
never rewire the existing catalog. New edges must touch at least one new node,
so an extension can add reachability but can never silently change what
"adjacent" meant for interests that were already there.
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

#: Edge strength for a proposed cross-edge into the existing catalog. Kept at
#: the catalog's own cross-edge ceiling so an extension never creates a
#: shortcut stronger than the hand-built structure.
CROSS_STRENGTH = 0.25

#: Neighbours proposed per unknown interest (the model is asked for this many;
#: fewer is accepted, more is truncated).
NEIGHBOURS_PER_INTEREST = 4

#: Bridges asked for per missing interest — links back into the EXISTING
#: catalog. Asked as an exact count, not a range: "zero to two" invited a
#: model that had already spent its budget to answer zero, and an interest
#: with no bridge is an island that contributes nothing to the adjacency
#: draw. The 2026-09-08 cold start produced exactly that — three interests,
#: one edge each. Validation still drops any bridge that does not land on a
#: pre-existing node, so asking for a fixed count cannot invent structure:
#: the worst case is a weak-but-real link instead of no link.
BRIDGES_PER_INTEREST = 2

#: Hard cap on nodes one extension may add, whatever the model returns.
MAX_NEW_NODES = 40

#: Longest accepted interest name, in characters.
MAX_NAME_CHARS = 40

#: Reasoning effort for the onboarding call.
#:
#: This is a naming task -- list related interests, emit JSON -- not one that
#: rewards deliberation, and it sits in front of a person waiting on
#: ``/setup``. Deep reasoning buys nothing here and costs the whole latency
#: budget: the call is made against a reasoning-capable model, and with no
#: effort sent at all it inherits the provider's default, which is how a
#: 240s attempt timed out with nothing to show (2026-09-07).
#:
#: Deliberately NOT read from ``HARNESS_THINKING_EFFORT``: that setting is
#: for conversation turns, and an onboarding utility call has no business
#: inheriting how hard the companion thinks when she talks.
EXTENSION_REASONING_EFFORT = "low"

#: Wall-clock budget for the onboarding call, in seconds.
#:
#: Sized from measurement, not taste. On the free lane the real prompt took
#: 25.3s / 33.6s / 59.2s across three runs (2026-09-07) -- a trivial probe on
#: the same client returned in 1.4s, so this is throughput throttling on a
#: ~400-character JSON reply, not a hard task. A 30s budget missed two runs
#: in three; 90s covers the observed spread with headroom.
#:
#: The bound exists because the client retries hard by design -- 7 attempts
#: at a 60s timeout plus exponential backoff, roughly SEVEN MINUTES -- and no
#: exception is raised while it waits, so catching errors is not enough. That
#: unbounded storm is the danger, not a bounded wait.
#:
#: Blocking for up to 90s is tolerable HERE and almost nowhere else: this
#: call only ever happens on a cold start, when there is no persona, no
#: conversation and no proactive schedule, so the runtime lock it holds is
#: not keeping anything else from running. Do not reuse this budget for a
#: call on a live turn.
#:
#: The fallback still matters: an extension is an enrichment, and a heuristic
#: graph in 90s beats a perfect one that never arrives.
EXTENSION_BUDGET_S = 90.0

# proposal cache

#: Cache namespace and schema marker for this builder's proposals. Bump the
#: schema whenever the prompt or the accepted shape changes, so a proposal
#: answered under different instructions is never reused. See
#: :mod:`harness.proposal_cache` for why the cache is on disk.
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

#: Bare words that mean two different things. A proposal naming one of these
#: alone is rejected -- the name would reach the model verbatim (as an
#: interest, an agenda activity, a life-arc name) and get read as the wrong
#: sense, and the mistake would be persisted into the graph. The qualified
#: form is always accepted ("metal music" passes, "metal" does not), so this
#: costs the model nothing except precision.
#:
#: Same rule the catalog itself follows since 2026-09-07 (see
#: ``harness.interests.CLUSTERS``).
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

    No invented neighbours -- there is nothing honest to invent offline. The
    interest is registered as a HUB so it anchors its own cluster; the sampler
    then treats it as a real exact interest rather than a floating name, and a
    later model-backed extension can attach neighbours to it.
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

    Every rule that keeps an extension safe lives here, so a hostile or
    malformed response cannot corrupt the structure:

    * only the interests we ASKED about are accepted as roots;
    * names are normalized and length-capped, junk is dropped;
    * an edge must touch at least one NEW node, so pre-existing catalog
      adjacency can never be rewired by a proposal;
    * bridges must name a node that already exists;
    * the node budget is enforced regardless of response size.

    Roots are walked in the order they were ASKED about, never in the
    proposal's own key order. The node budget cuts off by iteration order, so
    a dict that arrived sorted (which is how a cached proposal comes back off
    disk) would otherwise build a subtly different graph than the same answer
    fresh from the model.
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
            # A bridge must land on something that already existed, and the
            # root is new -- so the edge always touches a new node and can
            # never rewire two pre-existing catalog nodes to each other.
            if name is None or name == root or name not in known_before:
                continue
            graph.add_relation(root, name, CROSS_STRENGTH)
            added_edges += 1

    # Anything we asked about that the response ignored still has to land,
    # or it keeps its empty adjacency region.
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

    Prefers ``chat_with_meta``: it is the only surface that takes
    ``json_mode`` and ``reasoning_effort``, and both matter here --
    ``response_format`` keeps the reply parseable instead of prose-wrapped,
    and low effort keeps a reasoning model from spending the whole latency
    budget thinking about a list of hobbies. ``max_tokens`` stays unset (the
    repo pitfall: never cap a reasoning model).

    Falls back to the plain ``chat`` surface for minimal clients and fakes,
    which simply cannot carry those two options.
    """
    rich = getattr(client, "chat_with_meta", None) if client is not None else None
    if rich is not None:
        def _call(prompt: str):
            result = rich(
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

    The client's own retry policy can span minutes, and no exception is
    raised while it waits -- so catching errors is not enough, the WAIT
    itself has to be bounded. A worker thread makes the deadline enforceable
    without touching the shared client's configuration. On timeout the thread
    is left to finish on its own (it holds no lock and writes nothing) and
    the caller falls back.

    Returns the reply text, or raises TimeoutError / whatever the call raised.
    """
    # NOT a `with` block: ThreadPoolExecutor.__exit__ calls
    # shutdown(wait=True), which re-blocks for the full call and silently
    # undoes the deadline. Shut down explicitly, without waiting.
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

    At most one model call, at onboarding, bounded by ``budget_s``. ``client``
    is any object with the harness ``chat`` signature; None, any failure, or a
    call that overruns the budget falls back to :func:`heuristic_extension`,
    so onboarding always completes and never hangs the caller.

    A previously accepted proposal for the same SET of interests is reused
    from the on-disk cache and costs no call at all (``source='cache'``) —
    the case that matters is a DB reset with an unchanged interest list,
    which must not re-roll the dice on a slow gateway.
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
    except Exception as exc:  # noqa: BLE001 - deliberate: onboarding must
        # never die on a provider error, and a provider can raise anything.
        if logger is not None:
            logger(f"interest extension: model call failed ({exc}); using fallback")
        return heuristic_extension(graph, unknown)

    proposal = _parse_proposal(reply if isinstance(reply, str) else "")
    if proposal is None:
        if logger is not None:
            logger("interest extension: unparseable reply; using fallback")
        return heuristic_extension(graph, unknown)
    # Cached only once it parsed: a reply that fell back to the heuristic is
    # not an answer worth replaying on the next reset.
    store_cached_proposal(known, unknown, proposal)
    return apply_proposal(graph, unknown, proposal)
