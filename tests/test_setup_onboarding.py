"""Onboarding: /setup works, identity persists, the graph gets extended.

Three things were broken together, and each has a guard here.

1. ``/setup`` had NO reachable success path on the live runtime.
   ``AsyncRuntime._on_command`` built its ``CommandContext`` without
   ``request_setup``, so the command refused either as "already initialized"
   (persona present) or with a ``--defer-bootstrap`` message naming a flag
   only ``sim/run_async.py`` had. It was advertised in ``/help`` and could
   not work.

2. The onboarding identity was never persisted. ``BootstrapStore`` declared
   ``load_user_profile``/``save_user_profile`` but no table backed them, so
   the profile was re-derived from the environment on every start and the
   store had no record of who the companion thinks she is talking to.

3. The live launcher's fallback identity was the ABLATION MATRIX FIXTURE.
   With the owner env vars unset (they were), a real trial built its whole
   40/40/20 portfolio around an experiment's example user.
"""

from __future__ import annotations

import json

import pytest

from harness import proposal_cache
from harness.bootstrap import (
    DEFAULT_USER_INTERESTS,
    ensure_companion_initialized,
)
from harness.channels.telegram import ControlCommand
from harness.clock import VirtualClock
from harness.commands import CommandContext, handle_command
from harness.domain import UserProfile
from harness.interest_extension import (
    MAX_NEW_NODES,
    apply_proposal,
    extend_graph_for_user,
    unknown_interests,
)
from harness.interests import MAX_ADJACENCY_HOPS, build_catalog
from harness.persona import build_persona
from tests.helpers.store import make_store

MINE = ("mathematics", "lifting", "anime", "history")


