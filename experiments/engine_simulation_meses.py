"""¿Puede un mes perfecto vivir en 7–10 y uno horrible en 0–4?

Compara tres parametrizaciones de la memoria de eventos (k, ρ) bajo score
forzado +1 (mes perfecto) y −1 (mes horrible), 30 semillas × 30 días.
El techo del efecto del trato es μ∞ = k·(s − neutral)/(1−ρ); la cota de
estabilidad es k < 2(1−ρ)/g_max. Figura: engine_simulation/15_mes_perfecto_horrible.png

Regenerar: MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_meses
"""
import dataclasses

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine import validation
from engine.types import MoodVariant, PersonaParams, TimingParams
from sim.run_daily import run

DAYS = 30
SEEDS = list(range(5001, 5031))
BASE = PersonaParams()

PARAM_SETS = {
    "actual\nk=0.15, ρ=0.70 → μ∞=±0.50": BASE,
    "media\nk=0.25, ρ=0.80 → μ∞=±1.25": dataclasses.replace(BASE, k=0.25, rho=0.80),
    "lenta\nk=0.18, ρ=0.85 → μ∞=±1.20": dataclasses.replace(BASE, k=0.18, rho=0.85),
}
REGIMES = {
    "mes perfecto (score +1)": {t: +1.0 for t in range(DAYS)},
    "mes horrible (score −1)": {t: -1.0 for t in range(DAYS)},
}

tp = TimingParams()
for label, pp in PARAM_SETS.items():
    errs = validation.check(pp, tp)
    assert not errs, (label, errs)

fig, axes = plt.subplots(1, 3, figsize=(16, 5.5), sharey=True)
fig.suptitle(
    "¿Un mes perfecto vive en 7–10 y uno horrible en 0–4? — 30 semillas × 30 días",
    fontsize=13,
)

print(f"{'parametrización':<24} {'régimen':<26} {'M medio(d10+)':>13} "
      f"{'%días>=7':>9} {'%días<=4':>9}")
for ax, (label, pp) in zip(axes, PARAM_SETS.items()):
    ax.axhspan(7, 10.5, color="green", alpha=0.07)
    ax.axhspan(-0.5, 4, color="red", alpha=0.07)
    for (reg_label, shocks), color in zip(REGIMES.items(), ("tab:green", "tab:red")):
        M = np.array([
            run(DAYS, s, MoodVariant.DECOUPLED_OFFSETS, persona=pp, shocks=shocks).M
            for s in SEEDS
        ])  # (30 seeds, 30 days)
        mean_t = M.mean(axis=0)
        p10 = np.percentile(M, 10, axis=0)
        p90 = np.percentile(M, 90, axis=0)
        ax.plot(mean_t, color=color, lw=2, label=reg_label)
        ax.fill_between(range(DAYS), p10, p90, color=color, alpha=0.18,
                        label="p10–p90 de los días")
        post = M[:, 10:]  # drop the initial transient
        print(f"{label.splitlines()[1]:<24} {reg_label:<26} {post.mean():13.2f} "
              f"{(post >= 7).mean():9.1%} {(post <= 4).mean():9.1%}")
    ax.set_title(label, fontsize=10)
    ax.set_xlabel("Día")
    ax.set_ylim(-0.5, 10.5)
    ax.grid(alpha=0.3)
axes[0].set_ylabel("M (0..10)")
axes[0].legend(fontsize=8, loc="center left")

out = __import__("pathlib").Path(__file__).resolve().parent.parent / "engine_simulation"
out.mkdir(exist_ok=True)
path = out / "15_mes_perfecto_horrible.png"
fig.savefig(path, dpi=120, bbox_inches="tight")
plt.close(fig)
print(f"\nfigura: {path}")
