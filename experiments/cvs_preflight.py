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

Registro de claims: lista plana de ``AblationClaim`` en la sección marcada
de abajo. Aditivo por diseño. En G2 se sustituyeron los placeholders de los
canales B4 (generation_controls) y B5 (timing) por las claims preregistradas
que esos workstreams comprometieron (B4: set plano 600/5.0/0.5/banda media vs
FULL no degenerado + margen de amplitud 3.0x en delay; B5: la claim de
manifiesto ``harness.scheduler.structured_no_state_claim`` — divergencia de
conteo >= 15% y de gaps >= 10%, que se prueba en la matriz REAL en G5) y las
claims de memoria (RAW_HISTORY/SIMPLE_RAG) ahora verifican CONDUCTA
(evidencia recuperada no nula + conjunto recuperado distinto del de FULL),
no la identidad configurada de la lane.

SPLIT de compuerta (G2, corrección del usuario): el pre-flight responde
"¿está dormido el canal?" con la barra baja GATE_MIN_DIVERGENCE; el umbral
de hipótesis preregistrado (COUNT_DIVERGENCE_MIN = 0.15) NO se evalúa aquí —
se prueba en la matriz real. Las claims declaran ``min_days`` (el horizonte
en que su mecanismo puede haber actuado); por debajo se reportan NOT
EVALUABLE, nunca FAIL. Horizonte por defecto: 30 días (el confirmatorio de
la matriz); ``--smoke`` corre la leg estructural rápida de 3 días.

Uso:
    python -m experiments.cvs_preflight [--days 30] [--seeds 5001,5002]
                                        [--conditions FULL,NO_LIFE] [--out DIR]
                                        [--smoke]

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

# Pre-flight days: 30 (matrix horizon); structural smoke uses 3 via smoke=True.
DEFAULT_DAYS = 30

# Gate threshold: low nullity-detector bar; the 0.15 hypothesis threshold
# (scheduler.py) is tested on the real matrix.
GATE_MIN_DIVERGENCE = 0.05

# Claim registry: flat list of AblationClaim entries.


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


def _timing_measure(cell: dict, full: dict) -> dict:
    """Márgenes medidos del canal de timing (para el reporte y la
    reconciliación del manifiesto en G4): qué pata divergió y cuánto."""
    n_cell = int(cell.get("n_proactive") or 0)
    n_full = int(full.get("n_proactive") or 0)
    f_cell = int(cell.get("n_fired_schedule") or 0)
    f_full = int(full.get("n_fired_schedule") or 0)
    t_cell = sorted(float(t) for t in (cell.get("proactive_times") or ()))
    t_full = sorted(float(t) for t in (full.get("proactive_times") or ()))
    gap_div = 0.0
    if len(t_cell) >= 3 and len(t_full) >= 3:
        import numpy as np
        g_c = float(np.diff(np.asarray(t_cell)).mean())
        g_f = float(np.diff(np.asarray(t_full)).mean())
        if g_f > 0.0:
            gap_div = abs(g_c - g_f) / g_f
    return {
        "count_div": round(_pct_div(n_cell, n_full), 4) if n_full else None,
        "fired_div": round(_pct_div(f_cell, f_full), 4) if f_full else None,
        "gap_div": round(gap_div, 4),
        "times_identical": bool(t_cell) and t_cell == t_full,
    }


def _timing_channel_gate_check(cell: dict, full: dict) -> bool:
    """COMPUERTA del canal de timing (STRUCTURED_NO_STATE + control
    positivo NO_TIMING_FEEDBACK): el canal NO está dormido.

    Detector de nulidad con barra baja (GATE_MIN_DIVERGENCE): pasa si
    cualquiera de las patas muestra divergencia material — conteo de
    proactivos, eventos de agenda disparados, o artefactos realizados no
    idénticos (las horas proactivas difieren). Los márgenes medidos se
    reportan vía ``_timing_measure`` (reconciliación del manifiesto en
    G4). Esto NO es el umbral de hipótesis: el margen preregistrado
    (COUNT_DIVERGENCE_MIN = 0.15) se prueba en la matriz REAL en G5.
    """
    m = _timing_measure(cell, full)
    return bool(
        (m["count_div"] or 0.0) >= GATE_MIN_DIVERGENCE
        or (m["fired_div"] or 0.0) >= GATE_MIN_DIVERGENCE
        or not m["times_identical"]
    )


