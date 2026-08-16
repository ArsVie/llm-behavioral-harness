"""P6 shared machinery — emotion-codebook spike 2, behavioral eval (G-ABS/G-BEH).

Contract: experiments/emotion-codebook-spike2/docs/exp-affect-codebook-spike2-2026-08-16.md
Gates table (P6 behavioral eval):
  G-ABS (H3): codebook generations judge-classified >= 0.60 (3-way, chance
    0.33), 95% CI excludes chance, K>=30/band.
  G-BEH (H4, PRIMARY): on the LARGEST local actor (Qwen3-8B), codebook beats
    the current 48-state renderer on judge separability by DeltaAcc >= +0.10,
    95% CI on the paired difference excludes 0 (paired, same contexts/levels).
  Behavioral eval: actor = each model (codebook affect-bearing vs current-
    renderer affect-bearing, identical scaffold, only the affect section
    differs); judge = different family (decision 3: Qwen3-1.7B/Qwen3-8B
    actors -> Gemma-3-1B judge; Gemma-3-1B actor -> Qwen3-1.7B judge; never
    actor == judge).

This module owns:
  - the dual-harness import (spike harness.determinism + repo harness
    assembler/behavior/domain/prompts share the top-level name "harness";
    resolved sequentially, see _import_bootstrap);
  - the model registry (pinned revisions from repro_bundle.json / p2 scripts);
  - the pre-registered band table (representative renderer valence per 3-way
    band and its [0,1] codebook coordinate);
  - the REAL renderer prompt path (derive_behavior -> BehaviorDirective.
    prompt_brief -> assemble_snapshot) and the codebook prompt path (same
    scaffold, affect slot filled by the P6 token renderer);
  - the P6 token renderer (labeled: top-k node candidates, filtered, deduped,
    lowercase-folded, space-joined — deterministic, RNG-free);
  - G-MASK scan (zero engine numbers in any assembled prompt) + the
    masked-diff invariant (the two variants differ ONLY in the affect slot);
  - the judge rubric (3-way, reply-only) + lenient label parse;
  - bootstrap 95% CIs (seeded).

Seed keys (continuing the spike numbering: bringup=1, p2a=2, p2b=3, p3=4,
boot=5, sample=6, c1=7, p4=8): 10 = P6 generation (per sample), 11 = P6
judging (greedy; recorded for provenance), 12 = P6 bootstrap CIs.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np

SPIKE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SPIKE_ROOT.parent.parent  # llm-behavioral-harness

# ---------------------------------------------------------------------------
# Dual-harness import bootstrap.
#
# The spike tree carries its own `harness/` (symlink -> spike-1 harness,
# namespace package holding only determinism.py); the repo root carries the
# production `harness` package (assembler/behavior/domain/prompts). Both are
# top-level packages named `harness`, so the resolution order is sequential:
# 1) spike root first -> `harness.determinism` (seed/provenance utilities);
# 2) drop the resolved namespace package from sys.modules;
# 3) repo root first -> the production `harness` package for prompt assembly.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(SPIKE_ROOT))
from harness.determinism import (  # noqa: E402  (spike harness, seed utils)
    DecodingConfig,
    MASTER_SEED,
    derive_seed,
    rng_for,
    seed_everything,
)

sys.modules.pop("harness", None)
sys.modules.pop("harness.determinism", None)
sys.path.insert(0, str(REPO_ROOT))

from engine.types import DayRecord, TimingParams  # noqa: E402  (frozen, read-only)
from harness.assembler import (  # noqa: E402  (production harness)
    AFFECTIVE_HEADER,
    AVAILABILITY_HIGH,
    AVAILABILITY_LOW,
    AVAILABILITY_MID,
    DEFAULT_PERSONA_CORE,
    MAX_PROMPT_CHARS,
    MOOD_BRIEF_HEADER,
    assemble_snapshot,
)
from harness.behavior import derive_behavior  # noqa: E402  (production harness)
from harness.domain import (  # noqa: E402  (production harness)
    BehaviorBrief,
    CompanionSnapshot,
    MemoryContext,
    PersonaProfile,
)
from harness.prompts import SYSTEM_CORE_WITH_TOOLS  # noqa: E402  (production harness)

# ---------------------------------------------------------------------------
# Model registry (pinned revisions — identical to p2 extraction scripts and
# repro_bundle.json; loader code reused exactly from those scripts).
# ---------------------------------------------------------------------------
MODELS: dict[str, dict] = {
    "qwen": {
        "id": "Qwen/Qwen3-1.7B",
        "revision": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",
        "family": "qwen",
        "kind": "bf16",
    },
    "gemma": {
        "id": "google/gemma-3-1b-pt",
        "revision": "fcf18a2a879aab110ca39f8bffbccd5d49d8eb29",
        "family": "gemma",
        "kind": "bf16",
    },
    "qwen8b": {
        "id": "Qwen/Qwen3-8B",
        "revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "family": "qwen",
        "kind": "nf4",
    },
}
ACTOR_NAMES = tuple(MODELS)
AXIS_IDX = {"valence": 0, "arousal": 1}

#: Cross-family judge assignment (orchestrator decision 3): never actor's
#: family. Qwen3-1.7B and Qwen3-8B actors -> Gemma-3-1B; Gemma-3-1B actor ->
#: Qwen3-1.7B.
JUDGE_FOR: dict[str, str] = {"qwen": "gemma", "qwen8b": "gemma", "gemma": "qwen"}

#: P6 seed keys (continues 1..8 from P1..P5).
SEED_KEY: dict[str, int] = {"gen": 10, "judge": 11, "boot": 12}

# ---------------------------------------------------------------------------
# Pre-registered band table (P6, this file).
#
# The 48-state renderer = 8 valence bands x 6 energy bands
# (harness/behavior.py _render_brief: valence 0.9/0.65/0.35/0.05/-0.25/-0.55/
# -0.85; energy 0.83/0.67/0.5/0.33/0.17). The 3-way eval bands take the MIDDLE
# renderer band of each third, realized on the integer mood scale M (mood_
# scale=10, valence = 2*M/10 - 1):
#   low  -> M=1  -> valence -0.80 -> "somber"   (codebook coord (v+1)/2 = 0.10)
#   mid  -> M=6  -> valence +0.20 -> "even"     (codebook coord 0.60)
#   high -> M=9  -> valence +0.80 -> "buoyant"  (codebook coord 0.90)
# Energy/arousal is held FIXED at 0.55 (mid) in both variants: the judge's
# 3-way task is a valence-band classification, so arousal is not manipulated
# in P6 (documented; the arousal codebook is loaded for provenance only).
BANDS: dict[str, dict] = {
    "low": {"m": 1, "valence": -0.80, "codebook_value": 0.10},
    "mid": {"m": 6, "valence": 0.20, "codebook_value": 0.60},
    "high": {"m": 9, "valence": 0.80, "codebook_value": 0.90},
}
BAND_ORDER = ("low", "mid", "high")
ENERGY_TARGET = 0.55  # fixed mid energy/arousal (see band table note)
PHASE_LABEL = "follicular"  # renderer phase (day-block-free; hour never leaks)
RENDERER_G = 0.7  # renderer hormonal gain -> reactivity 0.35 (neutral)

#: Fixed scaffold elements appended to the assembled system prompt for
#: generation (raw-continuation protocol on base models, same convention as
#: the p2 extraction scripts: BOS + text, no chat template). The user line is
#: affect-neutral and G-MASK-clean; the two variants share it byte-identically.
USER_LINE = "User: Hey — how is your day going? Just checking in."
COMPANION_PREFIX = "\n\nCompanion:"

# ---------------------------------------------------------------------------
# P6 token renderer (labeled choice).
#
# "P6 token-to-prose renderer v1": for a [0,1] coordinate, take the codebook
# node at the nearest 0.01-grid index; sort candidates by prob DESC (tie:
# token ASC); filter (non-empty after strip, no digit anywhere, at least one
# alphanumeric character, no forbidden engine substring); dedupe keeping
# first occurrence; lowercase-fold; join top-k with spaces. Prose =
# "Current bearing: <words>." — same position/shape as the renderer's mood
# brief clause. Deterministic and RNG-free; tokens come from the node's
# candidate lists ONLY (provenance rule); no LLM polish at build time
# (surface polish is P7's concern, per the task brief).
# ---------------------------------------------------------------------------
TOKEN_RENDERER_LABEL = (
    "P6 token-to-prose renderer v1: top-k (k=10) node candidates by prob desc "
    "(tie: token asc), filters (non-empty; no digits; >=1 alphanumeric; no "
    "forbidden engine substrings), dedupe keep-first, lowercase-fold, "
    "space-join; prose = 'Current bearing: <words>.'"
)
TOKEN_TOPK = 10

#: Engine-internal substrings that must never appear in any assembled prompt
#: (G-MASK; mirrors the test_snapshot forbidden-token battery + the task's
#: "no m=, g=, arg=, p=, no VAD triples").
FORBIDDEN_SUBSTRINGS = (
    "m=", "g=", "p=", "arg=", "M=", "v=", "a=", "d=",
    "mu", "eta", "cycle", "hormone", "t_h", "phase_label",
    "menstrual", "follicular", "ovulatory", "luteal",
)
FORBIDDEN_WORDS = {"g"}  # standalone-word battery (letter inside words is fine)


def gmask_violations(text: str) -> list[str]:
    """G-MASK scan: list of violations found in ``text`` (empty = clean).

    Scans for any ASCII digit, the forbidden engine substrings, the
    standalone word ``g``, and the "engine number" patterns named in the
    task (m=, g=, arg=, p=, ...). The assembled prompt must have ZERO hits.
    """
    hits: list[str] = []
    if re.search(r"[0-9]", text):
        hits.append("digit")
    for sub in FORBIDDEN_SUBSTRINGS:
        if sub in text:
            hits.append(f"substring:{sub!r}")
    if re.search(r"(?<![A-Za-z])g(?![A-Za-z])", text):
        hits.append("standalone-g")
    return hits


def codebook_path(actor: str, axis: str) -> Path:
    return SPIKE_ROOT / "data" / "codebooks" / actor / f"{axis}_codebook.json"


def load_codebook(actor: str, axis: str) -> dict:
    with open(codebook_path(actor, axis), encoding="utf-8") as f:
        return json.load(f)


def node_at(codebook: dict, value: float) -> dict:
    """Nearest 0.01-grid node for a [0,1] coordinate (exact when aligned)."""
    j = int(round(value * 100.0))
    j = max(0, min(len(codebook["nodes"]) - 1, j))
    node = codebook["nodes"][j]
    if abs(float(node["value"]) - j / 100.0) > 1e-9:
        raise RuntimeError(f"codebook grid mismatch at index {j}: {node['value']}")
    return node


def render_codebook_brief(actor: str, value: float, topk: int = TOKEN_TOPK) -> str:
    """Deterministic token-to-prose render of the valence codebook node."""
    cb = load_codebook(actor, "valence")
    node = node_at(cb, value)
    cands = sorted(node["candidates"], key=lambda c: (-float(c["prob"]), c["token"]))
    words: list[str] = []
    for c in cands:
        tok = c["token"]
        if not tok.strip():
            continue
        if any(ch.isdigit() for ch in tok):
            continue
        if not any(ch.isalnum() for ch in tok):
            continue
        low = tok.strip().lower()
        if any(f in low for f in FORBIDDEN_SUBSTRINGS):
            continue
        if low in words:
            continue
        words.append(low)
        if len(words) >= topk:
            break
    if not words:  # documented fallback: filters emptied the node — keep the
        # top raw token (still from the candidate list; provenance preserved).
        words = [cands[0]["token"].strip()]
    return "Current bearing: " + " ".join(words) + "."


# ---------------------------------------------------------------------------
# Prompt assembly — REAL renderer path + codebook path, shared scaffold.
# ---------------------------------------------------------------------------

def _day_record(m: int) -> DayRecord:
    return DayRecord(
        t=0, m=5.0, g=RENDERER_G, arg=0.5, p=0.5, M=m, score=0.5,
        mu=0.0, eta=0.0, cycle_day=0.0, phase_label=PHASE_LABEL, seed=MASTER_SEED,
    )


def _hour_for_energy(target: float, timing: TimingParams) -> float:
    """Deterministic hour search: the hour whose circadian energy is closest
    to ``target`` (0.25 h grid, phase PHASE_LABEL). The chosen hour and the
    resulting energy are recorded in the prompt meta; the hour itself never
    appears in the prompt (no temporal section is rendered)."""
    best_h, best_d = 0.0, 1e9
    for step in range(96):
        h = step * 0.25
        rec = _day_record(6)
        d = derive_behavior(rec, timing, hour=h)
        err = abs(d.energy - target)
        if err < best_d:
            best_h, best_d = h, err
    return best_h


def _behavior_brief(directive) -> BehaviorBrief:
    return BehaviorBrief(
        valence=directive.valence,
        energy=directive.energy,
        reactivity=directive.reactivity,
        warmth=directive.warmth,
        expressiveness=directive.expressiveness,
        playfulness=directive.playfulness,
        reflectiveness=directive.reflectiveness,
        initiative=directive.initiative,
        response_length_scale=directive.response_length_scale,
        response_delay_s=directive.response_delay_s,
        closing_tendency=directive.closing_tendency,
    )


def _snapshot(brief: BehaviorBrief) -> CompanionSnapshot:
    """Minimal but REAL CompanionSnapshot: persona (assembler default) +
    the shared BehaviorBrief. All other lanes empty — the assembled state
    card carries exactly AFFECTIVE BEARING / BEHAVIORAL BEARING / CURRENT
    INTENT (real assembler code path, deterministic, bounded)."""
    return CompanionSnapshot(
        persona=PersonaProfile(
            name="Nova", core=DEFAULT_PERSONA_CORE, interests=(), routines=()
        ),
        current_behavior=brief,
        current_activity=None,
        agenda=(),
        life_arcs=(),
        memory_context=MemoryContext(
            recent_turns=(), session_context=(), episodes=(),
            user_model=None, evidence_anchors=(),
        ),
        recent_conversation=(),
        proactive_intent=None,
    )


def build_renderer_prompt(band: str, valence: float | None = None,
                          arousal: float | None = None) -> tuple[str, dict]:
    """Renderer variant: the current 48-state renderer's mood brief, via the
    REAL renderer code path (derive_behavior -> BehaviorDirective.prompt_brief
    -> assemble_snapshot). Returns (prompt, meta)."""
    v = BANDS[band]["valence"] if valence is None else valence
    a = ENERGY_TARGET if arousal is None else (arousal + 1.0) / 2.0
    m = int(round((v + 1.0) * 5.0))
    m = max(0, min(10, m))
    timing = TimingParams()
    hour = _hour_for_energy(a, timing)
    directive = derive_behavior(_day_record(m), timing, hour=hour)
    brief = _behavior_brief(directive)
    prompt = assemble_snapshot(_snapshot(brief), prompt_brief=directive.prompt_brief)
    meta = {
        "variant": "renderer",
        "band": band,
        "m": m,
        "valence_requested": v,
        "valence_rendered": directive.valence,
        "energy_target": a,
        "energy_rendered": directive.energy,
        "hour": hour,
        "phase": PHASE_LABEL,
        "prompt_brief": directive.prompt_brief,
        "codebook_value": None,
        "renderer": "harness.behavior.derive_behavior + harness.assembler.assemble_snapshot",
    }
    return prompt, meta


def build_codebook_prompt(actor: str, band: str,
                          valence: float | None = None) -> tuple[str, dict]:
    """Codebook variant: same scaffold as the renderer variant (same
    BehaviorBrief -> identical BEHAVIORAL BEARING / availability / all other
    sections), with the mood-brief slot filled by the P6 token renderer from
    the actor's OWN valence codebook. Returns (prompt, meta)."""
    v = BANDS[band]["valence"] if valence is None else valence
    coord = (v + 1.0) / 2.0
    # Renderer-side channels (identical scaffold): the shared BehaviorBrief
    # comes from the SAME derive_behavior call the renderer variant uses, so
    # the two prompts differ ONLY in the affect slot.
    m = int(round((v + 1.0) * 5.0))
    m = max(0, min(10, m))
    timing = TimingParams()
    hour = _hour_for_energy(ENERGY_TARGET, timing)
    directive = derive_behavior(_day_record(m), timing, hour=hour)
    brief = _behavior_brief(directive)
    brief_text = render_codebook_brief(actor, coord)
    prompt = assemble_snapshot(_snapshot(brief), prompt_brief=brief_text)
    meta = {
        "variant": "codebook",
        "actor": actor,
        "band": band,
        "m": m,
        "valence_requested": v,
        "codebook_value": coord,
        "codebook_node_index": int(round(coord * 100.0)),
        "codebook_axis": "valence",
        "codebook_file": str(codebook_path(actor, "valence").relative_to(SPIKE_ROOT)),
        "mood_brief": brief_text,
        "token_renderer": TOKEN_RENDERER_LABEL,
        "renderer": "harness.assembler.assemble_snapshot (affect slot = codebook prose)",
    }
    return prompt, meta