class StubExtender:
    """A client whose reply is a well-formed extension proposal."""

    def __init__(self, payload: str | None = None, boom: bool = False):
        self.payload = payload if payload is not None else json.dumps({
            "lifting": {"neighbours": ["strength training", "powerlifting"],
                        "bridges": ["outdoors"]},
            "anime": {"neighbours": ["manga", "animation"], "bridges": ["fantasy novels"]},
            "history": {"neighbours": ["archaeology", "museums"],
                        "bridges": ["literature"]},
        })
        self.boom = boom
        self.calls = 0

    def chat(self, messages, *, system=None, temperature=0.8, **kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("provider down")
        return self.payload


@pytest.fixture
def proposal_cache_dir(tmp_path, monkeypatch):
    """Enable the on-disk proposal cache, pointed at a throwaway directory.

    ``conftest`` disables it suite-wide so no test writes the developer's real
    cache; the caching tests opt back in here.
    """
    monkeypatch.setenv(proposal_cache.CACHE_ENV_VAR, str(tmp_path / "cache"))
    return tmp_path / "cache"


# -- 0. the proposal cache: a repeated interest set costs no call ---------- #


def test_the_same_interest_set_is_free_the_second_time(proposal_cache_dir):
    """The case that matters is a DB RESET with unchanged interests.

    On 2026-09-08 that reset paid the full call again, timed out at 90s, and
    handed the experiment a 31-relation graph where the run before it had 45.
    Two consecutive resets have to be comparable, so the accepted proposal is
    replayed from disk instead of re-rolled.
    """
    client = StubExtender()
    first = extend_graph_for_user(build_catalog(), MINE, client=client)
    assert first.source == "model" and client.calls == 1

    # No client at all: a miss would fall back to the heuristic, so resolving
    # the same nodes proves the answer came off disk.
    second = extend_graph_for_user(build_catalog(), MINE, client=None)
    assert second.source == "cache"
    assert second.added_nodes == first.added_nodes
    assert second.added_edges == first.added_edges


def test_interest_order_does_not_change_the_question(proposal_cache_dir):
    extend_graph_for_user(build_catalog(), MINE, client=StubExtender())
    hit = extend_graph_for_user(build_catalog(), tuple(reversed(MINE)), client=None)
    assert hit.source == "cache", "the same SET of interests must be one key"


def test_a_fallback_is_never_cached(proposal_cache_dir):
    """A heuristic answer is not worth replaying on the next reset."""
    extend_graph_for_user(
        build_catalog(), MINE, client=StubExtender(payload="not json"),
    )
    client = StubExtender()
    again = extend_graph_for_user(build_catalog(), MINE, client=client)
    assert again.source == "model" and client.calls == 1


# -- 1. the interest graph actually gets extended -------------------------- #


def test_off_catalog_interests_have_no_adjacency_without_extension():
    """The defect, pinned: an unknown interest anchors nothing.

    ``build_persona`` accepts it as exact and says its "adjacency region is
    just themselves" -- so it contributes zero adjacent candidates and the
    portfolio collapses onto whichever interests the catalog happens to have.
    """
    graph = build_catalog()
    assert unknown_interests(graph, MINE) == ("lifting", "anime", "history")
    pool = [
        n for n in graph.nodes()
        if n not in MINE
        and any(graph.path_exists(n, u, MAX_ADJACENCY_HOPS) for u in MINE)
    ]
    # Only `mathematics` anchors anything at all.
    assert len(pool) == 8


def test_extension_gives_unknown_interests_a_real_adjacency_region():
    graph = build_catalog()
    before = len(graph.nodes())
    result = extend_graph_for_user(graph, MINE, client=StubExtender())

    assert result.source == "model"
    assert "manga" in graph.nodes() and "archaeology" in graph.nodes()
    assert len(graph.nodes()) > before

    pool = [
        n for n in graph.nodes()
        if n not in MINE
        and any(graph.path_exists(n, u, MAX_ADJACENCY_HOPS) for u in MINE)
    ]
    assert len(pool) > 8, "the adjacency pool did not actually grow"

    # And the sampler now draws anime/lifting-adjacent interests, not just maths.
    persona = build_persona(8001, graph=graph, user_interests=MINE)
    adjacent = {i.name for i in persona.interests if i.bucket == "adjacent"}
    assert adjacent & {"manga", "animation", "strength training", "powerlifting",
                       "archaeology", "museums"}, adjacent


def test_buckets_stay_structural_not_model_assigned():
    """The frozen invariant: the model proposes NAMES, never buckets.

    Every bucket is recomputed from graph distance after the extension, so
    40/40/20 stays true by construction rather than by trusting a reply.
    """
    graph = build_catalog()
    extend_graph_for_user(graph, MINE, client=StubExtender())
    persona = build_persona(8001, graph=graph, user_interests=MINE)
    for interest in persona.interests:
        if interest.bucket == "exact":
            assert interest.name in MINE
        elif interest.bucket == "adjacent":
            assert any(
                graph.path_exists(interest.name, u, MAX_ADJACENCY_HOPS) for u in MINE
            ), f"{interest.name} is not actually adjacent"
        else:
            assert not any(
                graph.path_exists(interest.name, u, MAX_ADJACENCY_HOPS) for u in MINE
            ), f"{interest.name} is not actually independent"


@pytest.mark.parametrize("client", [None, StubExtender(boom=True),
                                    StubExtender(payload="not json at all")])
def test_onboarding_never_dies_on_the_extension(client):
    """No client, a provider error, or junk all fall back — never raise."""
    graph = build_catalog()
    result = extend_graph_for_user(graph, MINE, client=client)
    assert result.source == "heuristic"
    for name in ("lifting", "anime", "history"):
        assert name in graph.nodes()


def test_a_proposal_cannot_rewire_the_existing_catalog():
    """A hostile proposal may add reachability, never change what was there."""
    graph = build_catalog()
    before = set(graph.edges())
    apply_proposal(graph, ("anime",), {
        # not asked about -> ignored entirely
        "mathematics": {"neighbours": ["cooking"], "bridges": ["food"]},
        # a bridge between two PRE-EXISTING nodes must not be created
        "anime": {"neighbours": ["manga"], "bridges": ["metal music", "food"]},
    })
    after = set(graph.edges())
    catalog_pairs = {(a, b) for a, b, _ in before}
    new_pairs = {(a, b) for a, b, _ in after} - catalog_pairs
    assert before <= after, "an existing catalog edge was changed or dropped"
    for a, b in new_pairs:
        assert "anime" in (a, b), f"proposal rewired existing nodes: {a}-{b}"


def test_extension_respects_the_node_budget():
    flood = {"anime": {"neighbours": [f"thing {i}" for i in range(500)]}}
    graph = build_catalog()
    result = apply_proposal(graph, ("anime",), flood)
    assert len(result.added_nodes) <= MAX_NEW_NODES + 1


# -- 2. identity is persisted and reproducible ----------------------------- #


def test_bootstrap_persists_the_user_profile_and_graph(tmp_path):
    store = make_store(tmp_path, "boot.db")
    try:
        ensure_companion_initialized(
            store, seed=8001,
            user=UserProfile(name="Ars", interests=MINE),
            day=0, client=StubExtender(),
        )
        stored = store.load_user_profile()
        assert stored is not None
        assert stored.name == "Ars" and stored.interests == MINE
        graph = store.load_interest_graph()
        assert graph is not None and "manga" in graph.nodes()
    finally:
        store.close()


def test_resume_samples_against_the_stored_graph_not_the_catalog(tmp_path):
    """A warm start must not re-extend, and must not silently re-sample
    against the bare catalog — the persona's buckets have to stay
    reproducible from the store alone."""
    store = make_store(tmp_path, "resume.db")
    client = StubExtender()
    try:
        first = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE),
            day=0, client=client,
        )
        # Two setup calls on a cold start: the interest extension and the
        # routine catalog. The stub only answers the first shape, so the
        # routine builder falls back to the default catalog — which is the
        # point here: what must not happen is a THIRD call on resume.
        assert client.calls == 2
        second = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE),
            day=0, client=client,
        )
        assert client.calls == 2, "a warm start called the model again"
        assert first.persona == second.persona
    finally:
        store.close()


