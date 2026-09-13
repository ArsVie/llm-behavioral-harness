"""Idempotent clean-start bootstrap — blank DB → coherent companion (A1, Iteration-2).

``ensure_companion_initialized`` walks the clean-start chain:

    DB has persona?
    ├─ yes → load it
    └─ no
        ↓
        resolve a UserProfile (stored profile seam > supplied ``user`` >
        ``OnboardingConfig`` > onboarding defaults)
        ↓
        build a USER-relative Companion Persona (40/40/20, a frozen persona invariant)
        ↓
        persist persona + interests
        ↓
        ensure initial life arcs (A2's ``init_life`` — only when none exist)
        ↓
        ensure today's agenda (A2's ``generate_agenda`` — only when missing)

Idempotency
-----------
The persona row is the bootstrap-complete marker: once it exists, every
downstream step is skipped, so repeated calls (any seed/user) never regenerate
identity, interests, arcs or agendas. ``save_persona`` replaces the interest
portfolio, ``save_agenda`` replaces a day's items, and ``init_life`` is only
invoked on an empty arc table — nothing in this chain can duplicate.

Determinism
-----------
All stochastic draws happen inside the consumed modules on their reserved
seeded streams — persona on stream key 5 (``harness.persona``), life on stream
key 4 (``harness.life``: ``stream_rng(seed, LIFE_STREAM)`` for init,
``stream_rng(seed, LIFE_STREAM, day)`` for the day's agenda). This module
reads no real clock and draws no randomness itself: the caller supplies the
day (``day``) and the seed.

Store seam
----------
Duck-typed subset of ``SQLiteStore``: ``load_persona``/``save_persona``,
``list_life_arcs``, ``load_agenda`` (plus, additively, the optional
``load_user_profile``/``save_user_profile`` persistence seam if A7 lands it —
absent today, the profile is resolved from the caller's config and carried in
the result). The seam is injected, so fakes and the real store both work
unchanged.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional

from engine.rng import stream_rng

from harness.domain import DailyAgenda, LifeArc, PersonaProfile, UserProfile
from harness.clock import VirtualClock
from harness.interest_extension import extend_graph_for_user
from harness.interests import InterestGraph, MAX_ADJACENCY_HOPS, build_catalog
from harness.life import LIFE_STREAM, generate_agenda, init_life
from harness.metering import MeteredClient
from harness.persona import (
    DEFAULT_NAME,
    DEFAULT_VOICE,
    ROUTINE_CATALOG,
    build_persona,
    compose_core,
    split_core,
)
from harness.persona_file import load_authored_core
from harness.routine_setup import build_routine_catalog

#: Onboarding defaults — the PRODUCT fallback identity.
#:
#: Deliberately NOT the ablation matrix's fixture
#: (``experiments.cvs_common.GATE2_USER_INTERESTS``). Those two were the same
#: list, and the live launcher fell back to the research fixture, so a real
#: trial built its 40/40/20 portfolio against an experiment's example user.
#: Keep them separate: this list is what a person gets, the fixture is what a
#: sweep gets.
#:
#: Several of these have no node in the built-in catalog. That is expected —
#: ``harness.interest_extension`` places them into the graph at onboarding, so
#: they carry a real adjacency region instead of being exact-only names.
DEFAULT_USER_NAME = "User"
DEFAULT_USER_INTERESTS: tuple[str, ...] = (
    "mathematics",
    "lifting",
    "anime",
    "history",
)


@dataclass(frozen=True)
class OnboardingConfig:
    """Minimal structured onboarding fallback — a config/CLI representation,
    no UI (plan §5-A1 task 3). Used when no UserProfile exists and none is
    supplied to the bootstrap.
    """

    user_name: str = DEFAULT_USER_NAME
    user_interests: tuple[str, ...] = DEFAULT_USER_INTERESTS
    #: Configured adjacency boundary: graph distance that still counts as
    #: "adjacent" to a user interest (plan §5-A1 task 2).
    adjacency_hops: int = MAX_ADJACENCY_HOPS
    n_exact: int = 4
    n_adjacent: int = 4
    n_independent: int = 2

    def to_user_profile(self) -> UserProfile:
        """Materialize the configured identity as a ``UserProfile``."""
        return UserProfile(name=self.user_name, interests=self.user_interests)


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of ``ensure_companion_initialized``: the identity chain that
    now exists in the store, as loaded/created by this call.
    """

    user_profile: UserProfile
    persona: PersonaProfile
    life_arcs: tuple[LifeArc, ...]
    today_agenda: DailyAgenda | None


