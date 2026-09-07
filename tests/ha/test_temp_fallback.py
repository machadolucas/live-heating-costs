"""Outdoor-temperature fallback chain: pump -> override -> hub -> weather.

Regression cover for the failure that motivated the chain: the hub's outdoor
sensor (a battery RuuviTag) went ``unavailable``, so every heat pump reported
"temperature unavailable" and dropped out of the ranking — while the hub's
own weather entity was reporting a perfectly good current temperature.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from conftest import async_setup_hub, curve_subentry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs.const import (
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_WEATHER_ENTITY,
    REASON_TEMP_UNAVAILABLE,
)
from custom_components.live_heating_costs.curves import PRESETS

WEATHER_ATTRS = {"temperature": -5.0, "temperature_unit": "°C"}


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """Settle, fire a loop timer, settle again (see tests/ha/test_coordinator.py)."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _setup_pump(hass: HomeAssistant, *, pump_temp_entity: str | None = None, **hub_overrides):
    hass.states.async_set("sensor.buy_price", "0.10")
    entry = await async_setup_hub(
        hass,
        [
            curve_subentry(
                title="FD25",
                preset="msz_fd25va",
                power_entity="sensor.fd25_power",
                energy_entity="sensor.fd25_energy",
                pump_temp_entity=pump_temp_entity,
            )
        ],
        **hub_overrides,
    )
    return entry


def _meta(entry):
    return next(iter(entry.runtime_data.source_meta.values()))


def _snapshot(entry):
    return next(iter(entry.runtime_data.data.snapshots.values()))


async def test_weather_covers_a_dead_outdoor_sensor(hass: HomeAssistant) -> None:
    """The pump stays priced and ranked, sourcing its temperature from weather."""
    hass.states.async_set("sensor.outdoor_temp", "unavailable")
    hass.states.async_set("weather.home", "cloudy", WEATHER_ATTRS)
    entry = await _setup_pump(hass, **{CONF_WEATHER_ENTITY: "weather.home"})

    meta, snap = _meta(entry), _snapshot(entry)
    assert meta["temp_source"] == "weather"
    assert meta["temp_used_c"] == pytest.approx(-5.0)
    # -5.0 is an exact knot on the FD25 preset's COP curve.
    assert snap.cop == pytest.approx(dict(PRESETS["msz_fd25va"].cop_points)[-5.0])
    assert snap.unavailable_reason is None
    assert snap.cost_eur_per_kwh is not None
    assert entry.runtime_data.data.ranked[0].name == "FD25"


async def test_hub_sensor_still_wins_over_weather(hass: HomeAssistant) -> None:
    """Weather is a fallback, not a replacement — the real sensor keeps priority."""
    hass.states.async_set("sensor.outdoor_temp", "-12.0")
    hass.states.async_set("weather.home", "cloudy", WEATHER_ATTRS)
    entry = await _setup_pump(hass, **{CONF_WEATHER_ENTITY: "weather.home"})

    meta = _meta(entry)
    assert meta["temp_source"] == "hub"
    assert meta["temp_used_c"] == pytest.approx(-12.0)


async def test_pump_sensor_wins_over_everything(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-12.0")
    hass.states.async_set("sensor.pump_outside", "-8.0")
    hass.states.async_set("weather.home", "cloudy", WEATHER_ATTRS)
    entry = await _setup_pump(
        hass, pump_temp_entity="sensor.pump_outside", **{CONF_WEATHER_ENTITY: "weather.home"}
    )

    meta = _meta(entry)
    assert meta["temp_source"] == "pump"
    assert meta["temp_used_c"] == pytest.approx(-8.0)


async def test_missing_pump_sensor_falls_through_to_weather(hass: HomeAssistant) -> None:
    """A renamed pump sensor plus a dead hub sensor is the exact live failure."""
    hass.states.async_set("sensor.outdoor_temp", "unavailable")
    hass.states.async_set("weather.home", "cloudy", WEATHER_ATTRS)
    entry = await _setup_pump(
        hass,
        pump_temp_entity="sensor.renamed_away",
        **{CONF_WEATHER_ENTITY: "weather.home"},
    )

    assert _meta(entry)["temp_source"] == "weather"
    assert _snapshot(entry).unavailable_reason is None


async def test_hub_temp_and_degree_hours_use_the_fallback(hass: HomeAssistant) -> None:
    """Degree-hour accumulation must not silently stall while the sensor is down.

    A half-day gap is worse than a full one: delivered heat keeps accruing
    while degree-hours do not, which would teach the UA learner roughly double
    the true heat-loss coefficient.
    """
    hass.states.async_set("sensor.outdoor_temp", "unavailable")
    hass.states.async_set("weather.home", "cloudy", WEATHER_ATTRS)
    entry = await _setup_pump(hass, **{CONF_WEATHER_ENTITY: "weather.home"})

    assert entry.runtime_data.data.temp_c == pytest.approx(-5.0)


async def test_no_usable_temperature_still_excludes_the_source(hass: HomeAssistant) -> None:
    """The None-propagation contract holds: nothing is invented when all links fail."""
    hass.states.async_set("sensor.outdoor_temp", "unavailable")
    hass.states.async_set("weather.home", "cloudy", {"temperature": None})
    entry = await _setup_pump(hass, **{CONF_WEATHER_ENTITY: "weather.home"})

    snap = _snapshot(entry)
    assert snap.unavailable_reason == REASON_TEMP_UNAVAILABLE
    assert snap.cop is None
    assert snap.cost_eur_per_kwh is None
    assert _meta(entry)["temp_source"] is None
    assert entry.runtime_data.data.ranked == []


async def test_weather_entity_update_recomputes_the_snapshot(hass: HomeAssistant) -> None:
    """While weather is the temperature source, its updates must drive a recompute."""
    hass.states.async_set("sensor.outdoor_temp", "unavailable")
    hass.states.async_set("weather.home", "cloudy", WEATHER_ATTRS)
    entry = await _setup_pump(hass, **{CONF_WEATHER_ENTITY: "weather.home"})
    assert _meta(entry)["temp_used_c"] == pytest.approx(-5.0)

    hass.states.async_set("weather.home", "sunny", {"temperature": 7.0})
    await _tick(hass, 3)  # past the coordinator's recompute debounce

    assert _meta(entry)["temp_used_c"] == pytest.approx(7.0)
    assert _snapshot(entry).cop == pytest.approx(dict(PRESETS["msz_fd25va"].cop_points)[7.0])


async def test_missing_entity_is_logged_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A mistyped id looks like a temporary outage; say so in the log instead."""
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await _setup_pump(hass, **{CONF_OUTDOOR_TEMP_ENTITY: "sensor.outdoor_temp"})
    coordinator = entry.runtime_data

    with caplog.at_level(logging.WARNING):
        coordinator._warn_missing_entities()
        coordinator._warn_missing_entities()

    warnings = [r for r in caplog.records if "sensor.fd25_power" in r.getMessage()]
    assert len(warnings) == 1
    assert "does not exist" in warnings[0].getMessage()
    # A configured entity that does exist must not be reported.
    assert not [r for r in caplog.records if "sensor.outdoor_temp" in r.getMessage()]


async def test_missing_entity_not_logged_before_startup_completes(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An integration that has not been set up yet is not a missing entity."""
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await _setup_pump(hass)
    coordinator = entry.runtime_data

    hass.set_state(CoreState.starting)
    try:
        with caplog.at_level(logging.WARNING):
            coordinator._warn_missing_entities()
        assert not [r for r in caplog.records if "does not exist" in r.getMessage()]
    finally:
        hass.set_state(CoreState.running)
