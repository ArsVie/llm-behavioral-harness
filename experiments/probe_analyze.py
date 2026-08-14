"""Decision probe v2 — analysis + report writer over classified records (A4).

Consumes the *classified* probe.json written after
``experiments/probe_outcome.py`` (A3) has run: the run meta plus a list of
ProbeRecord-shaped dicts (shape frozen in ``experiments/probe_schema.py``).
Additive tolerance: every classification field defaults, so records written
before a field existed still parse.

Analysis produced (all offline, no model calls):

1. Per-scenario_id dose-response: P(positive choice | mood dose) over the K
   reps — n per cell, observed proportion, Wilson 95% binomial CI, and the
   observed rep-level spread. Positive choice = ``reply`` (tool_decide_reply),
   ``initiate`` (event start), ``follow`` (event end).
2. Per-channel sweeps, one lever per channel: valence sweep (M values over
   0..10 -> P(positive)) and energy sweep (engineered hour values ->
   P(positive)), per scenario.
3. references_state by mood dose: rate over K with n and CI.
4. THE HEADLINE SPLIT with rates over K: *state never entered deliberation*
   (references_state=False) vs *state entered and was discounted*
   (references_state=True but the choice went against the state's pull) vs
   *state entered and followed*. The pull mapping is a documented, steerable
   definition (module constants + ``state_pull``).
5. Acceptance checks: every leg carries non-empty reasoning_content (empty
   count), responded and choice are separate fields (type + consistency
   conflation scan), and the runtime tool schema is untouched
   (``harness/tools.py`` unchanged per git — reported, never modified).

Report: OKF-style YAML frontmatter (same shape as the v1 report in
results/decision-probe-2026-08-14/report.md), dose-response tables, the
references_state breakdown, the headline split, and EVERY verbatim trace
(short reasoning inlined in the report; long reasoning written per-leg into
``traces/leg_<leg_id>.md`` and referenced; samples at each mood extreme quoted
in full).

Run::

    .venv/bin/python -m experiments.probe_analyze \\
        --in probe.classified.json \\
        --out results/decision-probe-v2-2026-08-14 \\
        --report report.md

The report references the input probe.json and the sidecar decision_probe.db.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from experiments.probe_schema import (
    EVENT_CLOSE_CHOICES,
    EVENT_START_CHOICES,
    MOOD_SCALE,
    REPLY_CHOICES,
)

# --------------------------------------------------------------------------- #
# Steerable definitions (recorded in the report; change here, re-run)
# --------------------------------------------------------------------------- #

#: Low-mood thresholds for the state-pull mapping (``state_pull``). M is the
#: engine mood scale 0..MOOD_SCALE (valence = 2*M/scale - 1, so M=5 is
#: valence 0 — neutral counts as NOT low). Fallbacks when M is absent:
#: valence < 0 or energy < 0.4 -> low.
LOW_M_THRESHOLD: int = 5
LOW_VALENCE_THRESHOLD: float = 0.0
LOW_ENERGY_THRESHOLD: float = 0.4

#: The state's pull (choice family the mood pushes toward). Steerable:
#: low mood pulls toward withdrawal/restraint, otherwise toward engagement.
PULL_LOW_REPLY: tuple[str, ...] = ("no_reply",)
PULL_HIGH_REPLY: tuple[str, ...] = ("reply",)
PULL_LOW_START: tuple[str, ...] = ("skip",)
PULL_HIGH_START: tuple[str, ...] = ("initiate",)
PULL_LOW_END: tuple[str, ...] = ("abandon", "defer")  # withdrawal/restraint
PULL_HIGH_END: tuple[str, ...] = ("follow",)

#: Reasoning shorter than this is inlined in the report; longer reasoning is
#: written to traces/leg_<leg_id>.md and referenced.
INLINE_REASONING_CHARS: int = 500

#: Wilson interval z (95%).
WILSON_Z: float = 1.96

ALL_CHOICES: tuple[str, ...] = (
    REPLY_CHOICES + EVENT_START_CHOICES + EVENT_CLOSE_CHOICES
)

_DOSE_ID_M_RE = re.compile(r"-M(\d+)$")
_DOSE_ID_HOUR_RE = re.compile(r"-h(\d+(?:\.\d+)?)$")

DEFAULT_IN = Path("probe.classified.json")
DEFAULT_OUT = Path("results/decision-probe-v2-2026-08-14")
DEFAULT_REPORT = "report.md"

# --------------------------------------------------------------------------- #
# record loading / normalization
# --------------------------------------------------------------------------- #


def record_from_dict(raw: dict) -> dict:
    """Normalize one raw ProbeRecord dict (additive tolerance).

    Every field defaults per probe_schema.ProbeRecord; unknown additive
    fields are kept untouched.
    """
    mood_vector = raw.get("mood_vector") or {}
    return {
        "scenario_id": raw.get("scenario_id", ""),
        "sample_id": raw.get("sample_id", ""),
        "popup_kind": raw.get("popup_kind", ""),
        "event_label": raw.get("event_label", ""),
        "state_label": raw.get("state_label", ""),
        "time": raw.get("time", 0.0),
        "conversation_context": raw.get("conversation_context", ""),
        "transport": raw.get("transport", ""),
        "dose_id": raw.get("dose_id", ""),
        "mood_vector": dict(mood_vector),
        "engineered": dict(raw.get("engineered") or {}),
        "brief": raw.get("brief", ""),
        "brief_hash": raw.get("brief_hash", ""),
        "leg_id": raw.get("leg_id", ""),
        "rep_k": raw.get("rep_k", 0),
        "reasoning_content": raw.get("reasoning_content", ""),
        "reasoning_present": raw.get("reasoning_present", False),
        "raw_reply": raw.get("raw_reply", ""),
        "verdict": raw.get("verdict"),
        "source": raw.get("source", ""),
        "parse_failure": raw.get("parse_failure", False),
        # A3 classification (defaults: old records still parse)
        "responded": raw.get("responded"),
        "choice": raw.get("choice"),
        "terminate_event": raw.get("terminate_event"),
        "boundary_set": list(raw.get("boundary_set") or []),
        "references_state": bool(raw.get("references_state", False)),
        "references_state_detail": raw.get("references_state_detail"),
    }


def load_records(path: Path) -> tuple[list[dict], dict]:
    """Load ``{meta, records}`` (or v1-style ``{meta, evaluations}``) JSON.

    Bare-list input (the run loop writes a bare records list to probe.json and
    probe_outcome preserves that shape) falls back to the sibling ``meta.json``
    for metadata.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        meta: dict = {}
        meta_path = Path(path).with_name("meta.json")
        if meta_path.exists():
            meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = meta_raw if isinstance(meta_raw, dict) else {}
        return [record_from_dict(r) for r in data], meta
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object with meta + records")
    records = data.get("records", data.get("evaluations"))
    if not isinstance(records, list):
        raise ValueError(
            f"{path}: no 'records' (or 'evaluations') list found"
        )
    meta_raw = data.get("meta")
    meta: dict = meta_raw if isinstance(meta_raw, dict) else {}
    return [record_from_dict(r) for r in records], meta


