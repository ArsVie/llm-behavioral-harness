"""W3.2 — Comparativa de variantes de MoodVariant (Ola 3).

PROPIEDAD: tarea W3.2 (este archivo + carpeta results/w32-variantes/). Corre
las 3 variantes de engine.types.MoodVariant (ORIGINAL, DECOUPLED,
DECOUPLED_OFFSETS) con las mismas semillas y PersonaParams por defecto, y
documenta cuantitativamente las diferencias estructurales esperadas
(research/05-reevaluacion-diseno.md §2.1-2.2):

  1. Acoplamiento media-ganancia del ORIGINAL: en ORIGINAL la ganancia g(t)
     multiplica también (logit λ + μ), así que el ciclo desplaza el NIVEL de
     M además de su varianza. En DECOUPLED (B=0, g solo sobre μ+η) ese
     desplazamiento de nivel debe ser mucho menor. Métrica: Δmedia =
     mean(M | g alto) − mean(M | g bajo) (cuartil superior vs inferior de g)
     y correlación de Pearson corr(g, M).
  2. Autocorrelación con/sin η: ORIGINAL no tiene término η (arg no depende
     de η) así que su autocorr lag-1 de M debe acercarse al piso que da μ
     solamente; DECOUPLED y DECOUPLED_OFFSETS sí tienen η y deberían mostrar
     autocorrelación mayor.
  3. Efecto de B (offset de media): DECOUPLED_OFFSETS añade m(t) al argumento
     (DECOUPLED no). Métrica: correlación de la media móvil de M (ventana de
     7 días) con m(t) — se espera mayor en DECOUPLED_OFFSETS que en
     DECOUPLED.
  4. Recomendación razonada de variante para el POC.

CLI:
    python -m experiments.w32_variantes
    Regenera todas las figuras y el reporte en results/w32-variantes/.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from engine.types import MoodVariant, PersonaParams
from sim.metrics import autocorr_lag1, mean_sd
from sim.plots import plot_variant_comparison
from sim.run_daily import run

# ---------------------------------------------------------------------------
# Frozen experiment configuration

SEEDS: tuple[int, ...] = (111, 222, 333, 444, 555)
DAYS = 90
VARIANTS: tuple[MoodVariant, ...] = (
    MoodVariant.ORIGINAL,
    MoodVariant.DECOUPLED,
    MoodVariant.DECOUPLED_OFFSETS,
)
SEED_FOR_COMPARISON_PLOT = 111
MA_WINDOW = 7  # moving-average window (days) for the offset metric (3)
QUARTILE_Q = 0.25  # upper/lower g quartiles for Δmedia

OUT_DIR = Path(__file__).resolve().parents[1] / "results" / "w32-variantes"


# ---------------------------------------------------------------------------
# Experiment-specific metrics (private helpers)


def delta_media_by_gain(M: np.ndarray, g: np.ndarray, q: float = QUARTILE_Q) -> float:
    """mean(M | g >= Q(1-q)) − mean(M | g <= Q(q)): desplazamiento de NIVEL.

    Distinto de sim.metrics.var_ratio_by_gain (que mide razón de VARIANZA):
    aquí medimos si el ciclo mueve la media de M, no solo su dispersión —
    exactamente el acoplamiento media-ganancia que research/05 §2.1(a)
    describe para ORIGINAL.
    """
    M = np.asarray(M, dtype=float)
    g = np.asarray(g, dtype=float)
    lo_thresh = np.quantile(g, q)
    hi_thresh = np.quantile(g, 1.0 - q)
    mean_lo = float(np.mean(M[g <= lo_thresh]))
    mean_hi = float(np.mean(M[g >= hi_thresh]))
    return mean_hi - mean_lo


def pearson_corr(x: np.ndarray, y: np.ndarray) -> float:
    """Correlación de Pearson simple (envoltorio legible de np.corrcoef)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    return float(np.corrcoef(x, y)[0, 1])


def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    """Media móvil centrada de ventana `window` (recorta bordes, sin padding).

    Devuelve un array más corto que `x` (longitud len(x) - window + 1); el
    llamador debe recortar la serie de referencia al mismo tramo antes de
    correlacionar.
    """
    x = np.asarray(x, dtype=float)
    kernel = np.ones(window) / window
    return np.convolve(x, kernel, mode="valid")


