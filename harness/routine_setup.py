"""A routine catalog built for the companion at onboarding, not hardcoded.

One bounded model call at cold start turns her interests into a routine
catalog; every name, timing and count the model proposes is validated and
clamped. No client, a provider error, junk, a timeout or a proposal that
validates to nothing all fall back to the default catalog.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import re
from dataclasses import dataclass

from harness import proposal_cache
from harness.domain import Routine
from harness.interest_extension import (
    AMBIGUOUS_BARE_NAMES,
    MAX_NAME_CHARS,
)

#: How many routines to ask for; the sampler then draws 3-4 of them.
ROUTINES_REQUESTED = 6

#: Accepted catalog size after validation; below the minimum the proposal is
#: rejected in favour of the offline default.
MIN_ROUTINES = 4
MAX_ROUTINES = 8

#: Clamp ranges for every number the model proposes; ``start_frac`` is a
#: fraction of the day, bounded to the awake window.
MIN_START_FRAC = 0.25          # ~06:00
MAX_START_FRAC = 0.94          # ~22:30
MIN_DURATION_H = 0.25
MAX_DURATION_H = 2.5
MIN_CADENCE = 0.20
MAX_CADENCE = 0.95

#: Reasoning effort for the builder — a one-shot structural call, so it does
#: not read ``HARNESS_THINKING_EFFORT`` (that caps a conversational turn).
SETUP_REASONING_EFFORT = "low"

#: Wall-clock ceiling for the call — cold start only, never reuse it on a live turn.
SETUP_BUDGET_S = 90.0

CACHE_NAMESPACE = "routine-catalog"
CACHE_SCHEMA = "v1"

_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9 \-'&/]*$")

#: Day-specific words are rejected: nothing in the engine knows the weekday,
#: so the name promises something the routine cannot keep.
_DAY_SPECIFIC = re.compile(
    r"\b(weekend|weekday|monday|tuesday|wednesday|thursday|friday|saturday"
    r"|sunday|sunday's|weekly|fortnightly|monthly)\b"
)

ROUTINE_PROMPT = """\
A young woman's interests are: {interests}

Give exactly {n} daily routines — the small fixed rhythm of an ordinary day
for her. Anchors, not events: the things she does most days that a day feels
wrong without. Some should come from her interests, some should just be how
she lives.

For each: "name" (lowercase, two or three words, something she DOES),
"start" (0.0-1.0 fraction of the day: 0.29 is morning, 0.5 midday, 0.79
evening), "hours" (0.25-2.5, how long it takes) and "cadence" (0.2-0.95, the
chance she does it on any given day).

Nothing tied to a particular day of the week — no "weekend" or "sunday"
anything. Every routine must make sense on a Tuesday.

JSON only:
{{"routines": [{{"name": "morning coffee", "start": 0.29, "hours": 0.5, "cadence": 0.9}}]}}
"""


@dataclass(frozen=True)
class RoutineSetupResult:
    """What the builder produced, for logging and the /setup reply."""

    routines: tuple[Routine, ...]
    source: str  # 'model' | 'cache' | 'default'

    @property
    def built(self) -> bool:
        return self.source in ("model", "cache")


def _clean_name(raw: object, seen: set[str]) -> str | None:
    """Normalize a proposed routine name, or None when it is not usable."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.strip().lower().split())
    if not name or len(name) > MAX_NAME_CHARS:
        return None
    if not _NAME_OK.match(name):
        return None
    if name in AMBIGUOUS_BARE_NAMES:
        return None
    if _DAY_SPECIFIC.search(name):
        return None
    if name in seen:
        return None
    return name