class BootstrapStore:
    """Store seam subset used by the bootstrap (duck-typed; ``SQLiteStore``
    implements every member, test doubles may implement a subset).
    """

    def load_persona(self) -> PersonaProfile | None: ...
    def save_persona(self, profile: PersonaProfile) -> None: ...
    def list_life_arcs(self, status: str | None = None) -> list[LifeArc]: ...
    def load_agenda(self, day: int) -> DailyAgenda | None: ...
    def save_agenda(self, day: int, agenda: DailyAgenda) -> None: ...
    def load_user_profile(self) -> UserProfile | None: ...
    def save_user_profile(self, profile: UserProfile) -> None: ...


def _apply_authored_voice(store, persona, voice, logger):
    """Re-open a stored persona's core against the current persona file.

    Warm-start half of "the file is authoritative". The stored core is
    ``voice + interest sentence``: the voice is swapped and the interest
    sentence carried across VERBATIM, so not a single drawn interest, routine
    or seed is disturbed.

    The sentence is never regenerated. It is built from the portfolio in DRAW
    order while the stored interests are name-sorted, so recomputing it from a
    loaded persona silently rewrites which interests she is "absorbed in" —
    which is what the idempotency tests caught.

    Returns the persona to use — the stored one untouched when the composed
    core already matches, so an unchanged file is a no-op and the row is not
    rewritten on every start.
    """
    parts = split_core(persona.core)
    if parts is None:
        return persona  # unrecognized shape: leave it strictly alone
    _, sentence = parts
    desired = compose_core(voice if voice is not None else DEFAULT_VOICE, sentence)
    # The companion's display name is applied over the core downstream (the
    # live runner rewrites "Nova" to her configured name), so compare against
    # the same substitution rather than fighting it back and forth.
    if persona.name and persona.name != DEFAULT_NAME:
        desired = desired.replace(DEFAULT_NAME, persona.name)
    if desired == persona.core:
        return persona
    updated = dataclasses.replace(persona, core=desired)
    store.save_persona(updated)
    if logger is not None:
        logger(
            "persona voice refreshed from the persona file "
            f"({len(desired)} chars)"
        )
    return updated


def _resolve_user_profile(
    store,
    user: Optional[UserProfile],
    config: Optional[OnboardingConfig],
) -> UserProfile:
    """Stored profile seam > supplied ``user`` > ``config`` > defaults."""
    loader = getattr(store, "load_user_profile", None)
    if loader is not None:
        stored = loader()
        if stored is not None:
            return stored
    if user is not None:
        return user
    cfg = config if config is not None else OnboardingConfig()
    return cfg.to_user_profile()


