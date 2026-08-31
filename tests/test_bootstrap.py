"""A1b acceptance tests (Iteration-2): idempotent clean-start bootstrap,
user-relative 40/40/20 interests, onboarding fallback, distance queries.

Acceptance criteria (brief /tmp/llh-vslice/it2-a1b.md):
* Blank DB → profile != None, interests > 0, life_arcs > 0, today_agenda != None.
* bootstrap() x3 → no duplicated persona/interests/arcs.
* Population: across many seeds the bucket-fraction means approach 40/40/20
  RELATIVE to the user's interests.

Stated population tolerance: mean bucket fractions within +-5 percentage
points of the 40/40/20 targets (``FRACTION_TOL``), and — where no pool is
clamped — mean bucket counts within +-0.5 of the 4/4/2 targets
(``COUNT_TOL``). The plan's Gate-2 user (mathematics, lifting, movies, metal)
clamps the exact pool to its 4 interests (counts {3,4}), which pulls the exact
fraction to ~0.37 — still inside the +-5-point band; the all-hubs user has no
clamping and lands exactly on 0.40/0.40/0.20.
"""

import numpy as np
import pytest

from pathlib import Path

from engine.rng import stream_rng
from harness.bootstrap import (
    DEFAULT_USER_INTERESTS,
    DEFAULT_USER_NAME,
    BootstrapResult,
    OnboardingConfig,
    ensure_companion_initialized,
)
from harness.domain import UserProfile
from harness.interests import MAX_ADJACENCY_HOPS, build_catalog
from harness.persona import build_persona
from harness.store import SQLiteStore

#: The plan's Gate-2 user (plan §8): mathematics, lifting, movies, metal.
PLAN_USER = UserProfile(
    name="Ars", interests=("mathematics", "metal", "lifting", "movies")
)

#: Every catalog hub as the user set: each sample lands on its target
#: pool (exact 8, adjacent cluster members, independent 3-node island).
ALL_HUBS = UserProfile(name="Omni", interests=tuple(build_catalog().hubs()))

N_SEEDS = 100

#: Population tolerance, stated per the brief: +-5 percentage points per
#: bucket on mean fractions; +-0.5 on mean counts where pools do not clamp.
FRACTION_TOL = 0.05
COUNT_TOL = 0.5

TARGETS = {"exact": 0.40, "adjacent": 0.40, "independent": 0.20}
TARGET_COUNTS = {"exact": 4, "adjacent": 4, "independent": 2}


def _fresh_store(base) -> SQLiteStore:
    Path(base).mkdir(parents=True, exist_ok=True)
    return SQLiteStore(str(Path(base) / "companion.db"))


def _agenda_map(agenda) -> dict[str, object]:
    """Agenda equality via id-keyed map: store list/load orders are not part
    of the persistence contract (tie orders are unspecified)."""
    return {item.id: item for item in agenda.items}


# -- interest graph distance queries (brief §T2) -----------------------------


def test_graph_distance_query():
    g = build_catalog()
    assert g.distance("mathematics", "mathematics") == 0
    assert g.distance("mathematics", "statistics") == 1
    # physics -> guitar is exactly 3 hops: physics-mathematics-programming-guitar
    assert g.distance("physics", "guitar") == 3
    assert g.distance("guitar", "physics") == 3  # undirected
    assert (g.distance("baking", "pottery") or 10**6) > MAX_ADJACENCY_HOPS
    assert g.distance("gardening", "mathematics") is None  # island: unreachable
    assert g.distance("unknown", "mathematics") is None
    assert g.distance("mathematics", "unknown") is None


def test_graph_distance_consistent_with_path_exists():
    g = build_catalog()
    nodes = g.nodes()
    for a in nodes[:12]:
        for b in nodes[:12]:
            d = g.distance(a, b)
            if d is None:
                assert not g.path_exists(a, b, 20)
            else:
                assert g.path_exists(a, b, d)
                assert not g.path_exists(a, b, d - 1) or d == 0


# -- user-relative persona (brief §T2) ---------------------------------------


def _bucket_counts(profile):
    counts = {"exact": 0, "adjacent": 0, "independent": 0}
    for interest in profile.interests:
        counts[interest.bucket] += 1
    return counts


def _user_relative_profiles(user, n=N_SEEDS):
    g = build_catalog()
    return [build_persona(seed, graph=g, user_interests=user.interests) for seed in range(n)]


def test_user_relative_buckets_structural():
    g = build_catalog()
    user = PLAN_USER
    for seed in range(30):
        p = build_persona(seed, graph=g, user_interests=user.interests)
        names = [i.name for i in p.interests]
        assert len(names) == len(set(names))  # no repeats
        for i in p.interests:
            if i.bucket == "exact":
                assert i.name in user.interests, (p, i)
            elif i.bucket == "adjacent":
                assert i.name not in user.interests, (p, i)
                assert any(
                    (g.distance(i.name, u) or 10**6) <= MAX_ADJACENCY_HOPS
                    for u in user.interests
                ), (p, i)
            else:
                assert i.name not in user.interests, (p, i)
                assert all(
                    (g.distance(i.name, u) or 10**6) > MAX_ADJACENCY_HOPS
                    for u in user.interests
                ), (p, i)
        assert set(i.bucket for i in p.interests) == {
            "exact", "adjacent", "independent",
        }  # every bucket fillable for this user


