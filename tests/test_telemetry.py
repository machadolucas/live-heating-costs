"""Reject real recorder aggregate artifacts before estimating delivered heat."""

from datetime import UTC, datetime, timedelta

import _pure_imports  # noqa: F401 -- registers the isolated pure-module namespace
import pytest

# isort: split

from _live_heating_costs_unit.telemetry import (
    aggregate_energy,
    delivered_energy,
    validate_counter_delta,
)

T0 = datetime(2026, 9, 13, tzinfo=UTC)


def delta(a, b, seconds=300):
    return validate_counter_delta(a, b, T0, T0 + timedelta(seconds=seconds), 2.1)


def test_real_floor_aggregate_spike_reset_and_rebaseline():
    assert delta(100, 8481.205455, 3600).reason == "sampling_gap"
    bounded = validate_counter_delta(
        100, 8481.205455, T0, T0 + timedelta(hours=1), 2.1, maximum_gap_s=3600
    )
    assert bounded.reason == "implausible_increment"
    assert delta(100, 2).reason == "counter_reset"
    # After rejected interval caller uses the new baseline, not the old counter.
    assert delta(2, 2.1).delta_kwh == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("a", "b", "seconds", "reason"),
    [
        (None, 1, 300, "missing_counter"),
        (1, None, 300, "missing_counter"),
        (1, float("nan"), 300, "invalid_counter"),
        (1, 2, 0, "nonpositive_interval"),
        (1, 2, 601, "sampling_gap"),
        (-1, 2, 300, "invalid_counter"),
    ],
)
def test_rejected_data_stays_unknown(a, b, seconds, reason):
    result = delta(a, b, seconds)
    assert result.delta_kwh is None
    assert not result.valid
    assert result.reason == reason


def test_physical_bound_and_zero_are_elapsed_time_aware():
    assert delta(1, 1).delta_kwh == 0
    assert delta(1, 1.18).valid
    assert not delta(1, 1.2).valid
    with pytest.raises(ValueError):
        validate_counter_delta(1, 1.1, T0.replace(tzinfo=None), T0, 2)


def test_unknown_cop_is_never_resistive_and_pump_heat_remains_modelled():
    pump = delivered_energy("pump", delta(1, 1.1), 4)
    floor = delivered_energy("floor", delta(2, 2.1), 1, resistive=True)
    assert pump.provenance == "modelled"
    assert floor.provenance == "measured"
    assert not delivered_energy("pump", delta(1, 1.1), None).valid
    result = aggregate_energy([pump, floor], ["pump", "floor"])
    assert result.valid
    assert result.delivered_kwh == pytest.approx(0.5)
    assert result.electric_kwh == pytest.approx(0.2)
    assert result.measured_heat_kwh == pytest.approx(0.1)
    assert result.modelled_heat_kwh == pytest.approx(0.4)


def test_incomplete_aggregate_reports_coverage_without_fabricated_total():
    pump = delivered_energy("pump", delta(1, 1.1), 4)
    result = aggregate_energy([pump], ["pump", "floor"])
    assert result.coverage == 0.5
    assert result.delivered_kwh is None
    assert result.electric_kwh is None
    assert result.missing == ("floor",)
    with pytest.raises(ValueError):
        aggregate_energy([pump, pump], ["pump"])
