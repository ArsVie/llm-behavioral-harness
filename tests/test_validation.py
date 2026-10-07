"""Tests for engine/validation.py (W1.7)."""
from dataclasses import replace


from engine.types import (
    DEFAULT_PHASE_MULTIPLIERS,
    PersonaParams,
    TimingParams,
)
from engine.validation import check


class TestDefaultsValid:
    """Defaults (PersonaParams(), TimingParams()) must be valid."""

    def test_defaults_pass(self):
        """Unchanged defaults produce an empty list."""
        persona = PersonaParams()
        timing = TimingParams()
        errors = check(persona, timing)
        assert errors == []


class TestPersonaParamsBasic:
    """Basic PersonaParams tests (one field at a time)."""

    def test_N_valid(self):
        """N >= 1 is valid."""
        persona = replace(PersonaParams(), N=1)
        errors = check(persona, TimingParams())
        assert all("N" not in e for e in errors)

    def test_N_zero(self):
        """N = 0 violates N >= 1."""
        persona = replace(PersonaParams(), N=0)
        errors = check(persona, TimingParams())
        assert any("N" in e for e in errors)

    def test_lam_zero(self):
        """lam = 0 violates 0 < lam < 1."""
        persona = replace(PersonaParams(), lam=0.0)
        errors = check(persona, TimingParams())
        assert any("lam" in e for e in errors)

    def test_lam_one(self):
        """lam = 1 violates 0 < lam < 1."""
        persona = replace(PersonaParams(), lam=1.0)
        errors = check(persona, TimingParams())
        assert any("lam" in e for e in errors)

    def test_nu_zero(self):
        """nu = 0 violates nu > 0."""
        persona = replace(PersonaParams(), nu=0.0)
        errors = check(persona, TimingParams())
        assert any("nu" in e for e in errors)

    def test_nu_negative(self):
        """nu < 0 violates nu > 0."""
        persona = replace(PersonaParams(), nu=-1.0)
        errors = check(persona, TimingParams())
        assert any("nu" in e for e in errors)

    def test_k_nonnegative(self):
        """k >= 0 is valid."""
        persona = replace(PersonaParams(), k=0.0)
        errors = check(persona, TimingParams())
        assert all("k" not in e or "stability" in e for e in errors)

    def test_k_negative(self):
        """k < 0 violates k >= 0."""
        persona = replace(PersonaParams(), k=-0.1)
        errors = check(persona, TimingParams())
        assert any("k" in e for e in errors)

    def test_rho_valid_range(self):
        """rho in [0, 1) is valid."""
        persona = replace(PersonaParams(), rho=0.0)
        errors = check(persona, TimingParams())
