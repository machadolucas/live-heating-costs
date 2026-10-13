"""Day accumulator integrity: missed captures, downtime gaps, coverage, shadow MAE.

Each of these used to let a day teach the UA learner something it never
observed: a midnight missed while HA was down folded two days into one, a
restart credited the whole outage's energy counter jump, a day with half its
temperature readings passed the quality gate, and the shadow MAE scored the
model on the very day it had just learned from.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import async_setup_hub, hub_entry_data, resistive_subentry
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.live_heating_costs.const import (
    DOMAIN,
    SAMPLE_INTERVAL_S,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)
from custom_components.live_heating_costs.coordinator import DayAccum
from custom_components.live_heating_costs.history import local_day_length_s


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=dt_util.get_default_time_zone())


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """See test_coordinator._tick: settle, fire mocked time, settle again."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _setup_with_store(hass: HomeAssistant, hass_storage: dict, stored: dict):
    """Set up a one-resistive-source hub whose Store already holds ``stored``."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=hub_entry_data(),
        unique_id=None,
        subentries_data=[resistive_subentry(energy_entity="sensor.resistive_energy")],
    )
    key = STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
    hass_storage[key] = {"version": STORAGE_VERSION, "minor_version": 1, "key": key, "data": stored}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _stale_accum(date: str, **overrides) -> dict:
    accum = {
        "date": date,
        "degree_hours": 100.0,
        "delivered_kwh": 40.0,
        "electricity_kwh": 40.0,
        "wood_kwh": 0.0,
        "per_source_kwh": {},
        "last_energy_counters": {"meter": 500.0},
        "temp_covered_s": 86400.0,
    }
    accum.update(overrides)
    return accum


async def test_missed_midnight_is_captured_on_load(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory
) -> None:
    """HA down across midnight: the 00:05 job never fired for that day."""
    freezer.move_to(_local(2026, 1, 11, 10))
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await _setup_with_store(hass, hass_storage, {"day_accum": _stale_accum("2026-01-09")})
    coordinator = entry.runtime_data

    assert [r.date for r in coordinator.ua_state.rows] == ["2026-01-09"]
    assert coordinator.ua_state.rows[0].quality is True
    assert coordinator.ua_state.sample_count == 1
    # Today starts fresh — none of the stale day's totals leak into it — but
    # keeps the counter baselines.
    assert coordinator.day_accum.date == "2026-01-11"
    assert coordinator.day_accum.delivered_kwh == 0.0
    assert coordinator.day_accum.last_energy_counters == {"meter": 500.0}


@pytest.mark.parametrize(
    "overrides",
    [
        {"temp_covered_s": 43200.0},  # temperature known for only half the day
        {"temp_covered_s": None},  # store written before coverage was tracked
    ],
)
async def test_partially_observed_day_does_not_teach(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory, overrides
) -> None:
    freezer.move_to(_local(2026, 1, 11, 10))
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    accum = _stale_accum("2026-01-10", **overrides)
    if accum["temp_covered_s"] is None:
        del accum["temp_covered_s"]
    entry = await _setup_with_store(hass, hass_storage, {"day_accum": accum})
    coordinator = entry.runtime_data

    row = coordinator.ua_state.rows[-1]
    assert row.date == "2026-01-10"
    assert row.quality is False  # stored for display, never learned from
    assert coordinator.ua_state.sample_count == 0
    assert coordinator.ua_state.learned is None


async def test_shadow_mae_scores_the_prediction_made_before_learning(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_local(2026, 1, 11, 10))
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await _setup_with_store(hass, hass_storage, {"day_accum": _stale_accum("2026-01-10")})
    state = entry.runtime_data.ua_state

    # Seed UA 140 W/°C x 100 °C·h = 14 kWh predicted vs 40 kWh delivered.
    # (Scored after ingest, the MAE would be None on the first day and would
    # then measure the model against a day it had already learned from.)
    assert state.rolling_mae_kwh == pytest.approx(26.0)
    assert state.last_predicted_kwh is None


async def test_sampling_gap_rebaselines_energy_counters(hass: HomeAssistant, freezer) -> None:
    hass.states.async_set("sensor.buy_price", "0.10")
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.resistive_energy", "100.0")
    entry = await async_setup_hub(
        hass, [resistive_subentry(cop=1.0, energy_entity="sensor.resistive_energy")]
    )
    coordinator = entry.runtime_data
    freezer.tick(SAMPLE_INTERVAL_S)
    await _tick(hass, SAMPLE_INTERVAL_S + 5)  # seeds the baseline
    assert coordinator.day_accum.last_tick_utc is not None

    # Simulate a 10-minute outage: the counter moved while nothing was
    # sampled, so the jump is re-baselined, not credited.
    coordinator.day_accum.last_tick_utc = dt_util.utcnow() - timedelta(minutes=10)
    hass.states.async_set("sensor.resistive_energy", "105.0")
    freezer.tick(SAMPLE_INTERVAL_S)
    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    assert coordinator.day_accum.electricity_kwh == 0.0
    assert coordinator.day_accum.delivered_kwh == 0.0
    sub_id = next(iter(coordinator.sources))
    assert coordinator.day_accum.last_energy_counters[sub_id] == pytest.approx(105.0)

    # Normal cadence resumes crediting deltas.
    hass.states.async_set("sensor.resistive_energy", "105.015")
    freezer.tick(SAMPLE_INTERVAL_S)
    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    assert coordinator.day_accum.electricity_kwh == pytest.approx(0.015)


async def test_temperature_coverage_tracks_ticks_with_a_reading(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data

    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    assert coordinator.day_accum.temp_covered_s == pytest.approx(SAMPLE_INTERVAL_S)

    hass.states.async_set("sensor.outdoor_temp", "unavailable")  # and no weather entity
    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    assert coordinator.day_accum.temp_covered_s == pytest.approx(SAMPLE_INTERVAL_S)


async def test_day_length_is_dst_aware(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Helsinki")
    assert local_day_length_s("2026-01-15") == 24 * 3600
    assert local_day_length_s("2026-03-29") == 23 * 3600  # spring forward
    assert local_day_length_s("2026-10-25") == 25 * 3600  # fall back

    full_short_day = DayAccum(date="2026-03-29", temp_covered_s=23 * 3600)
    assert full_short_day.coverage() == pytest.approx(1.0)
    assert DayAccum(date="2026-10-25", temp_covered_s=23 * 3600).coverage() < 0.93


def test_day_accum_round_trip_and_legacy_load() -> None:
    now = datetime(2026, 1, 10, 12, tzinfo=dt_util.UTC)
    accum = DayAccum(date="2026-01-10", temp_covered_s=600.0, last_tick_utc=now)
    restored = DayAccum.from_dict(accum.to_dict())
    assert restored == accum

    legacy = DayAccum.from_dict({"date": "2026-01-10", "degree_hours": 5.0})
    assert legacy.last_tick_utc is None  # first tick will re-baseline
    assert legacy.temp_covered_s == 0.0