def _clamp(raw: object, low: float, high: float) -> float | None:
    """A proposed number clamped into range, or None when it is not one."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return min(max(value, low), high)


def parse_proposal(raw: str) -> list | None:
    """Pull the routine list out of a raw reply, or None when there is none.

    Tolerates prose around the JSON and both shapes sent (a ``{"routines":
    [...]}`` wrapper or a bare list).
    """
    text = (raw or "").strip()
    if not text:
        return None
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1),
                default=-1)
    if start == -1:
        return None
    for end in range(len(text), start, -1):
        try:
            obj = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(obj, dict):
            obj = obj.get("routines")
        return obj if isinstance(obj, list) else None
    return None


def apply_proposal(proposal: list) -> tuple[Routine, ...]:
    """Validate a proposal into a routine catalog (possibly empty).

    Salience is left at 0.0: ``build_persona`` re-samples it per profile from
    its own seeded stream.
    """
    out: list[Routine] = []
    seen: set[str] = set()
    for entry in proposal:
        if len(out) >= MAX_ROUTINES:
            break
        if not isinstance(entry, dict):
            continue
        name = _clean_name(entry.get("name"), seen)
        if name is None:
            continue
        start = _clamp(entry.get("start"), MIN_START_FRAC, MAX_START_FRAC)
        hours = _clamp(entry.get("hours"), MIN_DURATION_H, MAX_DURATION_H)
        cadence = _clamp(entry.get("cadence"), MIN_CADENCE, MAX_CADENCE)
        if start is None or hours is None or cadence is None:
            continue
        seen.add(name)
        out.append(Routine(
            name=name,
            start_frac=round(start, 4),
            duration_h=round(hours, 4),
            cadence=round(cadence, 4),
            salience=0.0,
        ))
    # Catalog order is the day's order (start_frac, then name).
    out.sort(key=lambda r: (r.start_frac, r.name))
    return tuple(out)


def _make_call(client):
    """A one-argument ``prompt -> reply`` callable, or None without a client."""
    if client is None:
        return None
    if hasattr(client, "chat_with_meta"):
        def call(prompt: str) -> str:
            result = client.chat_with_meta(
                # One-off aux task call: not an event in her conversation.
                [{"role": "user", "content": prompt}],
                json_mode=True,
                reasoning_effort=SETUP_REASONING_EFFORT,
            )
            return getattr(result, "content", "") or ""
        return call
    if hasattr(client, "chat"):
        def call(prompt: str) -> str:
            return client.chat([{"role": "user", "content": prompt}]) or ""
        return call
    return None


def _call_within_budget(call, prompt: str, budget_s: float) -> str:
    """Run ``call`` with a hard wall-clock ceiling.

    Not a ``with`` block: ``ThreadPoolExecutor.__exit__`` blocks on
    ``shutdown(wait=True)``.
    """
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="routine-setup",
    )
    try:
        return pool.submit(call, prompt).result(timeout=budget_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def build_routine_catalog(
    interests,
    *,
    default,
    client=None,
    logger=None,
    budget_s: float = SETUP_BUDGET_S,
) -> RoutineSetupResult:
    """Build this companion's routine catalog from her interests.

    The cache key is the interest SET, nothing else. ``default`` is the
    offline catalog to fall back on — normally
    ``harness.persona.ROUTINE_CATALOG``, passed in rather than imported.

    At most one model call, bounded by ``budget_s``; an accepted proposal
    cached for the same interest set is reused from disk
    (``source='cache'``). Anything that goes wrong yields ``default``.
    """
    interests = tuple(interests)
    payload = {"interests": list(interests), "n": ROUTINES_REQUESTED}

    cached = proposal_cache.load(CACHE_NAMESPACE, CACHE_SCHEMA, payload)
    if isinstance(cached, list):
        routines = apply_proposal(cached)
        if len(routines) >= MIN_ROUTINES:
            if logger is not None:
                logger("routine catalog: reusing the cached proposal (no call)")
            return RoutineSetupResult(routines=routines, source="cache")

    call = _make_call(client)
    if call is None:
        return RoutineSetupResult(routines=tuple(default), source="default")

    prompt = ROUTINE_PROMPT.format(
        interests=", ".join(interests) or "no stated interests",
        n=ROUTINES_REQUESTED,
    )
    try:
        reply = _call_within_budget(call, prompt, budget_s)
    except concurrent.futures.TimeoutError:
        if logger is not None:
            logger(
                f"routine catalog: no reply within {budget_s:.0f}s; "
                "using the default catalog"
            )
        return RoutineSetupResult(routines=tuple(default), source="default")
    except Exception as exc:  # noqa: BLE001 - onboarding must never die on a
        # provider error, and a provider can raise anything.
        if logger is not None:
            logger(f"routine catalog: model call failed ({exc}); using the default")
        return RoutineSetupResult(routines=tuple(default), source="default")

    proposal = parse_proposal(reply if isinstance(reply, str) else "")
    routines = apply_proposal(proposal) if proposal is not None else ()
    if len(routines) < MIN_ROUTINES:
        if logger is not None:
            logger(
                f"routine catalog: proposal kept only {len(routines)} routines "
                f"(need {MIN_ROUTINES}); using the default"
            )
        return RoutineSetupResult(routines=tuple(default), source="default")

    # Cached only once it validated; a fallback is not worth replaying.
    proposal_cache.store(CACHE_NAMESPACE, CACHE_SCHEMA, payload, proposal)
    return RoutineSetupResult(routines=routines, source="model")