# rho=0 can violate the stability bound; only rho is checked.
        assert all("rho" not in e or "stability" in e for e in errors)

    def test_rho_one(self):
        """rho = 1 violates 0 <= rho < 1."""
        persona = replace(PersonaParams(), rho=1.0)
        errors = check(persona, TimingParams())
        assert any("rho" in e for e in errors)

    def test_rho_e_negative(self):
        """rho_e < 0 violates 0 <= rho_e < 1."""
        persona = replace(PersonaParams(), rho_e=-0.1)
        errors = check(persona, TimingParams())
        assert any("rho_e" in e for e in errors)

    def test_sigma_e_nonnegative(self):
        """sigma_e >= 0 is valid."""
        persona = replace(PersonaParams(), sigma_e=0.0)
        errors = check(persona, TimingParams())
        assert all("sigma_e" not in e for e in errors)

    def test_sigma_e_negative(self):
        """sigma_e < 0 violates sigma_e >= 0."""
        persona = replace(PersonaParams(), sigma_e=-0.1)
        errors = check(persona, TimingParams())
        assert any("sigma_e" in e for e in errors)

    def test_B_nonnegative(self):
        """B >= 0 is valid."""
        persona = replace(PersonaParams(), B=0.0)
        errors = check(persona, TimingParams())
        assert all("B" not in e for e in errors)

    def test_B_negative(self):
        """B < 0 violates B >= 0."""
        persona = replace(PersonaParams(), B=-0.1)
        errors = check(persona, TimingParams())
        assert any("B" in e for e in errors)

    def test_A_nonnegative(self):
        """A >= 0 is valid."""
        persona = replace(PersonaParams(), A=0.0)
        errors = check(persona, TimingParams())
        assert all("A" not in e for e in errors)

    def test_A_negative(self):
        """A < 0 violates A >= 0."""
        persona = replace(PersonaParams(), A=-0.1)
        errors = check(persona, TimingParams())
        assert any("A" in e for e in errors)

    def test_sigma_eps_nonnegative(self):
        """sigma_eps >= 0 is valid."""
        persona = replace(PersonaParams(), sigma_eps=0.0)
        errors = check(persona, TimingParams())
        assert all("sigma_eps" not in e for e in errors)

    def test_sigma_eps_negative(self):
        """sigma_eps < 0 violates sigma_eps >= 0."""
        persona = replace(PersonaParams(), sigma_eps=-0.1)
        errors = check(persona, TimingParams())
        assert any("sigma_eps" in e for e in errors)

    def test_L_mean_positive(self):
        """L_mean > 0 is valid."""
        persona = replace(PersonaParams(), L_mean=1.0)
        errors = check(persona, TimingParams())
        assert all("L_mean" not in e for e in errors)

    def test_L_mean_zero(self):
        """L_mean = 0 violates L_mean > 0."""
        persona = replace(PersonaParams(), L_mean=0.0)
        errors = check(persona, TimingParams())
        assert any("L_mean" in e for e in errors)

    def test_L_sd_nonnegative(self):
        """L_sd >= 0 is valid."""
        persona = replace(PersonaParams(), L_sd=0.0)
        errors = check(persona, TimingParams())
        assert all("L_sd" not in e for e in errors)

    def test_L_sd_negative(self):
        """L_sd < 0 violates L_sd >= 0."""
        persona = replace(PersonaParams(), L_sd=-0.1)
        errors = check(persona, TimingParams())
        assert any("L_sd" in e for e in errors)


class TestStabilityBound:
    """Tests of the stability bound k < 2·(1−rho)/g_max."""

# Boundary tests set rho=0.7 explicitly (bound ≈ 0.44776).

    def test_stability_just_below_bound(self):
        """k = 0.447 just below the bound (rho=0.7), must pass."""
        persona = replace(PersonaParams(), rho=0.7, k=0.447)
        errors = check(persona, TimingParams())
        assert all("stability" not in e for e in errors)

    def test_stability_just_above_bound(self):
        """k = 0.44777 just above the bound (rho=0.7), must fail."""
        persona = replace(PersonaParams(), rho=0.7, k=0.44777)
        errors = check(persona, TimingParams())
        assert any("stability" in e for e in errors)

    def test_stability_exact_bound_violates(self):
        """k == exact bound must violate (strict inequality)."""
# Exact bound: 2 * (1 - 0.7) / (1 + 0.25 + 3*0.03)
        bound = 2 * (1 - 0.7) / (1 + 0.25 + 3 * 0.03)
        persona = replace(PersonaParams(), rho=0.7, k=bound)
        errors = check(persona, TimingParams())
        assert any("stability" in e for e in errors)

