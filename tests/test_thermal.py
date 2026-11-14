"""Physical balances, storage and quality gates for the bounded shadow RC model."""

from datetime import UTC, datetime, timedelta

import _pure_imports  # noqa: F401 -- registers the isolated pure-module namespace
import pytest

# isort: split

from _live_heating_costs_unit.thermal import (
    DispatchSource,
    ThermalInputs,
    ThermalParameters,
    ThermalState,
    WeightedIndoorReading,
    advance,
    aggregate_indoor,
    allocate_headroom,
    target_demand,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PARAMS = ThermalParameters(0.2, 0.3, 8, 0.5)


def test_steady_state_and_serialization():
    state = ThermalState(21, 21)
    result = advance(state, PARAMS, ThermalInputs(1, 4), T0, T0 + timedelta(hours=1))
    assert result.state.air_c == pytest.approx(21)
    assert result.state.fabric_c == pytest.approx(21)
    assert result.storage_change_kwh == pytest.approx(0, abs=1e-10)
    assert result.loss_kwh == pytest.approx(4)
    assert ThermalParameters.from_dict(PARAMS.to_dict()) == PARAMS
    assert ThermalState.from_dict(state.to_dict()) == state


def test_energy_balance_counts_air_and_fabric_storage_once():
    result = advance(
        ThermalState(18, 17),
        PARAMS,
        ThermalInputs(-10, 5, 0.4, 0.1, -0.2, 1.5),
        T0,
        T0 + timedelta(hours=1),
    )
    assert result.heat_kwh == pytest.approx(6.5)
    assert result.heat_kwh + result.gains_kwh + result.ventilation_kwh == pytest.approx(
        result.loss_kwh + result.storage_change_kwh, abs=1e-9
    )


def test_fabric_heating_is_slower_than_direct_air_heating():
    state = ThermalState(20, 20)
    end = T0 + timedelta(minutes=5)
    direct = advance(state, PARAMS, ThermalInputs(20, heat_kw=3), T0, end)
    fabric = advance(state, PARAMS, ThermalInputs(20, fabric_heat_kw=3), T0, end)
    assert fabric.state.air_c < direct.state.air_c
    assert fabric.state.fabric_c > direct.state.fabric_c


def test_target_solves_forward_balance_and_unknown_heat_stays_unknown():
    state = ThermalState(20, 19)
    result = target_demand(state, PARAMS, ThermalInputs(0), T0, current_heat_kw=2)
    assert result.predicted_air_c == pytest.approx(21, abs=1e-7)
    assert result.total_kw == pytest.approx(result.continuation_kw + result.remaining_kw)
    unknown = target_demand(state, PARAMS, ThermalInputs(0), T0)
    assert unknown.remaining_kw is None
    assert unknown.continuation_kw is None
    warm = target_demand(state, PARAMS, ThermalInputs(0, ventilation_kw=2), T0)
    assert warm.total_kw < unknown.total_kw


def test_committed_fabric_heat_is_exposed_and_not_added_to_remaining():
    result = target_demand(
        ThermalState(20, 20), PARAMS, ThermalInputs(0, fabric_heat_kw=1), T0, current_heat_kw=0
    )
    assert result.total_kw == pytest.approx(result.remaining_kw + 1)
    assert result.fabric_heat_kw == 1
    assert result.air_heat_kw == result.remaining_kw


def test_unreachable_target_and_hot_house_do_not_invent_capacity():
    result = target_demand(
        ThermalState(10, 10), PARAMS, ThermalInputs(-30), T0, max_heat_kw=1, current_heat_kw=0
    )
    assert not result.target_reachable
    assert result.total_kw == 1
    assert target_demand(ThermalState(30, 30), PARAMS, ThermalInputs(25), T0).total_kw == 0


def test_stiff_model_stable_and_invalid_windows_rejected():
    tiny = ThermalParameters(10, 0.001, 0.001, 100)
    assert (
        advance(
            ThermalState(25, 25), tiny, ThermalInputs(0), T0, T0 + timedelta(hours=1)
        ).state.air_c
        >= 0
    )
    for end in (T0 - timedelta(seconds=1), T0 + timedelta(hours=7), T0.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            advance(ThermalState(20, 20), PARAMS, ThermalInputs(0), T0, end)
    with pytest.raises(ValueError):
        ThermalParameters(0.2, 0, 8, 0.5)


def test_weighted_room_coverage_offset_stale_missing_and_membership():
    rows = [
        WeightedIndoorReading("a", 19, 3, 1, T0, True),
        WeightedIndoorReading("b", None, 1, observed_at=T0, calibrated=True),
    ]
    value = aggregate_indoor(rows, T0)
    assert value.temp_c == 20
    assert value.coverage == 0.75
    assert value.missing == ("b",)
    assert not value.calibrated
    assert aggregate_indoor(rows, T0, minimum_coverage=0.9).temp_c is None
    assert aggregate_indoor(rows, T0 + timedelta(hours=1)).temp_c is None
    assert aggregate_indoor([], T0).temp_c is None
    with pytest.raises(ValueError):
        aggregate_indoor([rows[0], rows[0]], T0)


def test_reserved_local_capacity_ineligible_sources_and_unknown_cost():
    result = allocate_headroom(
        5,
        [
            DispatchSource("pump", 5, 0.05, reserved_kw=2),
            DispatchSource("floor", 4, 0.03, eligible=False),
            DispatchSource("unknown", 50, None),
            DispatchSource("electric", 1, 0.1),
        ],
    )
    assert result.allocations == (("pump", 3), ("electric", 1))
    assert result.uncovered_kw == 1
