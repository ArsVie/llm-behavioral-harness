"""Pre-flight de ablación (Iteración 3, B8 / Gate G2) — la compuerta barata.

Cierra F4 (descubrimiento después del gasto): cinco de siete ablaciones no
ablaron y nadie lo supo hasta después de 4h12m de API real. Este driver corre
TODAS las condiciones de la matriz × todas las semillas congeladas × 3 días
con el cliente FAKE (segundos de costo) y evalúa cada condición contra su
``AblationClaim`` declarado (harness/domain.py, invariante 9). Una condición
cuya claim falla es una ABLACIÓN NULA: bloquea la matriz (exit != 0 / reporte
fuerte) hasta arreglarse o descartarse.

El veredicto es una función del código, no una expectativa hardcodeada: las
claims se evalúan contra los resúmenes reales de las células (hook
``records_summary`` de cvs_common).

Registro de claims: lista plana de ``AblationClaim`` en la sección marcada de
abajo. Aditivo por diseño — el orquestador APPENDEA en G2 las claims
preregistradas de B4 (generation_controls) y B5 (timing) sin reestructurar
nada. Los placeholders de esos canales están marcados y se sustituyen en G2.

Uso:
    python -m experiments.cvs_preflight [--days 3] [--seeds 5001,5002]
                                        [--conditions FULL,NO_LIFE] [--out DIR]

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from harness.domain import AblationClaim
from experiments.cvs_common import records_summary, run_cell
from experiments.cvs_manifest import MATRIX_CONDITIONS, SEEDS
from experiments.validation.hard_invariants import (
    check_hard_invariants,
    failure_messages,
)

#: Días del pre-flight (plan §6-B8: 3 días, costo en segundos).
DEFAULT_DAYS = 3

# --------------------------------------------------------------------------- #
# REGISTRO DE CLAIMS — sección ÚNICA (it3 B8 / G2)
#
# Mecanismo: lista plana de AblationClaim (contrato congelado). Aditivo: el
# orquestador appendea en G2 las claims preregistradas de B4/B5 sin
# reestructurar. Los placeholders de canal B4/B5 están marcados como
# PLACEHOLDER y se sustituyen en G2 por las claims de sus reportes.
# --------------------------------------------------------------------------- #


def _pct_div(cell_v: float, full_v: float) -> float:
    """Divergencia porcentual |cell-full|/full (1.0 si full=0 y cell>0)."""
    if full_v == 0:
        return 0.0 if cell_v == 0 else 1.0
    return abs(cell_v - full_v) / full_v


def _goldfish_seed_ok(summary: dict) -> bool:
    """Goldfish per seed: per-day arc id sets exist, each day's arcs > 0,
    and CONSECUTIVE days share NO arc ids (arc identity dies at midnight).

    The records carry ``arc_progress_by_day`` snapshots (arc ids + progress
    per day), surfaced in the summary as ``life_arc_ids_by_day``; the
    per-day id sets are the identity trace the ablation must destroy.
    """
    by_day = summary.get("life_arc_ids_by_day") or {}
    days = sorted(int(d) for d in by_day)
    if len(days) < 2:
        return False
    sets = [set(by_day[str(d)]) for d in days]
    if any(not s for s in sets):
        return False
    return all(not (sets[i] & sets[i + 1]) for i in range(len(sets) - 1))


def _persistent_seed_ok(summary: dict) -> bool:
    """FULL side of the same mechanism: SOME arc id appears on >= 2 days
    (identity survives midnight; FULL never wipes, so every day's arc ids
    are a superset-ish of the previous day's — overlap is guaranteed)."""
    by_day = summary.get("life_arc_ids_by_day") or {}
    seen: set[str] = set()
    for ids in by_day.values():
        for aid in ids:
            if aid in seen:
                return True
            seen.add(aid)
    return False


def _no_life_goldfish_check(cell: dict, full: dict) -> bool:
    """NO_LIFE must be goldfish AND FULL must persist — the check
    DISCRIMINATES (no tautology): if FULL ever stopped persisting arc ids
    across days, or NO_LIFE ever carried an id over midnight, the claim
    fails and the matrix blocks. Missing per-day data -> False (a null
    ablation flagged loudly, never a silent pass)."""
    per_seed = cell.get("per_seed") or {}
    if not per_seed or not all(_goldfish_seed_ok(s) for s in per_seed.values()):
        return False
    full_seeds = (full.get("per_seed") or {}).values()
    return bool(full_seeds) and all(_persistent_seed_ok(s) for s in full_seeds)


CLAIMS: list[AblationClaim] = [
    AblationClaim(
        condition="NO_LIFE",
        channel="life_state",
        assertion=(
            "life_state channel ablated (goldfish): life-arc IDENTITY does "
            "not survive midnight — every seed shows non-empty per-day arc "
            "id sets with consecutive days DISJOINT (fresh arcs, fresh "
            "progress each day), while FULL persists arc ids across days"
        ),
        check=_no_life_goldfish_check,
    ),
    AblationClaim(
        condition="STRUCTURED_NO_STATE",
        channel="timing",
        assertion=(
            "PLACEHOLDER (G2: B5's preregistered claim replaces this): timing "
            "channel ablated: n_proactive or fired schedule events differ "
            "from FULL by >= 15% (aggregated over seeds)"
        ),
        check=lambda cell, full: (
            _pct_div(cell["n_proactive"], full["n_proactive"]) >= 0.15
            or _pct_div(cell["n_fired_schedule"], full["n_fired_schedule"]) >= 0.15
        ),
    ),
    AblationClaim(
        condition="NO_ACTUATORS",
        channel="generation_controls",
        assertion=(
            "PLACEHOLDER (G2: B4's preregistered claim replaces this): "
            "generation_controls channel ablated: mean assistant reply length "
            "differs from FULL by >= 15%"
        ),
        check=lambda cell, full: (
            _pct_div(cell["mean_reply_len"], full["mean_reply_len"]) >= 0.15
        ),
    ),
    AblationClaim(
        condition="NO_TIMING_FEEDBACK",
        channel="timing",
        assertion=(
            "POSITIVE CONTROL: timing channel must differ from FULL: "
            "n_proactive or fired schedule events differ by >= 15% "
            "(aggregated over seeds)"
        ),
        check=lambda cell, full: (
            _pct_div(cell["n_proactive"], full["n_proactive"]) >= 0.15
            or _pct_div(cell["n_fired_schedule"], full["n_fired_schedule"]) >= 0.15
        ),
    ),
    AblationClaim(
        condition="RAW_HISTORY",
        channel="memory_store",
        assertion=(
            "memory_store channel ablated (per B6 lane routing): memory lane "
            "identity differs from FULL (raw_history raw-dialogue lane vs "
            "structured lane)"
        ),
        check=lambda cell, full: cell["memory_lane"] != full["memory_lane"],
    ),
    AblationClaim(
        condition="SIMPLE_RAG",
        channel="memory_store",
        assertion=(
            "memory_store channel ablated (per B6 lane routing): memory lane "
            "identity differs from FULL (simple_rag lexical top-k lane vs "
            "structured policy lane)"
        ),
        check=lambda cell, full: cell["memory_lane"] != full["memory_lane"],
    ),
]

# --------------------------------------------------------------------------- #
# Agregación y evaluación
# --------------------------------------------------------------------------- #


def _merge_controls_stats(summaries: Sequence[dict]) -> dict:
    """Funde los ``controls_stats`` de las células (semillas) en el de la
    condición.

    ``n`` suma; ``min``/``max`` toman los extremos; ``mean`` es media
    ponderada por ``n``; ``varied`` = OR sobre las semillas (si algún run
    varió el control, la condición lo varía). Los controles textuales
    (``closing_guidance``) conservan ``min``/``max``/``mean`` = None.
    """
    merged: dict[str, dict] = {}
    for s in summaries:
        for name, st in (s.get("controls_stats") or {}).items():
            m = merged.setdefault(name, {
                "n": 0, "min": None, "max": None, "mean": None,
                "varied": False,
            })
            m["n"] += int(st["n"])
            m["varied"] = m["varied"] or bool(st["varied"])
            if st["min"] is not None:
                m["min"] = st["min"] if m["min"] is None else min(m["min"], st["min"])
                m["max"] = st["max"] if m["max"] is None else max(m["max"], st["max"])
    for name, m in merged.items():
        num = 0.0
        den = 0
        for s in summaries:
            st = (s.get("controls_stats") or {}).get(name)
            if st and st["mean"] is not None:
                num += st["mean"] * st["n"]
                den += st["n"]
        if den:
            m["mean"] = round(num / den, 6)
    return merged


def _aggregate(summaries: Sequence[dict]) -> dict:
    """Agrega los resúmenes por célula (semillas) en el resumen por condición.

    Sumas para conteos, media ponderada para longitudes de réplica,
    identidad de lane = primer valor no None. Los campos de conversación se
    agregan SOLO si todas las células los tienen disponibles (degradación
    con gracia: si el seam de B2 no existe, quedan None).
    """
    agg: dict = {
        "condition": summaries[0]["condition"],
        "seed": summaries[0]["seed"],
        "days": summaries[0]["days"],
        "seeds": [s["seed"] for s in summaries],
        "n_messages": sum(s["n_messages"] for s in summaries),
        "n_proactive": sum(s["n_proactive"] for s in summaries),
        "n_reactive": sum(s["n_reactive"] for s in summaries),
        "n_assistant_turns": sum(s["n_assistant_turns"] for s in summaries),
        "n_blank_assistant_turns": sum(
            s["n_blank_assistant_turns"] for s in summaries
        ),
        "n_life_arcs": sum(s["n_life_arcs"] for s in summaries),
        "n_agenda_items": sum(s["n_agenda_items"] for s in summaries),
        "n_episodes": sum(s["n_episodes"] for s in summaries),
        "n_fired_schedule": sum(s["n_fired_schedule"] for s in summaries),
        "memory_lane": next(
            (s["memory_lane"] for s in summaries if s["memory_lane"]), None
        ),
        "controls_stats": _merge_controls_stats(summaries),
        # Identity trace (NO_LIFE goldfish claim): per-day union of the
        # seeds' arc id sets — NO_LIFE shows disjoint consecutive days,
        # FULL shows persistent ids (visible in the report JSON).
        "life_arc_ids_by_day": {
            d: sorted(ids)
            for d, ids in _union_arc_ids_by_day(summaries).items()
        },
        "per_seed": {str(s["seed"]): s for s in summaries},
    }
    total_len = sum(s["n_assistant_turns"] for s in summaries)
    if total_len:
        agg["mean_reply_len"] = round(
            sum(s["mean_reply_len"] * s["n_assistant_turns"] for s in summaries)
            / total_len,
            2,
        )
        var = sum(
            (s["std_reply_len"] ** 2) * (s["n_assistant_turns"] - 1)
            for s in summaries
            if s["n_assistant_turns"] > 1
        )
        n = sum(1 for s in summaries if s["n_assistant_turns"] > 1)
        agg["std_reply_len"] = round(
            (var / n) ** 0.5 if n else 0.0, 2
        )
    else:
        agg["mean_reply_len"] = 0.0
        agg["std_reply_len"] = 0.0
    if all(s.get("conversations_available") for s in summaries):
        agg["n_conversations"] = sum(
            s["n_conversations"] or 0 for s in summaries
        )
        turns = sum(
            (s["mean_turns_per_conversation"] or 0.0) * (s["n_conversations"] or 0)
            for s in summaries
        )
        agg["mean_turns_per_conversation"] = round(
            turns / agg["n_conversations"], 2
        ) if agg["n_conversations"] else None
        agg["conversations_available"] = True
    else:
        agg["n_conversations"] = None
        agg["mean_turns_per_conversation"] = None
        agg["conversations_available"] = False
    return agg


def _union_arc_ids_by_day(summaries: Sequence[dict]) -> dict[str, set[str]]:
    """Per-day union of the seeds' arc id sets (identity trace)."""
    by_day: dict[str, set[str]] = {}
    for s in summaries:
        for d, aids in (s.get("life_arc_ids_by_day") or {}).items():
            by_day.setdefault(d, set()).update(aids)
    return by_day


def _summary_diff(a: dict, b: dict) -> list[str]:
    """Diferencias entre dos resúmenes agregados (chequeo de determinismo).

    Compara las claves numéricas de resumen (excluye los detalles por
    semilla y los reportes de validadores); devuelve las diferencias como
    mensajes legibles. Vacío = resúmenes idénticos.
    """
    skip = {"per_seed", "validator_report", "validator_failures",
            "condition", "seed", "seeds", "memory_lane",
            "conversations_available"}
    diffs: list[str] = []
    for key in sorted(set(a) | set(b)):
        if key in skip:
            continue
        va, vb = a.get(key), b.get(key)
        if va != vb:
            diffs.append(f"{key}: {va!r} vs {vb!r}")
    return diffs


def evaluate_claims(
    condition: str,
    cell: dict,
    full: dict,
    claims: Sequence[AblationClaim],
) -> list[dict]:
    """Evalúa las claims de una condición contra FULL; devuelve los veredictos."""
    verdicts: list[dict] = []
    for claim in claims:
        if claim.condition != condition:
            continue
        try:
            passed = bool(claim.check(cell, full))
        except (KeyError, TypeError) as exc:
            passed = False
            verdicts.append({
                "condition": claim.condition,
                "channel": claim.channel,
                "assertion": claim.assertion,
                "passed": False,
                "error": f"claim check raised: {exc}",
            })
            continue
        verdicts.append({
            "condition": claim.condition,
            "channel": claim.channel,
            "assertion": claim.assertion,
            "passed": passed,
        })
    return verdicts


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def run_preflight(
    *,
    days: int = DEFAULT_DAYS,
    seeds: Sequence[int] = SEEDS,
    conditions: Sequence[str] = MATRIX_CONDITIONS,
    claims: Sequence[AblationClaim] | None = None,
    out_dir: Path | str | None = None,
    determinism_check: bool = True,
) -> dict:
    """Corre la matriz completa (fake) y evalúa las claims (Gate G2).

    Devuelve el reporte: resúmenes por condición, veredictos por claim,
    ablaciones nulas y ``ok`` (False si alguna claim falla o el chequeo de
    determinismo falla). El veredicto es función del código actual: las
    claims se evalúan contra los resúmenes reales de las células, nunca
    contra expectativas hardcodeadas.

    ``determinism_check`` (por defecto True): FULL y el control positivo
    NO_TIMING_FEEDBACK se corren DOS veces y se comparan los resúmenes
    agregados. El runner de células del harness (``_run_segment``,
    cvs_common) entrega los feeds del usuario con polling de reloj real
    (TIME_SCALE_S_PER_VH=0.0004) y bajo contention del event loop puede
    omitir feeds o expirar eventos de cola — una célula no reproducible
    invalida el veredicto de la compuerta. Si las dos pasadas divergen, el
    pre-flight lo reporta FUERTE y bloquea (``deterministic=False``).
    """
    claims = list(CLAIMS if claims is None else claims)
    conditions = list(conditions)
    out_dir = Path(out_dir) if out_dir else Path(
        tempfile.mkdtemp(prefix="cvs_preflight_")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    run_conditions = conditions
    if "FULL" not in run_conditions:
        run_conditions = ["FULL", *run_conditions]

    def _run_condition(condition: str, tag: str = "") -> dict:
        summaries: list[dict] = []
        validator_report: dict = {}
        for seed in seeds:
            records = run_cell(
                condition, int(seed), out_dir, days=days,
                fake=True, perturb=True,
            )
            store = _open_store(records["db"])
            summaries.append(records_summary(store, records))
            validator_report[str(seed)] = check_hard_invariants(store)
            store.close()
        agg = _aggregate(summaries)
        agg["validator_report"] = validator_report
        agg["validator_failures"] = _validator_failures(validator_report)
        return agg

    per_condition: dict[str, dict] = {}
    for condition in run_conditions:
        per_condition[condition] = _run_condition(condition)

    deterministic = True
    determinism_failures: list[str] = []
    if determinism_check:
        # La referencia (FULL) Y el control positivo (NO_TIMING_FEEDBACK)
        # deben ser reproducibles: la carrera de feeds de ``_run_segment``
        # puede golpear a UNA condición sin tocar FULL — un control positivo
        # no reproducible invalida el veredicto de la compuerta igual que
        # una referencia no reproducible.
        for cond in ("FULL", "NO_TIMING_FEEDBACK"):
            if cond not in run_conditions:
                continue
            again = _run_condition(cond, tag="determinism")
            diff = _summary_diff(per_condition[cond], again)
            if diff:
                deterministic = False
                determinism_failures.extend(
                    f"{cond}: {d}" for d in diff
                )

    full = per_condition["FULL"]
    verdicts: list[dict] = []
    for condition in run_conditions:
        cell = per_condition[condition]
        # FULL se compara contra sí mismo; el resto contra FULL. Toda claim
        # del registro se evalúa (mecanismo aditivo general).
        reference = full if condition != "FULL" else cell
        verdicts.extend(evaluate_claims(condition, cell, reference, claims))

    null_ablations = sorted({
        v["condition"] for v in verdicts if not v["passed"] and "error" not in v
    })
    errors = [v for v in verdicts if "error" in v]
    skipped = [
        c.condition for c in claims
        if c.condition not in run_conditions and c.condition != "FULL"
    ]
    report = {
        "ok": (not null_ablations and not errors and deterministic),
        "deterministic": deterministic,
        "determinism_failures": determinism_failures,
        "days": days,
        "seeds": [int(s) for s in seeds],
        "conditions": run_conditions,
        "full": {k: v for k, v in full.items()
                 if k not in ("per_seed", "validator_report")},
        "per_condition": {
            cond: {
                k: v for k, v in per_condition[cond].items()
                if k not in ("per_seed", "validator_report")
            }
            for cond in per_condition
        },
        "verdicts": verdicts,
        "null_ablations": null_ablations,
        "claim_errors": errors,
        "skipped_claims": sorted(set(skipped)),
        "out_dir": str(out_dir),
    }
    return report


def _open_store(db_path: str):
    from harness.store import SQLiteStore

    return SQLiteStore(db_path)


def _validator_failures(validator_report: dict) -> list[str]:
    failures: list[str] = []
    for seed, result in validator_report.items():
        for msg in failure_messages(result):
            failures.append(f"seed {seed}: {msg}")
    return failures


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _fmt_num(value) -> str:
    """Formatea un número para la tabla (None -> '-')."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _fmt_table(report: dict) -> str:
    lines = [
        "=" * 78,
        f"cvs_preflight — fake client, {report['days']} days, "
        f"seeds={report['seeds']}",
        "=" * 78,
    ]
    for v in report["verdicts"]:
        mark = "PASS" if v["passed"] else "FAIL"
        if "error" in v:
            lines.append(f"  [{mark}] {v['condition']} ({v['channel']}): {v['error']}")
        else:
            lines.append(
                f"  [{mark}] {v['condition']} ({v['channel']}): {v['assertion']}"
            )
    if report["null_ablations"]:
        lines.append("")
        lines.append(
            f"NULL ABLATIONS ({len(report['null_ablations'])}): "
            f"{', '.join(report['null_ablations'])} — MATRIX BLOCKED until "
            "fixed or dropped"
        )
    if not report.get("deterministic", True):
        lines.append("")
        lines.append("  !!! NONDETERMINISTIC CELLS — reference (FULL) or "
                     "positive control (NO_TIMING_FEEDBACK) differs between "
                     "two passes:")
        for diff in report.get("determinism_failures", []):
            lines.append(f"      {diff}")
    for cond, agg in report["per_condition"].items():
        vf = agg.get("validator_failures", [])
        if vf:
            lines.append("")
            lines.append(f"  !!! hard invariants failed for {cond}:")
            for msg in vf:
                lines.append(f"      {msg}")
    lines.append("")
    lines.append("controls_stats per condition (n/min/max/mean/varied):")
    for cond, agg in report["per_condition"].items():
        cs = agg.get("controls_stats") or {}
        if not cs:
            lines.append(f"  {cond:26s} (no controls recorded)")
            continue
        for name, st in sorted(cs.items()):
            lines.append(
                f"  {cond:26s} {name:18s} n={st['n']:3d} "
                f"min={_fmt_num(st['min']):>10s} "
                f"max={_fmt_num(st['max']):>10s} "
                f"mean={_fmt_num(st['mean']):>10s} varied={st['varied']}"
            )
    if report["skipped_claims"]:
        lines.append("")
        lines.append(
            "  skipped claims (condition not in run set): "
            f"{', '.join(report['skipped_claims'])}"
        )
    if report["claim_errors"]:
        lines.append("")
        lines.append("  claim evaluation errors present — treating as blocking")
    lines.append("")
    lines.append(f"verdict: {'GATE OPEN' if report['ok'] else 'GATE BLOCKED'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ablation pre-flight on the fake client (it3 B8 / G2)."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="virtual days per cell (default 3)")
    parser.add_argument("--seeds", type=str, default=",".join(map(str, SEEDS)),
                        help="comma-separated seeds (default all frozen)")
    parser.add_argument("--conditions", type=str, default=",".join(MATRIX_CONDITIONS),
                        help="comma-separated conditions (default all matrix)")
    parser.add_argument("--out", type=str, default=None,
                        help="scratch dir for cell DBs (default tempdir)")
    args = parser.parse_args(argv)

    seeds = tuple(int(s) for s in args.seeds.split(",") if s.strip())
    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    report = run_preflight(
        days=args.days, seeds=seeds, conditions=conditions,
        out_dir=Path(args.out) if args.out else None,
    )
    print(_fmt_table(report))
    out_dir = Path(report["out_dir"])
    (out_dir / "preflight_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport: {out_dir / 'preflight_report.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
