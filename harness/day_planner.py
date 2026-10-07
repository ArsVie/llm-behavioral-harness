"""Daily activity planner — concrete days instead of verb x noun.

One model call per day turns the day's hobby sessions (interest, kind, stage,
hours), her projects and what came of recent days into one plain line each;
the engine keeps all scheduling (sources, counts, windows, salience, ids). Opt-in via
``HARNESS_DAY_PLANNER`` (default OFF — the planner shares the conversation
client). Failure is never fatal: ``plan_day`` returns None and the caller keeps
the template behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from harness.client import fresh_session
from harness.concurrency import run_with_deadline

#: The length the prompt asks for.
ASK_ACTIVITY_CHARS = 90
#: Longest accepted activity string: a line somewhat over the ask is kept
#: (live 2026-09-27: three lines of 99-100 chars all dropped). Still well
#: below paragraph length, so no paragraph reaches the agenda or the card.
MAX_ACTIVITY_CHARS = 140

#: Recorded outcomes handed to the planner as continuity material.
OUTCOME_CONTEXT = 6

#: Reasoning effort for the plan call (invention, not deduction).
PLANNER_REASONING_EFFORT = "low"

#: Wall-clock budget, seconds: the client's own retry policy raises nothing
#: while it waits, so the wait itself must be bounded.
PLANNER_BUDGET_S = 90.0

PLANNER_PROMPT = """\
{name} is planning her own day. It is {weekday}.

Already fixed today:
{fixed}

Her ongoing projects:
{arcs}

What came of recent days:
{outcomes}

Write one plain line for each of these, in order, saying what she does in it:
{slots}

Keep it ordinary and concrete, the way she would write it in her own
planner: "squats and a couple of accessory sets", "the next two episodes".
Fit it to how far along she is: someone curious or starting does beginner
things, someone practised does the real thing. A taste is watching,
listening or reading, never practice or drills. No counts, weights,
measurements or other numbers. Where a recent day gives something to follow
on from, follow on from it; a project moves on rather than restarts.

It is her day on her own: no activity needs him or assumes he will be there.

Each line names the activity, not who does it: no "i", "my" or "me".
Lowercase, no trailing period, at most {chars} characters each, no names of
people. Reply with JSON only:
{{"activities": ["...", "..."]}}
"""


@dataclass(frozen=True)
class PlanSlot:
    """One hobby session the engine has already scheduled.

    The planner fills in ``activity``; everything else is the engine's.
    ``label`` reads "lifting, practised (skill), 18:30–19:40".
    """

    source_type: str  # "arc" | "interest"
    source_id: str
    fallback: str  # the template text, used when planning is unavailable
    label: str  # interest, stage, kind and hours, for the request


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
    active = [a for a in arcs if getattr(a, "status", "active") == "active"]
    if not active:
        return "  (none)"
    return "\n".join(
        f"  - {a.name}, next: {a.next_intention}" for a in active
    )


def _render_fixed(fixed) -> str:
    return "\n".join(f"  - {line}" for line in fixed) if fixed else "  (nothing)"


#: How a resolved status reads to the planner.
_STATUS_WORDS = {"completed": "done", "skipped": "skipped", "shifted": "moved"}


def _render_outcomes(outcomes) -> str:
    """What came of recent sessions: the status, and her own words or the
    recorded cause when there are any."""
    if not outcomes:
        return "  (nothing recorded yet)"
    lines = []
    for row in outcomes[:OUTCOME_CONTEXT]:
        activity = str(row.get("activity") or "").strip()
        if not activity:
            continue
        status = _STATUS_WORDS.get(str(row.get("status") or ""), "")
        outcome = str(row.get("outcome") or "").strip()
        if outcome.startswith("stopped early"):
            status = ""  # the outcome already says what happened
        said = ": ".join(x for x in (status, outcome) if x)
        lines.append(f"  - {activity} → {said}" if said else f"  - {activity}")
    return "\n".join(lines) or "  (nothing recorded yet)"


def _render_slots(slots: list[PlanSlot]) -> str:
    return "\n".join(f"  {i + 1}. {s.label}" for i, s in enumerate(slots))


def build_request(name: str, weekday: str, arcs, slots: list[PlanSlot],
                  outcomes, fixed=()) -> str:
    """The plan prompt for one day (pure — no I/O, no clock read)."""
    return PLANNER_PROMPT.format(
        name=name,
        weekday=weekday,
        fixed=_render_fixed(fixed),
        arcs=_render_arcs(arcs),
        outcomes=_render_outcomes(outcomes),
        slots=_render_slots(slots),
        chars=ASK_ACTIVITY_CHARS,
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
    guarded (see ``concurrency.run_with_deadline``).

    ``fork``: a callable(task) -> (system, messages, tools) | None that extends
    the mainline request instead of a standalone prompt, so the whole prefix
    banks on the provider cache — schemas included, since a fork without them
    breaks the prefix at the schemas block; ``response_format`` is dropped on
    the forked call for the same measured reason (its presence is part of the
    cached prefix, probed 2026-09-20) and ``parse_plan`` tolerates
    prose-wrapped JSON. It is CALLED ON THIS THREAD (the
    store reads behind it belong to it); only the client call moves to the
    budget worker. None — a fork that could not build, or a direct caller —
    keeps the standalone shape.
    """
    pair = None
    if fork is not None:
        try:
            pair = fork(prompt)
        except Exception:  # noqa: BLE001 - a fork that cannot build must not cost the day
            pair = None
    tools = None
    if pair is not None:
        system, messages, tools = pair
    else:
        # Aux task prompt, not an event in her conversation: the
        # system-not-user rule governs HER context, not this call.
        messages = [{"role": "user", "content": prompt}]
        system = "You return JSON only."
    rich = getattr(client, "chat_with_meta", None)
    if rich is not None:
        def _ask():
            result = rich(
                messages,
                system=system,
                temperature=0.9,  # invention: the point is variety
                # ON only for the standalone shape; a forked call drops
                # response_format because its presence is part of the
                # provider's cached prefix (measured 2026-09-20).
                json_mode=pair is None,
                max_tokens=None,
                reasoning_effort=PLANNER_REASONING_EFFORT,
                tools=tools,
            )
            return getattr(result, "content", result) or ""

        def _run():
            # An empty or unparseable reply is asked once more, on another
            # backend (harness.client.fresh_session), as the judge and the
            # memory fork do: prose kept the templates on days 5 and 6 of the
            # 2026-09-27 live run.
            text = _ask()
            if parse_plan(str(text), 1) is None:
                with fresh_session(client, "planner1"):
                    text = _ask()
            return text
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

    return run_with_deadline(_run, budget_s, thread_name="day-planner")


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
    fixed=(),
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
    prompt = build_request(name, weekday, arcs, slots, outcomes, fixed)
    try:
        reply = _call_within_budget(client, prompt, budget_s, fork)
    except TimeoutError:
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