class TestTimingParamsBasic:
    """Basic TimingParams tests (one field at a time)."""

    def test_k_w_zero(self):
        """k_w = 0 violates k_w > 0."""
        timing = replace(TimingParams(), k_w=0.0)
        errors = check(PersonaParams(), timing)
        assert any("k_w" in e for e in errors)

    def test_theta_h_zero(self):
        """theta_h = 0 violates theta_h > 0."""
        timing = replace(TimingParams(), theta_h=0.0)
        errors = check(PersonaParams(), timing)
        assert any("theta_h" in e for e in errors)

    def test_peak_hour_zero(self):
        """peak_hour = 0 is valid."""
        timing = replace(TimingParams(), peak_hour=0.0)
        errors = check(PersonaParams(), timing)
        assert all("peak_hour" not in e for e in errors)

    def test_peak_hour_24(self):
        """peak_hour = 24 violates [0, 24)."""
        timing = replace(TimingParams(), peak_hour=24.0)
        errors = check(PersonaParams(), timing)
        assert any("peak_hour" in e for e in errors)

    def test_diurnal_amp_nonnegative(self):
        """diurnal_amp >= 0 is valid."""
        timing = replace(TimingParams(), diurnal_amp=0.0)
        errors = check(PersonaParams(), timing)
        assert all("diurnal_amp" not in e for e in errors)

    def test_diurnal_amp_negative(self):
        """diurnal_amp < 0 violates diurnal_amp >= 0."""
        timing = replace(TimingParams(), diurnal_amp=-0.1)
        errors = check(PersonaParams(), timing)
        assert any("diurnal_amp" in e for e in errors)


class TestQuietHours:
    """quiet_hours tests."""

    def test_quiet_hours_equal_invalid(self):
        """quiet_hours = (8, 8) invalid (equal)."""
        timing = replace(TimingParams(), quiet_hours=(8.0, 8.0))
        errors = check(PersonaParams(), timing)
        assert any("quiet_hours" in e for e in errors)

    def test_quiet_hours_ini_out_of_range(self):
        """quiet_hours = (25, 8) invalid (start out of [0, 24))."""
        timing = replace(TimingParams(), quiet_hours=(25.0, 8.0))
        errors = check(PersonaParams(), timing)
        assert any("quiet_hours" in e for e in errors)

    def test_quiet_hours_fin_out_of_range(self):
        """quiet_hours = (23, 25) invalid (end out of [0, 24))."""
        timing = replace(TimingParams(), quiet_hours=(23.0, 25.0))
        errors = check(PersonaParams(), timing)
        assert any("quiet_hours" in e for e in errors)

    def test_quiet_hours_normal_order_valid(self):
        """quiet_hours = (8, 12) valid (normal order, does not cross)."""
        timing = replace(TimingParams(), quiet_hours=(8.0, 12.0))
        errors = check(PersonaParams(), timing)
        assert all("quiet_hours" not in e for e in errors)


class TestPhaseMultipliers:
    """phase_multipliers tests."""

    def test_phase_multipliers_missing_phase(self):
        """Removing one phase is invalid."""
        phases = dict(DEFAULT_PHASE_MULTIPLIERS)
        del phases["menstrual"]
        timing = replace(TimingParams(), phase_multipliers=phases)
        errors = check(PersonaParams(), timing)
        assert any("phase_multipliers" in e for e in errors)

    def test_phase_multipliers_extra_phase(self):
        """Adding an extra key is invalid."""
        phases = dict(DEFAULT_PHASE_MULTIPLIERS)
        phases["extra_phase"] = 1.0
        timing = replace(TimingParams(), phase_multipliers=phases)
        errors = check(PersonaParams(), timing)
        assert any("phase_multipliers" in e for e in errors)

    def test_phase_multipliers_zero_value(self):
        """A multiplier = 0 violates > 0."""
        phases = dict(DEFAULT_PHASE_MULTIPLIERS)
        phases["menstrual"] = 0.0
        timing = replace(TimingParams(), phase_multipliers=phases)
        errors = check(PersonaParams(), timing)
        assert any("phase_multipliers" in e for e in errors)

    def test_phase_multipliers_negative_value(self):
        """A multiplier < 0 violates > 0."""
        phases = dict(DEFAULT_PHASE_MULTIPLIERS)
        phases["menstrual"] = -0.5
        timing = replace(TimingParams(), phase_multipliers=phases)
        errors = check(PersonaParams(), timing)
        assert any("phase_multipliers" in e for e in errors)


