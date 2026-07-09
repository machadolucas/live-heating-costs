"""Contract lock: the card is written against these exact attribute shapes.

If a field here is renamed without updating
``custom_components/live_heating_costs/frontend/live-heating-costs-card.js``
(and ``docs/card.md``), the card silently breaks — this test exists to catch
that at the integration layer before it ships.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs.const import DOMAIN, SAMPLE_INTERVAL_S
from custom_components.live_heating_costs.sensor import CheapestSourceSensor, HubCostForecastSensor

_RANKING_KEYS = {
    "name",
    "subentry_id",
    "source_type",
    "cost_c_per_kwh",
    "cop",
    "capacity_kw",
    "can_meet_demand",
    "allocated_kw",
    "allocated_fraction",
    "active",
    "heating",
    "measured_blend",
}
_MAIN_KEYS = {
    "integration",
    "ranking",
    "excluded",
    "demand_kw",
    "demand_covered",
    "shortfall_kw",
    "blended_cost_c_per_kwh",
    "buy_price_c_per_kwh",
}
_FORECAST_KEYS = {"integration", "slots", "names", "horizon_end", "has_tomorrow"}


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """See test_coordinator._tick: settle, fire mocked time, settle again."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _setup_four_sources(hass: HomeAssistant):
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "0.053")
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
            ),
            curve_subentry(
                title="RW35",
                preset="msz_rw35vg",
                power_entity="sensor.rw35_power",
                energy_entity="sensor.rw35_energy",
            ),
        ],
    )
    await _tick(hass, SAMPLE_INTERVAL_S + 5)  # populate temp history -> known demand_kw
    return entry


async def test_cheapest_source_attribute_contract(hass: HomeAssistant) -> None:
    entry = await _setup_four_sources(hass)
    coordinator = entry.runtime_data

    entity = CheapestSourceSensor(coordinator)
    attrs = entity.extra_state_attributes

    assert set(attrs.keys()) == _MAIN_KEYS
    assert attrs["integration"] == DOMAIN

    ranking = attrs["ranking"]
    assert ranking, "expected at least one ranked source"
    for row in ranking:
        assert set(row.keys()) == _RANKING_KEYS

    for row in attrs["excluded"]:
        assert set(row.keys()) == {"name", "reason"}

    # Ranked ascending by cost.
    costs = [row["cost_c_per_kwh"] for row in ranking]
    assert costs == sorted(costs)

    # Allocated kW sums to min(demand, total dispatchable capacity).
    demand_kw = attrs["demand_kw"]
    assert demand_kw is not None
    total_capacity = sum(row["capacity_kw"] for row in ranking if row["capacity_kw"] > 0)
    total_allocated = sum(row["allocated_kw"] for row in ranking)
    assert total_allocated == pytest.approx(min(demand_kw, total_capacity), abs=0.01)

    # Cheapest state == ranking[0]'s name, given it's dispatchable.
    assert ranking[0]["capacity_kw"] > 0
    assert entity.native_value == ranking[0]["name"]


async def test_hub_cost_forecast_attribute_contract(hass: HomeAssistant) -> None:
    entry = await _setup_four_sources(hass)
    coordinator = entry.runtime_data

    entity = HubCostForecastSensor(coordinator)
    attrs = entity.extra_state_attributes

    assert set(attrs.keys()) == _FORECAST_KEYS
    assert attrs["integration"] == DOMAIN
    assert isinstance(attrs["slots"], list)
    assert isinstance(attrs["names"], dict)
