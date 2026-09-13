"""Daily activity planner — concrete days instead of verb x noun.

One model call per day turns the persona's arcs, interests and the outcomes
recorded on previous days into concrete activity text; the engine keeps all
scheduling (sources, counts, windows, salience, ids). Opt-in via
``HARNESS_DAY_PLANNER`` (default OFF — the planner shares the conversation
client). Failure is never fatal: ``plan_day`` returns None and the caller keeps
the template behaviour.
"""

from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass

#: Longest accepted activity string. Bounds an activity well below paragraph
#: length so no paragraph can reach the agenda or the state card.
MAX_ACTIVITY_CHARS = 90

#: Most activities one plan may supply, whatever the reply contains.
MAX_PLANNED = 8

#: Recorded outcomes handed to the planner as continuity material.
OUTCOME_CONTEXT = 6

#: Reasoning effort for the plan call (invention, not deduction).
PLANNER_REASONING_EFFORT = "low"

#: Wall-clock budget, seconds: the client's own retry policy raises nothing
#: while it waits, so the wait itself must be bounded.
PLANNER_BUDGET_S = 90.0

PLANNER_PROMPT = """\
{name} is planning her own day. It is {weekday}.

Her ongoing projects:
{arcs}

Her interests:
{interests}

What actually came of recent days:
{outcomes}

Write {n} things she does today — one per source listed below, in order:
{slots}

Each one must be a SPECIFIC thing with a real object: not "practice guitar"
but "work out the bridge of a song she keeps half-finishing"; not "read about
history" but "get through the chapter on the Antonine plague". Invent the
specific object; that is the point.

Where the outcomes above give you something to follow on from, follow on from
it. A project should visibly move rather than restart.

Marked HERS is something she does on her own; marked SHARED is an interest he
shares too. Either way this is HER day: write what SHE does and phrase it so
it stands without him — she has a life of her own, and no activity may need
him or assume he will be there. Do not explain the difference, just let it
read differently.

Lowercase, no trailing period, at most {chars} characters each, no names of
people. Reply with JSON only:
{{"activities": ["...", "..."]}}
"""


@dataclass(frozen=True)
class PlanSlot:
    """One thing the engine has already decided to schedule.

    The planner fills in ``activity``; everything else is the engine's.
    """

    source_type: str  # "arc" | "interest"
    source_id: str
    fallback: str  # the template text, used when planning is unavailable
    label: str  # human description of the source, for the request
    bucket: str | None = None  # interest bucket, when source_type == "interest"


