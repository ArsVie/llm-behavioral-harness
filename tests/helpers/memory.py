"""Memory-pipeline day driver (ex test_memory).

``run_day`` moves VERBATIM from test_memory.py (with its
``save_day_judgement`` helper), including the store-contract bridging:
``save_day_judgement`` tries the defaulted FakeStore signature first and
falls back to the all-positional SQLiteStore signature on TypeError — the
divergence the review flagged. Both store fakes now share the uniform
``save_judgement(day, score, justification="", model="test", shadow=1)``
signature, so the try/except stays as a harmless belt-and-braces bridge.
"""

from __future__ import annotations

from harness.memory import MemoryAgent
from harness.domain import SessionSummary


def save_day_judgement(store, day: int, score: float) -> None:
    """Persist a synthetic judge score through either store contract."""
    try:
        store.save_judgement(day, score)
    except TypeError:
        # SQLiteStore's signature requires justification/model/shadow.
        store.save_judgement(day, score, "", "test", True)


def run_day(
    store,
    day: int,
    turns: list[tuple[str, str]],
    *,
    judgement: float | None = None,
    agent: MemoryAgent | None = None,
) -> SessionSummary:
    """Record turns for a day, close the session, promote, update the model."""
    agent = agent or MemoryAgent(store)
    session_id = f"day-{day}"
    t = day * 24.0 + 9.0
    for role, text in turns:
        agent.record_turn(role, text, t, session_id)
        t += 0.1
    if judgement is not None:
        save_day_judgement(store, day, judgement)
    summary = agent.close_session(session_id, ended_at_t_h=day * 24.0 + 23.0)
    agent.promote(summary)
    agent.update_user_model(summary)
    return summary