def test_configured_adjacency_boundary():
    g = build_catalog()
    for seed in range(20):
        p = build_persona(
            seed, graph=g, user_interests=PLAN_USER.interests, adjacency_hops=1
        )
        for i in p.interests:
            if i.bucket == "adjacent":
                assert i.name not in PLAN_USER.interests
                assert any(
                    (g.distance(i.name, u) or 10**6) == 1 for u in PLAN_USER.interests
                ), (p, i)


def test_user_relative_population_fractions_plan_user():
    """Plan user (4 interests, exact pool clamps to {3,4}): mean fractions
    stay within +-5 points of 40/40/20 (exact lands ~0.37)."""
    profiles = _user_relative_profiles(PLAN_USER)
    fracs = {b: [] for b in TARGETS}
    for p in profiles:
        c = _bucket_counts(p)
        total = sum(c.values())
        for b in c:
            fracs[b].append(c[b] / total)
    for bucket, target in TARGETS.items():
        mean = float(np.mean(fracs[bucket]))
        assert abs(mean - target) <= FRACTION_TOL, (bucket, mean)


def test_user_relative_population_counts_all_hubs():
    """Unclamped user set: mean counts land within +-0.5 of 4/4/2 and mean
    fractions within +-5 points of 40/40/20."""
    profiles = _user_relative_profiles(ALL_HUBS)
    counts = {b: [] for b in TARGETS}
    fracs = {b: [] for b in TARGETS}
    for p in profiles:
        c = _bucket_counts(p)
        total = sum(c.values())
        for b in c:
            counts[b].append(c[b])
            fracs[b].append(c[b] / total)
    for bucket in TARGETS:
        mean_count = float(np.mean(counts[bucket]))
        assert abs(mean_count - TARGET_COUNTS[bucket]) <= COUNT_TOL, (bucket, mean_count)
        mean_frac = float(np.mean(fracs[bucket]))
        assert abs(mean_frac - TARGETS[bucket]) <= FRACTION_TOL, (bucket, mean_frac)


def test_user_relative_deterministic_per_seed():
    g = build_catalog()
    a = build_persona(42, graph=g, user_interests=PLAN_USER.interests)
    b = build_persona(42, graph=g, user_interests=PLAN_USER.interests)
    assert a == b
    c = build_persona(42, graph=g, user_interests=PLAN_USER.interests,
                      rng=stream_rng(42, 5))
    assert a == c  # explicit persona-stream rng reproduces the default


def test_legacy_hub_relative_path_unchanged():
    """Without user_interests, exact interests remain cluster hubs (the
    pre-A1b contract pinned by tests/test_persona.py)."""
    g = build_catalog()
    hubs = set(g.hubs())
    for seed in range(30):
        p = build_persona(seed, graph=g)
        for i in p.interests:
            if i.bucket == "exact":
                assert i.name in hubs, (p, i)


# -- bootstrap acceptance (brief ACCEPTANCE) ---------------------------------


def test_blank_db_bootstrap_full_chain(tmp_path):
    """Blank DB → profile != None, interests > 0, life_arcs > 0,
    today_agenda != None; interests cover exact/adjacent/independent."""
    store = _fresh_store(tmp_path)
    try:
        result = ensure_companion_initialized(store, seed=7, user=PLAN_USER, day=1)

        assert isinstance(result, BootstrapResult)
        # UserProfile exists
        assert result.user_profile is not None
        assert result.user_profile.name == PLAN_USER.name
        assert tuple(result.user_profile.interests) == PLAN_USER.interests
        # PersonaProfile exists + interests > 0
        assert result.persona is not None
        assert len(result.persona.interests) > 0
        assert store.load_persona() == result.persona  # persisted
        assert len(store.list_interests()) == len(result.persona.interests)
        # interests include exact user overlap + adjacent + independent
        buckets = {i.bucket for i in result.persona.interests}
        assert buckets == {"exact", "adjacent", "independent"}
        g = build_catalog()
        for i in result.persona.interests:
            if i.bucket == "exact":
                assert i.name in PLAN_USER.interests
            elif i.bucket == "adjacent":
                assert any(
                    (g.distance(i.name, u) or 10**6) <= MAX_ADJACENCY_HOPS
                    for u in PLAN_USER.interests
                )
            else:
                assert all(
                    (g.distance(i.name, u) or 10**6) > MAX_ADJACENCY_HOPS
                    for u in PLAN_USER.interests
                )
        # life arcs > 0 (persisted, identical ids)
        assert len(result.life_arcs) > 0
        assert {a.id for a in store.list_life_arcs()} == {a.id for a in result.life_arcs}
        # today's agenda exists and is persisted
        assert result.today_agenda is not None
        assert result.today_agenda.day == 1
        assert len(result.today_agenda.items) > 0
        assert store.load_agenda(1) is not None
        assert _agenda_map(store.load_agenda(1)) == _agenda_map(result.today_agenda)
    finally:
        store.close()