def test_product_default_is_not_the_ablation_fixture():
    """The live launcher's fallback must be the product identity."""
    from experiments.cvs_common import GATE2_USER_INTERESTS
    from experiments.live_companion import owner_profile

    assert DEFAULT_USER_INTERESTS != GATE2_USER_INTERESTS
    assert owner_profile().interests == DEFAULT_USER_INTERESTS


# -- 3. /setup succeeds on the live runtime -------------------------------- #


def test_setup_command_succeeds_when_the_hook_is_wired(tmp_path):
    """The end-to-end command path, on a blank store."""
    store = make_store(tmp_path, "setup.db")
    try:
        def _hook() -> str:
            boot = ensure_companion_initialized(
                store, seed=8001,
                user=UserProfile(name="Ars", interests=MINE),
                day=0, client=StubExtender(),
            )
            return f"{boot.persona.name} ready for {boot.user_profile.name}"

        ctx = CommandContext(
            store=store, clock=VirtualClock(t_h=8.0), request_setup=_hook,
        )
        reply = handle_command(ControlCommand(name="setup", args="", sender_id=1), ctx)
        assert reply.startswith("setup complete"), reply
        assert store.load_persona() is not None
        assert store.load_user_profile().name == "Ars"

        # ... and it refuses a second time rather than regenerating.
        again = handle_command(ControlCommand(name="setup", args="", sender_id=1), ctx)
        assert "already initialized" in again
    finally:
        store.close()


def test_live_runtime_wires_the_setup_hook():
    """The regression that made /setup dead: the hook must be in the context.

    Asserted against the source rather than a live runtime, because building
    one needs a channel, a schedule and a client — and what actually broke
    was a missing keyword at the CommandContext call site.
    """
    import inspect

    from harness.runtime import AsyncRuntime

    source = inspect.getsource(AsyncRuntime._on_command)
    assert "request_setup=" in source, (
        "AsyncRuntime builds its CommandContext without request_setup — "
        "/setup has no reachable success path on the live runtime"
    )
    assert hasattr(AsyncRuntime, "_request_setup")


# -- 4. interest names must mean one thing ---------------------------------- #


def test_no_catalog_name_is_an_ambiguous_bare_word():
    """A catalog name reaches the model verbatim, so it must mean ONE thing.

    ``metal`` produced the live life-arc "learning metal" — music, or
    metalworking? ``rock`` sits in the same graph as ``hiking`` and
    ``camping``, where rock climbing is a live misreading. Both are now
    qualified; this fails if a bare ambiguous word comes back.
    """
    from harness.interest_extension import AMBIGUOUS_BARE_NAMES

    offenders = sorted(set(build_catalog().nodes()) & AMBIGUOUS_BARE_NAMES)
    assert not offenders, (
        f"ambiguous bare names in the catalog: {offenders} — qualify them "
        "(e.g. 'metal' -> 'metal music'); every consumer renders the raw name"
    )


def test_extension_rejects_ambiguous_names_from_the_model():
    """The model may not reintroduce what the catalog just removed."""
    graph = build_catalog()
    apply_proposal(graph, ("anime",), {
        "anime": {"neighbours": ["metal", "rock", "manga", "fantasy"]},
    })
    nodes = set(graph.nodes())
    assert "manga" in nodes
    for bare in ("metal", "rock", "fantasy"):
        assert bare not in nodes, f"{bare!r} was accepted as a node"


