"""Sensor + number platform: per-source cost/capacity/COP, hub demand/UA, fuel-price override."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs.const import DOMAIN, SAMPLE_INTERVAL_S
from custom_components.live_heating_costs.curves import PRESETS, Curve
from custom_components.live_heating_costs.sensor import (
    CheapestSourceSensor,
    HubCostForecastSensor,
    SourceCostForecastSensor,
)


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """See test_coordinator._tick: settle, fire mocked time, settle again."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"no {platform} entity registered for {unique_id!r}"
    return entity_id


def _sub_id(entry, title: str) -> str:
    return next(sid for sid, sub in entry.subentries.items() if sub.title == title)


async def _setup_four_sources(hass: HomeAssistant, **hub_overrides):
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(
        hass,
        [
            wood_subentry(),
            resistive_subentry(),
            curve_subentry(
                title="FD25",
                preset="msz_fd25va",
                power_entity="sensor.fd25_power",
                energy_entity="sensor.fd25_energy",
                climate_entity="climate.fd25",
            ),
            curve_subentry(
                title="RW35",
                preset="msz_rw35vg",
                power_entity="sensor.rw35_power",
                energy_entity="sensor.rw35_energy",
                climate_entity="climate.rw35",
            ),
        ],
        **hub_overrides,
    )
    return entry


async def test_heat_cost_and_capacity_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    resistive_id = _sub_id(entry, "Resistive")
    fd25_id = _sub_id(entry, "FD25")
    wood_id = _sub_id(entry, "Wood stove")

    resistive_cost = hass.states.get(_entity_id(hass, f"{resistive_id}_heat_cost"))
    assert float(resistive_cost.state) == pytest.approx(5.30)

    fd25_cost = hass.states.get(_entity_id(hass, f"{fd25_id}_heat_cost"))
    assert float(fd25_cost.state) == pytest.approx(round(5.3 / 2.9, 2))

    wood_cost = hass.states.get(_entity_id(hass, f"{wood_id}_heat_cost"))
    assert float(wood_cost.state) == pytest.approx(10.0)  # 0.10 EUR/kWh * 100

    fd25_capacity = hass.states.get(_entity_id(hass, f"{fd25_id}_capacity"))
    expected_capacity = Curve.from_points(PRESETS["msz_fd25va"].capacity_points).value_at(-5.0)
    assert float(fd25_capacity.state) == pytest.approx(round(expected_capacity, 2))


async def test_cop_sensor_dimensionless_with_attrs(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    resistive_id = _sub_id(entry, "Resistive")
    fd25_id = _sub_id(entry, "FD25")

    resistive_cop = hass.states.get(_entity_id(hass, f"{resistive_id}_cop"))
    assert resistive_cop.attributes.get("unit_of_measurement") is None
    assert resistive_cop.attributes.get("device_class") is None
    assert float(resistive_cop.state) == pytest.approx(1.0)

    fd25_cop = hass.states.get(_entity_id(hass, f"{fd25_id}_cop"))
    assert fd25_cop.attributes.get("unit_of_measurement") is None
    assert "modeled_cop" in fd25_cop.attributes
    assert "measured_cop" in fd25_cop.attributes
    assert fd25_cop.attributes["modeled_cop"] == pytest.approx(2.9)


async def test_buy_price_unavailable_makes_electric_heat_cost_unknown(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    hass.states.async_set("sensor.buy_price", "unavailable")
    await _tick(hass, 3)  # let the debounced recompute fire

    resistive_id = _sub_id(entry, "Resistive")
    fd25_id = _sub_id(entry, "FD25")
    wood_id = _sub_id(entry, "Wood stove")

    resistive_cost = hass.states.get(_entity_id(hass, f"{resistive_id}_heat_cost"))
    assert resistive_cost.state in ("unknown", "None")

    fd25_cost = hass.states.get(_entity_id(hass, f"{fd25_id}_heat_cost"))
    assert fd25_cost.state in ("unknown", "None")

    wood_cost = hass.states.get(_entity_id(hass, f"{wood_id}_heat_cost"))
    assert float(wood_cost.state) == pytest.approx(10.0)  # unaffected by buy price


async def test_ua_coefficient_initial_state(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    ua_state = hass.states.get(_entity_id(hass, f"{entry.entry_id}_ua_coefficient"))
    assert float(ua_state.state) == pytest.approx(140.0)


async def test_heating_demand_after_sampling_tick(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    await _tick(hass, SAMPLE_INTERVAL_S + 5)

    demand_state = hass.states.get(_entity_id(hass, f"{entry.entry_id}_heating_demand"))
    assert float(demand_state.state) == pytest.approx(round(0.14 * (17 - (-5)), 2))


async def test_fuel_price_number_override_recomputes_heat_cost(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)
    wood_id = _sub_id(entry, "Wood stove")

    number_entity_id = _entity_id(hass, f"{wood_id}_fuel_price", platform="number")
    number_state = hass.states.get(number_entity_id)
    assert float(number_state.state) == pytest.approx(0.10)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": number_entity_id, "value": 0.20},
        blocking=True,
    )
    await hass.async_block_till_done()

    wood_cost = hass.states.get(_entity_id(hass, f"{wood_id}_heat_cost"))
    assert float(wood_cost.state) == pytest.approx(20.0)

    number_state = hass.states.get(number_entity_id)
    assert float(number_state.state) == pytest.approx(0.20)


def test_unrecorded_attributes_exclude_bulky_fields() -> None:
    assert "forecast" in SourceCostForecastSensor._unrecorded_attributes
    assert {"ranking", "excluded"} <= CheapestSourceSensor._unrecorded_attributes
    assert {"slots", "names"} <= HubCostForecastSensor._unrecorded_attributes