def test_bootstrap_idempotent_x3(tmp_path):
    """bootstrap() x3 (even with a different seed) → no duplicated
    persona/interests/arcs/agenda."""
    store = _fresh_store(tmp_path)
    try:
        r1 = ensure_companion_initialized(store, seed=7, user=PLAN_USER, day=1)
        r2 = ensure_companion_initialized(store, seed=7, user=PLAN_USER, day=1)
        r3 = ensure_companion_initialized(store, seed=99, user=PLAN_USER, day=1)

        assert r1.persona == r2.persona == r3.persona
        assert r1.life_arcs == r2.life_arcs == r3.life_arcs
        assert r1.today_agenda is not None
        assert _agenda_map(r1.today_agenda) == _agenda_map(r2.today_agenda) == _agenda_map(r3.today_agenda)

        # store-level singletons: exactly one persona row, one portfolio, one
        # arc set, one day-1 agenda
        n_persona = store.conn.execute("SELECT COUNT(*) FROM persona").fetchone()[0]
        assert n_persona == 1
        assert len(store.list_interests()) == len(r1.persona.interests)
        assert len(store.list_life_arcs()) == len(r1.life_arcs)
        assert len(store.list_agenda_items(day=1)) == len(r1.today_agenda.items)
    finally:
        store.close()


def test_bootstrap_does_not_regenerate_identity(tmp_path):
    """A later call with a different user must not rebuild the persona."""
    store = _fresh_store(tmp_path)
    try:
        r1 = ensure_companion_initialized(store, seed=7, user=PLAN_USER, day=1)
        other = UserProfile(name="SomeoneElse", interests=("food", "outdoors"))
        r2 = ensure_companion_initialized(store, seed=7, user=other, day=1)
        assert r2.persona == r1.persona
        assert {i.name for i in r2.persona.interests} == {i.name for i in r1.persona.interests}
        assert r2.life_arcs == r1.life_arcs
    finally:
        store.close()


def test_onboarding_fallback_no_user_profile(tmp_path):
    """No UserProfile → minimal structured configuration (or defaults) is
    enough; identity is built from it and never overridden later."""
    store = _fresh_store(tmp_path)
    try:
        result = ensure_companion_initialized(store, seed=7, day=1)
        assert result.user_profile.name == DEFAULT_USER_NAME
        assert tuple(result.user_profile.interests) == DEFAULT_USER_INTERESTS
        assert result.persona is not None and len(result.persona.interests) > 0
        assert len(result.life_arcs) > 0
        assert result.today_agenda is not None

        cfg = OnboardingConfig(
            user_name="Ada", user_interests=("food", "art", "literature")
        )
        result2 = ensure_companion_initialized(store, seed=7, config=cfg, day=2)
        # identity already exists → the config must NOT override it
        assert result2.persona == result.persona
        assert store.load_agenda(2) is not None
    finally:
        store.close()


def test_bootstrap_deterministic_across_stores(tmp_path):
    s1 = _fresh_store(tmp_path)
    s2 = _fresh_store(tmp_path / "other")
    try:
        r1 = ensure_companion_initialized(s1, seed=123, user=PLAN_USER, day=1)
        r2 = ensure_companion_initialized(s2, seed=123, user=PLAN_USER, day=1)
        assert r1.persona == r2.persona
        assert r1.life_arcs == r2.life_arcs
        assert r1.today_agenda == r2.today_agenda
    finally:
        s1.close()
        s2.close()


def test_bootstrap_ensures_each_day_agenda_independently(tmp_path):
    """Agenda for a later day is ensured without touching earlier days, and
    matches a fresh store's agenda for that day (canonical arc order)."""
    store = _fresh_store(tmp_path)
    fresh = _fresh_store(tmp_path / "fresh")
    try:
        r1 = ensure_companion_initialized(store, seed=7, user=PLAN_USER, day=1)
        r2 = ensure_companion_initialized(store, seed=7, user=PLAN_USER, day=2)
        assert r2.today_agenda is not None and r2.today_agenda.day == 2
        assert _agenda_map(store.load_agenda(1)) == _agenda_map(r1.today_agenda)  # day 1 untouched

        rf = ensure_companion_initialized(fresh, seed=7, user=PLAN_USER, day=2)
        assert _agenda_map(rf.today_agenda) == _agenda_map(r2.today_agenda)
    finally:
        store.close()
        fresh.close()