def _b4_no_actuators_check(cell: dict, full: dict) -> bool:
    """Claim preregistrada de B4 (merge f48683d) para NO_ACTUATORS.

    Objetivo comprometido (reporte B4 §FROZEN TARGET RANGES, manifest-ready):
    el mapeo actuado ampliado hace que FULL realice controles NO degenerados
    (max_tokens y response_delay_s varían a lo largo de la banda congelada),
    mientras que la célula ablacionada fija el set plano 600 / 5.0 s / 0.5 /
    banda media (``_flat_controls``: todos los controles con varied=False).
    Margen de amplitud preregistrado (``ab_margins.response_delay_s``: low
    >= 3.0x high): el delay máximo realizado de FULL >= 3.0x el delay plano
    de la célula.
    """
    cc = cell.get("controls_stats") or {}
    fc = full.get("controls_stats") or {}
    if not cc or not fc:
        return False
    c_flat = (
        cc.get("max_tokens", {}).get("varied") is False
        and cc.get("max_tokens", {}).get("min") == 600.0
        and cc.get("response_delay_s", {}).get("varied") is False
        and cc.get("response_delay_s", {}).get("min") == 5.0
        and cc.get("closing_tendency", {}).get("varied") is False
        and cc.get("closing_tendency", {}).get("min") == 0.5
        and cc.get("closing_guidance", {}).get("varied") is False
    )
    f_varied = (
        fc.get("max_tokens", {}).get("varied") is True
        and fc.get("response_delay_s", {}).get("varied") is True
    )
    flat_delay = cc.get("response_delay_s", {}).get("min")
    full_delay_max = fc.get("response_delay_s", {}).get("max")
    delay_margin = (
        flat_delay is not None and full_delay_max is not None
        and full_delay_max >= 3.0 * flat_delay
    )
    return bool(c_flat and f_varied and delay_margin)


def _memory_behavioral_check(cell: dict, full: dict) -> bool:
    """Claim conductual de memoria (G2) para lanes de episodios (SIMPLE_RAG).

    La lane debe haber RECUPERADO evidencia no nula (``n_retrieved > 0``
    sobre las sondas de cadena enrutadas por lane) Y su conjunto recuperado
    (ids de episodios que la lane devolvió de verdad) debe DIFERIR del de
    FULL. Una lane cableada a nada devuelve conjunto vacío y falla la pata
    de no-nulidad — el modo de fallo de it2 (SIMPLE_RAG con store poblado,
    recuperación idéntica a FULL, AnyEvidence=0.0).
    """
    ce = cell.get("memory_evidence") or {}
    fe = full.get("memory_evidence") or {}
    if not ce or not fe:
        return False
    if int(ce.get("n_retrieved") or 0) <= 0:
        return False
    cell_ids = set(ce.get("retrieved_ids") or ())
    full_ids = set(fe.get("retrieved_ids") or ())
    return cell_ids != full_ids


