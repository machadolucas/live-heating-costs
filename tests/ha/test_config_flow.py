"""Config-flow tests: the hub (price/weather/demand) and heat-source subentry flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.live_heating_costs import curves, models
from custom_components.live_heating_costs.const import (
    DOMAIN,
    SOURCE_TYPE_CURVE,
    SOURCE_TYPE_DYNAMIC_FIXED,
    SOURCE_TYPE_STATIC,
    SUBENTRY_TYPE_SOURCE,
)

BUY = "sensor.buy_price"
OUTDOOR = "sensor.outdoor_temp"


def _hub_user_fields(**overrides) -> dict:
    data = {
        "name": "Heating Costs",
        "buy_price_entity": BUY,
        "outdoor_temp_entity": OUTDOOR,
        "demand_mode": "internal_model",
    }
    data.update(overrides)
    return data


async def _init_hub_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def _hub_entry(hass: HomeAssistant, **kwargs) -> MockConfigEntry:
    hass.states.async_set(BUY, "0.1")
    entry = MockConfigEntry(domain=DOMAIN, data=_hub_user_fields(), unique_id=BUY, **kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _suggested_value(schema: vol.Schema, key: str):
    """Read back a field's ``suggested_value`` (UI prefill) from a built schema."""
    for marker in schema.schema:
        if str(marker) == key:
            return (marker.description or {}).get("suggested_value")
    raise KeyError(key)


# --------------------------------------------------------------------------- #
# Hub: happy paths
# --------------------------------------------------------------------------- #


async def test_hub_flow_internal_model_happy_path(hass: HomeAssistant) -> None:
    hass.states.async_set(BUY, "0.15")
    result = await _init_hub_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _hub_user_fields())
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "demand"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"base_temp": 18.0, "ua_seed_w_per_c": 150.0, "ua_learning": True, "backfill_days": 30},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["buy_price_entity"] == BUY
    assert data["outdoor_temp_entity"] == OUTDOOR
    assert data["demand_mode"] == "internal_model"
    assert data["base_temp"] == 18.0
    assert data["ua_seed_w_per_c"] == 150.0
    assert data["ua_learning"] is True
    assert data["backfill_days"] == 30


async def test_hub_flow_external_entity_happy_path(hass: HomeAssistant) -> None:
    hass.states.async_set(BUY, "0.2")
    result = await _init_hub_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _hub_user_fields(demand_mode="external_entity")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "demand"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"demand_entity": "sensor.demand", "demand_unit": "kw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["demand_entity"] == "sensor.demand"
    assert result["data"]["demand_unit"] == "kw"


# --------------------------------------------------------------------------- #
# Hub: validation
# --------------------------------------------------------------------------- #


async def test_buy_price_non_numeric_then_recovers(hass: HomeAssistant) -> None:
    hass.states.async_set(BUY, "not-a-number")
    result = await _init_hub_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _hub_user_fields())
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["buy_price_entity"] == "buy_price_not_numeric"

    hass.states.async_set(BUY, "0.12")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _hub_user_fields())
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "demand"


async def test_day_ahead_invalid_format_then_unknown_state_allowed(hass: HomeAssistant) -> None:
    hass.states.async_set(BUY, "0.1")
    hass.states.async_set("sensor.day_ahead", "5", {"foo": "bar"})
    result = await _init_hub_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _hub_user_fields(day_ahead_entity="sensor.day_ahead")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["day_ahead_entity"] == "invalid_price_format"

    # A not-yet-loaded sensor (unknown state) doesn't block setup.
    hass.states.async_set("sensor.day_ahead", "unknown")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _hub_user_fields(day_ahead_entity="sensor.day_ahead")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "demand"


async def test_hub_duplicate_buy_price_aborts(hass: HomeAssistant) -> None:
    hass.states.async_set(BUY, "0.1")
    MockConfigEntry(domain=DOMAIN, data=_hub_user_fields(), unique_id=BUY).add_to_hass(hass)

    result = await _init_hub_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _hub_user_fields())
    assert result["step_id"] == "demand"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --------------------------------------------------------------------------- #
