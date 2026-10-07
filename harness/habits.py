"""How her days are shaped: what each interest is to her, when she does it,
and the fixed commitment the rest of the day fits around.

Everything here is drawn ONCE per persona and saved (``kv_store``), never
hardcoded per day:

- per interest: its kind (a skill she practises or a taste she enjoys), her
  stage in it, the part of the day she does it and how many times a week;
- the commitment: a job, school, or staying at home, its hours and start
  time — or none (``light``).

The day's schedule (``harness.life.generate_agenda``) reads these; the day
planner only names the activity. Stages move with what actually happened:
each completed session counts, and enough of them move her up a stage.
The bucket (exact / adjacent / independent) stays a selection weight in
``harness.life`` and is never shown to her or the planner.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import asdict, dataclass, replace

from engine.rng import stream_rng

#: Reserved RNG stream for the one-time habit and commitment draws.
HABITS_STREAM = 8

SKILL = "skill"
TASTE = "taste"

#: Stages, lowest first.
STAGES = {
    SKILL: ("curious", "starting", "learning", "practised", "master"),
    TASTE: ("curious", "casual", "fan", "passionate"),
}

#: Completed sessions at a stage before she moves up from it.
SESSIONS_TO_ADVANCE = {
    SKILL: (3, 8, 20, 60),
    TASTE: (3, 10, 30),
}

#: Minutes one session takes, by kind and stage.
DURATION_MIN = {
    SKILL: ((10, 30), (20, 45), (30, 60), (45, 90), (60, 120)),
    TASTE: ((10, 30), (20, 50), (30, 90), (45, 120)),
}

#: Interests whose sessions have a shape of their own: an episode is ~24
#: minutes (1-4 of them by stage), a studio session is 2-3 hours.
EPISODE_MIN = 24
EPISODES_BY_STAGE = ((1, 1), (1, 2), (2, 3), (2, 4))
DURATION_OVERRIDES = {
    "anime": "episodes",
    "pottery": (120, 180),
}

#: Interests that are enjoyed rather than practised. Anything else is a skill.
_TASTE = re.compile(
    r"\b(music|anime|manga|films?|movies|cinema|tv|series|shows?|novels?"
    r"|podcasts?|book clubs?|birdwatching|concerts?|live music)\b"
)

#: Part-of-day windows, local hours [start, end).
PARTS = {
    "morning": (8.0, 12.0),
    "midday": (11.0, 14.5),
    "afternoon": (13.0, 18.0),
    "evening": (18.0, 22.0),
    "late": (21.0, 23.0),
}
_PART_WEIGHTS = {
    SKILL: {"morning": 0.25, "midday": 0.1, "afternoon": 0.3, "evening": 0.3,
            "late": 0.05},
    TASTE: {"morning": 0.1, "midday": 0.1, "afternoon": 0.2, "evening": 0.4,
            "late": 0.2},
}

#: Commitment kinds and the words that name them in a persona.
JOB, SCHOOL, HOME = "job", "school", "home"
COMMITMENT_TEXT = {JOB: "work", SCHOOL: "classes", HOME: "housework"}
_COMMITMENT_WORDS = (
    (SCHOOL, re.compile(r"\b(student|school|university|college|classes"
                        r"|studies|studying|lectures?|campus)\b")),
    (JOB, re.compile(r"\b(works?|working|job|shifts?|office|barista|clerk"
                     r"|nurse|waitress|intern|employed)\b")),
    (HOME, re.compile(r"\b(stays? at home|stay-at-home|homebody"
                      r"|keeps? house|housewife)\b")),
)
_HOURS_WORDS = re.compile(r"\b(\d{1,2})\s*(?:hours?|h)\s*(?:a|per)\s*day\b")
DEFAULT_COMMITMENT_HOURS = 4.0
#: When her commitment starts (local hour), drawn once on a 30-minute grid.
COMMITMENT_START = {JOB: (9.0, 14.0), SCHOOL: (8.5, 13.0), HOME: (9.0, 15.0)}
#: Job and school are weekday things; staying at home is every day.
WEEKDAYS_ONLY = {JOB: True, SCHOOL: True, HOME: False}

#: The setting's three answers.
MODE_PERSONA, MODE_USER, MODE_LIGHT = "persona", "user", "light"

KV_HABITS = "life.habits"
KV_COMMITMENT = "life.commitment"


@dataclass(frozen=True)
class Habit:
    """What one interest is to her and how it sits in her week."""

    interest: str
    kind: str          # SKILL | TASTE
    stage: str         # one of STAGES[kind]
    part: str          # key of PARTS
    per_week: int      # 1..6
    sessions: int = 0  # completed sessions at the current stage

    @property
    def window(self) -> tuple[float, float]:
        return PARTS[self.part]


@dataclass(frozen=True)
class Commitment:
    """The fixed block her day fits around; ``kind`` None means none."""

    mode: str                 # MODE_PERSONA | MODE_USER | MODE_LIGHT
    kind: str | None          # JOB | SCHOOL | HOME | None
    hours: float = 0.0
    start_h: float = 9.0

    @property
    def text(self) -> str | None:
        return COMMITMENT_TEXT.get(self.kind) if self.kind else None

    def on(self, weekday_index: int) -> bool:
        """Whether the block exists on a weekday (0 = Monday)."""
        if not self.kind or self.hours <= 0:
            return False
        return weekday_index < 5 or not WEEKDAYS_ONLY[self.kind]


@dataclass(frozen=True)
class LifePlan:
    """Her habits plus the commitment — what a day is drawn from."""

    habits: dict[str, Habit]
    commitment: Commitment


def kind_of(interest: str) -> str:
    return TASTE if _TASTE.search(interest.lower()) else SKILL


def _rng_for(seed: int, name: str):
    return stream_rng(seed, HABITS_STREAM, zlib.crc32(name.encode()))


def _weighted(rng, weights: dict[str, float]) -> str:
    names = list(weights)
    total = sum(weights.values())
    return names[int(rng.choice(len(names), p=[weights[n] / total for n in names]))]


def draw_habits(seed: int, interests) -> dict[str, Habit]:
    """One habit per interest. Levels are spread: her strongest interest is
    at the top stage, the next one or two a stage below it, the rest lower.
    Strength is salience; ties break by name."""
    ranked = sorted(interests, key=lambda i: (-float(i.salience), i.name))
    spread = stream_rng(seed, HABITS_STREAM)
    n_second = 1 + int(spread.integers(0, 2))
    habits: dict[str, Habit] = {}
    for rank, interest in enumerate(ranked):
        kind = kind_of(interest.name)
        stages = STAGES[kind]
        rng = _rng_for(seed, interest.name)
        if rank == 0:
            stage = stages[-1]
        elif rank <= n_second:
            stage = stages[-2]
        else:
            stage = stages[int(rng.integers(0, len(stages) - 2))]
        part = _weighted(rng, _PART_WEIGHTS[kind])
        per_week = 1 + round(float(interest.salience) * 4) + int(rng.integers(-1, 2))
        habits[interest.name] = Habit(
            interest=interest.name, kind=kind, stage=stage, part=part,
            per_week=max(1, min(6, per_week)),
        )
    return habits


def infer_commitment(seed: int, core: str) -> Commitment:
    """Her commitment from what her persona says. A persona that names none
    gets a job or school, drawn once; the hours are 4 unless it says."""
    text = (core or "").lower()
    kind = next((k for k, words in _COMMITMENT_WORDS if words.search(text)), None)
    rng = stream_rng(seed, HABITS_STREAM, 1)
    drawn = (JOB, SCHOOL)[int(rng.integers(0, 2))]
    kind = kind or drawn
    hours = DEFAULT_COMMITMENT_HOURS
    said = _HOURS_WORDS.search(text)
    if said:
        hours = float(min(10, max(1, int(said.group(1)))))
    return Commitment(MODE_PERSONA, kind, hours, _draw_start(rng, kind, hours))


def _draw_start(rng, kind: str, hours: float) -> float:
    low, high = COMMITMENT_START[kind]
    high = min(high, 22.0 - hours)
    steps = max(0, int((high - low) / 0.5))
    return low + 0.5 * int(rng.integers(0, steps + 1))


def user_commitment(seed: int, kind: str, hours: float,
                    start_h: float | None = None) -> Commitment:
    """The commitment he picked. Without a start time it is drawn once."""
    if start_h is None:
        start_h = _draw_start(stream_rng(seed, HABITS_STREAM, 2), kind, hours)
    return Commitment(MODE_USER, kind, float(hours), float(start_h))


LIGHT = Commitment(MODE_LIGHT, None)


# persistence (kv_store; a store without it keeps the drawn plan in memory)

def _get(store, key):
    getter = getattr(store, "get_kv", None)
    raw = getter(key) if getter is not None else None
    return json.loads(raw) if raw else None


def _set(store, key, value) -> None:
    setter = getattr(store, "set_kv", None)
    if setter is not None:
        setter(key, json.dumps(value, sort_keys=True))


def save_habits(store, habits: dict[str, Habit]) -> None:
    _set(store, KV_HABITS, {name: asdict(h) for name, h in habits.items()})


def save_commitment(store, commitment: Commitment) -> None:
    _set(store, KV_COMMITMENT, asdict(commitment))


def load_plan(store, seed: int, persona) -> LifePlan:
    """The saved plan, drawing and saving whatever is missing: a new
    interest gets its habit, a persona with no commitment gets one."""
    saved = _get(store, KV_HABITS) or {}
    habits = {name: Habit(**row) for name, row in saved.items()}
    missing = [i for i in persona.interests if i.name not in habits]
    if missing:
        drawn = draw_habits(seed, persona.interests)
        for interest in missing:
            habits[interest.name] = drawn[interest.name]
        save_habits(store, habits)
    row = _get(store, KV_COMMITMENT)
    if row is not None:
        commitment = Commitment(**row)
    else:
        commitment = infer_commitment(seed, persona.core)
        save_commitment(store, commitment)
    return LifePlan(habits, commitment)


def derive_plan(seed: int, persona) -> LifePlan:
    """The plan without a store (callers that never persisted one)."""
    return LifePlan(draw_habits(seed, persona.interests),
                    infer_commitment(seed, persona.core))


# sessions

def duration_h(habit: Habit, rng) -> float:
    """How long one session takes, rounded to 5 minutes."""
    stages = STAGES[habit.kind]
    level = stages.index(habit.stage)
    override = next((v for k, v in DURATION_OVERRIDES.items()
                     if k in habit.interest.lower()), None)
    if override == "episodes":
        low, high = EPISODES_BY_STAGE[min(level, len(EPISODES_BY_STAGE) - 1)]
        return EPISODE_MIN * int(rng.integers(low, high + 1)) / 60.0
    low, high = override if isinstance(override, tuple) else DURATION_MIN[habit.kind][level]
    minutes = low + float(rng.random()) * (high - low)
    return round(minutes / 5.0) * 5.0 / 60.0


def record_sessions(habit: Habit, completed: int) -> Habit:
    """Count completed sessions; enough of them move her up a stage."""
    stages = STAGES[habit.kind]
    level = stages.index(habit.stage)
    sessions = habit.sessions + completed
    needs = SESSIONS_TO_ADVANCE[habit.kind]
    while level < len(needs) and sessions >= needs[level]:
        sessions -= needs[level]
        level += 1
    return replace(habit, stage=stages[level], sessions=sessions)
