"""C8 — Message effects on high-valence days: threshold study (sim).

PROPIEDAD: experimento C8 del plan advisor-orchestration-2026-08-15.md (§C8).
Trabaja SOLO contra el contrato congelado (engine/types.py + sim/run_daily.py,
W2.1). NO toca harness/ ni engine/ — genera 5x28 días de trazas de
valencia/ánimo con el driver día-a-día congelado (semillas 5001-5005),
calcula la distribución de valencia y elige un umbral que seleccione
SOLO días del decil superior con frecuencia esperada <= 1 efecto/semana.

Valencia:
  - primaria (continua): p = DayRecord.p — probabilidad de ánimo del día
    (sigmoid del argumento logit; sin empates ⇒ decil superior bien definido).
  - secundaria (observable, discreta): M/N ∈ {0, 0.1, ..., 1.0}.

Regla elegida: efecto sii p_dia > v90(pooled), v90 = percentil 90 de la
distribución pooled (140 días). Con p continua, esto selecciona EXACTAMENTE
los 14 días de mayor rango (decil superior por rango, verificado en código)
y la frecuencia esperada es 14/140 = 0.7/semana <= 1/semana.

Reproducir (worktree llh-wt-c-effects, branch wip/c-effects):
    /home/vruizes/.hermes/projects/llm-behavioral-harness/.venv/bin/python \
        -m experiments.c8_effects
Escribe results/c8-effects/c8_effects.json (y nada más).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from engine.types import MoodVariant, PersonaParams
from sim.run_daily import run

# ---------------------------------------------------------------------------
# Constantes del experimento (plan §C8: 5 seeds × 28 días)
SEEDS: tuple[int, ...] = (5001, 5002, 5003, 5004, 5005)
DAYS = 28
VARIANT = MoodVariant.DECOUPLED_OFFSETS  # variante de producción (run_async/interactive/daily)
PERSONA = PersonaParams()  # defaults congelados de DESIGN.md

TOP_DECILE_FRAC = 0.10
MAX_EFFECTS_PER_WEEK = 1.0  # criterio del plan
WEEKS_PER_TRACE = DAYS / 7.0

OUT_DIR = Path(__file__).resolve().parent.parent / "results" / "c8-effects"


def generate_traces() -> dict[int, list[dict]]:
    """5x28 trazas con el driver congelado (determinista por semilla)."""
    traces: dict[int, list[dict]] = {}
    for seed in SEEDS:
        result = run(days=DAYS, seed=seed, variant=VARIANT, persona=PERSONA)
        traces[seed] = [
            {
                "t": r.t,
                "M": r.M,
                "MN": r.M / PERSONA.N,
                "p": r.p,
                "arg": r.arg,
                "phase": r.phase_label,
                "seed": seed,
            }
            for r in result.records
        ]
    return traces


def percentile_value(values: np.ndarray, q: float) -> float:
    """Valor v tal que ~q de los datos son <= v (sin interpolación, para
    que el umbral sea un valor realmente observado en la distribución)."""
    return float(np.percentile(values, 100.0 * q))


def main() -> None:
    traces = generate_traces()

    # Pooled (140 días): valencia primaria p y secundaria M/N
    pooled_p = np.concatenate([np.asarray([d["p"] for d in rec]) for rec in traces.values()])
    pooled_mn = np.concatenate([np.asarray([d["MN"] for d in rec]) for rec in traces.values()])
    assert len(pooled_p) == len(SEEDS) * DAYS == 140

    def dist_stats(v: np.ndarray) -> dict:
        return {
            "n": int(v.size),
            "mean": float(v.mean()),
            "sd": float(v.std(ddof=1)),
            "p50": percentile_value(v, 0.50),
            "p75": percentile_value(v, 0.75),
            "p90": percentile_value(v, 0.90),
            "p95": percentile_value(v, 0.95),
            "p99": percentile_value(v, 0.99),
            "min": float(v.min()),
            "max": float(v.max()),
        }

    # Umbral: percentil 90 de la distribución pooled de valencia primaria p
    threshold_p = percentile_value(pooled_p, 0.90)
    # Referencia observable (discreta, con empates): mismo percentil sobre M/N
    threshold_mn = percentile_value(pooled_mn, 0.90)

    # Decil superior por rango (p continua — sin empates prácticos):
    # los TOP_DECILE_FRAC·N días de mayor p, desempate determinista por t/seed.
    all_days: list[dict] = []
    for seed in SEEDS:
        for d in traces[seed]:
            all_days.append({"seed": seed, **d})
    order = sorted(
        range(len(all_days)),
        key=lambda i: (-all_days[i]["p"], all_days[i]["t"], all_days[i]["seed"]),
    )
    n_top = int(round(TOP_DECILE_FRAC * len(all_days)))  # 14
    top_decile_keys = {
        (all_days[i]["seed"], all_days[i]["t"]) for i in order[:n_top]
    }

    # Regla de selección: efecto sii p > umbral
    per_seed: list[dict] = []
    selected_total = 0
    for seed in SEEDS:
        sel = [d for d in traces[seed] if d["p"] > threshold_p]
        sel_keys = {(d["seed"], d["t"]) for d in sel}
        n_sel = len(sel)
        selected_total += n_sel
        # semanas (0-based) en las que cae cada día seleccionado
        weeks = sorted({d["t"] // 7 for d in sel})
        per_seed.append(
            {
                "seed": seed,
                "selected_days": [d["t"] for d in sel],
                "selected_count_28d": n_sel,
                "effects_per_week": n_sel / WEEKS_PER_TRACE,
                "weeks_with_effect": weeks,
                "all_selected_in_top_decile": sel_keys <= top_decile_keys,
                "selected_p_values": [round(d["p"], 6) for d in sel],
                "selected_MN_values": [d["MN"] for d in sel],
            }
        )

    # Comprobaciones del plan §C8(b)
    selected_keys = {
        (d["seed"], d["t"])
        for seed in SEEDS
        for d in traces[seed]
        if d["p"] > threshold_p
    }
    only_top_decile = selected_keys <= top_decile_keys
    exact_top_decile = selected_keys == top_decile_keys
    pooled_freq_per_week = selected_total / (len(SEEDS) * WEEKS_PER_TRACE)
    per_seed_max_28d = max(s["selected_count_28d"] for s in per_seed)
    freq_ok = pooled_freq_per_week <= MAX_EFFECTS_PER_WEEK

    # Suplementario (guardia operativa opcional, NO parte del criterio):
    # tope duro de 4 efectos/28d por seed — descarta los días seleccionados
    # de menor p dentro de la semilla cuando el mes excede el tope.
    CAP_PER_28D = int(MAX_EFFECTS_PER_WEEK * DAYS / 7)  # 4
    capped_counts: list[int] = []
    for seed in SEEDS:
        sel = sorted(
            [d for d in traces[seed] if d["p"] > threshold_p],
            key=lambda d: -d["p"],
        )
        capped_counts.append(min(len(sel), CAP_PER_28D))

    # Referencia M/N: cuántos días quedarían con regla estricta M/N > p90
    mn_strict = int((pooled_mn > threshold_mn).sum())

    payload = {
        "experiment": "C8 — message effects on high-valence days",
        "plan_ref": "plans/advisor-orchestration-2026-08-15.md §C8",
        "engine_contract": {
            "driver": "sim.run_daily.run",
            "variant": VARIANT.value,
            "persona": "PersonaParams() defaults (frozen DESIGN.md)",
            "days_per_seed": DAYS,
            "seeds": list(SEEDS),
            "total_days": len(all_days),
        },
        "valence_primary": "p (DayRecord.p, daily mood probability, continuous)",
        "valence_secondary": "M/N (observable daily mood, discrete 0..1 in 0.1 steps)",
        "distribution_p": dist_stats(pooled_p),
        "distribution_MN": dist_stats(pooled_mn),
        "threshold": {
            "rule": "effect iff day valence p > threshold_p",
            "threshold_p": threshold_p,
            "threshold_p_rank": f"90th percentile of pooled p ({len(all_days)} days)",
            "threshold_MN_reference": threshold_mn,
            "MN_strict_rule_days_selected_140": mn_strict,
            "top_decile_n": n_top,
        },
        "per_seed": per_seed,
        "checks": {
            "only_top_decile_days_selected": bool(only_top_decile),
            "selected_equals_top_decile_exactly": bool(exact_top_decile),
            "expected_freq_per_week": round(pooled_freq_per_week, 4),
            "freq_le_1_per_week": bool(freq_ok),
            "per_seed_max_28d": per_seed_max_28d,
            "criterion_b_pass": bool(only_top_decile and freq_ok),
        },
        "supplementary_cap": {
            "note": "optional hard guard, NOT part of plan criterion: at most "
            f"{CAP_PER_28D} effects per 28d per seed (drop lowest-p extras)",
            "capped_counts_per_seed": capped_counts,
            "capped_total_140d": sum(capped_counts),
            "capped_freq_per_week": round(
                sum(capped_counts) / (len(SEEDS) * WEEKS_PER_TRACE), 4
            ),
        },
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / "c8_effects.json"
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    # Resumen en stdout
    print(f"umbral p90(p) = {threshold_p:.6f}  |  umbral ref p90(M/N) = {threshold_mn:.2f}")
    print(f"días seleccionados totales: {selected_total}/140 = {pooled_freq_per_week:.3f}/semana")
    for s in per_seed:
        print(
            f"  seed {s['seed']}: {s['selected_count_28d']} días "
            f"({s['effects_per_week']:.2f}/semana)  días={s['selected_days']}"
        )
    print(f"solo decil superior: {only_top_decile} | igual al decil exacto: {exact_top_decile}")
    print(f"frecuencia esperada <= 1/semana: {freq_ok} ({pooled_freq_per_week:.4f}) "
          f"| max por seed/28d: {per_seed_max_28d} (agrupación en rachas)")
    print(f"guardia opcional tope 4/28d por seed -> counts {capped_counts} "
          f"({sum(capped_counts)}/140, {sum(capped_counts)/(len(SEEDS)*WEEKS_PER_TRACE):.3f}/semana)")
    print(f"CRITERIO (b) DEL PLAN: {'PASS' if payload['checks']['criterion_b_pass'] else 'FAIL'}")
    print(f"JSON -> {out_json}")


if __name__ == "__main__":
    main()
