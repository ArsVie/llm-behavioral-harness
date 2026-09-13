"""Idempotent clean-start bootstrap — blank DB → coherent companion.

``ensure_companion_initialized`` resolves a user profile, builds a
USER-relative persona, then ensures initial life arcs and today's agenda.
The persona row is the bootstrap-complete marker: once it exists, every
downstream step is skipped.
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
DEFAULT_USER_NAME = "User"
DEFAULT_USER_INTERESTS: tuple[str, ...] = (
    "mathematics",
    "lifting",
    "anime",
    "history",
)


@dataclass(frozen=True)
class OnboardingConfig:
    """Minimal structured onboarding fallback — a config/CLI representation, no
    UI. Used when no UserProfile exists and none is supplied to the bootstrap.
    """

    user_name: str = DEFAULT_USER_NAME
    user_interests: tuple[str, ...] = DEFAULT_USER_INTERESTS
    #: Graph distance that still counts as "adjacent" to a user interest.
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

    The interest sentence is carried across VERBATIM — it cannot be
    regenerated, being built in draw order while stored interests are
    name-sorted. Returns the stored persona untouched when the composed core
    already matches.
    """
    parts = split_core(persona.core)
    if parts is None:
        return persona  # unrecognized shape: leave it strictly alone
    _, sentence = parts
    desired = compose_core(voice if voice is not None else DEFAULT_VOICE, sentence)
    # The display name is applied over the core downstream, so compare against
    # the same substitution.
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
    """Idempotent clean-start initialization.

    :param store: the persistence seam (``SQLiteStore`` or a duck-typed fake).
    :param seed: master seed — the persona (stream 5) and life (stream 4)
        draws are fully determined by it.
    :param user: the user's onboarding identity; when ``None`` the bootstrap
        falls back to the stored profile seam, then ``config``, then the
        module defaults.
    :param config: minimal structured onboarding configuration.
    :param graph: interest catalog; defaults to the graph persisted by a
        previous bootstrap, else ``build_catalog()``.
    :param day: the day whose agenda must exist on return (the caller owns
        the clock — no real-clock reads here).
    :param client: optional LLM client used ONCE, on a cold start, to place
        the user's off-catalog interests into the interest graph
        (``harness.interest_extension``). None keeps onboarding fully offline.
    :param logger: optional ``callable(str)`` for onboarding progress lines.
    """
    # A stored graph wins: the persona's buckets must stay reproducible from
    # the store alone, so a resumed run samples against the same graph.
    if graph is None:
        loader = getattr(store, "load_interest_graph", None)
        graph = loader() if loader is not None else None
    if graph is None:
        graph = build_catalog()

    # 1. Identity: the persona row is the bootstrap-complete marker. The
    # authored voice comes from the configured persona file, on warm starts too.
    voice = load_authored_core(logger=logger)
    persona = store.load_persona() if hasattr(store, "load_persona") else None
    if persona is None:
        profile = _resolve_user_profile(store, user, config)
        cfg = config if config is not None else OnboardingConfig()
        # Place the user's off-catalog interests INTO the graph before
        # sampling: without this they are exact-only and add nothing adjacent.

        # Onboarding's aux calls reach the ledger like any other; the clock is
        # the day being onboarded, since none is read here.
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
        # The catalog is only the pool: build_persona still makes the seeded
        # draw over it.
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

    # 2. Life arcs: ensure initial arcs only when none exist.
    arcs = store.list_life_arcs() if hasattr(store, "list_life_arcs") else []
    if not arcs:
        arcs = init_life(seed, persona, store)
    else:
        # Canonical order (creation order == id order for arc_1..arc_N), so
        # agenda generation draws identically on fresh and resumed stores.
        arcs = sorted(arcs, key=lambda a: a.id)

    # 3. Today's agenda: ensure it only when missing.
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
