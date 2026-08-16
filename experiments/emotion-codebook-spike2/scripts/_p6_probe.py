"""Scratch probe (deleted before commit): codebook nodes + renderer path."""
import json
import sys
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SPIKE_ROOT.parent.parent
sys.path.insert(0, str(SPIKE_ROOT))
from harness.determinism import MASTER_SEED, derive_seed, seed_everything  # noqa: E402

sys.modules.pop("harness", None)
sys.modules.pop("harness.determinism", None)
sys.path.insert(0, str(REPO_ROOT))

from engine.types import DayRecord, TimingParams  # noqa: E402
from harness.behavior import derive_behavior  # noqa: E402
from harness.domain import (  # noqa: E402
    BehaviorBrief, CompanionSnapshot, MemoryContext, PersonaProfile,
)
from harness.assembler import (  # noqa: E402
    AFFECTIVE_HEADER, assemble_snapshot, DEFAULT_PERSONA_CORE,
)

# --- 1. renderer path ---
timing = TimingParams()
best = None
for h in [x * 0.25 for x in range(96)]:
    rec = DayRecord(t=0, m=5.0, g=0.7, arg=0.5, p=0.5, M=6, score=0.5,
                    mu=0.0, eta=0.0, cycle_day=0.0, phase_label="follicular", seed=MASTER_SEED)
    d = derive_behavior(rec, timing, hour=h)
    if best is None or abs(d.energy - 0.55) < abs(best[1].energy - 0.55):
        best = (h, d)
h, d = best
print("hour", h, "energy", round(d.energy, 4), "valence", round(d.valence, 4))
print("BRIEF:", d.prompt_brief)

brief = BehaviorBrief(
    valence=d.valence, energy=d.energy, reactivity=d.reactivity, warmth=d.warmth,
    expressiveness=d.expressiveness, playfulness=d.playfulness,
    reflectiveness=d.reflectiveness, initiative=d.initiative,
    response_length_scale=d.response_length_scale, response_delay_s=d.response_delay_s,
    closing_tendency=d.closing_tendency,
)
snap = CompanionSnapshot(
    persona=PersonaProfile(name="Nova", core=DEFAULT_PERSONA_CORE, interests=(), routines=()),
    current_behavior=brief,
    current_activity=None,
    agenda=(),
    life_arcs=(),
    memory_context=MemoryContext(recent_turns=(), session_context=(), episodes=(),
                                 user_model=None, evidence_anchors=()),
    recent_conversation=(),
    proactive_intent=None,
)
p = assemble_snapshot(snap, prompt_brief=d.prompt_brief)
print("---PROMPT---")
print(p)
print("---END---", len(p))

# --- 2. codebook nodes ---
for actor, coords in (("qwen", [0.10, 0.60, 0.90]), ("qwen8b", [0.10, 0.60, 0.90])):
    cb = json.loads((SPIKE_ROOT / "data" / "codebooks" / actor / "valence_codebook.json").read_text())
    print(f"--- {actor} codebook: keys={sorted(cb.keys())}, n_nodes={len(cb['nodes'])}")
    for v in coords:
        j = round(v * 100)
        node = cb["nodes"][j]
        cands = node["candidates"]
        cands = sorted(cands, key=lambda c: (-c["prob"], c["token"]))
        top = [c["token"] for c in cands[:12]]
        print(f"  v={v} j={j}: value={node['value']} n_cand={len(cands)} top12={top!r}")