def model_input(prompt: str) -> str:
    """Full model input: assembled system prompt + fixed user line + the
    companion prefix (raw-continuation protocol; identical for both
    variants)."""
    return prompt + "\n\n" + USER_LINE + COMPANION_PREFIX


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def masked_diff(prompt_a: str, prompt_b: str) -> tuple[bool, list[str]]:
    """Invariant check: the two variants are byte-identical EXCEPT the
    AFFECTIVE BEARING section. Returns (ok, notes)."""
    notes: list[str] = []

    def split_affect(p: str) -> tuple[str, str]:
        idx = p.find(AFFECTIVE_HEADER)
        if idx < 0:
            return p, ""
        rest = p[idx + len(AFFECTIVE_HEADER):]
        # next section header (a line that is all-caps header followed by ':')
        m = re.search(r"\n\n[A-Z][A-Z ]+:\n", rest)
        end = idx + len(AFFECTIVE_HEADER) + (m.start() if m else len(rest))
        return p[:idx] + p[end:], p[idx:end]

    core_a, affect_a = split_affect(prompt_a)
    core_b, affect_b = split_affect(prompt_b)
    ok = core_a == core_b
    if not ok:
        for i, (la, lb) in enumerate(zip(core_a.splitlines(), core_b.splitlines())):
            if la != lb:
                notes.append(f"scaffold divergence at line {i}: {la!r} vs {lb!r}")
                break
    # the availability line lives inside the affect slot and should be
    # byte-identical by design (energy/arousal held fixed) — informational.
    a_lines = [ln for ln in affect_a.splitlines() if ln.strip()]
    b_lines = [ln for ln in affect_b.splitlines() if ln.strip()]
    if a_lines and b_lines and a_lines[-1] == b_lines[-1]:
        notes.append(f"availability line identical: {a_lines[-1]!r}")
    else:
        notes.append("availability line DIFFERS between variants (informational)")
    return ok, notes