def test_qualified_names_are_still_accepted():
    graph = build_catalog()
    apply_proposal(graph, ("anime",), {
        "anime": {"neighbours": ["japanese rock music", "fantasy novels"]},
    })
    nodes = set(graph.nodes())
    assert "japanese rock music" in nodes


# -- 5. a slow provider must not hang onboarding ---------------------------- #


def test_a_hanging_provider_falls_back_within_the_budget():
    """The defect a real 240s timeout exposed on 2026-09-07.

    The harness client retries 7 times at a 60s timeout with exponential
    backoff — right for a conversation turn, minutes-long here. ``/setup``
    runs inside the runtime lock, so an unresponsive provider would hold the
    entire bot with no reply. Catching exceptions is not enough: nothing is
    raised while it waits, so the WAIT itself has to be bounded.
    """
    import time

    class Hangs:
        def chat(self, messages, *, system=None, temperature=0.8, **kw):
            time.sleep(30)  # far past the budget this test sets
            return "{}"

    graph = build_catalog()
    lines: list[str] = []
    started = time.monotonic()
    result = extend_graph_for_user(
        graph, MINE, client=Hangs(), logger=lines.append, budget_s=0.5,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"onboarding blocked for {elapsed:.1f}s on a hung provider"
    assert result.source == "heuristic"
    assert any("no reply within" in line for line in lines), lines
    # ... and onboarding still completed: the interests are in the graph.
    for name in ("lifting", "anime", "history"):
        assert name in graph.nodes()


def test_the_budget_is_bounded_and_far_under_the_retry_storm():
    """Bounded, and well under what the client would do left alone.

    The client's own policy is 7 attempts at a 60s timeout plus exponential
    backoff — about seven minutes, during which nothing is raised. The budget
    has to cover the measured latency (25-59s observed on the free lane) and
    still cut in long before that.
    """
    from harness.interest_extension import EXTENSION_BUDGET_S

    assert 60 <= EXTENSION_BUDGET_S <= 120, (
        "below ~60s the budget cuts off calls that would have succeeded; "
        "above ~120s it stops being a bound on the retry storm"
    )


# -- 6. the onboarding call is configured for a fast, parseable answer ------ #


def test_extension_call_sends_low_effort_and_json_mode():
    """A naming task in front of a waiting person, not a reasoning problem.

    The first version called the thin ``chat`` surface, which accepts
    neither option — so no ``reasoning_effort`` was sent at all and the model
    inherited the provider's default. A 240s attempt against the real
    provider returned nothing (2026-09-07).
    """
    from harness.interest_extension import EXTENSION_REASONING_EFFORT

    seen = {}

    class Recorder:
        def chat_with_meta(self, messages, **kw):
            seen.update(kw)
            seen["messages"] = messages

            class R:
                content = '{"anime": {"neighbours": ["manga"]}}'
            return R()

    extend_graph_for_user(build_catalog(), ("anime",), client=Recorder())

    assert seen["reasoning_effort"] == EXTENSION_REASONING_EFFORT == "low"
    assert seen["json_mode"] is True
    # Never cap a reasoning model (repo pitfall 3af0a5a).
    assert seen["max_tokens"] is None


def test_extension_effort_is_independent_of_the_conversation_setting(monkeypatch):
    """HARNESS_THINKING_EFFORT governs conversation turns, not onboarding."""
    monkeypatch.setenv("HARNESS_THINKING_EFFORT", "high")
    seen = {}

    class Recorder:
        def chat_with_meta(self, messages, **kw):
            seen.update(kw)

            class R:
                content = "{}"
            return R()

    extend_graph_for_user(build_catalog(), ("anime",), client=Recorder())
    assert seen["reasoning_effort"] == "low"


def test_minimal_clients_without_chat_with_meta_still_work():
    """A fake exposing only ``chat`` must not break onboarding."""
    class Plain:
        def chat(self, messages, *, system=None, temperature=0.8, **kw):
            return '{"anime": {"neighbours": ["manga"]}}'

    graph = build_catalog()
    result = extend_graph_for_user(graph, ("anime",), client=Plain())
    assert result.source == "model"
    assert "manga" in graph.nodes()