# Hub: reconfigure
# --------------------------------------------------------------------------- #


async def test_hub_reconfigure_changes_field_keeps_others(hass: HomeAssistant) -> None:
    hass.states.async_set(BUY, "0.1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **_hub_user_fields(),
            "base_temp": 19.5,
            "ua_seed_w_per_c": 200.0,
            "ua_learning": True,
            "backfill_days": 45,
        },
        unique_id=BUY,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _hub_user_fields(weather_entity="weather.home")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "demand"
    # Demand-step values from the existing entry survive as this form's defaults.
    assert _default_value(result["data_schema"], "base_temp") == 19.5
    assert _default_value(result["data_schema"], "ua_seed_w_per_c") == 200.0
    assert _default_value(result["data_schema"], "backfill_days") == 45

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["weather_entity"] == "weather.home"
    assert entry.data["buy_price_entity"] == BUY
    assert entry.data["base_temp"] == 19.5
    assert entry.data["ua_seed_w_per_c"] == 200.0
    assert entry.data["backfill_days"] == 45


def _default_value(schema: vol.Schema, key: str):
    for marker in schema.schema:
        if str(marker) == key:
            default = marker.default
            return default() if callable(default) else default
    raise KeyError(key)


# --------------------------------------------------------------------------- #
# Source subentry: add
# --------------------------------------------------------------------------- #


async def test_subentry_add_static_with_duration_activity(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "init"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Sauna Stove", "source_type": SOURCE_TYPE_STATIC}
    )
    assert result["step_id"] == "static"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "price_per_unit_eur": 60.0,
            "activity_mode": "duration",
            "activity_entity": "binary_sensor.sauna_active",
            "active_output_kw": 6.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sauna Stove"
    data = result["data"]
    assert data["source_type"] == SOURCE_TYPE_STATIC
    assert data["activity_mode"] == "duration"
    assert data["activity_entity"] == "binary_sensor.sauna_active"
    assert data["active_output_kw"] == 6.0
    assert data["capacity_kw"] == 10  # schema default


async def test_subentry_add_dynamic_fixed(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Resistive", "source_type": SOURCE_TYPE_DYNAMIC_FIXED}
    )
    assert result["step_id"] == "dynamic_fixed"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"cop": 1.0, "capacity_kw": 9.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cop"] == 1.0
    assert result["data"]["capacity_kw"] == 9.0
    assert result["data"]["source_type"] == SOURCE_TYPE_DYNAMIC_FIXED


async def test_subentry_add_curve_preset_switch_and_confirm(hass: HomeAssistant) -> None:
    """Switching the preset dropdown re-renders instead of saving; confirming stores its points."""
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Old ILP", "source_type": SOURCE_TYPE_CURVE}
    )
    assert result["step_id"] == "curve_entities"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"power_entity": "sensor.ilp_power", "energy_entity": "sensor.ilp_energy"},
    )
    assert result["step_id"] == "curve_tables"

    fd25 = curves.PRESETS["msz_fd25va"]
    rw35 = curves.PRESETS["msz_rw35vg"]
    assert _suggested_value(result["data_schema"], "cop_points") == curves.points_to_text(
        fd25.cop_points
    )

    # Only touch the dropdown; leave the (stale) fd25va text untouched.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "preset": "msz_rw35vg",
            "cop_points": curves.points_to_text(fd25.cop_points),
            "capacity_points": curves.points_to_text(fd25.capacity_points),
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "curve_tables"
    assert not result.get("errors")
    assert _suggested_value(result["data_schema"], "cop_points") == curves.points_to_text(
        rw35.cop_points
    )
    assert _suggested_value(result["data_schema"], "capacity_points") == curves.points_to_text(
        rw35.capacity_points
    )

    # Submit again with the (now current) rw35vg preset -> stores its canonical points.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "preset": "msz_rw35vg",
            "cop_points": curves.points_to_text(rw35.cop_points),
            "capacity_points": curves.points_to_text(rw35.capacity_points),
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["preset"] == "msz_rw35vg"
    assert data["cop_points"] == [list(p) for p in rw35.cop_points]
    assert data["capacity_points"] == [list(p) for p in rw35.capacity_points]
    assert data["operating_limit_c"] == rw35.operating_limit_c
    assert data["min_cop"] == rw35.min_cop
    assert data["max_cop"] == rw35.max_cop


