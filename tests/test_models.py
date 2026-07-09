"""Unit tests for models.py (pure, no Home Assistant)."""

from __future__ import annotations

import pytest

from custom_components.live_heating_costs.const import (
    ACTIVITY_MODE_DURATION,
    ACTIVITY_MODE_EVENT,
    ACTIVITY_MODE_NONE,
    CONF_ACTIVE_OUTPUT_KW,
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVITY_MODE,
    CONF_CAPACITY_KW,
    CONF_CAPACITY_POINTS,
    CONF_COP,
    CONF_COP_POINTS,
    CONF_EFFICIENCY,
    CONF_ENERGY_DENSITY,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_PER_EVENT_KWH,
    CONF_FUEL_UNIT,
    CONF_MAX_COP,
    CONF_MIN_COP,
    CONF_NAME,
    CONF_OPERATING_LIMIT_C,
    CONF_POWER_ENTITY,
    CONF_PRESET,
    CONF_PRICE_PER_UNIT,
    CONF_SOURCE_TYPE,
    DEFAULT_CAPACITY_KW,
    DEFAULT_EFFICIENCY,
    DEFAULT_ENERGY_DENSITY_M3,
    DEFAULT_FIXED_COP,
    FUEL_UNIT_KWH,
    FUEL_UNIT_M3,
    SOURCE_TYPE_CURVE,
    SOURCE_TYPE_DYNAMIC_FIXED,
    SOURCE_TYPE_STATIC,
)
from custom_components.live_heating_costs.cost_engine import wood_cost_eur_per_kwh
from custom_components.live_heating_costs.models import (
    ConfigError,
    CurveSourceConfig,
    DynamicFixedConfig,
    StaticSourceConfig,
    source_from_subentry,
)

# --------------------------------------------------------------------------- #
# static (wood) sources
# --------------------------------------------------------------------------- #


def test_static_source_round_trip_with_defaults():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_PRICE_PER_UNIT: 132.0,
    }
    cfg = source_from_subentry(data)

    assert isinstance(cfg, StaticSourceConfig)
    assert cfg.name == "Wood"
    assert cfg.capacity_kw == DEFAULT_CAPACITY_KW
    assert cfg.fuel_unit == FUEL_UNIT_M3
    assert cfg.energy_density_kwh_per_unit == DEFAULT_ENERGY_DENSITY_M3
    assert cfg.efficiency == DEFAULT_EFFICIENCY
    assert cfg.activity.mode == ACTIVITY_MODE_NONE
    assert cfg.activity.entity is None

    expected = wood_cost_eur_per_kwh(132.0, DEFAULT_ENERGY_DENSITY_M3, DEFAULT_EFFICIENCY)
    assert cfg.cost_eur_per_kwh() == pytest.approx(expected)


def test_static_source_explicit_fields_and_price_override():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_CAPACITY_KW: 8.0,
        CONF_FUEL_UNIT: FUEL_UNIT_KWH,
        CONF_PRICE_PER_UNIT: 0.08,
        CONF_ENERGY_DENSITY: 1.0,
        CONF_EFFICIENCY: 0.9,
    }
    cfg = source_from_subentry(data)
    assert cfg.capacity_kw == 8.0
    assert cfg.fuel_unit == FUEL_UNIT_KWH
    assert cfg.energy_density_kwh_per_unit == 1.0
    assert cfg.efficiency == 0.9
    assert cfg.cost_eur_per_kwh() == pytest.approx(0.08 / (1.0 * 0.9))
    assert cfg.cost_eur_per_kwh(price_override=0.16) == pytest.approx(0.16 / (1.0 * 0.9))


def test_static_source_duration_activity_mode():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_PRICE_PER_UNIT: 132.0,
        CONF_ACTIVITY_MODE: ACTIVITY_MODE_DURATION,
        CONF_ACTIVITY_ENTITY: "binary_sensor.stove_active",
        CONF_ACTIVE_OUTPUT_KW: 4.0,
    }
    cfg = source_from_subentry(data)
    assert cfg.activity.mode == ACTIVITY_MODE_DURATION
    assert cfg.activity.entity == "binary_sensor.stove_active"
    assert cfg.activity.active_output_kw == pytest.approx(4.0)


def test_static_source_duration_activity_missing_entity_raises():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_PRICE_PER_UNIT: 132.0,
        CONF_ACTIVITY_MODE: ACTIVITY_MODE_DURATION,
        CONF_ACTIVE_OUTPUT_KW: 4.0,
    }
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_ACTIVITY_ENTITY


def test_static_source_event_activity_mode():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_PRICE_PER_UNIT: 132.0,
        CONF_ACTIVITY_MODE: ACTIVITY_MODE_EVENT,
        CONF_ACTIVITY_ENTITY: "binary_sensor.stove_active",
        CONF_ENERGY_PER_EVENT_KWH: 3.5,
    }
    cfg = source_from_subentry(data)
    assert cfg.activity.mode == ACTIVITY_MODE_EVENT
    assert cfg.activity.energy_per_event_kwh == pytest.approx(3.5)


def test_static_source_event_activity_missing_energy_raises():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_PRICE_PER_UNIT: 132.0,
        CONF_ACTIVITY_MODE: ACTIVITY_MODE_EVENT,
        CONF_ACTIVITY_ENTITY: "binary_sensor.stove_active",
    }
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_ENERGY_PER_EVENT_KWH


