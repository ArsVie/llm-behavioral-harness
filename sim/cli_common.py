"""Wiring shared by the CLI launcher (``run_async``).

The entry builds the onboarding config from the flags, initialises identity
idempotently and says what exists, restores-or-plans the proactive horizon,
and stamps the commit. ``_bootstrap_and_report`` holds the startup contract
in one place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from engine.types import PersonaParams, TimingParams
from harness.bootstrap import (
    DEFAULT_USER_INTERESTS,
    DEFAULT_USER_NAME,
    OnboardingConfig,
    ensure_companion_initialized,
)
from harness.scheduler import ProactiveSchedule
from harness.store import SQLiteStore


def onboarding_config(args) -> OnboardingConfig:
    """Onboarding config from CLI args (shared by the startup bootstrap and
    the /setup hook so both initialize identically)."""
    user_interests = tuple(
        s.strip()
        for s in (args.user_interests or ",".join(DEFAULT_USER_INTERESTS)).split(",")
        if s.strip()
    )
    return OnboardingConfig(
        user_name=args.user_name or DEFAULT_USER_NAME,
        user_interests=user_interests,
    )


def bootstrap_and_report(store: SQLiteStore, seed: int, args) -> None:
    """Idempotent clean-start initialization (Iteration-2 A1b): blank DB →
    persona → user-relative interests → life arcs → today's agenda, then a
    one-line summary. Safe to call on every start (no-op once initialized)."""
    boot = ensure_companion_initialized(
        store, seed=seed, config=onboarding_config(args), day=0
    )
    counts: dict[str, int] = {}
    for interest in boot.persona.interests:
        counts[interest.bucket] = counts.get(interest.bucket, 0) + 1
    print(
        f"bootstrap: user={boot.user_profile.name} persona={boot.persona.name} "
        f"interests={len(boot.persona.interests)} "
        f"(exact {counts.get('exact', 0)} / adjacent {counts.get('adjacent', 0)} / "
        f"independent {counts.get('independent', 0)}) arcs={len(boot.life_arcs)} "
        f"agenda[0]={len(boot.today_agenda.items) if boot.today_agenda else 0}"
    )


def restore_or_plan(
    store: SQLiteStore, seed: int, persona: PersonaParams, timing: TimingParams,
    days: int,
) -> ProactiveSchedule:
    """Restart-resume: restore the persisted schedule when pending events
    exist, otherwise plan + persist a fresh horizon."""
    if store.pending_schedule_events(seed):
        return ProactiveSchedule.restore(seed, store)
    return ProactiveSchedule.plan_and_persist(days, seed, persona, timing, store)

def commit_sha() -> str | None:
    """Short HEAD sha of the harness repo, or None when unavailable (best
    effort — never fails the launcher)."""
    try:
        root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:  # noqa: BLE001 - best effort
        return None