async def test_subentry_add_curve_custom_edited_text(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Custom Pump", "source_type": SOURCE_TYPE_CURVE}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"power_entity": "sensor.pump_power", "energy_entity": "sensor.pump_energy"},
    )
    assert result["step_id"] == "curve_tables"

    # Switching to "custom" refreshes the prefill (discards the stale fd25va text).
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"preset": "custom"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "curve_tables"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "preset": "custom",
            "cop_points": "-20:2.0, 0:3.0, 10:4.0",
            "capacity_points": "-20:2.0, 10:6.0",
            "operating_limit_c": -25.0,
            "min_cop": 0.5,
            "max_cop": 6.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["preset"] == "custom"
    assert data["cop_points"] == [[-20.0, 2.0], [0.0, 3.0], [10.0, 4.0]]
    assert data["capacity_points"] == [[-20.0, 2.0], [10.0, 6.0]]
    assert data["operating_limit_c"] == -25.0


async def test_subentry_add_curve_invalid_text_then_recovers(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Bad Pump", "source_type": SOURCE_TYPE_CURVE}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"power_entity": "sensor.pump2_power", "energy_entity": "sensor.pump2_energy"},
    )
    assert result["step_id"] == "curve_tables"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"preset": "custom"}
    )
    assert result["step_id"] == "curve_tables"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"preset": "custom", "cop_points": "garbage", "capacity_points": "also garbage"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["cop_points"] == "invalid_curve_format"
    assert result["errors"]["capacity_points"] == "invalid_curve_format"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "preset": "custom",
            "cop_points": "-20:2.0, 10:4.0",
            "capacity_points": "-20:2.0, 10:6.0",
            "operating_limit_c": -25.0,
            "min_cop": 0.5,
            "max_cop": 6.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_subentry_static_event_mode_missing_energy_field_error(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Sauna", "source_type": SOURCE_TYPE_STATIC}
    )
    assert result["step_id"] == "static"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "price_per_unit_eur": 60.0,
            "activity_mode": "event",
            "activity_entity": "binary_sensor.sauna_active",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["energy_per_event_kwh"] == "invalid_config"
    assert result["description_placeholders"]["error"]


# --------------------------------------------------------------------------- #
# Source subentry: reconfigure
# --------------------------------------------------------------------------- #


async def test_subentry_reconfigure_static_price_type_stays(hass: HomeAssistant) -> None:
    entry = await _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_SOURCE,
                title="Wood Stove",
                unique_id=None,
                data={
                    "name": "Wood Stove",
                    "source_type": SOURCE_TYPE_STATIC,
                    "price_per_unit_eur": 55.0,
                    "fuel_unit": "m3",
                },
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "static"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Wood Stove", "price_per_unit_eur": 70.0}
    )
    assert result["type"] is FlowResultType.ABORT

    updated = entry.subentries[subentry_id].data
    assert updated["price_per_unit_eur"] == 70.0
    assert updated["source_type"] == SOURCE_TYPE_STATIC
    assert updated["fuel_unit"] == "m3"


# --------------------------------------------------------------------------- #
# Reconfigure can clear optional fields (the frontend omits a cleared field)
# --------------------------------------------------------------------------- #


