"""Fixtures + shared builders for the Home-Assistant-backed tests."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.live_heating_costs.const import (
    ACTIVITY_MODE_DURATION,
    ACTIVITY_MODE_EVENT,
    CONF_ACTIVE_OUTPUT_KW,
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVITY_MODE,
    CONF_BASE_TEMP,
    CONF_BUY_PRICE_ENTITY,
    CONF_CAPACITY_KW,
    CONF_CLIMATE_ENTITY,
    CONF_COP,
    CONF_DEMAND_MODE,
    CONF_EFFICIENCY,
    CONF_ENERGY_DENSITY,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_PER_EVENT_KWH,
    CONF_EVENT_DEBOUNCE_HOURS,
    CONF_FUEL_UNIT,
    CONF_NAME,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRESET,
    CONF_PRICE_PER_UNIT,
    CONF_SOURCE_TYPE,
    CONF_UA_LEARNING,
    CONF_UA_SEED_W_PER_C,
    DEMAND_MODE_INTERNAL,
    DOMAIN,
    FUEL_UNIT_KWH,
    SOURCE_TYPE_CURVE,
    SOURCE_TYPE_DYNAMIC_FIXED,
    SOURCE_TYPE_STATIC,
    SUBENTRY_TYPE_SOURCE,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the custom integration in every HA test."""
    yield


def hub_entry_data(**overrides) -> dict:
    """Hub config-entry ``data`` with sane internal-demand-mode defaults."""
    data = {
        CONF_NAME: "Heating",
        CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        CONF_OUTDOOR_TEMP_ENTITY: "sensor.outdoor_temp",
        CONF_DEMAND_MODE: DEMAND_MODE_INTERNAL,
        CONF_BASE_TEMP: 17.0,
        CONF_UA_SEED_W_PER_C: 140.0,
        CONF_UA_LEARNING: True,
    }
    data.update(overrides)
    return data


def wood_subentry(
    *,
    title: str = "Wood stove",
    activity_entity: str = "switch.wood_stove",
    active_output_kw: float = 5.0,
    price_per_unit_eur: float = 0.10,
    energy_density: float = 1.0,
    efficiency: float = 1.0,
    capacity_kw: float = 8.0,
    mode: str = ACTIVITY_MODE_DURATION,
    energy_per_event_kwh: float | None = None,
    event_debounce_hours: float | None = None,
) -> ConfigSubentryData:
    """A static (wood) source subentry, duration or event activity mode."""
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_STATIC,
        CONF_NAME: title,
        CONF_CAPACITY_KW: capacity_kw,
        CONF_FUEL_UNIT: FUEL_UNIT_KWH,
        CONF_PRICE_PER_UNIT: price_per_unit_eur,
        CONF_ENERGY_DENSITY: energy_density,
        CONF_EFFICIENCY: efficiency,
        CONF_ACTIVITY_MODE: mode,
        CONF_ACTIVITY_ENTITY: activity_entity,
    }
    if mode == ACTIVITY_MODE_DURATION:
        data[CONF_ACTIVE_OUTPUT_KW] = active_output_kw
    elif mode == ACTIVITY_MODE_EVENT:
        data[CONF_ENERGY_PER_EVENT_KWH] = energy_per_event_kwh or 1.0
        if event_debounce_hours is not None:
            data[CONF_EVENT_DEBOUNCE_HOURS] = event_debounce_hours
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_SOURCE, title=title, unique_id=None, data=data
    )


def resistive_subentry(
    *,
    title: str = "Resistive",
    capacity_kw: float = 9.0,
    cop: float = 1.0,
    energy_entity: str | None = None,
) -> ConfigSubentryData:
    """A dynamic_fixed (resistive) electric source subentry."""
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_DYNAMIC_FIXED,
        CONF_NAME: title,
        CONF_CAPACITY_KW: capacity_kw,
        CONF_COP: cop,
    }
    if energy_entity:
        data[CONF_ENERGY_ENTITY] = energy_entity
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_SOURCE, title=title, unique_id=None, data=data
    )


def curve_subentry(
    *,
    title: str,
    preset: str,
    power_entity: str,
    energy_entity: str,
    climate_entity: str | None = None,
) -> ConfigSubentryData:
    """A curve (heat pump) source subentry, using a built-in preset."""
    data = {
        CONF_SOURCE_TYPE: SOURCE_TYPE_CURVE,
        CONF_NAME: title,
        CONF_POWER_ENTITY: power_entity,
        CONF_ENERGY_ENTITY: energy_entity,
        CONF_PRESET: preset,
    }
    if climate_entity:
        data[CONF_CLIMATE_ENTITY] = climate_entity
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_SOURCE, title=title, unique_id=None, data=data
    )


async def async_setup_hub(
    hass: HomeAssistant, subentries: list[ConfigSubentryData], **hub_overrides
) -> MockConfigEntry:
    """Build, add and set up a hub config entry with the given source subentries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=hub_entry_data(**hub_overrides),
        unique_id=None,
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
