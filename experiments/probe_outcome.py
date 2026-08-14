"""A3 — post-hoc outcome classification for decision probe v2.

Implements ``probe_schema.classify``: one leg's captured artifacts (verdict,
reason, reasoning_content) -> the A3 outcome fields:

    responded               bool|None — did she respond at all (SEPARATE from
                            choice, per the s06 fix). Never merged with choice.
    choice                  str|None  — frozen enum value (REPLY_CHOICES /
                            EVENT_START_CHOICES / EVENT_CLOSE_CHOICES).
    terminate_event         bool|None — verdict.terminate_event when present.
    boundary_set            list[str] — distinct boundaries invoked in
                            reasoning_content or verdict.reason.
    references_state        bool — did reasoning_content reference the state
                            card (mood brief) at all. Deterministic keyword
                            scan; never overwritten by the LLM steer.
    references_state_detail str|None — steer hook output (PROBE_CLASSIFIER_LLM=1
                            second-model pass). A DISTINCT field by design.

Deterministic, cheap, no LLM by default.

Mapping rules
-------------
Verdict shapes are the canonical ones normalized by ``harness.tools``
(``tool_decide_event`` -> ``{initiate, reason, action?}``; ``tool_decide_reply``
-> ``{reply, reason, terminate_event?}``):

* reply pop-up (``popup_kind == "tool_decide_reply"``):
    responded = verdict.reply (coerced to bool|None)
    choice    = "reply" | "no_reply" from responded
* event start (``tool_decide_event``, state_label "start", any non-"end"):
    responded = verdict.initiate (coerced to bool|None)
    choice    = "initiate" | "skip" from responded
* event close (``tool_decide_event``, state_label "end"):
    responded = True            # a close pop-up always draws a decision
    choice    = verdict.action  # "follow" | "abandon" | "defer"; anything
                                # outside the frozen enum -> None
* verdict missing / not a dict / unparseable (parse_failure): responded and
  choice stay None, except event close where responded stays True (the
  decision point fired regardless of whether the verdict parsed).
* terminate_event = verdict.terminate_event coerced to bool|None (reply
  pop-ups carry it; event pop-ups do not -> None).

boundary_set — deterministic term scan over ``reasoning_content`` +
``verdict.reason`` (top-level ``reason`` accepted as a fallback for the older
row shape). Vocabulary: the scenario's ``event_label`` (normalized: lowercase,
underscores -> spaces) plus the boundary-family terms

    class, work, gym, study, run, sleep, quiet hours, deep work, dinner, rest

matched case-insensitively at word boundaries (explicit inflection patterns:
class(es), work(ing), gym(s), stud(y|ies|ying), run(s|ning), sleep(s|ing),
quiet\\s+hours, deep\\s+work, dinner(s), rest(ing) — "rested"/"restful" are mood
words, not boundary invocations, and are deliberately NOT matched here).
Only invoked boundaries are emitted (the model must actually mention the
boundary in its reasoning or verdict reason). Patterns are independent, so a
phrase match also emits its component words ("deep work" -> "work" + "deep
work"). Order is deterministic: family terms in the order above, then the
event_label if it matched and is distinct.

references_state — deterministic keyword scan over ``reasoning_content`` ONLY
(case-insensitive; word boundaries for single words, whitespace-normalized
substring for phrases). Keyword list = RENDERED BRIEF VOCABULARY harvested
from ``harness.behavior._render_brief`` (read-only reference; the bearing /
pace / continuity / texture / care / closing lines) plus the energy-tier
availability vocabulary (heavy, drained, tired, exhausted, energetic, rested,
quiet, warm), plus generic mood-family words (mood, energy, tired, sleepy,
energized, low, drained, bright, calm, restless, anxious, happy, sad,
irritable). Multi-word brief phrases (e.g. "calmly present", "opening up",
"even and grounded", "quietly bright") are matched as phrases so quoting or
paraphrasing the brief flags True. Empty reasoning -> False.

Steer hook (BUILT, NOT ENABLED)
-------------------------------
``PROBE_CLASSIFIER_LLM=1`` switches references_state to a second-model pass
whose output goes ONLY into ``references_state_detail`` — the rule-based
``references_state`` is never overwritten. Off by default; the hook then costs
nothing (no import of httpx, no network).

Idempotency
-----------
CLI: ``.venv/bin/python -m experiments.probe_outcome --in probe.json
--out probe.classified.json``. A record is "already classified" when its
``responded`` key is a real bool (None on fresh ProbeRecords, always set by
classify) — key presence alone is NOT the marker, because ProbeRecord
serializes all A3 keys with null defaults; such records are skipped
verbatim. Output mirrors the input container (top-level list, or dict with a
"records"/"evaluations" list key).

Wiring
------
``probe_schema.classify`` re-exports this implementation: at import time this
module replaces the NotImplementedError stub in ``experiments.probe_schema``
with ``probe_outcome.classify`` — but ONLY if the schema still carries the
stub. If the schema ever imports this module itself (making ``schema.classify
is probe_outcome.classify``), nothing is replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from experiments.probe_schema import (
    EVENT_CLOSE_CHOICES,
    ProbeRecord,
)

# --------------------------------------------------------------------------- #
# Frozen enum (mirrored from probe_schema for the dict-level core)
# --------------------------------------------------------------------------- #
REPLY_CHOICES: tuple[str, ...] = ("reply", "no_reply")
EVENT_START_CHOICES: tuple[str, ...] = ("initiate", "skip")

# --------------------------------------------------------------------------- #
# boundary_set vocabulary
# --------------------------------------------------------------------------- #
#: Boundary-family terms (canonical output label -> word-boundary regex).
#: "rest(ing)" only: "rested"/"restful" describe mood, not a boundary.
BOUNDARY_PATTERNS: dict[str, str] = {
    "class": r"class(?:es)?\b",
    "work": r"work(?:ing)?\b",
    "gym": r"gym(?:s)?\b",
    "study": r"stud(?:y|ies|ying)\b",
    "run": r"run(?:s|ning)?\b",
    "sleep": r"sleep(?:s|ing)?\b",
    "quiet hours": r"quiet\s+hours\b",
    "deep work": r"deep\s+work\b",
    "dinner": r"dinner(?:s)?\b",
    "rest": r"rest(?:ing)?\b",
}
_BOUNDARY_RES: list[tuple[str, re.Pattern[str]]] = [
    (term, re.compile(pat, re.IGNORECASE)) for term, pat in BOUNDARY_PATTERNS.items()
]

# --------------------------------------------------------------------------- #
# references_state vocabulary (RENDERED BRIEF VOCABULARY + mood family)
# --------------------------------------------------------------------------- #
#: Single-word brief vocabulary: harvested from harness.behavior._render_brief
#: branches (bearing/pace/continuity/texture/care/closing lines) plus the
#: energy-tier availability vocabulary (heavy, drained, tired, exhausted,
#: energetic, rested, quiet, warm) as listed in the A3 brief.
BRIEF_VOCAB: tuple[str, ...] = (
    "bearing", "bright", "tender", "inward", "even", "grounded", "lively",
    "unhurried", "withdrawn", "restraint", "dip", "cadence", "quiet", "warm",
    "heavy", "drained", "tired", "exhausted", "energetic", "rested",
)
_BRIEF_VOCAB_RES: list[re.Pattern[str]] = [
    re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE) for w in BRIEF_VOCAB
]

#: Generic mood-family words (independent of the brief's phrasing).
MOOD_FAMILY_WORDS: tuple[str, ...] = (
    "mood", "energy", "tired", "sleepy", "energized", "low", "drained",
    "bright", "calm", "restless", "anxious", "happy", "sad", "irritable",
)
_MOOD_FAMILY_RES: list[re.Pattern[str]] = [
    re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE) for w in MOOD_FAMILY_WORDS
]

#: Multi-word brief phrases: quoting/paraphrasing these flags references_state
#: (whitespace-normalized case-insensitive substring match).
BRIEF_PHRASES: tuple[str, ...] = (
    "calmly present",
    "low-energy",
    "opening up",
    "quietly bright",
    "even and grounded",
    "a little tender",
    "readily engaged",
    "current bearing",
    "recent dip",
    "withdrawing affection",
    "emotional continuity",
    "light wit",
    "thoughtful pauses",
    "sincere touch",
    "personal touch",
    "keep care intact",
    "exaggerated sweetness",
    "word choice",
)

#: The six A3 outcome fields (idempotency marker + dict merge keys).
A3_FIELDS: tuple[str, ...] = (
    "responded", "choice", "terminate_event", "boundary_set",
    "references_state", "references_state_detail",
)

#: Steer-hook switch (off by default; see module docstring).
_LLM_SWITCH = "PROBE_CLASSIFIER_LLM"


# --------------------------------------------------------------------------- #
# coercion helpers
# --------------------------------------------------------------------------- #
def _as_bool(value: Any) -> bool | None:
    """Coerce a verdict flag to bool|None (mirrors harness.tools._as_bool)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true", "1", "y"):
            return True
        if v in ("no", "false", "0", "n"):
            return False
    return None