def offset_corr_ma(M: np.ndarray, m: np.ndarray, window: int = MA_WINDOW) -> float:
    """corr(media móvil de M ventana `window`, m(t)) recortando m al mismo tramo.

    Métrica (3): mide si el offset de media m(t) (solo en DECOUPLED_OFFSETS)
    deja huella en la tendencia lenta de M. `np.convolve(..., mode="valid")`
    con ventana impar centra el resultado; recortamos m simétricamente.
    """
    M = np.asarray(M, dtype=float)
    m = np.asarray(m, dtype=float)
    ma = moving_average(M, window)
    trim = (len(m) - len(ma)) // 2
    m_trimmed = m[trim : trim + len(ma)]
    return pearson_corr(ma, m_trimmed)


# ---------------------------------------------------------------------------
# Main run


def run_all() -> dict[MoodVariant, dict[int, "SimResultLike"]]:  # noqa: F821
    """Corre las 3 variantes x 5 semillas. Devuelve {variant: {seed: SimResult}}."""
    persona = PersonaParams()
    results: dict[MoodVariant, dict[int, object]] = {}
    for variant in VARIANTS:
        results[variant] = {}
        for seed in SEEDS:
            results[variant][seed] = run(
                days=DAYS, seed=seed, variant=variant, persona=persona
            )
    return results


def compute_metrics_table(
    results: dict[MoodVariant, dict[int, object]],
) -> dict[MoodVariant, dict[str, dict[int, float]]]:
    """Métricas crudas por variante x semilla (antes de agregar entre semillas).

    Estructura: {variant: {metric_name: {seed: value}}}. metric_name en
    {"delta_media", "corr_g_M", "autocorr_lag1", "offset_corr_ma"}.
    `offset_corr_ma` se calcula para las 3 variantes por uniformidad de tabla,
    aunque el criterio (3) solo contrasta DECOUPLED vs DECOUPLED_OFFSETS (en
    ORIGINAL y DECOUPLED m(t)=0 idénticamente vía B=0 salvo que DECOUPLED
    ignora m por fórmula y ORIGINAL no lo usa tampoco -> se espera ~NaN o
    ruido puro en ambos, documentado en el reporte).
    """
    metrics: dict[MoodVariant, dict[str, dict[int, float]]] = {
        variant: {
            "delta_media": {},
            "corr_g_M": {},
            "autocorr_lag1": {},
            "offset_corr_ma": {},
        }
        for variant in VARIANTS
    }

    for variant in VARIANTS:
        for seed in SEEDS:
            res = results[variant][seed]
            M = res.M
            g = res.g
            m = res.m

            metrics[variant]["delta_media"][seed] = delta_media_by_gain(M, g)
            metrics[variant]["corr_g_M"][seed] = pearson_corr(g, M)
            metrics[variant]["autocorr_lag1"][seed] = autocorr_lag1(M)
            metrics[variant]["offset_corr_ma"][seed] = offset_corr_ma(M, m)

    return metrics


def aggregate_across_seeds(
    metrics: dict[MoodVariant, dict[str, dict[int, float]]],
) -> dict[MoodVariant, dict[str, tuple[float, float]]]:
    """(media, sd muestral ddof=1) entre semillas, por variante y métrica."""
    agg: dict[MoodVariant, dict[str, tuple[float, float]]] = {}
    for variant, metric_dict in metrics.items():
        agg[variant] = {}
        for metric_name, per_seed in metric_dict.items():
            values = np.asarray(list(per_seed.values()), dtype=float)
            agg[variant][metric_name] = mean_sd(values)
    return agg


# ---------------------------------------------------------------------------
# Figures