# --------------------------------------------------------------------------- #
# dose axes
# --------------------------------------------------------------------------- #


def dose_axis_value(record: dict, axis: str) -> float | None:
    """Numeric dose-axis value for a record.

    Valence axis ``"M"``: explicit ``mood_vector["M"]`` ->
    ``engineered["M"]`` -> ``dose_id`` pattern ``-M<n>`` (e.g. "val-M8").
    Energy axis ``"hour"``: ``mood_vector["hour"]`` -> ``engineered["hour"]``
    -> ``dose_id`` pattern ``-h<n>`` (e.g. "ene-h16"). None when absent.
    """
    vec = record.get("mood_vector") or {}
    eng = record.get("engineered") or {}
    dose_id = record.get("dose_id") or ""
    if axis == "M":
        for src in (vec, eng):
            v = src.get("M")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        m = _DOSE_ID_M_RE.search(dose_id)
        return float(m.group(1)) if m else None
    if axis == "hour":
        for src in (vec, eng):
            v = src.get("hour")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        h = _DOSE_ID_HOUR_RE.search(dose_id)
        return float(h.group(1)) if h else None
    raise ValueError(f"unknown dose axis: {axis!r}")


def positive_choice(record: dict) -> bool | None:
    """The engaged/positive choice: reply / initiate / follow. None when the
    leg has no classified choice."""
    choice = record.get("choice")
    if choice is None:
        return None
    if record.get("popup_kind") == "tool_decide_reply":
        return choice == "reply"
    if record.get("popup_kind") == "tool_decide_event":
        if record.get("state_label") == "end":
            return choice == "follow"
        return choice == "initiate"
    return None


# --------------------------------------------------------------------------- #
# statistics (Wilson 95% binomial CI)
# --------------------------------------------------------------------------- #


