"""Tests de aceptación de engine/mood.py (W1.1).

PROPIEDAD: tarea W1.1 (este archivo + engine/mood.py). Usa las fixtures
compartidas de tests/conftest.py (rng, persona, timing) — congelado, no se
toca. Semillas y tolerancias documentadas en cada test.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from scipy import stats

from engine.types import MoodState, MoodVariant, PersonaParams
from engine import mood


# ---------------------------------------------------------------------------
# 1. nu = inf: M debe reproducir Binomial(N, p) exactamente (sin muestrear Beta)


def test_step_nu_inf_matches_binomial_distribution(persona: PersonaParams) -> None:
    """Con nu=inf (default de PersonaParams), la distribución empírica de M
    sobre >=5000 muestras debe coincidir con Binomial(N, p) conocida.

    p se fija eligiendo mu tal que compute_arg produzca un p conocido (usamos
    variant=DECOUPLED con g=1, m=0 para que arg = logit(lam) + mu = logit(p)).
    Criterio: test chi-cuadrado de bondad de ajuste sobre las N+1 categorías
    de la Binomial, alpha=0.01 (test estadístico generoso, ver CONVENTIONS.md
    §5). N=10 (default) da 11 categorías; se agrupan colas con conteo
    esperado < 5 para que el chi-cuadrado sea válido.
    """
    assert math.isinf(persona.nu)  # precondición estructural del caso especial

    p_target = 0.35
    mu = mood.logit(p_target) - mood.logit(persona.lam)
    state = MoodState(mu=mu, eta=0.0)

    n_samples = 8000
    rng = np.random.default_rng(20260703)
    samples = np.empty(n_samples, dtype=int)
    for i in range(n_samples):
        M, p, arg = mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=rng)
        samples[i] = M

    # p y arg deben ser deterministas (no dependen del rng) e iguales en cada draw.
    assert p == pytest.approx(p_target, abs=1e-9)

    N = persona.N
    observed_counts = np.bincount(samples, minlength=N + 1).astype(float)
    expected_probs = stats.binom.pmf(np.arange(N + 1), N, p_target)
    expected_counts = expected_probs * n_samples

    # Agrupar categorías con conteo esperado < 5 (regla estándar de chi-cuadrado)
    # fusionándolas con la categoría vecina más cercana, empezando por las colas.
    obs = list(observed_counts)
    exp = list(expected_counts)

    def merge_left_tail(obs_l, exp_l):
        while len(exp_l) > 1 and exp_l[0] < 5.0:
            exp_l[1] += exp_l[0]
            obs_l[1] += obs_l[0]
            del exp_l[0]
            del obs_l[0]
        return obs_l, exp_l

    obs, exp = merge_left_tail(obs, exp)
    obs.reverse()
    exp.reverse()
    obs, exp = merge_left_tail(obs, exp)
    obs.reverse()
    exp.reverse()

    obs_arr = np.asarray(obs)
    exp_arr = np.asarray(exp)
    # Renormalizar exp_arr a la suma exacta de obs_arr (evita drift num. residual)
    exp_arr = exp_arr * (obs_arr.sum() / exp_arr.sum())

    chi2, pvalue = stats.chisquare(f_obs=obs_arr, f_exp=exp_arr)
    assert pvalue > 0.01, f"chi-cuadrado rechaza H0 (Binomial): chi2={chi2}, p={pvalue}"


def test_step_nu_inf_ks_matches_binomial_cdf(persona: PersonaParams) -> None:
    """Chequeo independiente del anterior (chi-cuadrado): compara la CDF
    empírica discreta de M contra la CDF teórica de Binomial(N, p) mediante el
    estadístico de Kolmogorov-Smirnov D = sup_x |ecdf(x) - cdf(x)|, acotado con
    la banda de confianza de Dvoretzky-Kiefer-Wolfowitz (DKW):
        P(D > eps) <= 2*exp(-2*n*eps^2)
    válida para cualquier distribución (continua o discreta) sin necesitar la
    distribución exacta del estadístico KS bajo discreción (a diferencia de
    scipy.stats.kstest, que asume una cdf de referencia continua y da falsos
    rechazos aquí — verificado empíricamente contra muestras Binomial() puras
    de numpy, ver notas de implementación). alpha=0.01 -> eps = sqrt(ln(2/alpha)/(2n)).
    """
    p_target = 0.62
    mu = mood.logit(p_target) - mood.logit(persona.lam)
    state = MoodState(mu=mu, eta=0.0)

    n_samples = 6000
    rng = np.random.default_rng(777)
    samples = np.empty(n_samples, dtype=int)
    for i in range(n_samples):
        M, _, _ = mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.ORIGINAL, rng=rng)
        samples[i] = M

    xs = np.arange(persona.N + 1)
    ecdf = np.array([(samples <= x).mean() for x in xs])
    cdf_theoretical = stats.binom.cdf(xs, persona.N, p_target)
    D = np.max(np.abs(ecdf - cdf_theoretical))

    alpha = 0.01
    eps = math.sqrt(math.log(2.0 / alpha) / (2.0 * n_samples))
    assert D <= eps, f"KS/DKW rechaza H0 (Binomial): D={D:.5f} > eps={eps:.5f} (alpha={alpha})"


class _BetaSpyRng:
    """Envoltorio de un Generator real que cuenta llamadas a .beta().

    numpy.random.Generator es un objeto nativo (Cython) con atributos
    read-only: no admite monkeypatch.setattr directo sobre una instancia. Se
    usa composición (delegación por __getattr__) en vez de parchear el objeto.
    """

    def __init__(self, inner: np.random.Generator) -> None:
        self._inner = inner
        self.beta_calls = 0

    def beta(self, *args, **kwargs):
        self.beta_calls += 1
        return self._inner.beta(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_step_nu_inf_never_calls_beta(persona: PersonaParams) -> None:
    """El caso nu=inf es ESTRUCTURAL: no debe invocar Generator.beta en absoluto
    (no es un límite numérico con alpha/beta gigantes). Se usa un spy que
    delega en un Generator real pero cuenta llamadas a .beta().
    """
    state = MoodState(mu=0.1, eta=0.0)
    spy = _BetaSpyRng(np.random.default_rng(1))

    M, p, arg = mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=spy)

    assert spy.beta_calls == 0, "step llamó a rng.beta() con nu=inf (debería ser Binomial pura)"
    assert 0 <= M <= persona.N


def test_step_finite_nu_does_call_beta(persona: PersonaParams) -> None:
    """Contraparte del test anterior: con nu finita SÍ debe pasar por
    rng.beta() (rama beta-binomial), para no dejar pasar una implementación
    que ignore nu por completo."""
    params_finite_nu = dataclasses.replace(persona, nu=8.0)
    state = MoodState(mu=0.1, eta=0.0)
    spy = _BetaSpyRng(np.random.default_rng(1))

    mood.step(state, params_finite_nu, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=spy)

    assert spy.beta_calls == 1


# ---------------------------------------------------------------------------
# 2. Recursión de mu bajo score constante converge a la forma cerrada


def test_update_converges_to_closed_form(persona: PersonaParams) -> None:
    """mu' = rho*mu + k*(s - score_neutral) iterada ~50 veces bajo score
    constante s converge a mu_inf = k*(s - score_neutral)/(1 - rho).
    Tolerancia 1e-6 (absoluta) con rho=0.70 FIJADO explícitamente (no el
    default, que puede afinarse) ⇒ convergencia geométrica rápida
    (rho^50 ~ 8e-8), holgada respecto a la tolerancia pedida.
    """
    s = 0.8
    persona = dataclasses.replace(persona, k=0.15, rho=0.70)
    state = MoodState(mu=0.0, eta=0.0)
    for _ in range(50):
        state = mood.update(state, persona, score=s)

    mu_inf_expected = persona.k * (s - persona.score_neutral) / (1.0 - persona.rho)
    assert state.mu == pytest.approx(mu_inf_expected, abs=1e-6)


def test_update_converges_to_closed_form_negative_score(persona: PersonaParams) -> None:
    """Mismo criterio que el test anterior pero con score negativo (mu_inf < 0),
    para no depender solo de la rama positiva de la recursión."""
    s = -0.5
    persona = dataclasses.replace(persona, k=0.15, rho=0.70)  # ver test anterior
    state = MoodState(mu=0.3, eta=-0.2)  # estado inicial arbitrario no nulo
    for _ in range(60):
        state = mood.update(state, persona, score=s)

    mu_inf_expected = persona.k * (s - persona.score_neutral) / (1.0 - persona.rho)
    assert state.mu == pytest.approx(mu_inf_expected, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. sd estacionaria de eta ~ sigma_e / sqrt(1 - rho_e^2)


def test_step_endogenous_stationary_sd(persona: PersonaParams) -> None:
    """Itera step_endogenous >=20000 pasos, descarta burn-in (primeros 2000,
    ~irrelevante frente a la escala de autocorrelación rho_e=0.5) y compara
    la sd muestral de eta con la sd estacionaria teórica sigma_e/sqrt(1-rho_e^2),
    tolerancia relativa 5%. Semilla fija para reproducibilidad.
    """
    rng = np.random.default_rng(4242)
    n_steps = 25000
    burn_in = 2000

    state = MoodState(mu=0.0, eta=0.0)
    etas = np.empty(n_steps)
    for i in range(n_steps):
        state = mood.step_endogenous(state, persona, rng)
        etas[i] = state.eta

    sample_sd = np.std(etas[burn_in:], ddof=1)
    theoretical_sd = persona.sigma_e / math.sqrt(1.0 - persona.rho_e ** 2)

    rel_error = abs(sample_sd - theoretical_sd) / theoretical_sd
    assert rel_error < 0.05, (
        f"sd muestral {sample_sd:.5f} vs teórica {theoretical_sd:.5f} "
        f"(error relativo {rel_error:.3%}, tolerancia 5%)"
    )


# ---------------------------------------------------------------------------
# 4. Las 3 variantes son idénticas cuando m=0 (B=0), eta=0, g=1


@pytest.mark.parametrize(
    "variant",
    [MoodVariant.ORIGINAL, MoodVariant.DECOUPLED, MoodVariant.DECOUPLED_OFFSETS],
)
def test_variants_identical_arg_when_degenerate(persona: PersonaParams, variant: MoodVariant) -> None:
    """Con m=0, eta=0, g=1 las tres variantes deben producir EXACTAMENTE el
    mismo arg (comparación exacta, no approx: son las mismas operaciones
    aritméticas de punto flotante bajo estas condiciones degeneradas)."""
    state = MoodState(mu=0.37, eta=0.0)
    arg = mood.compute_arg(state, persona, m=0.0, g=1.0, variant=variant)
    expected = mood.logit(persona.lam) + state.mu
    assert arg == expected


def test_variants_all_pairwise_identical_when_degenerate(persona: PersonaParams) -> None:
    """Comparación cruzada explícita de las tres variantes entre sí."""
    state = MoodState(mu=-0.6, eta=0.0)
    args = {
        variant: mood.compute_arg(state, persona, m=0.0, g=1.0, variant=variant)
        for variant in MoodVariant
    }
    values = list(args.values())
    assert values[0] == values[1] == values[2]


def test_variants_same_M_with_same_rng_when_degenerate(persona: PersonaParams) -> None:
    """step con el mismo Generator (misma semilla, reconstruido para cada
    variante) debe producir el mismo M cuando m=0, eta=0, g=1, ya que arg (y
    por tanto p) es idéntico entre variantes y el consumo de aleatoriedad de
    step (rng.binomial, sin rng.beta porque nu=inf) es el mismo."""
    state = MoodState(mu=0.2, eta=0.0)
    results = {}
    for variant in MoodVariant:
        rng = np.random.default_rng(999)
        M, p, arg = mood.step(state, persona, m=0.0, g=1.0, variant=variant, rng=rng)
        results[variant] = (M, p, arg)

    Ms = [v[0] for v in results.values()]
    ps = [v[1] for v in results.values()]
    args_ = [v[2] for v in results.values()]
    assert Ms[0] == Ms[1] == Ms[2]
    assert ps[0] == ps[1] == ps[2]
    assert args_[0] == args_[1] == args_[2]


# ---------------------------------------------------------------------------
# 5. step no muta el estado; update/step_endogenous devuelven instancias nuevas


def test_step_does_not_mutate_state(persona: PersonaParams) -> None:
    state = MoodState(mu=0.25, eta=-0.1)
    mu_before, eta_before = state.mu, state.eta
    rng = np.random.default_rng(5)

    mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng)

    assert state.mu == mu_before
    assert state.eta == eta_before


def test_update_returns_new_instance_without_mutating_original(persona: PersonaParams) -> None:
    state = MoodState(mu=0.1, eta=0.2)
    new_state = mood.update(state, persona, score=0.5)

    assert new_state is not state
    assert isinstance(new_state, MoodState)
    # Original intacto.
    assert state.mu == 0.1
    assert state.eta == 0.2
    # eta se preserva sin cambios en update (solo toca mu).
    assert new_state.eta == state.eta
    # mu sí cambió (score != score_neutral ni mu fijo en el punto de equilibrio trivial)
    assert new_state.mu != state.mu


def test_step_endogenous_returns_new_instance_without_mutating_original(persona: PersonaParams) -> None:
    state = MoodState(mu=0.3, eta=0.4)
    rng = np.random.default_rng(6)
    new_state = mood.step_endogenous(state, persona, rng)

    assert new_state is not state
    assert isinstance(new_state, MoodState)
    # Original intacto.
    assert state.mu == 0.3
    assert state.eta == 0.4
    # mu se preserva sin cambios en step_endogenous (solo toca eta).
    assert new_state.mu == state.mu
    assert new_state.eta != state.eta


# ---------------------------------------------------------------------------
# 6. Determinismo: mismo Generator (misma semilla) => mismo M


def test_step_deterministic_with_same_seed(persona: PersonaParams) -> None:
    state = MoodState(mu=0.15, eta=-0.05)

    rng_a = np.random.default_rng(31415)
    rng_b = np.random.default_rng(31415)

    result_a = mood.step(state, persona, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_a)
    result_b = mood.step(state, persona, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_b)

    assert result_a == result_b


def test_step_deterministic_with_same_seed_finite_nu(persona: PersonaParams) -> None:
    """Mismo criterio que el anterior pero con nu finita (ejercita rng.beta
    además de rng.binomial) para no dejar sin cubrir la rama beta-binomial."""
    params_finite_nu = dataclasses.replace(persona, nu=8.0)
    state = MoodState(mu=0.15, eta=-0.05)

    rng_a = np.random.default_rng(2718)
    rng_b = np.random.default_rng(2718)

    result_a = mood.step(state, params_finite_nu, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_a)
    result_b = mood.step(state, params_finite_nu, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_b)

    assert result_a == result_b


def test_step_different_seeds_generally_differ(persona: PersonaParams) -> None:
    """Chequeo de cordura complementario al determinismo: semillas distintas
    deben (típicamente) producir secuencias de M distintas. Se comparan 30
    draws consecutivos entre dos rngs con semillas distintas; con N=10 y p
    intermedio la probabilidad de que las 30 secuencias coincidan por azar es
    despreciable, así que no es un test flaky en la práctica."""
    state = MoodState(mu=0.0, eta=0.0)
    rng_a = np.random.default_rng(1)
    rng_b = np.random.default_rng(2)

    Ms_a = [mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=rng_a)[0] for _ in range(30)]
    Ms_b = [mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=rng_b)[0] for _ in range(30)]

    assert Ms_a != Ms_b


# ---------------------------------------------------------------------------
# Extras: logit/sigmoid (utilidades base) — no pedidos explícitamente en la
# lista de aceptación pero triviales de cubrir y usados por todo lo demás.


def test_sigmoid_logit_are_inverse() -> None:
    for p in (0.001, 0.1, 0.35, 0.5, 0.62, 0.9, 0.999):
        assert mood.sigmoid(mood.logit(p)) == pytest.approx(p, abs=1e-9)


def test_sigmoid_stable_for_large_magnitude() -> None:
    # No debe producir overflow ni nan; debe saturar a 0/1 dentro de eps float.
    assert mood.sigmoid(1000.0) == pytest.approx(1.0, abs=1e-12)
    assert mood.sigmoid(-1000.0) == pytest.approx(0.0, abs=1e-12)
    assert math.isfinite(mood.sigmoid(1000.0))
    assert math.isfinite(mood.sigmoid(-1000.0))


def test_sigmoid_zero_is_half() -> None:
    assert mood.sigmoid(0.0) == pytest.approx(0.5, abs=1e-12)