def plot_M_series_by_variant(
    results: dict[MoodVariant, dict[int, object]], out_dir: Path
) -> Path:
    """Series M(t) por variante: un panel por variante, las 5 semillas
    superpuestas (líneas finas) + media entre semillas (línea gruesa).

    Nombre: mood_series_by_variant.png
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(len(VARIANTS), 1, figsize=(11, 10), sharex=True, sharey=True)

    for ax, variant in zip(axes, VARIANTS):
        M_by_seed = []
        for seed in SEEDS:
            res = results[variant][seed]
            t = res.t
            M = res.M
            M_by_seed.append(M)
            ax.plot(t, M, "-", linewidth=1.0, alpha=0.35, color="C0")

        M_mean = np.mean(np.vstack(M_by_seed), axis=0)
        ax.plot(t, M_mean, "-", linewidth=2.2, color="C1", label="media entre semillas")

        ax.set_ylabel("M(t)")
        ax.set_title(f"{variant.value}")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)

    axes[-1].set_xlabel("Día")
    fig.suptitle(
        f"M(t) por variante — semillas {list(SEEDS)}, {DAYS} días", fontsize=12
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.97))

    png_path = out_dir / "mood_series_by_variant.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_metrics_barplot(
    agg: dict[MoodVariant, dict[str, tuple[float, float]]], out_dir: Path
) -> Path:
    """Barplot propio: Δmedia, corr(g,M), autocorr_lag1 por variante, con
    barras de error = sd entre semillas.

    Nombre: metrics_barplot.png
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    metric_names = ["delta_media", "corr_g_M", "autocorr_lag1"]
    metric_labels = [
        "Δmedia = mean(M|g alto) − mean(M|g bajo)",
        "corr(g, M)",
        "autocorr_lag1(M)",
    ]

    fig, axes = plt.subplots(1, len(metric_names), figsize=(15, 5))

    x = np.arange(len(VARIANTS))
    variant_labels = [v.value for v in VARIANTS]
    colors = ["C0", "C1", "C2"]

    for ax, metric_name, metric_label in zip(axes, metric_names, metric_labels):
        means = [agg[v][metric_name][0] for v in VARIANTS]
        sds = [agg[v][metric_name][1] for v in VARIANTS]

        ax.bar(x, means, yerr=sds, capsize=5, color=colors, edgecolor="black", alpha=0.8)
        ax.axhline(0.0, color="black", linewidth=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(variant_labels, rotation=20, ha="right", fontsize=8)
        ax.set_title(metric_label, fontsize=9)
        ax.grid(True, alpha=0.3, axis="y")

    fig.suptitle(
        f"Métricas de comparación de variantes — media ± sd entre {len(SEEDS)} semillas",
        fontsize=11,
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.94))

    png_path = out_dir / "metrics_barplot.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


# ---------------------------------------------------------------------------
# Report


def _fmt(mean: float, sd: float) -> str:
    return f"{mean:.4f} ± {sd:.4f}"


