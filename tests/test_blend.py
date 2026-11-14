"""Unit tests for measured/modeled COP blending (pure, no Home Assistant).

The headline test (`test_idle_regression_never_floors_cop`) is the whole
reason this module exists: the legacy card computed COP = capacity / max(power,
floor), which pins an idle pump's COP to ~20-22 and its delivered-heat cost to
an absurd ~0.24 c/kWh. Idle must yield the pure *modeled* cost of running now.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _pure_imports  # noqa: F401 -- registers the isolated pure-module namespace
import pytest

# isort: split

from _live_heating_costs_unit.blend import (
    Sample,
    blend_cop,
    is_active,
    is_producing_heat,
    window_cop,
)
from _live_heating_costs_unit.const import DEFAULT_STANDBY_W, MIN_WINDOW_ENERGY_KWH

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)


def _series(n: int, **kwargs) -> list[Sample]:
    """n+1 one-minute-spaced samples; per-field overrides may be callables of i."""

    def _val(key, default, i):
        v = kwargs.get(key, default)
        return v(i) if callable(v) else v

    return [
        Sample(
            ts=T0 + timedelta(minutes=i),
            power_w=_val("power_w", 0.0, i),
            energy_kwh=_val("energy_kwh", 0.0, i),
            temp_c=_val("temp_c", 0.0, i),
            hvac_action=_val("hvac_action", None, i),
        )
        for i in range(n + 1)
    ]


# --------------------------------------------------------------------------- #
# The headline regression
# --------------------------------------------------------------------------- #


def test_idle_regression_never_floors_cop():
    """30 min idle: power ~2 W, flat counter, no climate telemetry, mild temp.

    Legacy bug: COP = capacity / max(power, 0.28 kW) => 6.3/0.28 ~= 22.5 =>
    cost = buy/22.5 ~= 0.24 c/kWh. Fixed behaviour: idle -> measured None ->
    blend collapses to the pure modeled COP.
    """
    samples = _series(30, power_w=2.0, energy_kwh=100.0, temp_c=15.0, hvac_action=None)

    assert is_active(samples[0], DEFAULT_STANDBY_W) is False
    assert is_producing_heat(samples[0], DEFAULT_STANDBY_W) is False

    wc = window_cop(samples, cop_at=lambda t: 3.8, standby_w=DEFAULT_STANDBY_W)
    assert wc.measured_cop is None
    assert wc.heating_fraction == 0.0
    assert wc.energy_kwh == 0.0

    modeled = 3.8
    blended = blend_cop(modeled, wc.measured_cop, wc.heating_fraction, 0.5, 6.5)
    assert blended == 3.8

    # The cost story: buy price / modeled COP, never buy price / floored-COP.
    buy_eur_per_kwh = 0.053  # ~5.3 c/kWh, the realistic all-in floor
    fixed_cost = buy_eur_per_kwh / blended  # ~1.39 c/kWh
    legacy_bug_cost = buy_eur_per_kwh / 22.5  # ~0.24 c/kWh
    assert fixed_cost > legacy_bug_cost  # the "bargain" was the bug, not real


# --------------------------------------------------------------------------- #
# window_cop behaviour
# --------------------------------------------------------------------------- #


def test_full_window_heating_matches_modeled_exactly():
    samples = _series(
        30,
        power_w=3000.0,
        energy_kwh=lambda i: 100.0 + 0.05 * i,
        temp_c=-5.0,
        hvac_action="heating",
    )
    modeled = 2.9
    wc = window_cop(samples, cop_at=lambda t: modeled, standby_w=DEFAULT_STANDBY_W)
    assert wc.heating_fraction == 1.0
    assert wc.measured_cop == pytest.approx(modeled)
    assert wc.energy_kwh == pytest.approx(30 * 0.05)

    blended = blend_cop(modeled, wc.measured_cop, wc.heating_fraction, 0.5, 6.5)
    assert blended == pytest.approx(modeled)


def test_full_window_heating_sorts_samples_defensively():
    samples = _series(
        30,
        power_w=3000.0,
        energy_kwh=lambda i: 100.0 + 0.05 * i,
        temp_c=-5.0,
        hvac_action="heating",
    )
    shuffled = list(reversed(samples))
    wc = window_cop(shuffled, cop_at=lambda t: 2.9, standby_w=DEFAULT_STANDBY_W)
    assert wc.heating_fraction == 1.0
    assert wc.measured_cop == pytest.approx(2.9)


def test_defrost_drag_pulls_measured_below_modeled():
    """Half the active samples are defrosting: electricity spent, zero credited heat."""
    hvac = lambda i: "heating" if i % 2 == 0 else "defrosting"  # noqa: E731
    samples = _series(
        30, power_w=3000.0, energy_kwh=lambda i: 100.0 + 0.05 * i, temp_c=-5.0, hvac_action=hvac
    )
    modeled = 2.9
    wc = window_cop(samples, cop_at=lambda t: modeled, standby_w=DEFAULT_STANDBY_W)
    # Both heating and defrosting count as "active", so the window is fully active...
    assert wc.heating_fraction == 1.0
    # ...but only the heating half's energy is credited with modeled heat output.
    assert wc.measured_cop is not None
    assert 0.0 < wc.measured_cop < modeled
    assert wc.measured_cop == pytest.approx(modeled / 2.0)


def test_mixed_idle_and_heating_window_blends_between_values():
    """First half of the window idle, second half heating steadily."""

    def hvac_of(i):
        return None if i < 15 else "heating"

    def power_of(i):
        return 2.0 if i < 15 else 3000.0

    def temp_of(i):
        return 15.0 if i < 15 else -5.0

    def energy_of(i):
        return 100.0 if i < 15 else 100.0 + 0.05 * (i - 15)

    samples = _series(
        30, power_w=power_of, energy_kwh=energy_of, temp_c=temp_of, hvac_action=hvac_of
    )

    measured_modeled = 2.5
    wc = window_cop(samples, cop_at=lambda t: measured_modeled, standby_w=DEFAULT_STANDBY_W)
    assert wc.heating_fraction == pytest.approx(0.5)
    assert wc.measured_cop == pytest.approx(measured_modeled)

    current_modeled = 4.0  # e.g. current outdoor temp differs from the window's average
    blended = blend_cop(current_modeled, wc.measured_cop, wc.heating_fraction, 0.5, 6.5)
    lo, hi = sorted((current_modeled, wc.measured_cop))
    assert lo < blended < hi


def test_counter_reset_makes_window_unusable():
    samples = [
        Sample(ts=T0, power_w=3000.0, energy_kwh=10.0, temp_c=-5.0, hvac_action="heating"),
        Sample(
            ts=T0 + timedelta(minutes=1),
            power_w=3000.0,
            energy_kwh=9.5,  # counter reset
            temp_c=-5.0,
            hvac_action="heating",
        ),
    ]
    wc = window_cop(samples, cop_at=lambda t: 2.9, standby_w=DEFAULT_STANDBY_W)
    assert wc.measured_cop is None
    assert wc.energy_kwh == 0.0


def test_energy_below_minimum_is_unusable():
    tiny_delta = MIN_WINDOW_ENERGY_KWH / 4.0
    samples = [
        Sample(ts=T0, power_w=500.0, energy_kwh=10.0, temp_c=-5.0, hvac_action="heating"),
        Sample(
            ts=T0 + timedelta(minutes=1),
            power_w=500.0,
            energy_kwh=10.0 + tiny_delta,
            temp_c=-5.0,
            hvac_action="heating",
        ),
    ]
    wc = window_cop(samples, cop_at=lambda t: 2.9, standby_w=DEFAULT_STANDBY_W)
    assert wc.measured_cop is None
    assert wc.energy_kwh == pytest.approx(tiny_delta)


def test_missing_energy_readings_are_skipped():
    samples = [
        Sample(ts=T0, power_w=3000.0, energy_kwh=0.0, temp_c=-5.0, hvac_action="heating"),
        Sample(
            ts=T0 + timedelta(minutes=1),
            power_w=3000.0,
            energy_kwh=None,
            temp_c=-5.0,
            hvac_action="heating",
        ),
        Sample(
            ts=T0 + timedelta(minutes=2),
            power_w=3000.0,
            energy_kwh=0.1,
            temp_c=-5.0,
            hvac_action="heating",
        ),
        Sample(
            ts=T0 + timedelta(minutes=3),
            power_w=3000.0,
            energy_kwh=0.2,
            temp_c=-5.0,
            hvac_action="heating",
        ),
    ]
    wc = window_cop(samples, cop_at=lambda t: 2.9, standby_w=DEFAULT_STANDBY_W)
    assert wc.heating_fraction == 1.0
    assert wc.measured_cop == pytest.approx(2.9)
    assert wc.energy_kwh == pytest.approx(0.1)


def test_sensibo_power_only_fallback_detects_heat():
    sample = Sample(ts=T0, power_w=1500.0, energy_kwh=0.0, temp_c=-5.0, hvac_action=None)
    assert is_active(sample, DEFAULT_STANDBY_W) is True
    assert is_producing_heat(sample, DEFAULT_STANDBY_W) is True


# --------------------------------------------------------------------------- #
# blend_cop clamping and validation
# --------------------------------------------------------------------------- #


def test_blend_cop_none_modeled_propagates_none():
    assert blend_cop(None, 5.0, 1.0, 0.5, 6.5) is None


def test_blend_cop_clamps_absurd_measured_high():
    assert blend_cop(4.0, 100.0, 1.0, 0.5, 6.5) == 6.5


def test_blend_cop_clamps_below_minimum():
    assert blend_cop(4.0, -5.0, 1.0, 0.5, 6.5) == 0.5


def test_blend_cop_clamps_modeled_only_case():
    assert blend_cop(50.0, None, 0.0, 0.5, 6.5) == 6.5


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        Sample(
            ts=datetime(2026, 1, 1, 0, 0), power_w=0.0, energy_kwh=0.0, temp_c=0.0, hvac_action=None
        )


def test_heating_segment_without_temperature_makes_window_unusable():
    """Heat produced at an unknown temperature can't be modeled; counting it as
    zero would fake a COP collapse — no measurement, the blend uses modeled."""
    samples = _series(
        30,
        power_w=800.0,
        energy_kwh=lambda i: 100.0 + i * 0.02,
        temp_c=lambda i: None if i == 10 else 0.0,
        hvac_action="heating",
    )
    wc = window_cop(samples, lambda t: 3.0, DEFAULT_STANDBY_W)
    assert wc.measured_cop is None
    assert wc.heating_fraction == pytest.approx(1.0)
    assert blend_cop(3.0, wc.measured_cop, wc.heating_fraction, 0.5, 6.5) == pytest.approx(3.0)


def test_defrost_segment_without_temperature_still_counts_as_zero_heat():
    """Defrost energy is known to make no heat, so a missing temperature there
    doesn't matter — the deliberate zero-numerator drag stays."""
    samples = _series(
        30,
        power_w=800.0,
        energy_kwh=lambda i: 100.0 + i * 0.02,
        temp_c=lambda i: None if i >= 20 else 0.0,
        hvac_action=lambda i: "defrosting" if i >= 20 else "heating",
    )
    wc = window_cop(samples, lambda t: 3.0, DEFAULT_STANDBY_W)
    assert wc.measured_cop == pytest.approx(3.0 * 20 / 30)


def test_idle_segment_without_temperature_is_harmless():
    samples = _series(
        30,
        power_w=lambda i: 2.0 if i >= 20 else 800.0,
        energy_kwh=lambda i: 100.0 + min(i, 20) * 0.03,
        temp_c=lambda i: None if i >= 20 else 0.0,
        hvac_action=None,
    )
    wc = window_cop(samples, lambda t: 3.0, DEFAULT_STANDBY_W)
    assert wc.measured_cop == pytest.approx(3.0)
