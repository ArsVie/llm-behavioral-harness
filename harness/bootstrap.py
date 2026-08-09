"""Idempotent clean-start bootstrap — blank DB → coherent companion (A1, Iteration-2).

``ensure_companion_initialized`` walks the plan's clean-start chain (§0 of
plans/iteration-2-integration-2026-08-09.md, §5-A1 task 1):

    DB has persona?
    ├─ yes → load it
    └─ no
        ↓
        resolve a UserProfile (stored profile seam > supplied ``user`` >
        ``OnboardingConfig`` > onboarding defaults)
        ↓
        build a USER-relative Companion Persona (40/40/20, plan §16 invariant 2)
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
from harness.interests import InterestGraph, MAX_ADJACENCY_HOPS, build_catalog
from harness.life import LIFE_STREAM, generate_agenda, init_life
from harness.persona import build_persona

#: Onboarding defaults (plan §8 Gate-2 example user).
DEFAULT_USER_NAME = "User"
DEFAULT_USER_INTERESTS: tuple[str, ...] = (
    "mathematics",
    "metal",
    "lifting",
    "movies",
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
) -> BootstrapResult:
    """Idempotent clean-start initialization (plan §5-A1 task 1).

    :param store: the persistence seam (``SQLiteStore`` or a duck-typed fake).
    :param seed: master seed — the persona (stream 5) and life (stream 4)
        draws are fully determined by it.
    :param user: the user's onboarding identity; when ``None`` the bootstrap
        falls back to the stored profile seam, then ``config``, then the
        module defaults (task 3 onboarding fallback).
    :param config: minimal structured onboarding configuration.
    :param graph: interest catalog; defaults to ``build_catalog()``.
    :param day: the day whose agenda must exist on return (the caller owns
        the clock — no real-clock reads here).
    """
    graph = graph if graph is not None else build_catalog()

    # 1. Identity: the persona row is the bootstrap-complete marker.
    persona = store.load_persona() if hasattr(store, "load_persona") else None
    if persona is None:
        profile = _resolve_user_profile(store, user, config)
        cfg = config if config is not None else OnboardingConfig()
        persona = build_persona(
            seed,
            graph=graph,
            user_interests=profile.interests,
            n_exact=cfg.n_exact,
            n_adjacent=cfg.n_adjacent,
            n_independent=cfg.n_independent,
            adjacency_hops=cfg.adjacency_hops,
        )
        # Canonical interest order (name-sorted): the store reloads interests
        # in name order, so persisting the same order makes a loaded persona
        # byte-identical to the freshly built one (idempotency requires exact
        # equality across calls). No life draw depends on portfolio order —
        # arc/agenda selection is by index or by interest name.
        persona = dataclasses.replace(
            persona, interests=tuple(sorted(persona.interests, key=lambda i: i.name))
        )
        store.save_persona(persona)  # persists persona + interest portfolio
        saver = getattr(store, "save_user_profile", None)
        if saver is not None:
            saver(profile)
    else:
        profile = _resolve_user_profile(store, user, config)

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
