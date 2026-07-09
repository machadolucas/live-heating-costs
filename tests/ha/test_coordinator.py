"""Coordinator: snapshots, ranking, merit-order allocation, sampling, wood."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs.const import (
    ACTIVITY_MODE_EVENT,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_MODE,
    CONF_DEMAND_UNIT,
    DEMAND_MODE_EXTERNAL,
    DEMAND_UNIT_KW,
    REASON_PRICE_UNAVAILABLE,
    SAMPLE_INTERVAL_S,
)
from custom_components.live_heating_costs.curves import PRESETS


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """Force-fire any loop timer within ``seconds`` of now (see HA test harness).

    State-changed listeners run as background jobs (queued via ``call_soon``,
    not synchronously inside ``async_set``), so any debounce timer they
    schedule (e.g. the coordinator's recompute debounce) only exists on the
    loop *after* a ``block_till_done`` — hence settling once before firing the
    mocked time, and once more after to let the fired callback's own
    coroutines finish.
    """
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


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


def _snap_by_name(data, name):
    return next(s for s in data.snapshots.values() if s.name == name)


async def test_snapshots_at_buy_0_053(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)
    data = entry.runtime_data.data

    fd25_cop = PRESETS["msz_fd25va"].cop_points  # sanity: -5.0 is an exact knot (2.9)
    assert dict(fd25_cop)[-5.0] == pytest.approx(2.9)

    resistive = _snap_by_name(data, "Resistive")
    assert resistive.cost_eur_per_kwh == pytest.approx(0.053)
    assert resistive.cop == pytest.approx(1.0)

    fd25 = _snap_by_name(data, "FD25")
    assert fd25.cop == pytest.approx(2.9)
    assert fd25.cost_eur_per_kwh == pytest.approx(0.053 / 2.9)
    assert fd25.capacity_kw > 0

    rw35 = _snap_by_name(data, "RW35")
    assert rw35.cop == pytest.approx(3.622222, rel=1e-4)
    assert rw35.cost_eur_per_kwh == pytest.approx(0.053 / 3.622222, rel=1e-4)

    wood = _snap_by_name(data, "Wood stove")
    assert wood.cost_eur_per_kwh == pytest.approx(0.10)
    assert wood.cop is None


async def test_unavailable_buy_excludes_electric_sources(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    hass.states.async_set("sensor.buy_price", "unavailable")
    await _tick(hass, 3)  # let the debounced recompute fire

    data = entry.runtime_data.data
    wood = _snap_by_name(data, "Wood stove")
    resistive = _snap_by_name(data, "Resistive")
    fd25 = _snap_by_name(data, "FD25")
    rw35 = _snap_by_name(data, "RW35")

    assert wood.cost_eur_per_kwh == pytest.approx(0.10)
    assert resistive.cost_eur_per_kwh is None
    assert resistive.unavailable_reason == REASON_PRICE_UNAVAILABLE
    assert fd25.cost_eur_per_kwh is None
    assert rw35.cost_eur_per_kwh is None

    ranked_names = [s.name for s in data.ranked]
    excluded_names = {s.name for s in data.excluded}
    assert ranked_names == ["Wood stove"]
    assert excluded_names == {"Resistive", "FD25", "RW35"}


async def test_ranking_order_at_buy_0_35(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await _setup_four_sources(hass)

    hass.states.async_set("sensor.buy_price", "0.35")
    await _tick(hass, 3)

    data = entry.runtime_data.data
    assert [s.name for s in data.ranked] == ["RW35", "Wood stove", "FD25", "Resistive"]
    assert data.cheapest is not None
    assert data.cheapest.name == "RW35"


async def test_merit_order_allocation_covers_external_demand(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.35")
    hass.states.async_set("sensor.demand", "10.0")
    entry = await _setup_four_sources(
        hass,
        **{
            CONF_DEMAND_MODE: DEMAND_MODE_EXTERNAL,
            CONF_DEMAND_ENTITY: "sensor.demand",
            CONF_DEMAND_UNIT: DEMAND_UNIT_KW,
        },
    )
    await _tick(hass, 3)
    data = entry.runtime_data.data

    assert data.demand_kw == pytest.approx(10.0)
    assert data.demand_covered is True
    assert [a.name for a in data.allocations][:2] == ["RW35", "Wood stove"]
    assert sum(a.kw for a in data.allocations) == pytest.approx(10.0)
    # blended cost sits between the two cheapest sources' unit costs
    rw35_cost = _snap_by_name(data, "RW35").cost_eur_per_kwh
    wood_cost = _snap_by_name(data, "Wood stove").cost_eur_per_kwh
    assert rw35_cost < data.blended_cost < wood_cost


async def test_duration_mode_wood_accrues_across_ticks(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await async_setup_hub(
        hass, [wood_subentry(activity_entity="switch.wood_stove", active_output_kw=5.0)]
    )
    coordinator = entry.runtime_data

    hass.states.async_set("switch.wood_stove", "on")
    for _ in range(3):
        await _tick(hass, SAMPLE_INTERVAL_S + 5)

    expected = 3 * 5.0 * (SAMPLE_INTERVAL_S / 3600.0)
    assert coordinator.day_accum.wood_kwh == pytest.approx(expected)
    assert coordinator.day_accum.delivered_kwh == pytest.approx(expected)


async def test_event_mode_debounces_double_transition(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.buy_price", "0.053")
    entry = await async_setup_hub(
        hass,
        [
            wood_subentry(
                activity_entity="binary_sensor.wood_event",
                mode=ACTIVITY_MODE_EVENT,
                energy_per_event_kwh=2.0,
                event_debounce_hours=4.0,
            )
        ],
    )
    coordinator = entry.runtime_data

    hass.states.async_set("binary_sensor.wood_event", "off")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.wood_event", "on")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.wood_event", "off")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.wood_event", "on")  # second off->on, still debounced
    await hass.async_block_till_done()

    assert coordinator.day_accum.wood_kwh == pytest.approx(2.0)
    assert len(coordinator.wood_log) == 1
