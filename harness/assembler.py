"""Prompt assembler — BehaviorDirective → system prompt section (W-E1).

Advisor-reviewed decision (2026-08-08): the behavioral guidance lives in a
DYNAMIC SYSTEM-LEVEL block rebuilt for every call — persona core first, then
current behavioral guidance, then the transcript, then the current user
request. Guidance is never injected as user text (correct authority, no
contamination of the request). Persona + guidance are concatenated into ONE
system message for portability across OpenAI-compatible endpoints.

Leakage invariant (frozen in Phase 2): the directive's `prompt_brief` never
contains raw numbers, `mu`/`eta`, or phase labels. This module adds nothing
to the brief — it only wraps it.
"""

from __future__ import annotations

from harness.behavior import BehaviorDirective

#: Default persona core used when the caller provides none. Configurable at
#: runtime via --persona-core / config; this is a neutral starting voice.
DEFAULT_PERSONA_CORE = (
    "You are Nova, a warm and attentive companion. You have your own days, "
    "your own rhythms, and a genuine interest in the person you talk to. You "
    "are not a customer-service assistant: you speak like a close friend "
    "would — with your own cadence, your own quirks, your own silences."
)

#: How many recent transcript turns to carry into the prompt.
RECENT_TURNS = 12


def build_system_prompt(
    persona_core: str | None = None,
    directive: BehaviorDirective | None = None,
) -> str:
    """One system message: persona core + optional current behavioral guidance.

    With `directive=None` the prompt contains ONLY the persona — this is the
    "harness off" condition for the ablation experiment (persona preserved,
    dynamic guidance removed).
    """
    core = (persona_core or DEFAULT_PERSONA_CORE).strip()
    if directive is None:
        return core
    brief = directive.prompt_brief.strip()
    if brief:
        return f"{core}\n\nCurrent behavioral guidance: {brief}"
    return core


def build_messages(
    recent_turns: list[dict],
    user_request: str,
    limit: int = RECENT_TURNS,
) -> list[dict]:
    """Transcript (tail-limited, oldest→newest) + current user request.

    `recent_turns` are store message rows ({role, content, ...}); only role
    and content are used. Assistant turns are included so the model keeps
    style continuity; the user request is always last.
    """
    messages: list[dict] = []
    for turn in recent_turns[-limit:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_request})
    return messages
