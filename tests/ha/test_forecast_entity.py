"""cost_forecast sensors (per-source + hub): price-slot union, weather-driven COP."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.live_heating_costs.const import (
    CONF_DAY_AHEAD_ENTITY,
    CONF_FORECAST_PRICE_ENTITY,
    CONF_WEATHER_ENTITY,
    DOMAIN,
)


def _entity_id(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"no {platform} entity registered for {unique_id!r}"
    return entity_id


def _sub_id(entry, title: str) -> str:
    return next(sid for sid, sub in entry.subentries.items() if sub.title == title)


async def test_source_and_hub_cost_forecast(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "0.053")

    now = dt_util.utcnow().replace(microsecond=0)

    # Real (day-ahead) slots: four 15-min slots spanning exactly 1h from now.
    real_slots = [
        {"start": (now + timedelta(minutes=15 * i)).isoformat(), "buy": 0.05 + 0.01 * i}
        for i in range(4)
    ]
    # Estimated (beyond-horizon) slots: three 1h slots starting right after.
    estimated_slots = [
        {"start": (now + timedelta(hours=1 + i)).isoformat(), "buy": 0.09 + 0.01 * i}
        for i in range(3)
    ]
    hass.states.async_set("sensor.day_ahead", "0.05", {"data_today": real_slots})
    hass.states.async_set("sensor.forecast_price", "0.09", {"data_today": estimated_slots})

    # Constant -5.0C hourly weather forecast, covering the whole 4h window.
    hourly_points = [
        {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": -5.0}
        for h in range(0, 6)
    ]

    async def _fake_get_forecasts(call):
        entity_id = call.data.get("entity_id")
        return {entity_id: {"forecast": hourly_points}}

    hass.services.async_register(
        "weather", "get_forecasts", _fake_get_forecasts, supports_response=SupportsResponse.ONLY
    )

    entry = await async_setup_hub(
        hass,
        [
            curve_subentry(
                title="FD25",
                preset="msz_fd25va",
                power_entity="sensor.fd25_power",
                energy_entity="sensor.fd25_energy",
            )
        ],
        **{
            CONF_DAY_AHEAD_ENTITY: "sensor.day_ahead",
            CONF_FORECAST_PRICE_ENTITY: "sensor.forecast_price",
            CONF_WEATHER_ENTITY: "weather.home",
        },
    )
    fd25_id = _sub_id(entry, "FD25")

    # -5.0C is an exact knot on the FD25 preset: COP = 2.9 for every slot.
    cop = 2.9
    first_buy_eur = real_slots[0]["buy"]

    source_state = hass.states.get(_entity_id(hass, f"{fd25_id}_cost_forecast"))
    assert float(source_state.state) == pytest.approx(round(first_buy_eur / cop * 100, 2))

    forecast_attr = source_state.attributes["forecast"]
    assert forecast_attr, "expected at least one priced forecast slot"
    assert forecast_attr[0]["buy"] == pytest.approx(first_buy_eur / cop)  # EUR/kWh, not cents
    assert any(item["estimated"] for item in forecast_attr)
    assert any(not item["estimated"] for item in forecast_attr)
    # Real horizon is only 1h out from now -> not "tomorrow" by the >12h rule.
    assert source_state.attributes["has_tomorrow"] is False
    assert source_state.attributes["updated"] is not None

    hub_state = hass.states.get(_entity_id(hass, f"{entry.entry_id}_cost_forecast"))
    slots = hub_state.attributes["slots"]
    assert slots, "expected at least one combined forecast slot"
    assert slots[0]["costs"][fd25_id] == pytest.approx(round(first_buy_eur / cop * 100, 3))
    assert hub_state.attributes["names"][fd25_id] == "FD25"
    assert hub_state.attributes["has_tomorrow"] is False
    assert hub_state.attributes["integration"] == DOMAIN
    assert any(s["estimated"] for s in slots)


async def test_leading_slots_keep_curve_sources_and_static_line(hass: HomeAssistant) -> None:
    """The gap before the weather forecast's first point must not drop the pump.

    A weather integration's hourly series starts at the *next* full hour, so
    the slots between now and it used to contain only the resistive source —
    and the card duly announced "floor heating is cheapest" for the current
    hour. The measured outdoor temperature anchors the series instead. Static
    (wood) sources have no per-slot forecast but are folded in as a flat cost,
    so they can win a slot too.
    """
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "0.053")

    now = dt_util.utcnow().replace(microsecond=0)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    real_slots = [
        {"start": (now + timedelta(minutes=15 * i)).isoformat(), "buy": 0.20}
        for i in range(8)  # 2h of 15-min slots, starting *now*
    ]
    hass.states.async_set("sensor.day_ahead", "0.20", {"data_today": real_slots})

    # Hourly weather starts at the next full hour — nothing covers `now`.
    hourly_points = [
        {"datetime": (next_hour + timedelta(hours=h)).isoformat(), "temperature": -5.0}
        for h in range(0, 6)
    ]

    async def _fake_get_forecasts(call):
        entity_id = call.data.get("entity_id")
        return {entity_id: {"forecast": hourly_points}}

    hass.services.async_register(
        "weather", "get_forecasts", _fake_get_forecasts, supports_response=SupportsResponse.ONLY
    )

    entry = await async_setup_hub(
        hass,
        [
            wood_subentry(price_per_unit_eur=0.02),
            resistive_subentry(),
            curve_subentry(
                title="FD25",
                preset="msz_fd25va",
                power_entity="sensor.fd25_power",
                energy_entity="sensor.fd25_energy",
            ),
        ],
        **{CONF_DAY_AHEAD_ENTITY: "sensor.day_ahead", CONF_WEATHER_ENTITY: "weather.home"},
    )
    coordinator = entry.runtime_data
    await coordinator.async_rebuild_forecast()

    combined = coordinator.forecast_combined
    assert combined, "expected combined forecast slots"

    fd25_id = _sub_id(entry, "FD25")
    wood_id = _sub_id(entry, "Wood stove")

    first = combined[0]
    assert dt_util.parse_datetime(first["start"]) < next_hour  # before the weather series
    assert first["costs"][fd25_id] is not None
    assert first["partial"] is False

    # The static source is priced in every slot and (at 2 c/kWh) wins them all.
    assert all(item["costs"][wood_id] == pytest.approx(0.02) for item in combined)
    assert all(item["cheapest"] == wood_id for item in combined)