def write_report(
    agg: dict[MoodVariant, dict[str, tuple[float, float]]],
    figure_paths: dict[str, Path],
    out_dir: Path,
) -> Path:
    """Escribe reporte.md con tabla, veredicto (8a) y recomendación."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Per-criterion verdicts from the measured numbers ---

    # (1) Mean-gain coupling: ORIGINAL also amplifies the constant logit(lam);
    # DECOUPLED only amplifies mu+eta, which fluctuates around 0.
    delta_original = agg[MoodVariant.ORIGINAL]["delta_media"][0]
    delta_decoupled = agg[MoodVariant.DECOUPLED]["delta_media"][0]
    corr_g_M_original = agg[MoodVariant.ORIGINAL]["corr_g_M"][0]
    corr_g_M_decoupled = agg[MoodVariant.DECOUPLED]["corr_g_M"][0]

    criterio1_pass = abs(delta_original) > abs(delta_decoupled) and abs(
        corr_g_M_original
    ) > abs(corr_g_M_decoupled)

    # (2) Autocorrelation: DECOUPLED* > ORIGINAL
    ac_original = agg[MoodVariant.ORIGINAL]["autocorr_lag1"][0]
    ac_decoupled = agg[MoodVariant.DECOUPLED]["autocorr_lag1"][0]
    ac_decoupled_offsets = agg[MoodVariant.DECOUPLED_OFFSETS]["autocorr_lag1"][0]

    criterio2_pass = ac_decoupled > ac_original and ac_decoupled_offsets > ac_original

    # (3) B effect: |offset_corr_ma| of DECOUPLED_OFFSETS > DECOUPLED.
    offset_decoupled = agg[MoodVariant.DECOUPLED]["offset_corr_ma"][0]
    offset_decoupled_offsets = agg[MoodVariant.DECOUPLED_OFFSETS]["offset_corr_ma"][0]

    criterio3_pass = abs(offset_decoupled_offsets) > abs(offset_decoupled)

    overall_pass = criterio1_pass and criterio2_pass and criterio3_pass

    def pass_fail(b: bool) -> str:
        return "PASS" if b else "FAIL"

    lines: list[str] = []
    lines.append("# W3.2 — Comparativa de variantes de MoodVariant")
    lines.append("")
    lines.append(f"Semillas: {list(SEEDS)} · días: {DAYS} · PersonaParams() por defecto.")
    lines.append("")
    lines.append(
        "Script reproducible: `python -m experiments.w32_variantes` regenera "
        "todas las figuras y este reporte."
    )
    lines.append("")

    # --- Metric x variant table ---
    lines.append("## Tabla: métrica × variante (media ± sd entre semillas)")
    lines.append("")
    lines.append("| Métrica | ORIGINAL | DECOUPLED | DECOUPLED_OFFSETS |")
    lines.append("|---|---|---|---|")

    metric_rows = [
        ("Δmedia = mean(M\\|g alto) − mean(M\\|g bajo)", "delta_media"),
        ("corr(g, M) [Pearson]", "corr_g_M"),
        ("autocorr_lag1(M)", "autocorr_lag1"),
        (f"corr(MA{MA_WINDOW}(M), m(t))", "offset_corr_ma"),
    ]
    for label, key in metric_rows:
        row = [label]
        for variant in VARIANTS:
            mean, sd = agg[variant][key]
            row.append(_fmt(mean, sd))
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")

    # --- Detail per criterion ---
    lines.append("## Criterio (8a) — diferencias documentadas cuantitativamente")
    lines.append("")

    lines.append("### 1. Acoplamiento media-ganancia del ORIGINAL")
    lines.append("")
    lines.append(
        f"- Δmedia ORIGINAL = {_fmt(*agg[MoodVariant.ORIGINAL]['delta_media'])}; "
        f"Δmedia DECOUPLED = {_fmt(*agg[MoodVariant.DECOUPLED]['delta_media'])}; "
        f"Δmedia DECOUPLED_OFFSETS = {_fmt(*agg[MoodVariant.DECOUPLED_OFFSETS]['delta_media'])}."
    )
    lines.append(
        f"- corr(g, M) ORIGINAL = {_fmt(*agg[MoodVariant.ORIGINAL]['corr_g_M'])}; "
        f"corr(g, M) DECOUPLED = {_fmt(*agg[MoodVariant.DECOUPLED]['corr_g_M'])}; "
        f"corr(g, M) DECOUPLED_OFFSETS = {_fmt(*agg[MoodVariant.DECOUPLED_OFFSETS]['corr_g_M'])}."
    )
    lines.append(
        f"- Veredicto: **{pass_fail(criterio1_pass)}** — se espera "
        "|Δmedia| y |corr(g,M)| de ORIGINAL claramente mayores que en "
        "DECOUPLED, porque en ORIGINAL g(t) multiplica también la constante "
        "grande logit(λ)+μ, mientras que en DECOUPLED g(t) solo multiplica "
        "μ+η (que fluctúa cerca de 0)."
    )
    lines.append("")

    lines.append("### 2. Autocorrelación con/sin η")
    lines.append("")
    lines.append(
        f"- autocorr_lag1(M) ORIGINAL = {_fmt(*agg[MoodVariant.ORIGINAL]['autocorr_lag1'])}; "
        f"DECOUPLED = {_fmt(*agg[MoodVariant.DECOUPLED]['autocorr_lag1'])}; "
        f"DECOUPLED_OFFSETS = {_fmt(*agg[MoodVariant.DECOUPLED_OFFSETS]['autocorr_lag1'])}."
    )
    lines.append(
        f"- Veredicto: **{pass_fail(criterio2_pass)}** — se espera "
        "DECOUPLED y DECOUPLED_OFFSETS > ORIGINAL, porque solo esas dos "
        "variantes incluyen el término AR(1) η(t) en el argumento logit."
    )
    lines.append("")

    lines.append("### 3. Efecto de B (offset de media)")
    lines.append("")
    lines.append(
        f"- corr(MA{MA_WINDOW}(M), m(t)) DECOUPLED = "
        f"{_fmt(*agg[MoodVariant.DECOUPLED]['offset_corr_ma'])}; "
        f"DECOUPLED_OFFSETS = {_fmt(*agg[MoodVariant.DECOUPLED_OFFSETS]['offset_corr_ma'])}."
    )
    lines.append(
        f"- Veredicto: **{pass_fail(criterio3_pass)}** — se espera que "
        "DECOUPLED_OFFSETS muestre |correlación| mayor con m(t), porque es "
        "la única variante que suma m(t) al argumento logit; en DECOUPLED "
        "m(t) no entra en la fórmula (research/05 §2.2, compute_arg en "
        "engine/mood.py)."
    )
    lines.append("")

    lines.append(
        f"### Veredicto global (8a): **{pass_fail(overall_pass)}**"
    )
    lines.append("")
    confirmed = [
        name
        for name, ok in [
            ("(1) acoplamiento media-ganancia", criterio1_pass),
            ("(2) autocorrelación con/sin η", criterio2_pass),
            ("(3) efecto de B", criterio3_pass),
        ]
        if ok
    ]
    not_confirmed = [
        name
        for name, ok in [
            ("(1) acoplamiento media-ganancia", criterio1_pass),
            ("(2) autocorrelación con/sin η", criterio2_pass),
            ("(3) efecto de B", criterio3_pass),
        ]
        if not ok
    ]
    lines.append(
        "Confirmadas: " + (", ".join(confirmed) if confirmed else "ninguna") + "."
    )
    lines.append(
        "No confirmadas: " + (", ".join(not_confirmed) if not_confirmed else "ninguna")
        + "."
    )
    lines.append("")

    # --- Recommendation ---
    lines.append("## 4. Recomendación de variante para el POC")
    lines.append("")

    # Recommendation text is written below, conditioned on the measured results.
    lines.append(RECOMENDACION_PLACEHOLDER)
    lines.append("")

    # --- Figures ---
    lines.append("## Figuras")
    lines.append("")
    for name, path in figure_paths.items():
        lines.append(f"- `{path.name}` — {name}")
    lines.append("")

    report_path = out_dir / "reporte.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# Placeholder replaced in main() after the real numbers are computed.
RECOMENDACION_PLACEHOLDER = "__RECOMENDACION__"


def build_recomendacion(
    agg: dict[MoodVariant, dict[str, tuple[float, float]]],
) -> str:
    """Texto de recomendación (3-8 líneas) condicionado a los números medidos.

    Lógica: DECOUPLED_OFFSETS es la recomendación por diseño (research/05
    §2.2) SALVO que los datos muestren que m(t) no aporta señal distinguible
    de DECOUPLED (criterio 3 en FAIL) Y que el knob adicional (B) no separe
    fase de nivel de forma útil — en ese caso se recomienda DECOUPLED por
    parsimonia. ORIGINAL nunca se recomienda para Fase 2 porque el
    acoplamiento media-ganancia impide tunear amplitud del ciclo y
    temperamento por separado (un solo knob, dos efectos, research/05 §2.1a).
    """
    offset_decoupled = agg[MoodVariant.DECOUPLED]["offset_corr_ma"][0]
    offset_decoupled_offsets = agg[MoodVariant.DECOUPLED_OFFSETS]["offset_corr_ma"][0]
    b_effect_visible = abs(offset_decoupled_offsets) > abs(offset_decoupled)

    ac_original = agg[MoodVariant.ORIGINAL]["autocorr_lag1"][0]
    ac_decoupled_offsets = agg[MoodVariant.DECOUPLED_OFFSETS]["autocorr_lag1"][0]

    lines = []
    lines.append(
        "ORIGINAL acopla temperamento y ganancia (un knob, λ, mueve nivel Y "
        "reactividad a la vez) y carece de η — su autocorr_lag1(M) "
        f"({ac_original:.3f}) queda cerca del piso frente a "
        f"DECOUPLED_OFFSETS ({ac_decoupled_offsets:.3f}) — no permite tunear "
        "'racha sin causa externa' por separado de 'temperamento'; se "
        "descarta para Fase 2."
    )
    if b_effect_visible:
        lines.append(
            f"El offset de media m(t) sí deja huella medible (|corr(MA{MA_WINDOW}(M), m(t))| "
            f"mayor en DECOUPLED_OFFSETS que en DECOUPLED: "
            f"{abs(offset_decoupled_offsets):.4f} vs {abs(offset_decoupled):.4f}), "
            "es decir, B compra una señal real y no redundante con A/η."
        )
        lines.append(
            "**Recomendación: DECOUPLED_OFFSETS.** Compra tres knobs "
            "ortogonales (B = nivel por fase, A = reactividad por fase, "
            "ρ_e/σ_e = rachas sin causa) al costo de un parámetro extra (B) "
            "frente a DECOUPLED — costo bajo, la separación de knobs es "
            "justamente el objetivo de research/05 §2.2 y el efecto es "
            "verificable en los datos de esta corrida."
        )
    else:
        lines.append(
            f"El offset de media m(t) NO se distingue con margen claro de "
            f"DECOUPLED en esta corrida (|corr(MA{MA_WINDOW}(M), m(t))|: "
            f"{abs(offset_decoupled_offsets):.4f} vs {abs(offset_decoupled):.4f} "
            "en DECOUPLED) — el knob B añade complejidad sin una señal "
            "claramente separable de A/η en este régimen de parámetros."
        )
        lines.append(
            "**Recomendación: DECOUPLED** por parsimonia, dado que en esta "
            "corrida B no compra una separación de señal claramente medible; "
            "se sugiere revisar con B más grande o más días antes de "
            "descartar DECOUPLED_OFFSETS definitivamente para Fase 2."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Corriendo {len(VARIANTS)} variantes x {len(SEEDS)} semillas x {DAYS} días...")
    results = run_all()

    print("Calculando métricas por variante x semilla...")
    metrics = compute_metrics_table(results)
    agg = aggregate_across_seeds(metrics)

    print("Generando figuras...")
    figure_paths: dict[str, Path] = {}

    # (a) sim.plots.plot_variant_comparison, fixed seed 111
    comparison_inputs = {
        variant.value: results[variant][SEED_FOR_COMPARISON_PLOT] for variant in VARIANTS
    }
    figure_paths["comparación de variantes (sim.plots.plot_variant_comparison), semilla 111"] = (
        plot_variant_comparison(
            comparison_inputs, OUT_DIR, tag=f"w32_s{SEED_FOR_COMPARISON_PLOT}"
        )
    )

    # (b) M(t) series per variant, all seeds
    figure_paths["M(t) por variante, 5 semillas superpuestas + media"] = (
        plot_M_series_by_variant(results, OUT_DIR)
    )

    # (c) custom metrics barplot
    figure_paths["barplot de métricas (Δmedia, corr(g,M), autocorr_lag1) con error bars"] = (
        plot_metrics_barplot(agg, OUT_DIR)
    )

    print("Escribiendo reporte...")
    report_path = write_report(agg, figure_paths, OUT_DIR)

    # Replace the recommendation placeholder with the conditioned text.
    recomendacion_text = build_recomendacion(agg)
    report_text = report_path.read_text(encoding="utf-8")
    report_text = report_text.replace(RECOMENDACION_PLACEHOLDER, recomendacion_text)
    report_path.write_text(report_text, encoding="utf-8")

    # --- stdout summary ---
    print()
    print("=== Resumen (media ± sd entre semillas) ===")
    for variant in VARIANTS:
        print(f"\n{variant.value}:")
        for metric_name in ["delta_media", "corr_g_M", "autocorr_lag1", "offset_corr_ma"]:
            mean, sd = agg[variant][metric_name]
            print(f"  {metric_name}: {mean:.4f} ± {sd:.4f}")

    print()
    print(f"Reporte: {report_path}")
    for name, path in figure_paths.items():
        print(f"Figura ({name}): {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
