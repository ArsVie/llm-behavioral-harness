"""Store/session builders — the consolidated ``_store``/``_session`` helpers.

``make_store`` subsumes every per-file ``_store`` duplicate (7 byte-identical
``SQLiteStore(tmp_path / name)`` definitions + the defaulted-name variants):

- default name ``"s.db"`` (the byte-identical 7 used the caller's name);
- ``audit_mode`` / ``str()`` coercion variants are NOT folded in — those
  callers (test_tools, test_negotiation_schema, test_validation) keep their
  bespoke builders.

``make_session`` is the superset of all 13 ``_session`` variants; every
per-file call site is rewritten to a keyword-only call that exercises
exactly its previous signature.
"""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.store import SQLiteStore

_DEFAULT_PERSONA = PersonaParams()
_DEFAULT_TIMING = TimingParams()
_DEFAULT_VARIANT = MoodVariant.DECOUPLED_OFFSETS
_DEFAULT_SEED = 12345


def make_store(tmp_path, name: str = "s.db") -> SQLiteStore:
    """SQLiteStore over ``tmp_path / name`` (the byte-identical 7)."""
    return SQLiteStore(tmp_path / name)


def make_session(
    store,
    *,
    clock=None,
    client=None,
    judge_score: float = 0.5,
    feedback: bool = False,
    synthetic_score: bool = False,
    seed: int = _DEFAULT_SEED,
    persona: PersonaParams = _DEFAULT_PERSONA,
    timing: TimingParams = _DEFAULT_TIMING,
    variant: MoodVariant = _DEFAULT_VARIANT,
    persona_profile=None,
    decision_config=None,
    two_phase_close: bool = False,
    session_cls=Session,
):
    """Session factory — superset of every per-file ``_session`` variant.

    Defaults reproduce the most common shape (test_runtime /
    test_proactive_it2 / test_adversarial_*): ``FakeClient(responses=["ok!"])``,
    a fresh ``VirtualClock``, ``ScriptedJudge(score=0.5).judge_day``, seed
    12345. Callers that previously returned different tuples (store, clock,
    client, session) keep their own wrappers or unpack here.
    """
    return session_cls(
        store,
        persona=persona,
        timing=timing,
        variant=variant,
        seed=seed,
        client=client if client is not None else FakeClient(responses=["ok!"]),
        clock=clock if clock is not None else VirtualClock(),
        judge=ScriptedJudge(score=judge_score).judge_day,
        feedback=feedback,
        synthetic_score=synthetic_score,
        persona_profile=persona_profile,
        decision_config=decision_config,
        two_phase_close=two_phase_close,
    )