def _raw_history_behavioral_check(cell: dict, full: dict) -> bool:
    """Claim conductual de memoria (G2) para RAW_HISTORY.

    La lane debe haber RECUPERADO diálogo crudo no nulo (``context_turns >
    0``: la ventana L1 con turnos que el ensamblador recibe de verdad) y su
    recuperación debe diferir de la de FULL (ventana cruda sin ids de
    episodio vs el conjunto de episodios recuperados por la lane
    estructurada).
    """
    ce = cell.get("memory_evidence") or {}
    fe = full.get("memory_evidence") or {}
    if not ce or not fe:
        return False
    if int(ce.get("context_turns") or 0) <= 0:
        return False
    cell_ids = set(ce.get("retrieved_ids") or ())
    full_ids = set(fe.get("retrieved_ids") or ())
    return cell_ids != full_ids or ce.get("probe_lane") != fe.get("probe_lane")


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
        min_days=2,  # identity discontinuity needs >= 2 days
    ),
    # Timing-channel check: only nullity is detected here.
    AblationClaim(
        condition="STRUCTURED_NO_STATE",
        channel="timing",
        assertion=(
            "GATE (canal no dormido): n_proactive, eventos disparados o "
            "horas proactivas divergen de FULL por >= 5% (o artefactos "
            "realizados no idénticos) — el margen de efecto preregistrado "
            "0.15 se prueba en la matriz real"
        ),
        check=_timing_channel_gate_check,
        measure=_timing_measure,
        min_days=4,  # score feedback cannot land before day 2-3
    ),
    AblationClaim(
        condition="NO_ACTUATORS",
        channel="generation_controls",
        assertion=(
            "generation_controls channel ablated (B4 preregistered, merge "
            "f48683d): the cell's realized actuator controls are the pinned "
            "flat set (max_tokens 600, response_delay_s 5.0 s, "
            "closing_tendency 0.5, single closing_guidance — every control "
            "varied=False) while FULL's realized controls are "
            "non-degenerate (max_tokens and response_delay_s varied=True) "
            "and FULL's realized delay max >= 3.0x the flat delay (B4 "
            "ab_margins.response_delay_s: low >= 3.0x high)"
        ),
        check=_b4_no_actuators_check,
    ),
    AblationClaim(
        condition="NO_TIMING_FEEDBACK",
        channel="timing",
        assertion=(
            "POSITIVE CONTROL (gate, canal no dormido): el canal de timing "
            "produce artefactos realizados divergentes de FULL (n_proactive "
            "o eventos disparados por >= 5%, u horas proactivas no "
            "idénticas) — medido en el horizonte confirmatorio"
        ),
        check=_timing_channel_gate_check,
        measure=_timing_measure,
        min_days=4,  # score feedback cannot land before day 2-3
    ),
    AblationClaim(
        condition="RAW_HISTORY",
        channel="memory_store",
        assertion=(
            "memory_store channel ablated (behavioral, G2): the lane "
            "RETURNED non-zero raw-dialogue evidence (context_turns > 0 "
            "across lane-routed chain probes) and its retrieval differs "
            "from FULL's (raw window with no episode ids vs FULL's "
            "non-empty retrieved-episode set)"
        ),
        check=_raw_history_behavioral_check,
    ),
    AblationClaim(
        condition="SIMPLE_RAG",
        channel="memory_store",
        assertion=(
            "memory_store channel ablated (behavioral, G2): the lane "
            "RETURNED non-zero retrieved evidence (n_retrieved > 0 across "
            "lane-routed chain probes) AND its retrieved-episode set "
            "DIFFERS from FULL's (set-level comparison of what the lane "
            "actually retrieved — not lane identity)"
        ),
        check=_memory_behavioral_check,
        min_days=10,  # the store crosses the retrieval surface (limit=8) between days 5 and 10
    ),
]

# Aggregation and evaluation


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
        # Identity trace: per-day union of the seeds' arc id sets.
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
    # Pooled proactive times and merged retrieval evidence.
    agg["proactive_times"] = sorted(
        t for s in summaries for t in (s.get("proactive_times") or ())
    )
    agg["memory_evidence"] = _merge_memory_evidence(summaries)
    return agg


def _union_arc_ids_by_day(summaries: Sequence[dict]) -> dict[str, set[str]]:
    """Per-day union of the seeds' arc id sets (identity trace)."""
    by_day: dict[str, set[str]] = {}
    for s in summaries:
        for d, aids in (s.get("life_arc_ids_by_day") or {}).items():
            by_day.setdefault(d, set()).update(aids)
    return by_day


def _merge_memory_evidence(summaries: Sequence[dict]) -> dict:
    """Funde la ``memory_evidence`` de las células (semillas) en la de la
    condición.

    ``retrieved_ids`` es la unión ordenada (los ids viven por store de
    semilla); ``context_turns`` suma; ``AnyEvidence``/``M3_recall`` toman
    el máximo (basta que una semilla recupere/cubra para que la condición
    lo haga). Degradación con gracia: células sin la pierna contribuyen
    ceros.
    """
    merged: dict = {
        "probe_lane": None,
        "n_retrieved": 0,
        "retrieved_ids": [],
        "context_turns": 0,
        "AnyEvidence": 0.0,
        "M3_recall": 0.0,
    }
    for s in summaries:
        ev = s.get("memory_evidence") or {}
        merged["probe_lane"] = merged["probe_lane"] or ev.get("probe_lane")
        merged["retrieved_ids"] = sorted(
            set(merged["retrieved_ids"]) | set(ev.get("retrieved_ids") or ())
        )
        merged["context_turns"] += int(ev.get("context_turns") or 0)
        merged["AnyEvidence"] = max(
            merged["AnyEvidence"], float(ev.get("AnyEvidence") or 0.0)
        )
        merged["M3_recall"] = max(
            merged["M3_recall"], float(ev.get("M3_recall") or 0.0)
        )
    merged["n_retrieved"] = len(merged["retrieved_ids"])
    return merged


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
    *,
    days: int,
) -> list[dict]:
    """Evalúa las claims de una condición contra FULL; devuelve los veredictos.

    ``days`` es el horizonte del run: una claim cuyo mecanismo no ha podido
    actuar aún (``days < claim.min_days``) se reporta como NOT EVALUABLE
    (``passed=None``) — NUNCA como FAIL. Reportar un efecto antes de que su
    causa pueda existir es un artefacto de horizonte, no una ablación nula.
    """
    verdicts: list[dict] = []
    for claim in claims:
        if claim.condition != condition:
            continue
        if days < claim.min_days:
            verdicts.append({
                "condition": claim.condition,
                "channel": claim.channel,
                "assertion": claim.assertion,
                "passed": None,
                "status": "not_evaluable",
                "reason": (
                    f"horizon {days}d < min_days {claim.min_days}d — the "
                    "ablated mechanism cannot have acted yet"
                ),
            })
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
        verdict: dict = {
            "condition": claim.condition,
            "channel": claim.channel,
            "assertion": claim.assertion,
            "passed": passed,
        }
        if claim.measure is not None:
            try:
                verdict["measured"] = claim.measure(cell, full)
            except (KeyError, TypeError):
                pass
        verdicts.append(verdict)
    return verdicts