def _verdict_of(rec: dict) -> dict:
    verdict = rec.get("verdict")
    return verdict if isinstance(verdict, dict) else {}


def _state_label(rec: dict) -> str:
    return str(rec.get("state_label") or rec.get("state") or "")


def _reason_text(rec: dict, verdict: dict) -> str:
    """Scan source for boundary terms: verdict.reason, top-level reason."""
    reason = verdict.get("reason")
    if not isinstance(reason, str):
        reason = rec.get("reason")
    return reason if isinstance(reason, str) else ""


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for phrase matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


# --------------------------------------------------------------------------- #
# core classification (works on the serialized dict shape)
# --------------------------------------------------------------------------- #
def _classify_dict(rec: dict) -> dict:
    """Compute the A3 outcome fields for one record dict (no mutation)."""
    popup_kind = str(rec.get("popup_kind") or "")
    state = _state_label(rec)
    verdict = _verdict_of(rec)
    reasoning = str(rec.get("reasoning_content") or "")

    # -- responded + choice (mapping rules in the module docstring) --------- #
    responded: bool | None = None
    choice: str | None = None
    if popup_kind == "tool_decide_reply":
        responded = _as_bool(verdict.get("reply"))
        choice = "reply" if responded is True else (
            "no_reply" if responded is False else None
        )
    elif popup_kind == "tool_decide_event":
        if state == "end":
            # A close pop-up always draws a decision; the action is the
            # choice. Out-of-enum / missing action -> choice None.
            responded = True
            action = verdict.get("action")
            choice = action if action in EVENT_CLOSE_CHOICES else None
        else:
            # event start (or unknown state) -> initiate/skip
            responded = _as_bool(verdict.get("initiate"))
            choice = "initiate" if responded is True else (
                "skip" if responded is False else None
            )

    # -- terminate_event (verdict field; event pop-ups never carry it) ------ #
    terminate_event = _as_bool(verdict.get("terminate_event"))

    # -- boundary_set: reasoning_content + verdict.reason ------------------- #
    boundary_set: list[str] = []
    scanned = _normalize(f"{reasoning}\n{_reason_text(rec, verdict)}")
    for term, pattern in _BOUNDARY_RES:
        if pattern.search(scanned):
            boundary_set.append(term)
    event_label = str(rec.get("event_label") or "")
    if event_label:
        norm_label = _normalize(event_label.replace("_", " "))
        if norm_label and norm_label not in boundary_set and norm_label in scanned:
            boundary_set.append(event_label)

    # -- references_state: reasoning_content ONLY --------------------------- #
    references_state = False
    if reasoning.strip():
        lowered = _normalize(reasoning)
        references_state = (
            any(p.search(lowered) for p in _BRIEF_VOCAB_RES)
            or any(p.search(lowered) for p in _MOOD_FAMILY_RES)
            or any(phrase in lowered for phrase in BRIEF_PHRASES)
        )

    # -- steer hook: second-model pass -> references_state_detail only ------ #
    references_state_detail = _llm_reference_pass(reasoning, str(rec.get("brief") or ""))

    return {
        "responded": responded,
        "choice": choice,
        "terminate_event": terminate_event,
        "boundary_set": boundary_set,
        "references_state": references_state,
        "references_state_detail": references_state_detail,
    }