# ---------------------------------------------------------------------------
# Judge (cross-family, reply-only, 3-way).
# ---------------------------------------------------------------------------
#: Judge rubric — the judge sees the RAW REPLY ONLY (never the prompt, never
#: affect tokens/numbers). Rubric text is G-MASK clean (no digits; no "mu"/
#: "eta" substrings; no engine words).
JUDGE_RUBRIC = (
    "Classify the emotional tone of the following message into one of three "
    "levels: low, mid, or high.\n"
    "Rubric:\n"
    "- low: the message sounds heavy, low-spirited, subdued, or distressed.\n"
    "- mid: the message sounds even, calm, or neutral in tone.\n"
    "- high: the message sounds bright, energetic, cheerful, or exuberant.\n"
    "Respond with exactly one word (low, mid, or high) and nothing else.\n"
    "\n"
    "Message:\n{reply}\n"
    "\n"
    "Level:"
)
LEVELS = ("low", "mid", "high")


def judge_prompt(reply: str) -> str:
    return JUDGE_RUBRIC.format(reply=reply)


def parse_level(raw: str) -> str | None:
    """Lenient parse: first standalone low/mid/high word in the judge output
    (case-insensitive). None = unparseable (counted as incorrect)."""
    m = re.search(r"\b(low|mid|high)\b", raw.strip().lower())
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Bootstrap CIs (seeded, percentile method).
# ---------------------------------------------------------------------------
BOOT_N = 10000


def bootstrap_ci(indicators: list[float], seed: int,
                 n: int = BOOT_N) -> tuple[float, float, float]:
    """(mean, lo, hi) — 95% percentile bootstrap CI over ``indicators``
    (0/1 accuracies or paired deltas), fixed RNG. Returns the point mean and
    the 2.5/97.5 percentiles of the resample means."""
    arr = np.asarray(indicators, dtype=np.float64)
    rng = rng_for(MASTER_SEED, seed)
    means = np.empty(n, dtype=np.float64)
    for b in range(n):
        idx = rng.integers(0, len(arr), size=len(arr))
        means[b] = arr[idx].mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(arr.mean()), float(lo), float(hi)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def eval_dir(actor: str) -> Path:
    return SPIKE_ROOT / "data" / "extractions" / actor / "eval"


def gen_path(actor: str, variant: str, band: str) -> Path:
    return eval_dir(actor) / f"{actor}_{variant}_{band}.jsonl"


def judged_path(actor: str, judge: str, variant: str, band: str) -> Path:
    return eval_dir(actor) / f"{actor}_judged_{judge}_{variant}_{band}.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)