def _clean_activity(raw: object) -> str | None:
    """Normalize one proposed activity, or None when unusable."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.strip().split())
    if not text:
        return None
    text = text.rstrip(".")
    if len(text) > MAX_ACTIVITY_CHARS or "\n" in text:
        return None
    return text


def _render_arcs(arcs) -> str:
    if not arcs:
        return "  (none)"
    return "\n".join(
        f"  - {a.name} — next: {a.next_intention}" for a in arcs
    )


def _render_interests(slots: list[PlanSlot]) -> str:
    seen: dict[str, str] = {}
    for slot in slots:
        if slot.source_type == "interest" and slot.source_id not in seen:
            seen[slot.source_id] = "HERS" if slot.bucket == "independent" else "SHARED"
    if not seen:
        return "  (none)"
    return "\n".join(f"  - {name} ({tag})" for name, tag in seen.items())


def _render_outcomes(outcomes) -> str:
    if not outcomes:
        return "  (nothing recorded yet)"
    lines = []
    for row in outcomes[:OUTCOME_CONTEXT]:
        activity = str(row.get("activity") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        if activity and outcome:
            lines.append(f"  - {activity} → {outcome}")
    return "\n".join(lines) or "  (nothing recorded yet)"


def _render_slots(slots: list[PlanSlot]) -> str:
    return "\n".join(f"  {i + 1}. {s.label}" for i, s in enumerate(slots))


def build_request(name: str, weekday: str, arcs, slots: list[PlanSlot],
                  outcomes) -> str:
    """The plan prompt for one day (pure — no I/O, no clock read)."""
    return PLANNER_PROMPT.format(
        name=name,
        weekday=weekday,
        arcs=_render_arcs(arcs),
        interests=_render_interests(slots),
        outcomes=_render_outcomes(outcomes),
        n=len(slots),
        slots=_render_slots(slots),
        chars=MAX_ACTIVITY_CHARS,
    )


def parse_plan(reply: str, expected: int) -> list[str] | None:
    """Activities from a model reply, or None when unusable.

    Accepts the ``{"activities": [...]}`` shape and a bare list. Fewer activities
    than slots is accepted (the caller keeps its templates for the rest); more
    are truncated.
    """
    if not reply:
        return None
    start = min(
        (i for i in (reply.find("{"), reply.find("[")) if i >= 0), default=-1
    )
    if start < 0:
        return None
    end = max(reply.rfind("}"), reply.rfind("]"))
    if end <= start:
        return None
    try:
        parsed = json.loads(reply[start : end + 1])
    except ValueError:
        return None
    if isinstance(parsed, dict):
        parsed = parsed.get("activities")
    if not isinstance(parsed, list):
        return None
    out: list[str] = []
    for raw in parsed[:expected]:
        cleaned = _clean_activity(raw)
        out.append(cleaned if cleaned is not None else "")
    return out or None


def _call_within_budget(client, prompt: str, budget_s: float, fork=None) -> str:
    """One bounded model call. The wait itself has to be bounded, not merely
    guarded (see ``interest_extension._call_within_budget``).

    ``fork``: a callable(task) -> (system, messages) | None that extends the
    mainline request instead of a standalone prompt, so the whole prefix banks on
    the provider cache. It is CALLED ON THIS THREAD (the store reads behind it
    belong to it); only the client call moves to the budget worker. None — a fork
    that could not build, or a direct caller — keeps the standalone shape.
    """
    pair = None
    if fork is not None:
        try:
            pair = fork(prompt)
        except Exception:  # noqa: BLE001 - a fork that cannot build must not cost the day
            pair = None
    if pair is not None:
        system, messages = pair
    else:
        # Aux task prompt, not an event in her conversation: the
        # system-not-user rule governs HER context, not this call.
        messages = [{"role": "user", "content": prompt}]
        system = "You return JSON only."
    rich = getattr(client, "chat_with_meta", None)
    if rich is not None:
        def _run():
            result = rich(
                messages,
                system=system,
                temperature=0.9,  # invention: the point is variety
                json_mode=True,
                max_tokens=None,
                reasoning_effort=PLANNER_REASONING_EFFORT,
            )
            return getattr(result, "content", result) or ""
    else:
        plain = getattr(client, "chat", None)
        if plain is None:
            raise AttributeError("client exposes no chat surface")

        def _run():
            return plain(
                messages,
                system=system,
                temperature=0.9,
            ) or ""

    # not a with block: __exit__ waits for the worker and would undo the deadline.
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="day-planner"
    )
    try:
        return pool.submit(_run).result(timeout=budget_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def plan_day(
    *,
    name: str,
    weekday: str,
    arcs,
    slots: list[PlanSlot],
    outcomes=(),
    client=None,
    fork=None,
    logger=None,
    budget_s: float = PLANNER_BUDGET_S,
) -> list[str] | None:
    """Concrete activity text for each slot, or None to keep the templates.

    Returns a list the same length as ``slots``; an entry may be "" when the model
    supplied nothing usable for that slot (the caller keeps that slot's fallback).
    None means planning did not happen at all.

    ``fork`` (optional) routes the call through the session's mainline-request
    extension instead of a standalone prompt — see ``_call_within_budget``.
    """
    if not slots or client is None:
        return None
    prompt = build_request(name, weekday, arcs, slots, outcomes)
    try:
        reply = _call_within_budget(client, prompt, budget_s, fork)
    except concurrent.futures.TimeoutError:
        if logger is not None:
            logger(f"day planner: no reply within {budget_s:.0f}s; keeping templates")
        return None
    except Exception as exc:  # a day must happen even when the provider does not
        if logger is not None:
            logger(f"day planner: call failed ({exc}); keeping templates")
        return None
    activities = parse_plan(reply if isinstance(reply, str) else "", len(slots))
    if activities is None:
        if logger is not None:
            logger("day planner: unparseable reply; keeping templates")
        return None
    # Pad so the caller can zip slots and activities without checking length.
    activities.extend("" for _ in range(len(slots) - len(activities)))
    return activities[: len(slots)]
