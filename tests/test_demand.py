"""Unit tests for degree-hour integration and UA-based demand (pure, no HA)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _pure_imports  # noqa: F401 -- registers the isolated pure-module namespace
import pytest

# isort: split

from _live_heating_costs_unit.demand import (
    daily_energy_kwh,
    instant_demand_kw,
    integrate_degree_hours,
)

T0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def test_constant_temp_below_base_is_exact_rectangle():
    base = 17.0
    samples = [(T0, 7.0), (T0 + timedelta(hours=24), 7.0)]
    dh = integrate_degree_hours(samples, base, T0, T0 + timedelta(hours=24))
    assert dh == pytest.approx(240.0)  # (17-7) * 24h


def test_linear_ramp_crossing_base_counts_only_positive_triangle():
    base = 17.0
    start = T0
    end = T0 + timedelta(hours=2)
    # deficit goes from +2 (T=15) to -2 (T=19), crossing zero at the midpoint.
    samples = [(start, 15.0), (end, 19.0)]
    dh = integrate_degree_hours(samples, base, start, end)
    # Triangle: base 1h, height 2 => 0.5 * 1 * 2 = 1.0 (analytic).
    assert dh == pytest.approx(1.0)


def test_samples_beyond_boundaries_are_clipped_via_interpolation():
    base = 17.0
    # T(t) = 5 + 4*hours_since(T0 - 60min); samples at -60, +30, +90, +240 min.
    samples = [
        (T0, 5.0),
        (T0 + timedelta(minutes=90), 11.0),
        (T0 + timedelta(minutes=150), 15.0),
        (T0 + timedelta(minutes=240), 21.0),
    ]
    start = T0 + timedelta(minutes=60)
    end = T0 + timedelta(minutes=180)
    dh = integrate_degree_hours(samples, base, start, end)
    # T(h=1)=9 (deficit 8), T(h=3)=17 (deficit 0); linear throughout [1h,3h] =>
    # trapezoid (8+0)/2 * 2h = 8.0 (analytic, matches piecewise interpolation).
    assert dh == pytest.approx(8.0)


def test_above_base_throughout_is_zero():
    base = 17.0
    samples = [(T0, 20.0), (T0 + timedelta(hours=5), 25.0)]
    dh = integrate_degree_hours(samples, base, T0, T0 + timedelta(hours=5))
    assert dh == 0.0


def test_fewer_than_two_samples_is_zero():
    assert integrate_degree_hours([], 17.0, T0, T0 + timedelta(hours=1)) == 0.0
    assert integrate_degree_hours([(T0, 5.0)], 17.0, T0, T0 + timedelta(hours=1)) == 0.0


def test_degenerate_window_is_zero():
    samples = [(T0, 5.0), (T0 + timedelta(hours=1), 6.0)]
    assert integrate_degree_hours(samples, 17.0, T0, T0) == 0.0  # end == start


def test_naive_datetime_rejected():
    naive = datetime(2026, 1, 15, 0, 0)
    with pytest.raises(ValueError):
        integrate_degree_hours([(T0, 5.0), (T0 + timedelta(hours=1), 6.0)], 17.0, naive, T0)


def test_instant_demand_kw():
    assert instant_demand_kw(0.14, 7.0, 17.0) == pytest.approx(1.4)  # 0.14 * 10
    assert instant_demand_kw(0.14, 20.0, 17.0) == 0.0  # above base


def test_daily_energy_kwh():
    assert daily_energy_kwh(0.14, 240.0) == pytest.approx(33.6)