async def test_hub_reconfigure_clears_optional_entities_and_reloads_once(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(BUY, "0.1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **_hub_user_fields(),
            "weather_entity": "weather.home",
            "forecast_price_entity": "sensor.price_estimate",
            "base_temp": 19.5,
        },
        unique_id=BUY,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _hub_user_fields())
    assert result["step_id"] == "demand"

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert "weather_entity" not in entry.data
    assert "forecast_price_entity" not in entry.data
    assert entry.data["buy_price_entity"] == BUY
    assert entry.data["base_temp"] == 19.5  # demand step default re-submitted
    # The update listener reloads; the flow itself must not reload again.
    assert reload.call_count == 1


async def test_hub_reconfigure_of_unloaded_entry_reloads_it(hass: HomeAssistant) -> None:
    """No update listener exists before a successful setup, so the flow must reload."""
    hass.states.async_set(BUY, "0.1")
    entry = MockConfigEntry(domain=DOMAIN, data=_hub_user_fields(), unique_id=BUY)
    entry.add_to_hass(hass)  # never set up: e.g. its setup failed

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _hub_user_fields())
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert reload.call_count == 1


async def test_subentry_reconfigure_curve_clears_stale_climate_and_pump_temp(
    hass: HomeAssistant,
) -> None:
    """The real-world case: a heat pump whose climate/pump-temp entities are gone."""
    entry = await _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_SOURCE,
                title="New ILP",
                unique_id=None,
                data={
                    "name": "New ILP",
                    "source_type": SOURCE_TYPE_CURVE,
                    "power_entity": "sensor.ilp_power",
                    "energy_entity": "sensor.ilp_energy",
                    "climate_entity": "climate.gone",
                    "pump_temp_entity": "sensor.gone_outdoor",
                    "temp_entity_override": "sensor.gone_too",
                    "preset": "msz_rw35vg",
                },
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "curve_entities"
    assert _suggested_value(result["data_schema"], "climate_entity") == "climate.gone"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "New ILP",
            "power_entity": "sensor.ilp_power",
            "energy_entity": "sensor.ilp_energy",
        },
    )
    assert result["step_id"] == "curve_tables"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"preset": "msz_rw35vg"}
    )
    assert result["type"] is FlowResultType.ABORT

    data = entry.subentries[subentry_id].data
    for cleared in ("climate_entity", "pump_temp_entity", "temp_entity_override"):
        assert cleared not in data
    assert data["power_entity"] == "sensor.ilp_power"
    assert data["preset"] == "msz_rw35vg"
    cfg = models.source_from_subentry(data)
    assert cfg.climate_entity is None
    assert cfg.pump_temp_entity is None


async def test_subentry_reconfigure_dynamic_fixed_clears_energy_entity(
    hass: HomeAssistant,
) -> None:
    entry = await _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_SOURCE,
                title="Resistive",
                unique_id=None,
                data={
                    "name": "Resistive",
                    "source_type": SOURCE_TYPE_DYNAMIC_FIXED,
                    "cop": 1.0,
                    "capacity_kw": 9.0,
                    "energy_entity": "sensor.boiler_energy",
                },
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "dynamic_fixed"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Resistive", "cop": 1.0, "capacity_kw": 9.0}
    )
    assert result["type"] is FlowResultType.ABORT
    data = entry.subentries[subentry_id].data
    assert "energy_entity" not in data
    assert data["source_type"] == SOURCE_TYPE_DYNAMIC_FIXED


async def test_subentry_reconfigure_static_clears_energy_density(hass: HomeAssistant) -> None:
    entry = await _hub_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_SOURCE,
                title="Pellets",
                unique_id=None,
                data={
                    "name": "Pellets",
                    "source_type": SOURCE_TYPE_STATIC,
                    "price_per_unit_eur": 0.4,
                    "fuel_unit": "kg",
                    "energy_density_kwh_per_unit": 1700,
                },
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert _suggested_value(result["data_schema"], "energy_density_kwh_per_unit") == 1700
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Pellets", "price_per_unit_eur": 0.4, "fuel_unit": "kg"}
    )
    assert result["type"] is FlowResultType.ABORT
    data = entry.subentries[subentry_id].data
    assert "energy_density_kwh_per_unit" not in data
    cfg = models.source_from_subentry(data)
    assert cfg.energy_density_kwh_per_unit == pytest.approx(4.1)  # kg default


