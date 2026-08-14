"""Decision probe v2 — frozen shared shapes (dose-response).

FLOOR (never relaxed, even by live steering):
- The mood brief must come from the real engine chain:
  engine.cycle.step / engine.mood.step -> harness.behavior.derive_behavior
  -> BehaviorDirective.prompt_brief (rendered by _render_brief). Never
  hand-set labels. engine/ and harness/behavior.py are READ-ONLY here.
- reasoning_content is captured VERBATIM on every leg (never truncated,
  never a bool).
- No max_tokens cap in the model payload (repo guard 3af0a5a: capping
  starves the reasoning model into empty completions).
- `responded` and `choice` are SEPARATE fields (the s06 fix) — the
  boolean response signal and the categorical action are never merged.
- Runtime tool schema (harness/tools.py) is unchanged: choice is derived
  post-hoc in the measurement layer, not by new tool parameters.
- No decisions->episodes capability work in this branch.

STEERABLE (recorded, not frozen): grid size, choice-classification method,
orthogonal vs natural emphasis, concurrency pool, additive schema fields.
Additive fields must carry defaults so old records still parse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Choice enum (post-hoc derived by probe_outcome; separate from responded)
# --------------------------------------------------------------------------- #
# tool_decide_event at start -> initiate | skip
# tool_decide_event at close -> follow | abandon | defer
# tool_decide_reply          -> reply | no_reply
REPLY_CHOICES: tuple[str, ...] = ("reply", "no_reply")
EVENT_START_CHOICES: tuple[str, ...] = ("initiate", "skip")
EVENT_CLOSE_CHOICES: tuple[str, ...] = ("follow", "abandon", "defer")

# --------------------------------------------------------------------------- #
# Mood dose axes (engine-real; engineered levers logged for the dose curve)
# --------------------------------------------------------------------------- #
#: engine mood_scale: M in 0..MOOD_SCALE maps to valence = 2*M/scale - 1
MOOD_SCALE: int = 10

#: Default grid (steerable). 15 scenarios x ~6 doses x K=5 = 450 legs.
GRID_DEFAULT: dict[str, int] = {
    "scenarios": 15,
    "doses": 6,
    "K": 5,
}


@dataclass(frozen=True)
class MoodDose:
    """One engine-real mood sample: engineered levers + full scalar vector +
    verbatim brief prose. Produced by experiments/probe_moods.py (A1)."""

    dose_id: str                 # e.g. "val-M8", "ene-h16", "nat-d42", "ext-M10"
    set_kind: str                # natural | orthogonal_valence | orthogonal_energy | extremes
    engineered: dict             # engineered levers: {"M": int|None, "hour": float|None,
                                 #   "phase": str|None, "mu": float|None, "eta": float|None}
    record: dict                 # engine DayRecord as dict (real, engine-stepped)
    vector: dict                 # BehaviorDirective scalars: valence, energy, momentum,
                                 #   reactivity, warmth, expressiveness, playfulness,
                                 #   reflectiveness, initiative, response_length_scale,
                                 #   response_delay_s, closing_tendency
    trace: dict                  # BehaviorTrace: phase_label, hormonal_gain, event_memory,
                                 #   endogenous_tone, mood_delta
    brief: str                   # verbatim prompt_brief prose (the state-card mood line)
    availability: str | None     # _availability_line prose (energy tier), if any
    brief_hash: str              # sha1 of `brief` (grouping identical briefs)


@dataclass
class ProbeRecord:
    """One leg of the probe: scenario (everything-but-mood) x dose x rep K.

    Scenario identity is everything-but-mood: scenario_id = sample_id +
    transport, so a per-scenario_id dose-response curve is well defined.
    """

    # -- scenario (everything-but-mood) ------------------------------------ #
    scenario_id: str             # f"{sample_id}:{transport}" — mood is the ONLY varied thing
    sample_id: str
    popup_kind: str              # tool_decide_event | tool_decide_reply
    event_label: str
    state_label: str             # start | in_progress | end
    time: float                  # t_h of the pop-up
    conversation_context: str
    transport: str               # "native" (v2 default; steerable)

    # -- mood dose ---------------------------------------------------------- #
    dose_id: str
    mood_vector: dict            # full scalar vector: MoodDose.vector + trace + M, mu, eta
    brief: str                   # verbatim prompt_brief
    brief_hash: str

    # -- leg ---------------------------------------------------------------- #
    leg_id: str                  # f"{scenario_id}:{dose_id}:k{rep_k}"
    rep_k: int
    reasoning_content: str       # VERBATIM ("" when the route returns none) — FLOOR
    reasoning_present: bool      # bool(reasoning_content.strip()) — FLOOR
    raw_reply: str               # verbatim raw reply (text or tool_calls JSON)
    verdict: dict | None         # parsed verdict (DecisionResult.verdict)
    source: str                  # model | server_draw
    parse_failure: bool

    # -- A3 post-hoc classification (filled by probe_outcome.classify) ------ #
    responded: bool | None = None        # did she respond at all — SEPARATE from choice
    choice: str | None = None            # choice enum above
    terminate_event: bool | None = None
    boundary_set: list[str] = field(default_factory=list)  # boundaries invoked
    references_state: bool = False       # reasoning referenced the state card (mood)
    references_state_detail: str | None = None  # steerable: 2nd-model pass, distinct field


# --------------------------------------------------------------------------- #
# Interfaces (stub-first, steered to real signatures; A1 owns the sampler
# interface and may patch this file ADDITIVELY — new fields with defaults).
# --------------------------------------------------------------------------- #

def sample_moods(set_kind: str, seed: int, **cfg: Any) -> list[MoodDose]:
    """A1: engine-driven brief sampler. Real engine chain only."""
    raise NotImplementedError("implemented by experiments/probe_moods.py (A1)")


def classify(record: ProbeRecord) -> ProbeRecord:
    """A3: post-hoc (verdict, reason, reasoning_content) ->
    responded, choice, boundary_set, references_state. Rule-based by
    default; a second-model pass is a steer (logged as a distinct field)."""
    raise NotImplementedError("implemented by experiments/probe_outcome.py (A3)")