def wilson_ci(k: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for k/n with coverage z (95% default)."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:.1f}%"


def _fmt_ci(ci: tuple[float, float]) -> str:
    return f"{ci[0]:.2f}–{ci[1]:.2f}"


def _spread(records: list[dict]) -> str:
    """Observed rep-level spread: one char per leg (1 = positive choice)."""
    return "".join(
        "1" if positive_choice(r) is True else (
            "0" if positive_choice(r) is False else "?"
        )
        for r in records
    )


# --------------------------------------------------------------------------- #
# state-pull mapping (documented, steerable) + headline split
# --------------------------------------------------------------------------- #


def mood_is_low(record: dict) -> bool | None:
    """Low-mood signal for the pull mapping. None when no mood signal is
    present (pull undefined). Precedence: M -> valence -> energy."""
    m = dose_axis_value(record, "M")
    if m is not None:
        return m < LOW_M_THRESHOLD
    vec = record.get("mood_vector") or {}
    if vec.get("valence") is not None:
        return float(vec["valence"]) < LOW_VALENCE_THRESHOLD
    if vec.get("energy") is not None:
        return float(vec["energy"]) < LOW_ENERGY_THRESHOLD
    return None


def state_pull(record: dict) -> frozenset[str] | None:
    """The choice family the state's mood pulls toward.

    Steerable definition (module constants): low mood (M < 5, or valence < 0,
    or energy < 0.4) pulls toward withdrawal/restraint — ``no_reply`` for
    reply pop-ups, ``skip`` for event starts, ``{abandon, defer}`` at event
    closes. Otherwise the pull is engagement — ``reply`` / ``initiate`` /
    ``follow``. None when the mood signal is absent.
    """
    low = mood_is_low(record)
    if low is None:
        return None
    kind = record.get("popup_kind")
    if kind == "tool_decide_reply":
        return frozenset(PULL_LOW_REPLY if low else PULL_HIGH_REPLY)
    if kind == "tool_decide_event":
        if record.get("state_label") == "end":
            return frozenset(PULL_LOW_END if low else PULL_HIGH_END)
        return frozenset(PULL_LOW_START if low else PULL_HIGH_START)
    return None


def headline_bucket(record: dict) -> str:
    """Headline-split bucket for one leg:

    - ``never_entered``: the state card never entered deliberation
      (references_state=False).
    - ``entered_followed``: references_state=True and the choice is inside
      the state's pull family.
    - ``entered_discounted``: references_state=True but the choice went
      AGAINST the state's pull (state entered, then discounted).
    - ``unclassified``: no choice and/or no mood signal — cannot judge.
    """
    if not record.get("references_state"):
        return "never_entered"
    if record.get("choice") is None:
        return "unclassified"
    pull = state_pull(record)
    if pull is None:
        return "unclassified"
    return "entered_followed" if record["choice"] in pull else (
        "entered_discounted"
    )


_BUCKETS = ("never_entered", "entered_followed", "entered_discounted")


def _bucket_stats(records: list[dict]) -> dict:
    counts = {b: 0 for b in _BUCKETS}
    unclassified = 0
    for r in records:
        b = headline_bucket(r)
        if b == "unclassified":
            unclassified += 1
        else:
            counts[b] += 1
    classified = len(records) - unclassified
    out: dict[str, Any] = {"n_unclassified": unclassified, "n_classified": classified}
    for b in _BUCKETS:
        n = counts[b]
        ci = wilson_ci(n, classified) if classified else (0.0, 0.0)
        out[b] = {
            "n": n,
            "rate": (n / classified) if classified else 0.0,
            "ci": ci,
        }
    return out


# --------------------------------------------------------------------------- #
# acceptance checks
# --------------------------------------------------------------------------- #


def acceptance_checks(records: list[dict]) -> dict:
    """FLOOR invariants over classified records.

    - reasoning_content non-empty on every leg (count empties; also flag
      reasoning_present mismatches).
    - responded and choice are SEPARATE fields: full type scan (responded
      must be bool|None, choice must be a choice-enum str|None — no
      conflation) plus a semantic consistency scan on reply pop-ups
      (responded == (choice == "reply")).
    """
    empty_reasoning = [
        r["leg_id"] for r in records
        if not (r.get("reasoning_content") or "").strip()
    ]
    present_mismatch = [
        r["leg_id"] for r in records
        if r.get("reasoning_present")
        != bool((r.get("reasoning_content") or "").strip())
    ]
    conflation: list[tuple[str, str]] = []
    inconsistent: list[tuple[str, object, object]] = []
    for r in records:
        resp = r.get("responded")
        choice = r.get("choice")
        if resp is not None and not isinstance(resp, bool):
            conflation.append((r["leg_id"], "responded is not bool"))
        if choice is not None and not isinstance(choice, str):
            conflation.append((r["leg_id"], "choice is not str"))
        elif isinstance(choice, str) and choice not in ALL_CHOICES:
            conflation.append(
                (r["leg_id"], f"choice {choice!r} not in choice enum")
            )
        if (r.get("popup_kind") == "tool_decide_reply"
                and isinstance(resp, bool) and isinstance(choice, str)):
            if resp != (choice == "reply"):
                inconsistent.append((r["leg_id"], resp, choice))
    return {
        "total_legs": len(records),
        "n_empty_reasoning": len(empty_reasoning),
        "empty_reasoning_legs": empty_reasoning[:20],
        "n_reasoning_present_mismatch": len(present_mismatch),
        "reasoning_present_mismatch_legs": present_mismatch[:20],
        "n_conflation": len(conflation),
        "conflation": conflation[:20],
        "n_responded_choice_inconsistent": len(inconsistent),
        "responded_choice_inconsistent": inconsistent[:20],
    }


def runtime_schema_check(repo_root: Path | None = None) -> dict:
    """Best-effort check that the runtime tool schema is untouched.

    Runs ``git diff --quiet`` + ``git status --porcelain`` over the
    harness/ and engine/ trees (frozen); specifically calls out
    harness/tools.py. Never modifies anything.
    """
    root = Path(repo_root or Path(__file__).resolve().parents[1])
    result: dict[str, Any] = {"checked": False, "clean": None,
                              "tools_py_unchanged": None, "detail": ""}
    try:
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", "--", "harness", "engine"],
            capture_output=True, text=True, timeout=20,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain",
             "--", "harness", "engine"],
            capture_output=True, text=True, timeout=20,
        )
        tools_diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", "--", "harness/tools.py"],
            capture_output=True, text=True, timeout=20,
        )
        result["checked"] = True
        result["clean"] = (
            diff.returncode == 0 and status.stdout.strip() == ""
        )
        result["tools_py_unchanged"] = tools_diff.returncode == 0
        result["detail"] = (
            "harness/ + engine/ trees clean (git diff + status empty)"
            if result["clean"] else
            f"harness/engine dirty — git status: {status.stdout.strip()[:200]}"
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        result["detail"] = f"git unavailable: {exc}"
    return result


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #


def _cell(records: list[dict]) -> dict:
    """One dose cell: n, k (positive), p, Wilson CI, rep spread.

    n counts only legs with a classified choice (positive_choice not None);
    unclassified legs are excluded and noted in the report."""
    classified = [r for r in records if positive_choice(r) is not None]
    n = len(classified)
    k = sum(1 for r in classified if positive_choice(r) is True)
    return {
        "n": n,
        "k": k,
        "p": (k / n) if n else 0.0,
        "ci": wilson_ci(k, n),
        "spread": _spread(classified),
    }


def _dose_sort_key(record: dict) -> tuple:
    m, h = dose_axis_value(record, "M"), dose_axis_value(record, "hour")
    if m is not None:
        return (0, m)
    if h is not None:
        return (1, h)
    return (2, record.get("dose_id", ""))


def dose_response_by_scenario(records: list[dict]) -> dict[str, list[dict]]:
    """Per scenario_id: dose cells (n, k, p, CI, spread) over K reps,
    ordered M-ascending then hour-ascending."""
    by_scenario: dict[str, list[dict]] = {}
    for r in records:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    out: dict[str, list[dict]] = {}
    for scenario_id, legs in sorted(by_scenario.items()):
        by_dose: dict[str, list[dict]] = {}
        for leg in legs:
            by_dose.setdefault(leg["dose_id"], []).append(leg)
        cells = []
        for dose_id, dose_legs in by_dose.items():
            cell = _cell(dose_legs)
            cell["dose_id"] = dose_id
            m, h = dose_axis_value(dose_legs[0], "M"), dose_axis_value(
                dose_legs[0], "hour")
            if m is not None:
                cell["axis"], cell["value"] = "M", m
            elif h is not None:
                cell["axis"], cell["value"] = "hour", h
            else:
                cell["axis"], cell["value"] = "-", 0.0
            cells.append(cell)
        cells.sort(key=lambda c: (
            0 if c["axis"] == "M" else 1 if c["axis"] == "hour" else 2,
            c["value"],
            c["dose_id"],
        ))
        out[scenario_id] = cells
    return out


def channel_sweep(records: list[dict], axis: str) -> dict[str, list[dict]]:
    """Valence (M) or energy (hour) sweep per scenario: value -> dose cell."""
    by_scenario: dict[str, list[dict]] = {}
    for r in records:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    out: dict[str, list[dict]] = {}
    for scenario_id, legs in sorted(by_scenario.items()):
        by_value: dict[float, list[dict]] = {}
        for leg in legs:
            v = dose_axis_value(leg, axis)
            if v is None:
                continue
            by_value.setdefault(v, []).append(leg)
        cells = []
        for value, value_legs in sorted(by_value.items()):
            cell = _cell(value_legs)
            cell["value"] = value
            cells.append(cell)
        out[scenario_id] = cells
    return out


def references_by_dose(records: list[dict]) -> list[dict]:
    """references_state rate over K per mood dose, pooled across scenarios,
    plus a per-scenario rate grid."""
    by_dose: dict[str, list[dict]] = {}
    for r in records:
        by_dose.setdefault(r["dose_id"], []).append(r)
    rows = []
    for dose_id in sorted(by_dose):
        dose_legs = by_dose[dose_id]
        n = len(dose_legs)
        k = sum(1 for r in dose_legs if r["references_state"])
        rows.append({
            "dose_id": dose_id,
            "n": n,
            "k": k,
            "rate": k / n,
            "ci": wilson_ci(k, n),
        })
    return rows


def _terminate_spread(records: list[dict]) -> str:
    """Rep-level spread of terminate_event (1 = terminate)."""
    return "".join(
        "1" if r.get("terminate_event") is True else (
            "0" if r.get("terminate_event") is False else "?"
        )
        for r in records
    )


def terminate_by_dose(records: list[dict]) -> dict:
    """terminate_event rate over K per mood dose (reply pop-ups only).

    The reply axis itself (choice == 'reply') is often boundary-dominated;
    terminate_event is the discretionary action *inside* the reply verdict
    (leave rest to go out, end the event to follow the user's intent).
    Returns pooled rows plus per-scenario cells: n, k, rate, Wilson CI,
    rep spread. Legs with terminate_event None are excluded.
    """
    reply_legs = [
        r for r in records
        if r.get("popup_kind") == "tool_decide_reply"
        and r.get("terminate_event") is not None
    ]

    def _cells(legs: list[dict]) -> list[dict]:
        by_dose: dict[str, list[dict]] = {}
        for leg in legs:
            by_dose.setdefault(leg["dose_id"], []).append(leg)
        cells = []
        for dose_id in sorted(by_dose):
            dose_legs = by_dose[dose_id]
            n = len(dose_legs)
            k = sum(1 for r in dose_legs if r["terminate_event"] is True)
            cells.append({
                "dose_id": dose_id,
                "n": n,
                "k": k,
                "rate": k / n,
                "ci": wilson_ci(k, n),
                "spread": _terminate_spread(dose_legs),
            })
        return cells

    by_scenario: dict[str, list[dict]] = {}
    for r in reply_legs:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    return {
        "pooled": _cells(reply_legs),
        "per_scenario": {
            sid: _cells(legs) for sid, legs in sorted(by_scenario.items())
        },
    }


def headline_split(records: list[dict]) -> dict:
    """Pooled headline buckets + per-scenario breakdown (rates over K)."""
    pooled = _bucket_stats(records)
    by_scenario: dict[str, list[dict]] = {}
    for r in records:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    per_scenario = {
        sid: _bucket_stats(legs) for sid, legs in sorted(by_scenario.items())
    }
    return {"pooled": pooled, "per_scenario": per_scenario}


def analyze(records: list[dict], meta: dict | None = None) -> dict:
    """Full offline analysis (pure, no I/O)."""
    meta = meta or {}
    by_scenario: dict[str, list[dict]] = {}
    for r in records:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    return {
        "meta": meta,
        "n_legs": len(records),
        "n_scenarios": len(by_scenario),
        "scenario_ids": sorted(by_scenario),
        "scenarios": {
            sid: {
                "legs": len(legs),
                "doses": sorted({r["dose_id"] for r in legs}),
                "k": max((r["rep_k"] for r in legs), default=0) + 1,
            }
            for sid, legs in sorted(by_scenario.items())
        },
        "dose_response": dose_response_by_scenario(records),
        "valence_sweep": channel_sweep(records, "M"),
        "energy_sweep": channel_sweep(records, "hour"),
        "references_by_dose": references_by_dose(records),
        "terminate_by_dose": terminate_by_dose(records),
        "headline": headline_split(records),
        "acceptance": acceptance_checks(records),
        "runtime_schema": runtime_schema_check(),
    }


# --------------------------------------------------------------------------- #
# report writer
# --------------------------------------------------------------------------- #


def _frontmatter(meta: dict) -> str:
    seeds = meta.get("seeds") or (
        [meta["seed"]] if meta.get("seed") is not None else []
    )
    model = meta.get("model", "unknown")
    mode = meta.get("mode", "unknown")
    timestamp = (
        meta.get("timestamp") or meta.get("finished_at")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    return (
        "---\n"
        "type: decision-probe-v2-report\n"
        "title: \"decision probe v2 — mood dose-response analysis\"\n"
        "description: \"classified probe.json -> per-scenario dose-response "
        "P(choice|dose) over K, per-channel valence/energy sweeps, "
        "references_state by dose, headline split (never entered / "
        "entered-discounted / entered-followed).\"\n"
        f"seeds: {json.dumps(seeds, ensure_ascii=False)}\n"
        f"model: {model}\n"
        f"mode: {mode}\n"
        f"timestamp: {timestamp}\n"
        "tags: [decision-probe, v2, dose-response]\n"
        "---\n"
    )


def _render_cell_table(rows: list[dict], value_col: str,
                       label: str | None = None) -> str:
    header = label or value_col
    lines = [
        f"| {header} | n | positive | P(positive) | 95% CI (Wilson) | "
        "spread (reps) |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row[value_col]} | {row['n']} | {row['k']} | "
            f"{_fmt_pct(row['p'])} | {_fmt_ci(row['ci'])} | "
            f"`{row['spread']}` |"
        )
    return "\n".join(lines)


def _pull_mapping_doc() -> str:
    return (
        "Steerable pull definition (`probe_analyze.state_pull`): the mood "
        "dose defines a *pull* — the choice family the state pushes toward. "
        f"Low mood (M < {LOW_M_THRESHOLD} on the 0..{MOOD_SCALE} scale, or "
        f"valence < {LOW_VALENCE_THRESHOLD}, or energy < "
        f"{LOW_ENERGY_THRESHOLD}) pulls toward withdrawal/restraint: "
        f"`{PULL_LOW_REPLY}` for reply pop-ups, `{PULL_LOW_START}` for event "
        f"starts, `{PULL_LOW_END}` at event closes. Otherwise the pull is "
        f"engagement: `{PULL_HIGH_REPLY}` / `{PULL_HIGH_START}` / "
        f"`{PULL_HIGH_END}`. A leg is *followed* when its choice is inside "
        "the pull family, *discounted* when references_state=True and the "
        "choice is outside it. Change the constants, re-run, and the "
        "headline moves with the definition."
    )


def _render_headline(analysis: dict) -> str:
    pooled = analysis["headline"]["pooled"]
    lines = [_pull_mapping_doc(), ""]
    lines.append(f"Rates over the {pooled['n_classified']} classified legs "
                 f"({pooled['n_unclassified']} unclassified — no choice "
                 "and/or no mood signal — excluded from the rates).")
    lines.append("")
    lines.append("| bucket | n | rate | 95% CI (Wilson) |")
    lines.append("|---|---|---|---|")
    for b in _BUCKETS:
        s = pooled[b]
        lines.append(
            f"| {b} | {s['n']} | {_fmt_pct(s['rate'])} | "
            f"{_fmt_ci(s['ci'])} |"
        )
    lines.append("")
    lines.append("### Per scenario")
    lines.append("")
    lines.append("| scenario | never entered | entered & discounted | "
                 "entered & followed | n classified |")
    lines.append("|---|---|---|---|---|")
    for sid, stats in analysis["headline"]["per_scenario"].items():
        lines.append(
            f"| {sid} | {_fmt_pct(stats['never_entered']['rate'])} "
            f"({stats['never_entered']['n']}) | "
            f"{_fmt_pct(stats['entered_discounted']['rate'])} "
            f"({stats['entered_discounted']['n']}) | "
            f"{_fmt_pct(stats['entered_followed']['rate'])} "
            f"({stats['entered_followed']['n']}) | "
            f"{stats['n_classified']} |"
        )
    return "\n".join(lines)


def _render_acceptance(analysis: dict) -> str:
    a = analysis["acceptance"]
    rs = analysis["runtime_schema"]
    lines = [
        f"- **Reasoning captured verbatim**: {a['total_legs']} legs; "
        f"**{a['n_empty_reasoning']}** with empty reasoning_content "
        f"({'pass' if a['n_empty_reasoning'] == 0 else 'FAIL'}); "
        f"{a['n_reasoning_present_mismatch']} reasoning_present mismatches "
        f"({'pass' if a['n_reasoning_present_mismatch'] == 0 else 'FAIL'}).",
        f"- **responded and choice are separate fields**: "
        f"{a['n_conflation']} type-level conflation violations "
        f"({'pass' if a['n_conflation'] == 0 else 'FAIL'}), "
        f"{a['n_responded_choice_inconsistent']} semantic inconsistencies "
        f"(reply pop-ups: responded != (choice == 'reply')) "
        f"({'pass' if a['n_responded_choice_inconsistent'] == 0 else 'FAIL'}).",
        f"- **Runtime schema untouched**: harness/tools.py unchanged = "
        f"{rs.get('tools_py_unchanged')} "
        f"({'pass' if rs.get('tools_py_unchanged') else 'not verified' if not rs.get('checked') else 'FAIL'}) "
        f"— {rs.get('detail', '')}.",
    ]
    if a["empty_reasoning_legs"]:
        lines.append("")
        lines.append("Empty-reasoning legs: "
                     + ", ".join(a["empty_reasoning_legs"]))
    if a["conflation"]:
        lines.append("")
        lines.append("Conflation violations: "
                     + "; ".join(f"{lid}: {why}"
                                 for lid, why in a["conflation"]))
    if a["responded_choice_inconsistent"]:
        lines.append("")
        lines.append("responded/choice inconsistencies: "
                     + "; ".join(f"{lid}: responded={r!r} choice={c!r}"
                                 for lid, r, c
                                 in a["responded_choice_inconsistent"]))
    return "\n".join(lines)


def _trace_fname(leg_id: str) -> str:
    return "leg_" + leg_id.replace(":", "_").replace("/", "_") + ".md"


def _render_traces(analysis: dict, records: list[dict], out: Path) -> tuple[str, str]:
    """Write every leg's trace file; render the per-leg listing (short
    reasoning inline, long reasoning -> trace file) + mood-extreme samples."""
    traces_dir = out / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    inline_legs: list[str] = []
    for r in records:
        fname = _trace_fname(r["leg_id"])
        (traces_dir / fname).write_text(
            _render_trace_file(r), encoding="utf-8"
        )
        reasoning = (r.get("reasoning_content") or "").strip()
        if len(reasoning) <= INLINE_REASONING_CHARS:
            inline_legs.append(fname)

    lines = []
    by_scenario: dict[str, list[dict]] = {}
    for r in records:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    for sid, legs in sorted(by_scenario.items()):
        lines.append(f"### {sid}")
        lines.append("")
        lines.append("| leg | dose | rep | choice | references_state | "
                     "reasoning |")
        lines.append("|---|---|---|---|---|---|")
        for r in sorted(legs, key=lambda x: (x["dose_id"], x["rep_k"])):
            fname = _trace_fname(r["leg_id"])
            refs = "yes" if r.get("references_state") else "no"
            lines.append(
                f"| `{r['leg_id']}` | {r['dose_id']} | {r['rep_k']} | "
                f"{r.get('choice')} | {refs} | "
                f"[trace](traces/{fname}) |"
            )
        lines.append("")
        # inline the short reasoning verbatim, per leg
        for r in sorted(legs, key=lambda x: (x["dose_id"], x["rep_k"])):
            reasoning = (r.get("reasoning_content") or "").strip()
            if not reasoning:
                continue
            if len(reasoning) <= INLINE_REASONING_CHARS:
                lines.append(
                    f"**{r['leg_id']}** ({r['dose_id']} · k{r['rep_k']} · "
                    f"choice {r.get('choice')} · references_state "
                    f"{'yes' if r.get('references_state') else 'no'})"
                )
                lines.append("")
                lines.append(f"> {reasoning}")
                lines.append("")
            else:
                lines.append(
                    f"**{r['leg_id']}** ({r['dose_id']} · k{r['rep_k']} · "
                    f"choice {r.get('choice')}) — long reasoning → "
                    f"[trace file](traces/{_trace_fname(r['leg_id'])})"
                )
                lines.append("")

    lines.append("### Trace samples at mood extremes")
    lines.append("")
    lines.append(
        "Full verbatim reasoning for the extreme legs of each dose axis "
        "(lowest vs highest observed value), first rep."
    )
    lines.append("")
    for axis, label in (("M", "valence (M)"), ("hour", "energy (hour)")):
        valued: list[tuple[float, dict]] = []
        for r in records:
            v = dose_axis_value(r, axis)
            if v is not None:
                valued.append((v, r))
        if not valued:
            continue
        lo = min(valued, key=lambda pair: pair[0])[1]
        hi = max(valued, key=lambda pair: pair[0])[1]
        for tag, r in (("lowest", lo), ("highest", hi)):
            reasoning = (r.get("reasoning_content") or "").strip()
            lines.append(
                f"**{label} {tag}** — {r['leg_id']} ({r['dose_id']} · "
                f"k{r['rep_k']} · choice {r.get('choice')})"
            )
            lines.append("")
            lines.append(f"> {reasoning}" if reasoning else "> _(empty)_")
            lines.append("")
    return "\n".join(lines), "\n".join(inline_legs)


def _render_trace_file(r: dict) -> str:
    verdict = r.get("verdict")
    return (
        f"# Trace — {r['leg_id']}\n\n"
        f"- scenario: {r['scenario_id']}\n"
        f"- dose: {r['dose_id']} · rep k{r['rep_k']}\n"
        f"- popup_kind: {r.get('popup_kind')} · state_label: "
        f"{r.get('state_label')} · time: {r.get('time')}\n"
        f"- source: {r.get('source')} · parse_failure: "
        f"{r.get('parse_failure')}\n"
        f"- responded: {r.get('responded')} · choice: {r.get('choice')} · "
        f"references_state: {r.get('references_state')}\n\n"
        f"## brief (verbatim)\n\n> {r.get('brief') or '_(none)_'}\n\n"
        f"## reasoning_content (verbatim)\n\n"
        f"> {r.get('reasoning_content') or '_(empty)_'}\n\n"
        f"## raw_reply (verbatim)\n\n> {r.get('raw_reply') or '_(none)_'}\n\n"
        f"## verdict\n\n```json\n{json.dumps(verdict, ensure_ascii=False, indent=2)}\n```\n"
    )


def render_report(analysis: dict, records: list[dict],
                  in_path: Path, out: Path) -> str:
    """Render the full report.md text (also writes traces/)."""
    meta = analysis["meta"]
    lines: list[str] = [_frontmatter(meta).rstrip(), ""]
    lines.append("# Decision probe v2 — dose-response analysis")
    lines.append("")
    lines.append(
        f"Input: `{in_path}` · mode **{meta.get('mode', '?')}** · model "
        f"**{meta.get('model', '?')}** · seeds "
        f"{json.dumps(meta.get('seeds') or ([meta['seed']] if meta.get('seed') is not None else []), ensure_ascii=False)} · "
        f"{analysis['n_legs']} legs across {analysis['n_scenarios']} "
        "scenario_ids (everything-but-mood fixed per scenario_id; the mood "
        "dose is the only varied thing)."
    )
    lines.append("")
    lines.append("## Declared primary metrics")
    lines.append("")
    lines.append(
        "1. **Per-scenario P(choice | mood dose) dose slope** — the "
        "proportion of positive choices (reply / initiate / follow) per "
        "dose cell, across the K reps; slope across ordered doses is the "
        "headline dose-response signal."
    )
    lines.append(
        "2. **references_state rate** — the fraction of legs where the "
        "state card entered reasoning, per dose and overall."
    )
    lines.append("")
    lines.append(
        "Uncertainty: n per cell reported (K reps); 95% binomial "
        "**Wilson** CI per cell; observed rep-level spread reported as the "
        "per-rep positive vector. Seeds recorded in the frontmatter. One "
        "lever per channel: the valence channel varies only M; the energy "
        "channel varies only the engineered hour."
    )
    lines.append("")
    lines.append("## Acceptance checks")
    lines.append("")
    lines.append(_render_acceptance(analysis))
    lines.append("")
    lines.append("## Dose-response by scenario")
    lines.append("")
    lines.append(
        "P(positive choice | dose) over K reps. n counts legs with a "
        "classified choice (unclassified legs — choice=None — excluded)."
    )
    lines.append("")
    for sid, cells in analysis["dose_response"].items():
        lines.append(f"### {sid}")
        lines.append("")
        lines.append(_render_cell_table(cells, "dose_id"))
        lines.append("")
    lines.append("## Action level — terminate_event by dose (reply pop-ups)")
    lines.append("")
    lines.append(
        "The reply verdict carries a discretionary action: whether to "
        "terminate the in-progress event (leave rest to go out, end the "
        "event to follow the user's intent). `choice` is often "
        "boundary-dominated on reply pop-ups; `terminate_event` is where "
        "mood shows. Per scenario over K, then pooled across scenarios."
    )
    lines.append("")
    term = analysis.get("terminate_by_dose")
    if term:
        for sid, cells in term["per_scenario"].items():
            if not cells:
                continue
            lines.append(f"### {sid}")
            lines.append("")
            lines.append(
                "| dose | n | terminate | P(terminate) | 95% CI (Wilson) | "
                "spread (reps) |"
            )
            lines.append("|---|---|---|---|---|---|")
            for row in cells:
                lines.append(
                    f"| {row['dose_id']} | {row['n']} | {row['k']} | "
                    f"{_fmt_pct(row['rate'])} | {_fmt_ci(row['ci'])} | "
                    f"`{row['spread']}` |"
                )
            lines.append("")
        lines.append("### Pooled across scenarios")
        lines.append("")
        lines.append("| dose | n | terminate | P(terminate) | 95% CI (Wilson) |")
        lines.append("|---|---|---|---|---|")
        for row in term["pooled"]:
            lines.append(
                f"| {row['dose_id']} | {row['n']} | {row['k']} | "
                f"{_fmt_pct(row['rate'])} | {_fmt_ci(row['ci'])} |"
            )
        lines.append("")
    lines.append("## Per-channel sweeps (one lever per channel)")
    lines.append("")
    lines.append("### Valence sweep — M values over the 0..10 scale")
    lines.append("")
    lines.append("Engineered lever: **M only**; hour/phase held at the "
                 "scenario's natural values.")
    lines.append("")
    for sid, cells in analysis["valence_sweep"].items():
        if not cells:
            continue
        lines.append(f"#### {sid}")
        lines.append("")
        lines.append(_render_cell_table(cells, "value", label="M"))
        lines.append("")
    lines.append("### Energy sweep — engineered hour values")
    lines.append("")
    lines.append("Engineered lever: **hour only**; M held at the scenario's "
                 "natural value.")
    lines.append("")
    for sid, cells in analysis["energy_sweep"].items():
        if not cells:
            continue
        lines.append(f"#### {sid}")
        lines.append("")
        lines.append(_render_cell_table(cells, "value", label="hour"))
        lines.append("")
    lines.append("## references_state by mood dose")
    lines.append("")
    lines.append("Rate over K (pooled across scenarios; n per cell = "
                 "scenarios × K).")
    lines.append("")
    lines.append("| dose | n | references_state | rate | 95% CI (Wilson) |")
    lines.append("|---|---|---|---|---|")
    for row in analysis["references_by_dose"]:
        lines.append(
            f"| {row['dose_id']} | {row['n']} | {row['k']} | "
            f"{_fmt_pct(row['rate'])} | {_fmt_ci(row['ci'])} |"
        )
    lines.append("")
    lines.append("## THE HEADLINE SPLIT — did the state enter deliberation, "
                 "and was it followed?")
    lines.append("")
    lines.append(_render_headline(analysis))
    lines.append("")
    lines.append("## Verbatim traces")
    lines.append("")
    lines.append(
        "Every leg has a full trace file in `traces/` (brief, reasoning, "
        "raw reply, verdict — all verbatim). Short reasoning is additionally "
        "quoted inline below; long reasoning is referenced. Samples at each "
        "mood extreme are quoted in full at the end."
    )
    lines.append("")
    traces_text, _inline = _render_traces(analysis, records, out)
    lines.append(traces_text)
    lines.append("## Sources")
    lines.append("")
    lines.append(
        f"- Classified probe records: `{in_path}` (probe.json after "
        "probe_outcome.classify)"
    )
    lines.append(
        "- Decision store (sidecar): `decision_probe.db` alongside the "
        "input probe.json"
    )
    lines.append(
        "- Per-leg trace files: `traces/` under the output directory "
        "(one md per leg, verbatim brief + reasoning + raw reply + verdict)"
    )
    lines.append(
        "- Analyzer: `experiments/probe_analyze.py` (A4) — pure offline "
        "analysis; `harness/` and `engine/` untouched"
    )
    return "\n".join(lines)


def write_report(analysis: dict, records: list[dict], in_path: Path,
                 out: Path, report_name: str = DEFAULT_REPORT) -> Path:
    """Write report.md (and traces/) into ``out``; returns the report path."""
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / report_name
    report_path.write_text(
        render_report(analysis, records, in_path, out), encoding="utf-8"
    )
    return report_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decision probe v2 — analyze classified probe records "
                    "and write the dose-response report."
    )
    parser.add_argument("--in", dest="in_path", type=Path, default=DEFAULT_IN,
                        help=f"classified probe.json (default: {DEFAULT_IN})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output dir (default: {DEFAULT_OUT})")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help=f"report filename inside --out "
                             f"(default: {DEFAULT_REPORT})")
    args = parser.parse_args(argv)

    if not args.in_path.exists():
        print(f"[probe_analyze] input not found: {args.in_path}", file=sys.stderr)
        return 2

    records, meta = load_records(args.in_path)
    analysis = analyze(records, meta)
    report_path = write_report(analysis, records, args.in_path, args.out,
                               args.report)
    h = analysis["headline"]["pooled"]
    print(
        f"[probe_analyze] {analysis['n_legs']} legs, "
        f"{analysis['n_scenarios']} scenarios -> {report_path}; "
        f"headline: never entered {h['never_entered']['n']}, "
        f"entered+discounted {h['entered_discounted']['n']}, "
        f"entered+followed {h['entered_followed']['n']}, "
        f"unclassified {h['n_unclassified']}; empty reasoning "
        f"{analysis['acceptance']['n_empty_reasoning']}, conflation "
        f"{analysis['acceptance']['n_conflation']}"
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
