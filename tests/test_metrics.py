"""Tests de aceptación para sim.metrics (W1.5).

Cada métrica se valida contra una serie sintética con valor CONOCIDO,
construida a mano o analíticamente (no se comparan resultados de la propia
implementación contra sí misma).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from sim.metrics import (
    autocorr_lag1,
    daily_rate,
    envelope_violations,
    gap_stats,
    hourly_histogram,
    mean_sd,
    reversion_days,
    var_ratio_by_gain,
)


class TestMeanSd:
    """Test 1: mean_sd sobre una serie fija corta calculada a mano."""

    def test_known_series(self):
        """x = [2,4,4,4,5,5,7,9] -> mean=5.0, sd (ddof=1) = 2.138089935299395.

        Cálculo a mano: media = 40/8 = 5. Desviaciones al cuadrado:
        9+1+1+1+0+0+4+16 = 32; sd = sqrt(32/7) = 2.1380899...
        """
        x = np.array([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        mean, sd = mean_sd(x)
        assert mean == pytest.approx(5.0, abs=1e-12)
        assert sd == pytest.approx(math.sqrt(32.0 / 7.0), abs=1e-12)

    def test_constant_series_zero_sd(self):
        """Serie constante tiene sd = 0."""
        x = np.full(10, 3.5)
        mean, sd = mean_sd(x)
        assert mean == pytest.approx(3.5)
        assert sd == pytest.approx(0.0, abs=1e-12)


class TestAutocorrLag1:
    """Test 2: AR(1) simulada, i.i.d., y serie alternante determinista."""

    def test_ar1_phi_0_6(self):
        """AR(1) x[t] = 0.6*x[t-1] + eps[t], eps ~ N(0,1), n=20000, semilla fija.

        Autocorrelación teórica lag-1 de un AR(1) es phi=0.6; con n grande el
        estimador Pearson converge a ese valor.
        """
        rng = np.random.default_rng(42)
        n = 20000
        phi = 0.6
        eps = rng.normal(0.0, 1.0, n)
        x = np.zeros(n)
        for t in range(1, n):
            x[t] = phi * x[t - 1] + eps[t]

        result = autocorr_lag1(x)
        assert result == pytest.approx(0.6, abs=0.03)

    def test_iid_series_near_zero(self):
        """Serie i.i.d. (sin estructura temporal) tiene autocorrelación ~0."""
        rng = np.random.default_rng(123)
        x = rng.normal(0.0, 1.0, 20000)
        result = autocorr_lag1(x)
        assert result == pytest.approx(0.0, abs=0.03)

    def test_alternating_series_minus_one(self):
        """[1,-1,1,-1,...] es perfectamente anticorrelada en lag-1: exactamente -1."""
        x = np.array([1.0, -1.0] * 10)
        result = autocorr_lag1(x)
        assert result == pytest.approx(-1.0, abs=1e-12)


class TestVarRatioByGain:
    """Test 3: M con var 4 donde g=2 y var 1 donde g=1 (dos bloques)."""

    def test_two_block_variance_ratio(self):
        """Bloque g=1 con var(M)=1, bloque g=2 con var(M)=4 -> ratio ~4.

        Con q=0.25 default y solo dos valores de g, el cuartil inferior
        (g <= Q(0.25)) selecciona exactamente el bloque g=1 y el cuartil
        superior (g >= Q(0.75)) selecciona exactamente el bloque g=2.
        Tolerancia 25% (n grande, pero var muestral tiene ruido).
        """
        rng = np.random.default_rng(123)
        n_block = 5000
        g = np.concatenate([np.full(n_block, 1.0), np.full(n_block, 2.0)])
        M = np.concatenate(
            [
                rng.normal(0.0, 1.0, n_block),  # sd=1 -> var=1
                rng.normal(0.0, 2.0, n_block),  # sd=2 -> var=4
            ]
        )
        ratio = var_ratio_by_gain(M, g, q=0.25)
        assert ratio == pytest.approx(4.0, rel=0.25)

    def test_no_amplification_ratio_near_one(self):
        """Si g no afecta la varianza de M, el ratio debe rondar 1."""
        rng = np.random.default_rng(456)
        n = 10000
        g = rng.uniform(0.5, 1.5, n)
        M = rng.normal(0.0, 1.0, n)
        ratio = var_ratio_by_gain(M, g, q=0.25)
        assert ratio == pytest.approx(1.0, abs=0.25)


class TestReversionDays:
    """Test 4: serie determinista mu[t] = mu0*rho^(t-t_shock), rho=0.7."""

    def test_deterministic_ar1_decay_rounds_to_three(self):
        """e-folding teorico -1/ln(0.7) ~= 2.8037; primer entero bajo el umbral

        1/e del pico es t-t_peak=3 (dev en t_peak+3 es 0.7^3=0.343 del pico,
        que es <= 1/e ~= 0.3679; en t_peak+2, 0.7^2=0.49 > 1/e, no basta).
        """
        rho = 0.7
        mu0 = 10.0
        t_shock = 5
        n = 30
        mu = np.zeros(n)
        for t in range(t_shock, n):
            mu[t] = mu0 * rho ** (t - t_shock)

        result = reversion_days(mu, t_shock=t_shock, baseline=0.0)
        assert result == pytest.approx(3.0)

        theoretical = -1.0 / math.log(rho)
        assert theoretical == pytest.approx(2.8036732520571284, abs=1e-9)
        assert theoretical < result < theoretical + 1.0

    def test_never_reverts_returns_inf(self):
        """Serie que se aleja del baseline sin volver nunca -> inf."""
        t_shock = 2
        n = 20
        mu = np.zeros(n)
        for t in range(t_shock, n):
            mu[t] = 1.0 + 0.1 * (t - t_shock)  # grows without bound

        result = reversion_days(mu, t_shock=t_shock, baseline=0.0)
        assert math.isinf(result)

    def test_peak_at_shock_immediate_full_reversion(self):
        """Si el pico ocurre justo en t_shock y el siguiente valor ya cae
        bajo el umbral, t-t_peak debe ser el primer entero que cumple."""
        # dev: [5, 5, 1] from t_shock=0: peak at t=0, threshold 5/e ~= 1.839.
        mu = np.array([5.0, 5.0, 1.0])
        result = reversion_days(mu, t_shock=0, baseline=0.0)
        assert result == pytest.approx(2.0)


class TestDailyRate:
    """Test 5: daily_rate caso trivial exacto."""

    def test_trivial_case(self):
        """10 eventos en 5 dias -> 2.0 eventos/dia."""
        times_h = np.arange(10) * 12.0  # evenly spaced
        assert daily_rate(times_h, horizon_days=5.0) == pytest.approx(2.0)

    def test_zero_events(self):
        """Sin eventos -> tasa 0."""
        times_h = np.array([])
        assert daily_rate(times_h, horizon_days=10.0) == pytest.approx(0.0)


class TestGapStats:
    """Test 6: gaps construidos a mano [1,1,1,9]."""

    def test_hand_built_gaps(self):
        """4 eventos en t=[0,1,2,3,12] -> gaps=[1,1,1,9].

        A mano: mean=(1+1+1+9)/4=3; sorted=[1,1,1,9], median=(1+1)/2=1;
        sd(ddof=1)=sqrt(((1-3)^2*3+(9-3)^2)/3)=sqrt((4*3+36)/3)=sqrt(48/3)=4;
        cv=4/3; burstiness=(4-3)/(4+3)=1/7.
        Histograma bin_width=1 desde min=1: bins [1,2),[2,3),...,[8,9] (8
        bins); los tres gaps=1 caen en [1,2) (centro 1.5), el gap=9 en el
        ultimo bin [8,9] (centro 8.5). Bin mas poblado: [1,2) -> mode_h=1.5.
        """
        times_h = np.array([0.0, 1.0, 2.0, 3.0, 12.0])
        stats = gap_stats(times_h, bin_width_h=1.0)

        assert stats["mean_h"] == pytest.approx(3.0)
        assert stats["median_h"] == pytest.approx(1.0)
        assert stats["cv"] == pytest.approx(4.0 / 3.0)
        assert stats["burstiness"] == pytest.approx(1.0 / 7.0)
        assert stats["mode_h"] == pytest.approx(1.5)

    def test_unsorted_input_same_result(self):
        """El orden temporal de entrada no debe importar (se ordena antes de
        calcular los gaps)."""
        times_h = np.array([12.0, 0.0, 3.0, 1.0, 2.0])
        stats = gap_stats(times_h, bin_width_h=1.0)
        assert stats["mean_h"] == pytest.approx(3.0)
        assert stats["median_h"] == pytest.approx(1.0)

    def test_poisson_like_burstiness_near_zero(self):
        """Proceso de Poisson (gaps exponenciales, cv=1) -> burstiness ~ 0."""
        rng = np.random.default_rng(999)
        n = 20000
        gaps = rng.exponential(scale=2.0, size=n)
        times_h = np.concatenate([[0.0], np.cumsum(gaps)])
        stats = gap_stats(times_h, bin_width_h=0.5)
        assert stats["burstiness"] == pytest.approx(0.0, abs=0.03)
        assert stats["cv"] == pytest.approx(1.0, abs=0.03)


class TestHourlyHistogram:
    """Test 7: eventos en horas conocidas, incluyendo t>24 (wrap)."""

    def test_hand_placed_events_with_wrap(self):
        """Eventos en horas locales: 0(x2 via wrap), 5, 13, 13, 23.

        times_h = [0.5, 24.5, 5.0, 13.0, 37.0, 23.9]
        -> hora local (t%24) = [0.5, 0.5, 5.0, 13.0, 13.0, 23.9]
        -> bin 0 (hora [0,1)): 2 eventos (0.5 y 24.5%24=0.5)
        -> bin 5: 1 evento (5.0)
        -> bin 13: 2 eventos (13.0 y 37.0%24=13.0)
        -> bin 23: 1 evento (23.9)
        resto de bins: 0. Total 6 eventos, shape (24,).
        """
        times_h = np.array([0.5, 24.5, 5.0, 13.0, 37.0, 23.9])
        hist = hourly_histogram(times_h, bins=24)

        assert hist.shape == (24,)
        expected = np.zeros(24, dtype=hist.dtype)
        expected[0] = 2
        expected[5] = 1
        expected[13] = 2
        expected[23] = 1
        np.testing.assert_array_equal(hist, expected)
        assert hist.sum() == 6

    def test_custom_bins_count(self):
        """bins=4 agrupa el dia en cuartos de 6h; shape debe respetarlo."""
        times_h = np.array([1.0, 7.0, 13.0, 19.0])  # one per quarter
        hist = hourly_histogram(times_h, bins=4)
        assert hist.shape == (4,)
        np.testing.assert_array_equal(hist, np.array([1, 1, 1, 1]))


class TestEnvelopeViolations:
    """Test 8: envelope_fn escalon, eventos a ambos lados."""

    def test_step_envelope_exact_count(self):
        """Envelope = 0 (violacion) en [23,24)U[0,8) (quiet hours), 1 fuera.

        Eventos: 2 en horas permitidas (10, 15), 3 en quiet hours
        (0.5, 23.5, 7.9) -> 3 violaciones exactas.
        """

        def step_envelope(h: float) -> float:
            local = h % 24.0
            if local >= 23.0 or local < 8.0:
                return 0.0
            return 1.0

        times_h = np.array([10.0, 15.0, 0.5, 23.5, 7.9])
        count = envelope_violations(times_h, step_envelope, eps=1e-9)
        assert count == 3

    def test_no_violations_when_all_inside_envelope(self):
        """Si ningun evento cae en la zona prohibida, el conteo es 0."""

        def step_envelope(h: float) -> float:
            local = h % 24.0
            if local >= 23.0 or local < 8.0:
                return 0.0
            return 1.0

        times_h = np.array([9.0, 10.0, 20.0, 22.9])
        count = envelope_violations(times_h, step_envelope, eps=1e-9)
        assert count == 0


# B6: lane routing + fair probes (pure-logic units)


class TestTokensCovered:
    """Cobertura por token sobre textos (base de classify_chain y de la sonda
    justa RAW_HISTORY)."""

    def test_substring_match_case_insensitive(self):
        from experiments.cvs_common import _tokens_covered

        assert _tokens_covered(["ana", "friday"],
                               ["my sister ana arrived on Friday"]) == [True, True]
        assert _tokens_covered(["ana"], ["nothing here"]) == [False]

    def test_any_text_suffices(self):
        from experiments.cvs_common import _tokens_covered

        assert _tokens_covered(["guadalajara"],
                               ["other", "moving to Guadalajara soon"]) == [True]


class TestChainClassification:
    """Forma estándar §17.2 compartida por las lanes (episodios y raw)."""

    def test_levels(self):
        from experiments.cvs_common import _chain_classification

        chain = {"id": "c", "tokens": ("a", "b", "c")}
        assert _chain_classification(chain, [False, False, False]) == {
            "chain_id": "c", "events": 3, "covered": [False, False, False],
            "AnyEvidence": False, "LatestEvidence": False, "CompleteChain": False,
        }
        assert _chain_classification(chain, [True, False, False])["AnyEvidence"] is True
        assert _chain_classification(chain, [False, False, True])["LatestEvidence"] is True
        assert _chain_classification(chain, [True, True, True])["CompleteChain"] is True

    def test_empty_covered_does_not_crash(self):
        from experiments.cvs_common import _chain_classification

        cls = _chain_classification({"id": "c", "tokens": ()}, [])
        assert (cls["AnyEvidence"], cls["LatestEvidence"], cls["CompleteChain"]) == (
            False, False, False)


class TestRawHistoryWindow:
    """La ventana de la sonda justa: últimos N turnos persistidos con t_h' < t_q."""

    def test_window_returns_last_limit_turns_before_t(self, tmp_path):
        from harness.store import SQLiteStore
        from experiments.cvs_common import RAW_HISTORY_WINDOW_LIMIT, _raw_history_window

        store = SQLiteStore(str(tmp_path / "w.db"))
        for i in range(20):
            store.add_message("user", f"turn {i}", float(i), 0)
        window = _raw_history_window(store, 15.0)
        assert len(window) == RAW_HISTORY_WINDOW_LIMIT == 12
        # only turns with t_h' < 15, most recent 12, chronological order
        assert [text for _r, text in window] == [f"turn {i}" for i in range(3, 15)]
        assert _raw_history_window(store, 2.0) == (("user", "turn 0"), ("user", "turn 1"))
        store.close()

    def test_window_ignores_future_turns(self, tmp_path):
        from harness.store import SQLiteStore
        from experiments.cvs_common import _raw_history_window

        store = SQLiteStore(str(tmp_path / "w2.db"))
        store.add_message("user", "after", 100.0, 4)
        store.add_message("user", "before", 10.0, 0)
        window = _raw_history_window(store, 50.0)
        assert [text for _r, text in window] == ["before"]
        store.close()