# Driver


def run_preflight(
    *,
    days: int = DEFAULT_DAYS,
    seeds: Sequence[int] = SEEDS,
    conditions: Sequence[str] = MATRIX_CONDITIONS,
    claims: Sequence[AblationClaim] | None = None,
    out_dir: Path | str | None = None,
    determinism_check: bool = True,
    smoke: bool = False,
) -> dict:
    """Corre la matriz completa (fake) y evalúa las claims (Gate G2).

    Horizonte por defecto: 30 días (el confirmatorio de la matriz). Las
    claims declaran ``min_days`` — por debajo se reportan NOT EVALUABLE,
    nunca FAIL (un efecto no puede existir antes de que su mecanismo haya
    actuado).

    ``smoke=True``: leg estructural rápida de 3 días, SIN evaluación de
    claims — solo construcción de condiciones, invariantes duras,
    determinismo y ceros de turnos en blanco. Para iteración rápida, no
    para la compuerta.

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
    if smoke:
        # Fast structural leg: 3 days, no claims.
        days = 3
        claims = []
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
        # FULL and the NO_TIMING_FEEDBACK positive control must both be reproducible.
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
        # FULL compares against itself; the rest against FULL.
        reference = full if condition != "FULL" else cell
        verdicts.extend(evaluate_claims(
            condition, cell, reference, claims, days=days,
        ))

    # Only passed is False marks a null ablation; not-evaluable claims do not.
    null_ablations = sorted({
        v["condition"] for v in verdicts if v["passed"] is False and "error" not in v
    })
    not_evaluable = [
        v for v in verdicts if v.get("status") == "not_evaluable"
    ]
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
        "not_evaluable": not_evaluable,
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


# CLI


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
        if v["passed"] is None:
            lines.append(
                f"  [NE ] {v['condition']} ({v['channel']}): {v['reason']}"
            )
            continue
        mark = "PASS" if v["passed"] else "FAIL"
        if "error" in v:
            lines.append(f"  [{mark}] {v['condition']} ({v['channel']}): {v['error']}")
        else:
            lines.append(
                f"  [{mark}] {v['condition']} ({v['channel']}): {v['assertion']}"
            )
            if v.get("measured"):
                m = v["measured"]
                lines.append(
                    f"      measured: count_div={m.get('count_div')} "
                    f"fired_div={m.get('fired_div')} "
                    f"gap_div={m.get('gap_div')} "
                    f"times_identical={m.get('times_identical')}"
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
                        help="virtual days per cell (default 30 — confirmatory horizon)")
    parser.add_argument("--seeds", type=str, default=",".join(map(str, SEEDS)),
                        help="comma-separated seeds (default all frozen)")
    parser.add_argument("--conditions", type=str, default=",".join(MATRIX_CONDITIONS),
                        help="comma-separated conditions (default all matrix)")
    parser.add_argument("--out", type=str, default=None,
                        help="scratch dir for cell DBs (default tempdir)")
    parser.add_argument("--smoke", action="store_true",
                        help="fast 3-day structural smoke only (no claim evaluation)")
    args = parser.parse_args(argv)

    seeds = tuple(int(s) for s in args.seeds.split(",") if s.strip())
    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    report = run_preflight(
        days=args.days, seeds=seeds, conditions=conditions,
        out_dir=Path(args.out) if args.out else None,
        smoke=args.smoke,
    )
    print(_fmt_table(report))
    out_dir = Path(report["out_dir"])
    (out_dir / "preflight_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport: {out_dir / 'preflight_report.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