def ensure_companion_initialized(
    store,
    *,
    seed: int,
    user: Optional[UserProfile] = None,
    config: Optional[OnboardingConfig] = None,
    graph: Optional[InterestGraph] = None,
    day: int = 1,
    client=None,
    logger=None,
) -> BootstrapResult:
    """Idempotent clean-start initialization (plan §5-A1 task 1).

    :param store: the persistence seam (``SQLiteStore`` or a duck-typed fake).
    :param seed: master seed — the persona (stream 5) and life (stream 4)
        draws are fully determined by it.
    :param user: the user's onboarding identity; when ``None`` the bootstrap
        falls back to the stored profile seam, then ``config``, then the
        module defaults (task 3 onboarding fallback).
    :param config: minimal structured onboarding configuration.
    :param graph: interest catalog; defaults to the graph persisted by a
        previous bootstrap, else ``build_catalog()``.
    :param day: the day whose agenda must exist on return (the caller owns
        the clock — no real-clock reads here).
    :param client: optional LLM client used ONCE, on a cold start, to place
        the user's off-catalog interests into the interest graph
        (``harness.interest_extension``). None keeps onboarding fully offline
        and falls back to the heuristic extension.
    :param logger: optional ``callable(str)`` for onboarding progress lines.
    """
    # A stored graph wins: the persona's buckets must stay reproducible from
    # the store alone, so a resumed run samples against the SAME graph the
    # persona was built on -- including any onboarding extension.
    if graph is None:
        loader = getattr(store, "load_interest_graph", None)
        graph = loader() if loader is not None else None
    if graph is None:
        graph = build_catalog()

    # 1. Identity: the persona row is the bootstrap-complete marker.
    #
    # The authored voice comes from the configured persona file, and the FILE
    # IS AUTHORITATIVE — on a warm start too, not just a cold one. Editing it
    # and restarting is the whole workflow. Before this, the voice existed
    # only as a hand-edited `persona.core` row, so a DB reset silently
    # replaced the companion with the generic default and nothing recorded
    # that anything had been lost (2026-09-08).
    voice = load_authored_core(logger=logger)
    persona = store.load_persona() if hasattr(store, "load_persona") else None
    if persona is None:
        profile = _resolve_user_profile(store, user, config)
        cfg = config if config is not None else OnboardingConfig()
        # Place the user's off-catalog interests INTO the graph before
        # sampling. Without this an unknown interest is exact-only with an
        # adjacency region of itself, so it contributes nothing to the 40%
        # adjacent bucket and the portfolio collapses onto whichever
        # interests the hand-built catalog happens to contain.
        # Onboarding's own model calls reach the ledger like any other
        # (BACKLOG: aux calls were invisible to spend accounting). No clock is
        # read in this function, so the row carries the day being onboarded.
        onboarding_clock = VirtualClock(float(day) * 24.0)

        def _metered(role: str):
            if client is None:
                return None      # offline onboarding keeps its heuristic paths
            return MeteredClient(
                client, store, onboarding_clock, role, logger=logger
            )

        extension = extend_graph_for_user(
            graph, profile.interests,
            client=_metered("aux_interest_extension"), logger=logger,
        )
        graph = extension.graph
        graph_saver = getattr(store, "save_interest_graph", None)
        if graph_saver is not None:
            graph_saver(graph)
        if logger is not None and extension.extended:
            logger(
                f"interest graph extended ({extension.source}): "
                f"+{len(extension.added_nodes)} nodes, "
                f"+{extension.added_edges} edges "
                f"({', '.join(extension.added_nodes) or 'none'})"
            )
        # Her daily rhythm, built for THESE interests rather than drawn from
        # six hardcoded rows every companion shared. The catalog is only the
        # pool: build_persona still makes the seeded draw over it, so the
        # portfolio invariants and replay determinism are untouched.
        routines = build_routine_catalog(
            profile.interests,
            default=ROUTINE_CATALOG,
            client=_metered("aux_routine_setup"),
            logger=logger,
        )
        if logger is not None:
            logger(
                f"routine catalog ({routines.source}): "
                f"{', '.join(r.name for r in routines.routines)}"
            )
        persona = build_persona(
            seed,
            graph=graph,
            user_interests=profile.interests,
            n_exact=cfg.n_exact,
            n_adjacent=cfg.n_adjacent,
            n_independent=cfg.n_independent,
            adjacency_hops=cfg.adjacency_hops,
            routine_catalog=routines.routines,
            voice=voice,
        )
        # Canonical interest order (name-sorted): the store reloads in name
        # order, keeping a loaded persona byte-identical to a fresh one.
        persona = dataclasses.replace(
            persona, interests=tuple(sorted(persona.interests, key=lambda i: i.name))
        )
        store.save_persona(persona)  # persists persona + interest portfolio
        saver = getattr(store, "save_user_profile", None)
        if saver is not None:
            saver(profile)
    else:
        profile = _resolve_user_profile(store, user, config)
        persona = _apply_authored_voice(store, persona, voice, logger)

    # 2. Life arcs: ensure initial arcs only when none exist (A2's init_life).
    arcs = store.list_life_arcs() if hasattr(store, "list_life_arcs") else []
    if not arcs:
        arcs = init_life(seed, persona, store)
    else:
        # Canonical order (creation order == id order for arc_1..arc_N), so
        # agenda generation draws identically on fresh and resumed stores.
        arcs = sorted(arcs, key=lambda a: a.id)

    # 3. Today's agenda: ensure it only when missing (A2's generate_agenda).
    agenda = store.load_agenda(day) if hasattr(store, "load_agenda") else None
    if agenda is None:
        agenda = generate_agenda(
            day, persona, arcs, store, rng=stream_rng(seed, LIFE_STREAM, day)
        )

    return BootstrapResult(
        user_profile=profile,
        persona=persona,
        life_arcs=tuple(arcs),
        today_agenda=agenda,
    )
