"""Daily activity planner — concrete days instead of verb x noun.

The problem
-----------
``life.generate_agenda`` built every activity by formatting an interest NAME
into one of five verb templates: ``practice {interest}``, ``read about
{interest}``, ``watch a video on {interest}``. Arc items were worse — an
arc's ``next_intention`` is drawn once at creation from a pool of eight and
then FROZEN, so the same arc says "finish the current piece" every time it
surfaces, for its whole life, and there is no piece.

Measured over fourteen simulated days on a real profile: 58 agenda items,
25 distinct activity strings, and the four most common ("morning coffee",
"weekend market", "prepare the materials", "finish the current piece")
accounted for 52% of them.

Two properties are missing and no template can supply either:

* an OBJECT. "watch a video on alternative music" names no video. Nothing in
  a day has a proper noun, so nothing can be referred to later, and every
  reference has to stay vague.
* CONTINUITY. Templates have no memory, so day 3 repeats day 0 with a
  different verb and nothing ever follows on from anything.

What this module does
---------------------
Once per day, one model call turns the persona's arcs, interests and the
outcomes actually recorded on previous days into a handful of CONCRETE
activities. The engine keeps everything it was already good at -- which
sources are eligible, how many items a day gets, when their windows fall,
their salience and their ids. The planner supplies only the activity TEXT.

That split is deliberate: scheduling stays seeded and reproducible, and the
model is used for the one thing a seeded process cannot do, which is invent a
specific, plausible thing to have done.

Determinism and cost
--------------------
The plan is not a separate artefact -- the text lands in ``agenda_items`` via
the normal ``save_agenda`` path. Since every caller only generates a day when
``load_agenda(day)`` is None, a replayed or resumed day reads the stored text
and never calls the model again. One call per day, none on replay.

Opt-in via ``HARNESS_DAY_PLANNER`` (default OFF), because the planner shares
the conversation client: enabling it adds one call per day on that client,
which is right in production and would silently consume a scripted response
in offline runs and replays.

Failure is never fatal: no client, an error, an overrun budget, or an
unparseable reply all leave ``plan_day`` returning None, and the caller keeps
the existing template behaviour. A day with a boring agenda is much better
than a day that does not happen.

Interest buckets
----------------
The plan request labels each interest as SHARED (the user is into it too) or
HERS (the companion's own). ``Interest.bucket`` carried that all along and
nothing read it outside arc spawning, so the 40/40/20 portfolio was real in
the data and invisible in behaviour. A thing she does alone should not read
like a thing they do together.
"""

from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass

#: Longest accepted activity string. Long enough for "reread the chapter on
#: bootstrap resampling", short enough that a paragraph cannot land in the
#: agenda and from there into the state card.
MAX_ACTIVITY_CHARS = 90

#: Most activities one plan may supply, whatever the reply contains.
MAX_PLANNED = 8

#: Recorded outcomes handed to the planner as continuity material.
OUTCOME_CONTEXT = 6

#: Reasoning effort: this is invention, not deduction, and it sits on the
#: day-rollover path. Same rationale as the onboarding extension.
PLANNER_REASONING_EFFORT = "low"

#: Wall-clock budget, seconds. Sized like the onboarding extension against
#: measured free-lane latency; the point of the bound is the client's own
#: ~7-minute retry policy, which raises nothing while it waits.
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

    Accepts the documented ``{"activities": [...]}`` shape and a bare list.
    A reply with FEWER activities than slots is accepted and the caller keeps
    its template text for the rest; more are truncated.
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
    """One bounded model call. See ``interest_extension._call_within_budget``
    for why the wait itself has to be bounded rather than merely guarded.

    ``fork`` (owner ruling, 2026-09-13): a callable(task) -> (system,
    messages) | None that extends the mainline request — the exact pair the
    companion's last turn sent — instead of a standalone one-shot prompt, so
    the whole prefix banks on the provider cache. It is CALLED ON THIS THREAD
    (the store reads behind it belong to it); only the client call moves to
    the budget worker. None — a fork that could not build, or a direct
    caller — keeps the standalone shape; the session's own fork never
    returns None, carrying the system prompt as its base prefix even before
    the first turn.
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
        # system-not-user rule governs HER context (CONVENTIONS, ratified
        # 2026-09-12). A one-off call keeps role=user.
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

    # Not a `with` block: ThreadPoolExecutor.__exit__ waits for the worker
    # and would silently undo the deadline.
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

    Returns a list the same length as ``slots``; an entry may be "" when the
    model supplied nothing usable for that slot, and the caller keeps that
    slot's fallback. None means planning did not happen at all.

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