class TestAdjBounds:
    """adj_bounds tests."""

    def test_adj_bounds_lo_below_min(self):
        """adj_bounds = (0.4, 1.3) invalid (lo < 0.5)."""
        timing = replace(TimingParams(), adj_bounds=(0.4, 1.3))
        errors = check(PersonaParams(), timing)
        assert any("adj_bounds" in e for e in errors)

    def test_adj_bounds_hi_above_max(self):
        """adj_bounds = (0.7, 1.6) invalid (hi > 1.5)."""
        timing = replace(TimingParams(), adj_bounds=(0.7, 1.6))
        errors = check(PersonaParams(), timing)
        assert any("adj_bounds" in e for e in errors)

    def test_adj_bounds_inverted(self):
        """adj_bounds = (1.3, 0.7) invalid (lo > hi)."""
        timing = replace(TimingParams(), adj_bounds=(1.3, 0.7))
        errors = check(PersonaParams(), timing)
        assert any("adj_bounds" in e for e in errors)

    def test_adj_bounds_valid_inverted_fix(self):
        """adj_bounds = (0.5, 1.5) valid (the range endpoints)."""
        timing = replace(TimingParams(), adj_bounds=(0.5, 1.5))
        errors = check(PersonaParams(), timing)
        assert all("adj_bounds" not in e for e in errors)


class TestMinGapMin:
    """min_gap_min tests."""

    def test_min_gap_min_nonnegative(self):
        """min_gap_min >= 0 is valid."""
        timing = replace(TimingParams(), min_gap_min=0.0)
        errors = check(PersonaParams(), timing)
        assert all("min_gap_min" not in e for e in errors)

    def test_min_gap_min_negative(self):
        """min_gap_min < 0 violates min_gap_min >= 0."""
        timing = replace(TimingParams(), min_gap_min=-1.0)
        errors = check(PersonaParams(), timing)
        assert any("min_gap_min" in e for e in errors)


class TestDailyCap:
    """daily_cap tests."""

    def test_daily_cap_one(self):
        """daily_cap = 1 is valid."""
        timing = replace(TimingParams(), daily_cap=1)
        errors = check(PersonaParams(), timing)
        assert all("daily_cap" not in e for e in errors)

    def test_daily_cap_zero(self):
        """daily_cap = 0 violates daily_cap >= 1."""
        timing = replace(TimingParams(), daily_cap=0)
        errors = check(PersonaParams(), timing)
        assert any("daily_cap" in e for e in errors)


class TestMaxGapH:
    """Tests of max_gap_h > min_gap_min/60."""

    def test_max_gap_h_equal_violates(self):
        """max_gap_h = min_gap_min/60 violates the strict inequality."""
        timing = replace(TimingParams(), min_gap_min=60.0, max_gap_h=1.0)
        # 60.0 / 60 = 1.0, max_gap_h=1.0 <= 1.0 ✗
        errors = check(PersonaParams(), timing)
        assert any("max_gap_h" in e for e in errors)

    def test_max_gap_h_less_violates(self):
        """max_gap_h < min_gap_min/60 violates."""
        timing = replace(TimingParams(), min_gap_min=120.0, max_gap_h=1.0)
        # 120.0 / 60 = 2.0, max_gap_h=1.0 < 2.0 ✗
        errors = check(PersonaParams(), timing)
        assert any("max_gap_h" in e for e in errors)


class TestMultipleViolations:
    """Multiple simultaneous violation tests."""

    def test_multiple_both_params_violations(self):
        """Violations in both parameter sets are all reported."""
        persona = replace(PersonaParams(), lam=0.0, rho=1.0)
        timing = replace(TimingParams(), k_w=0.0)
        errors = check(persona, timing)
# Expect at least 3 errors
        assert len(errors) >= 3