@pytest.mark.parametrize("efficiency", [0.0, -0.2, 1.5])
def test_static_source_efficiency_out_of_range_raises(efficiency):
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: "Wood",
        CONF_PRICE_PER_UNIT: 132.0,
        CONF_EFFICIENCY: efficiency,
    }
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_EFFICIENCY


def test_static_source_missing_name_raises():
    data = {CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC, CONF_PRICE_PER_UNIT: 132.0}
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_NAME


def test_static_source_missing_price_raises():
    data = {CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC, CONF_NAME: "Wood"}
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_PRICE_PER_UNIT


# --------------------------------------------------------------------------- #
# dynamic_fixed (resistive) sources
# --------------------------------------------------------------------------- #


def test_dynamic_fixed_round_trip_with_defaults():
    data = {CONF_SOURCE_TYPE: SOURCE_TYPE_DYNAMIC_FIXED, CONF_NAME: "Resistive"}
    cfg = source_from_subentry(data)
    assert isinstance(cfg, DynamicFixedConfig)
    assert cfg.name == "Resistive"
    assert cfg.capacity_kw == DEFAULT_CAPACITY_KW
    assert cfg.cop == DEFAULT_FIXED_COP


def test_dynamic_fixed_explicit_fields():
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_DYNAMIC_FIXED,
        CONF_NAME: "Resistive",
        CONF_CAPACITY_KW: 12.0,
        CONF_COP: 1.0,
    }
    cfg = source_from_subentry(data)
    assert cfg.capacity_kw == 12.0
    assert cfg.cop == 1.0


@pytest.mark.parametrize("cop", [0.0, -1.0])
def test_dynamic_fixed_non_positive_cop_raises(cop):
    data = {CONF_SOURCE_TYPE: SOURCE_TYPE_DYNAMIC_FIXED, CONF_NAME: "Resistive", CONF_COP: cop}
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_COP


# --------------------------------------------------------------------------- #
# curve (heat pump) sources
# --------------------------------------------------------------------------- #


def _curve_data(**overrides) -> dict:
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_CURVE,
        CONF_NAME: "Old ILP",
        CONF_POWER_ENTITY: "sensor.fireplace_room_ilp_meter_power",
        CONF_ENERGY_ENTITY: "sensor.fireplace_room_ilp_meter_energy",
        CONF_COP_POINTS: [[-30, 1.35], [7, 4.2], [13, 3.8]],
        CONF_CAPACITY_POINTS: [[-30, 1.5], [7, 6.3]],
        CONF_OPERATING_LIMIT_C: -28.0,
        CONF_MIN_COP: 0.5,
        CONF_MAX_COP: 6.5,
    }
    data.update(overrides)
    return data


def test_curve_source_round_trip_with_explicit_points():
    cfg = source_from_subentry(_curve_data())
    assert isinstance(cfg, CurveSourceConfig)
    assert cfg.name == "Old ILP"
    assert cfg.preset_key is None
    assert cfg.cop_points == ((-30.0, 1.35), (7.0, 4.2), (13.0, 3.8))
    assert cfg.capacity_points == ((-30.0, 1.5), (7.0, 6.3))
    assert cfg.operating_limit_c == -28.0
    assert cfg.cop_curve.value_at(7.0) == pytest.approx(4.2)
    assert cfg.capacity_curve.value_at(-30.0) == pytest.approx(1.5)


def test_curve_source_defaults_applied():
    cfg = source_from_subentry(_curve_data())
    assert cfg.climate_entity is None
    assert cfg.pump_temp_entity is None
    assert cfg.temp_entity_override is None
    assert cfg.standby_w > 0
    assert cfg.blend_window_min > 0


def test_curve_source_resolves_preset_points_when_omitted():
    data = _curve_data(preset="msz_fd25va")
    for key in (CONF_COP_POINTS, CONF_CAPACITY_POINTS, CONF_OPERATING_LIMIT_C):
        data.pop(key, None)
    cfg = source_from_subentry(data)
    assert cfg.preset_key == "msz_fd25va"
    assert cfg.cop_curve.value_at(-15.0) == pytest.approx(2.15)
    assert cfg.operating_limit_c == pytest.approx(-28.0)


def test_curve_source_unknown_preset_raises():
    data = _curve_data(preset="not_a_real_preset")
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_PRESET


def test_curve_source_missing_power_entity_raises():
    data = _curve_data()
    del data[CONF_POWER_ENTITY]
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_POWER_ENTITY


def test_curve_source_missing_energy_entity_raises():
    data = _curve_data()
    del data[CONF_ENERGY_ENTITY]
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_ENERGY_ENTITY


def test_curve_source_missing_points_without_preset_raises():
    data = _curve_data()
    del data[CONF_COP_POINTS]
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_COP_POINTS


def test_curve_source_duplicate_x_points_raises():
    data = _curve_data(cop_points=[[-30, 1.35], [-30, 2.0], [7, 4.2]])
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_COP_POINTS


def test_curve_source_max_cop_must_exceed_min_cop():
    data = _curve_data(min_cop=5.0, max_cop=4.0)
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry(data)
    assert exc_info.value.field == CONF_MAX_COP


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def test_source_from_subentry_unknown_type_raises():
    with pytest.raises(ConfigError) as exc_info:
        source_from_subentry({CONF_SOURCE_TYPE: "not_a_type", CONF_NAME: "X"})
    assert exc_info.value.field == CONF_SOURCE_TYPE
