"""¿Cuánto pesan los días buenos y los días malos? — shocks forzados ±1.0.

PROPIEDAD: este archivo + las figuras `engine_simulation/13_dias_buenos_malos.png`
y `engine_simulation/14_dias_buenos_malos_promedio.png` EN LA RAIZ del
proyecto. No modifica engine_simulation.py ni engine_simulation_promedio.py;
solo anade filas a la seccion existente "## Lecturas adicionales" del README.

Motivacion: la memoria de eventos mu seharia hacia un equilibrio
mu_inf = k*(s - score_neutral)/(1-rho) cuando el score del juez es constante.
Con los defaults de Fase 1 (k=0.15, rho=0.70) y s=+-1.0, score_neutral=0.0:
    mu_inf = 0.15*(+-1.0)/(1-0.70) = +-0.5
y la vida media de mu es ln(2)/-ln(rho) ~= 1.9 dias, por lo que el equilibrio
se alcanza en ~5-7 dias (aprox 3 vidas medias). Esta figura compara ese
regimen extremo (shocks<=+1.0 o -1.0 TODOS los dias) contra el baseline
endogeno (score sintetico normal, sin shocks) para ver cuanto separan el
animo observable M(t) los "dias siempre buenos" de los "dias siempre malos".

30 dias, variante DECOUPLED_OFFSETS, persona base = PersonaParams() (defaults
de Fase 1 + B=0.5 post-Fase 1 — no se tocan).

Reproducir:
    wsl.exe -d Ubuntu -- bash -lc \
        'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && \
         MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_dias'
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine.mood import logit, sigmoid
from engine.types import MoodVariant, PersonaParams, SimResult
from sim.run_daily import run

# ---------------------------------------------------------------------------
# Constantes del experimento

DAYS = 30
SEED_SINGLE = 3001
SEEDS_AVG = tuple(range(4001, 4031))  # 30 semillas
VARIANT = MoodVariant.DECOUPLED_OFFSETS
OUT_DIR = Path(__file__).resolve().parent.parent / "engine_simulation"

_BASE_PERSONA = PersonaParams()
_LAM_LOGIT = logit(_BASE_PERSONA.lam)  # logit(0.6)

# mu_inf teorico = k*(s - score_neutral)/(1-rho); con k=0.15, rho=0.70,
# score_neutral=0.0, s=+-1.0 => +-0.5
MU_INF_BUENOS = _BASE_PERSONA.k * (1.0 - _BASE_PERSONA.score_neutral) / (1.0 - _BASE_PERSONA.rho)
MU_INF_MALOS = _BASE_PERSONA.k * (-1.0 - _BASE_PERSONA.score_neutral) / (1.0 - _BASE_PERSONA.rho)

REGIMES: tuple[tuple[str, str, float | None, str], ...] = (
    # (key, label, shock_value_or_None, color)
    ("buenos", "siempre buenos (shock=+1.0)", 1.0, "green"),
    ("baseline", "baseline (score endógeno)", None, "C0"),
    ("malos", "siempre malos (shock=-1.0)", -1.0, "red"),
)


def build_shocks(shock_value: float | None) -> dict[int, float]:
    if shock_value is None:
        return {}
    return {t: shock_value for t in range(DAYS)}


def run_scenario(shock_value: float | None, seed: int) -> SimResult:
    shocks = build_shocks(shock_value)
    return run(days=DAYS, seed=seed, variant=VARIANT, persona=_BASE_PERSONA, shocks=shocks)


def theoretical_curve(mu_inf: float, t: np.ndarray, N: int) -> np.ndarray:
    """N·sigmoid(logit(lam) + B·sin(2π·t/28) + mu_inf) — referencia simplificada."""
    arg = _LAM_LOGIT + _BASE_PERSONA.B * np.sin(2.0 * math.pi * t / 28.0) + mu_inf
    p = np.array([sigmoid(a) for a in arg])
    return N * p


# ---------------------------------------------------------------------------
# Figura 13 — una vida (semilla 3001)


def plot_una_vida() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    N = _BASE_PERSONA.N

    fig, axes = plt.subplots(3, 1, figsize=(10, 11), sharex=True, dpi=120)

    for ax, (key, label, shock_value, color) in zip(axes, REGIMES):
        result = run_scenario(shock_value, SEED_SINGLE)
        t = result.t
        M = result.M
        p = result.p
        mu = result.mu
        Np = N * p
        sd_binom = np.sqrt(N * p * (1.0 - p))

        ax.plot(t, M, ".", color="gray", markersize=4, alpha=0.7, label="M(t) (dado diario)")
        ax.plot(t, Np, "-", color=color, linewidth=2.0, label="N·p(t) (ánimo real)")
        ax.fill_between(t, Np - sd_binom, Np + sd_binom, color=color, alpha=0.15,
                         label="± sd binomial")
        ax.set_ylim([-0.5, 10.5])
        ax.set_ylabel(f"M (0..{N})")
        ax.grid(True, alpha=0.3)

        ax2 = ax.twinx()
        ax2.plot(t, mu, ":", color="black", linewidth=1.2, label="μ(t)")
        if shock_value is not None:
            mu_inf_teo = MU_INF_BUENOS if shock_value > 0 else MU_INF_MALOS
            ax2.axhline(mu_inf_teo, color="black", linewidth=0.8, linestyle="--", alpha=0.6)
            mu_final_medido = float(np.mean(mu[-5:]))
            titulo_mu = f"μ final medido (últimos 5 días): {mu_final_medido:.3f} vs teórico {mu_inf_teo:+.2f}"
        else:
            mu_final_medido = float(np.mean(mu[-5:]))
            titulo_mu = f"μ final medido (últimos 5 días): {mu_final_medido:.3f} (sin shock, teórico 0.0)"
        ax2.set_ylabel("μ(t)", fontsize=9)
        ax2.set_ylim([-0.6, 0.6])

        ax.set_title(f"{label} — {titulo_mu}", fontsize=10)

        if key == "buenos":
            lines1, labels1 = ax.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax.legend(lines1 + lines2, labels1 + labels2, loc="lower right", fontsize=7)

    axes[-1].set_xlabel("Día")
    fig.suptitle(
        f"Días buenos vs baseline vs días malos — una vida (semilla {SEED_SINGLE})"
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.96))

    png_path = OUT_DIR / "13_dias_buenos_malos.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


# ---------------------------------------------------------------------------
# Figura 14 — 30 semillas, un solo eje


def collect_stats(shock_value: float | None) -> dict[str, np.ndarray]:
    n_seeds = len(SEEDS_AVG)
    M_runs = np.empty((n_seeds, DAYS))
    mu_runs = np.empty((n_seeds, DAYS))
    t = None

    for i, seed in enumerate(SEEDS_AVG):
        result = run_scenario(shock_value, seed)
        if t is None:
            t = result.t
        M_runs[i, :] = result.M
        mu_runs[i, :] = result.mu

    mean_M = M_runs.mean(axis=0)
    sem_M = M_runs.std(axis=0, ddof=1) / math.sqrt(n_seeds)
    mean_mu = mu_runs.mean(axis=0)
    sem_mu = mu_runs.std(axis=0, ddof=1) / math.sqrt(n_seeds)

    return {"t": t, "mean_M": mean_M, "sem_M": sem_M, "mean_mu": mean_mu, "sem_mu": sem_mu}


def plot_promedio() -> tuple[Path, dict[str, dict]]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    N = _BASE_PERSONA.N

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(10, 8), dpi=120, sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    stats_by_regime: dict[str, dict] = {}
    mu_inf_map = {"buenos": MU_INF_BUENOS, "baseline": 0.0, "malos": MU_INF_MALOS}

    for key, label, shock_value, color in REGIMES:
        stats = collect_stats(shock_value)
        stats_by_regime[key] = stats
        t = stats["t"]
        mean_M = stats["mean_M"]
        sem_M = stats["sem_M"]
        mean_mu = stats["mean_mu"]

        ax_top.plot(t, mean_M, "-", color=color, linewidth=2.2,
                    label=f"media entre semillas de M(t) — {label}")
        ax_top.fill_between(t, mean_M - sem_M, mean_M + sem_M, color=color, alpha=0.2)

        mu_inf = mu_inf_map[key]
        theo = theoretical_curve(mu_inf, t, N)
        ax_top.plot(t, theo, ":", color=color, linewidth=1.4, alpha=0.8)

        ax_bot.plot(t, mean_mu, "-", color=color, linewidth=1.8)
        if mu_inf != 0.0:
            ax_bot.axhline(mu_inf, color=color, linestyle="--", linewidth=0.8, alpha=0.6)

    ax_top.set_ylabel(f"M (0..{N})")
    ax_top.set_ylim([-0.5, 10.5])
    ax_top.grid(True, alpha=0.3)
    ax_top.legend(loc="center left", fontsize=8, bbox_to_anchor=(1.01, 0.5))

    ax_bot.set_ylabel("μ(t) medio", fontsize=9)
    ax_bot.set_ylim([-0.6, 0.6])
    ax_bot.set_xlabel("Día")
    ax_bot.grid(True, alpha=0.3)

    fig.suptitle("¿Cuánto pesan los días buenos y los malos? — 30 semillas × 30 días")
    fig.tight_layout(rect=(0.0, 0.0, 0.82, 0.96))

    png_path = OUT_DIR / "14_dias_buenos_malos_promedio.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path, stats_by_regime


# ---------------------------------------------------------------------------
# README.md — anade filas a la seccion existente "## Lecturas adicionales"


def append_readme_lines() -> Path:
    readme_path = OUT_DIR / "README.md"
    existing = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    marker = "## Lecturas adicionales"
    new_lines = (
        "| `13_dias_buenos_malos.png` | 3 paneles apilados (una sola semilla, 3001): "
        "\"siempre buenos\" (shock=+1.0 todos los días), baseline (score endógeno, sin "
        "shocks) y \"siempre malos\" (shock=−1.0 todos los días); M(t) crudo (gris), "
        "N·p(t) ± sd binomial (verde/azul/rojo) y μ(t) en eje secundario con la línea de "
        "equilibrio teórico μ∞=±0.5 | Compara el μ(t) final medido (título de cada panel) "
        "contra el equilibrio teórico μ∞=k·(s−score_neutral)/(1−ρ)=±0.5; con ρ=0.70 la "
        "vida media de μ es ≈1.9 días, así que el equilibrio se alcanza en ≈5–7 días |\n"
        "| `14_dias_buenos_malos_promedio.png` | Media entre 30 semillas (4001–4030) de "
        "M(t) para los 3 regímenes en un solo eje (verde/azul/rojo, ± sem sombreado), con "
        "las curvas de referencia N·sigmoid(logit(0.6)+B·sin(2πt/28)+μ∞) punteadas "
        "(μ∞∈{+0.5, 0, −0.5}); panel inferior: media entre semillas de μ(t) por régimen "
        "con las asíntotas ±0.5 | Muestra cuánto separa en pasos de M un régimen de "
        "\"siempre buenos\" de uno de \"siempre malos\" una vez que μ converge, y en "
        "cuántos días se abre esa separación desde el arranque compartido en μ=0 |\n"
    )

    if marker in existing:
        idx_section = existing.index(marker)
        idx_after = existing.index("### Regenerar", idx_section)
        before = existing[:idx_after]
        after = existing[idx_after:]
        before = before.rstrip("\n") + "\n" + new_lines + "\n"
        readme_path.write_text(before + after, encoding="utf-8")
    else:
        section_lines: list[str] = []
        if existing and not existing.endswith("\n"):
            section_lines.append("")
        section_lines.append("")
        section_lines.append(marker)
        section_lines.append("")
        section_lines.append("| Figura | Qué muestra | Cómo leerla |")
        section_lines.append("|---|---|---|")
        section_lines.append(new_lines.rstrip("\n"))
        section_lines.append("")
        readme_path.write_text(existing + "\n".join(section_lines) + "\n", encoding="utf-8")

    return readme_path


# ---------------------------------------------------------------------------
# Orquestacion


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    png13 = plot_una_vida()
    print(f"escrito: {png13}")

    png14, stats_by_regime = plot_promedio()
    print(f"escrito: {png14}")

    readme_path = append_readme_lines()
    print(f"escrito: {readme_path}")

    print()
    print(f"Teoría: μ∞ buenos = {MU_INF_BUENOS:+.3f}, μ∞ malos = {MU_INF_MALOS:+.3f}")
    print()

    mean_M_last15 = {}
    mean_mu_last10 = {}
    for key, label, shock_value, color in REGIMES:
        stats = stats_by_regime[key]
        mean_M_l15 = float(np.mean(stats["mean_M"][-15:]))
        mean_mu_l10 = float(np.mean(stats["mean_mu"][-10:]))
        mean_M_last15[key] = mean_M_l15
        mean_mu_last10[key] = mean_mu_l10
        teo = MU_INF_BUENOS if key == "buenos" else (MU_INF_MALOS if key == "malos" else 0.0)
        print(f"{label:35s} | mean M últimos 15 días: {mean_M_l15:6.3f} | "
              f"μ medio últimos 10 días: {mean_mu_l10:+.3f} (teórico {teo:+.2f})")

    separacion = mean_M_last15["buenos"] - mean_M_last15["malos"]
    print()
    print(f"Separación buenos−malos en M (últimos 15 días, medias entre semillas): {separacion:.3f} pasos")

    mean_M_buenos = stats_by_regime["buenos"]["mean_M"]
    mean_M_malos = stats_by_regime["malos"]["mean_M"]
    dia_apertura = None
    for i, t_val in enumerate(stats_by_regime["buenos"]["t"]):
        if abs(mean_M_buenos[i] - mean_M_malos[i]) > 1.0:
            dia_apertura = int(t_val)
            break
    if dia_apertura is not None:
        print(f"Primer día en que |mean_buenos − mean_malos| > 1 paso de M: día {dia_apertura}")
    else:
        print("La separación nunca superó 1 paso de M en la ventana de 30 días.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