# --------------------------------------------------------------------------- #
# New static source: no unit-blind 1700 prefill
# --------------------------------------------------------------------------- #


async def test_subentry_add_static_kg_gets_per_unit_density_default(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Pellet stove", "source_type": SOURCE_TYPE_STATIC}
    )
    assert result["step_id"] == "static"
    assert _suggested_value(result["data_schema"], "energy_density_kwh_per_unit") is None

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"price_per_unit_eur": 0.4, "fuel_unit": "kg"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "energy_density_kwh_per_unit" not in result["data"]
    cfg = models.source_from_subentry(result["data"])
    assert cfg.energy_density_kwh_per_unit == pytest.approx(4.1)


# --------------------------------------------------------------------------- #
# Curve tables: an edited preset is stored as custom
# --------------------------------------------------------------------------- #


async def _curve_tables_form(hass: HomeAssistant, name: str):
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": name, "source_type": SOURCE_TYPE_CURVE}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"power_entity": "sensor.p_power", "energy_entity": "sensor.p_energy"},
    )
    assert result["step_id"] == "curve_tables"
    return result


async def test_curve_tables_edited_preset_text_becomes_custom(hass: HomeAssistant) -> None:
    fd25 = curves.PRESETS["msz_fd25va"]
    result = await _curve_tables_form(hass, "Tweaked FD25")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "preset": "msz_fd25va",
            "cop_points": "-20:2.0, 0:3.1, 10:4.0",  # edited
            "capacity_points": curves.points_to_text(fd25.capacity_points),
            "operating_limit_c": fd25.operating_limit_c,
            "min_cop": fd25.min_cop,
            "max_cop": 5.0,  # edited too
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["preset"] == "custom"
    assert data["cop_points"] == [[-20.0, 2.0], [0.0, 3.1], [10.0, 4.0]]
    assert data["capacity_points"] == [list(p) for p in fd25.capacity_points]
    assert data["operating_limit_c"] == fd25.operating_limit_c
    assert data["max_cop"] == 5.0
    cfg = models.source_from_subentry(data)
    assert cfg.preset_key is None


async def test_curve_tables_preset_reformatted_text_stays_preset(hass: HomeAssistant) -> None:
    fd25 = curves.PRESETS["msz_fd25va"]
    result = await _curve_tables_form(hass, "Plain FD25")
    reformatted = "\n".join(f"{x}:{y}" for x, y in reversed(fd25.cop_points))
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"preset": "msz_fd25va", "cop_points": reformatted}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["preset"] == "msz_fd25va"


async def test_curve_tables_edited_preset_bad_text_errors_then_saves_custom(
    hass: HomeAssistant,
) -> None:
    result = await _curve_tables_form(hass, "Typo FD25")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"preset": "msz_fd25va", "cop_points": "-20:2.0, oops"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["cop_points"] == "invalid_curve_format"
    assert _default_value(result["data_schema"], "preset") == "custom"

    # Resubmitting as "custom" is accepted, not treated as a dropdown change.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "preset": "custom",
            "cop_points": "-20:2.0, 10:4.0",
            "capacity_points": "-20:2.0, 10:6.0",
            "operating_limit_c": -25.0,
            "min_cop": 0.5,
            "max_cop": 6.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cop_points"] == [[-20.0, 2.0], [10.0, 4.0]]


async def test_curve_entities_rejects_negative_standby(hass: HomeAssistant) -> None:
    entry = await _hub_entry(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SOURCE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Pump", "source_type": SOURCE_TYPE_CURVE}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"power_entity": "sensor.p", "energy_entity": "sensor.e", "standby_w": -5},
        )