# --------------------------------------------------------------------------- #
# probe_schema.classify — the frozen interface
# --------------------------------------------------------------------------- #
def classify(record: ProbeRecord) -> ProbeRecord:
    """Fill the A3 outcome fields onto ``record`` and return it.

    Rule-based and deterministic by default (see module docstring for the
    mapping rules and vocabulary). Mutates and returns the same record.
    """
    fields = _classify_dict(asdict(record))
    for key, value in fields.items():
        setattr(record, key, value)
    return record


# --------------------------------------------------------------------------- #
# steer hook: second-model pass (built, NOT enabled by default)
# --------------------------------------------------------------------------- #
def _llm_reference_pass(reasoning_content: str, brief: str) -> str | None:
    """Optional second-model pass on references_state.

    Enabled ONLY by ``PROBE_CLASSIFIER_LLM=1``; otherwise returns None without
    importing httpx or touching the network. When enabled, asks the configured
    model whether the reasoning referenced the state card (mood brief) and
    returns its raw answer (or an ``ERROR: ...`` string on failure). The
    caller stores this ONLY in ``references_state_detail`` — the rule-based
    ``references_state`` is never overwritten.
    """
    if os.environ.get(_LLM_SWITCH, "0") != "1":
        return None
    import httpx  # lazy: default path never imports it

    _load_env()
    base_url = (os.environ.get("LLM_BASE_URL") or
                "https://opencode.ai/zen/go/v1/").rstrip("/")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    prompt = (
        "You are a classifier for a behavioral experiment. Here is the "
        "state-card mood brief that was injected into the assistant's system "
        "prompt:\n\n"
        f"<brief>\n{brief}\n</brief>\n\n"
        "And here is the assistant's verbatim reasoning when a decision "
        "pop-up fired:\n\n"
        f"<reasoning>\n{reasoning_content}\n</reasoning>\n\n"
        'Answer with a JSON object: {"references_state": bool, '
        '"quoted_phrases": [..], "note": ".."} — references_state is true iff '
        "the reasoning references the mood/state card at all (mood words, "
        "energy, bearing, or paraphrases of the brief), false otherwise."
    )
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0.0,
                    "messages": [
                        {"role": "system",
                         "content": "You answer with JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        return str(message.get("content") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 — loud failure, visible in the field
        return f"ERROR: {type(exc).__name__}: {exc}"


def _load_env() -> None:
    """Load ~/.hermes/.env + map OPENCODE_GO_* -> LLM_* (mirrors
    experiments/decision_probe._load_env; never overrides, never prints)."""
    env_file = Path.home() / ".hermes/.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    if "LLM_API_KEY" not in os.environ and os.environ.get("OPENCODE_GO_API_KEY"):
        os.environ["LLM_API_KEY"] = os.environ["OPENCODE_GO_API_KEY"]
    if "LLM_BASE_URL" not in os.environ and os.environ.get("OPENCODE_GO_BASE_URL"):
        os.environ["LLM_BASE_URL"] = os.environ["OPENCODE_GO_BASE_URL"]


# --------------------------------------------------------------------------- #
# probe_schema wiring (idempotent, non-destructive)
# --------------------------------------------------------------------------- #
def _install_into_schema() -> None:
    """Make ``from experiments.probe_schema import classify`` resolve to this
    implementation.

    Replaces the NotImplementedError stub in ``probe_schema`` ONLY while the
    stub is still there; if the schema ever imports this module itself
    (``schema.classify is classify``) or carries another real implementation,
    nothing is clobbered.
    """
    import experiments.probe_schema as _schema

    if _schema.classify is classify:
        return
    try:
        _schema.classify(None)  # type: ignore[arg-type]  # stub raises first
    except NotImplementedError:
        _schema.classify = classify


_install_into_schema()


# --------------------------------------------------------------------------- #
# CLI: --in probe.json --out probe.classified.json
# --------------------------------------------------------------------------- #
def _load_records(path: Path) -> tuple[Any, list[dict]]:
    """Load records, tolerating a top-level list or a dict with a
    "records"/"evaluations" list key. Returns (container, records)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, data
    if isinstance(data, dict):
        for key in ("records", "evaluations"):
            value = data.get(key)
            if isinstance(value, list):
                return data, value
        raise ValueError(
            f"{path}: expected a list of records or a dict with a "
            f"'records'/'evaluations' list key, got keys {sorted(data)}"
        )
    raise ValueError(f"{path}: unsupported JSON container {type(data).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A3 post-hoc outcome classification for decision probe v2 "
                    "(deterministic; PROBE_CLASSIFIER_LLM=1 enables the "
                    "references_state steer pass).",
    )
    parser.add_argument("--in", dest="in_path", type=Path, required=True,
                        help="input probe.json (records list or dict with a "
                             "'records'/'evaluations' list key)")
    parser.add_argument("--out", dest="out_path", type=Path, required=True,
                        help="output probe.classified.json (same container "
                             "shape, A3 fields added)")
    args = parser.parse_args(argv)

    container, records = _load_records(args.in_path)
    classified, skipped = 0, 0
    for rec in records:
        if not isinstance(rec, dict):
            raise ValueError(f"record is not a dict: {rec!r}")
        if rec.get("responded") is not None:
            skipped += 1  # already classified — idempotent skip
            continue
        rec.update(_classify_dict(rec))
        classified += 1

    if isinstance(container, dict):
        key = "records" if "records" in container else "evaluations"
        container[key] = records
    args.out_path.write_text(
        json.dumps(container, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"classified: {classified}  skipped (already classified): {skipped}")
    for rec in records:
        print(json.dumps(
            {k: rec.get(k) for k in
             ("leg_id", "sample_id", "popup_kind", "state_label",
              "event_label") + A3_FIELDS},
            ensure_ascii=False,
        ))
    print(f"wrote {args.out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
