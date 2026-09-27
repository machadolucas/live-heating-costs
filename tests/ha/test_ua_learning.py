"""UA-learner persistence, daily capture, log_wood service, recorder backfill."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry, wood_subentry
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs import history
from custom_components.live_heating_costs.const import (
    ATTR_ENERGY_KWH,
    ATTR_UNITS,
    DOMAIN,
    SAMPLE_INTERVAL_S,
    SERVICE_LOG_WOOD,
)
from custom_components.live_heating_costs.ua_learner import DailyRow, ingest


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """An aware datetime in the test instance's local time zone."""
    return datetime(year, month, day, hour, minute, tzinfo=dt_util.get_default_time_zone())


async def test_ua_state_persists_across_reload(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data

    row = DailyRow(
        date="2026-01-01",
        degree_hours=100.0,
        delivered_kwh=50.0,
        electricity_kwh=50.0,
        wood_kwh=0.0,
        quality=True,
    )
    coordinator.ua_state = ingest(coordinator.ua_state, row)
    assert coordinator.ua_state.sample_count == 1
    learned_before = coordinator.ua_state.learned

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reloaded = entry.runtime_data
    assert reloaded is not coordinator
    assert reloaded.ua_state.sample_count == 1
    assert reloaded.ua_state.learned == pytest.approx(learned_before)


async def test_daily_capture_ingests_row_and_resets_accumulator(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_local(2026, 1, 10, 12))
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    assert coordinator.day_accum.date == "2026-01-10"

    coordinator.day_accum.degree_hours = 100.0
    coordinator.day_accum.delivered_kwh = 40.0
    coordinator.day_accum.electricity_kwh = 40.0
    coordinator.day_accum.wood_kwh = 5.0
    coordinator.day_accum.temp_covered_s = 86400.0  # fully observed day
    coordinator.day_accum.last_energy_counters = {"meter": 123.0}

    freezer.move_to(_local(2026, 1, 11, 0, 5))
    # Ticked continuously up to a minute ago: the jump above isn't an outage
    # (an outage would rightly re-baseline the counters asserted below).
    coordinator.day_accum.last_tick_utc = dt_util.utcnow() - timedelta(seconds=SAMPLE_INTERVAL_S)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert coordinator.ua_state.sample_count == 1
    row = coordinator.ua_state.rows[-1]
    assert row.date == "2026-01-10"
    assert row.quality is True
    assert coordinator.day_accum.date == "2026-01-11"
    assert coordinator.day_accum.degree_hours < 1.0  # at most the one tick since midnight
    assert coordinator.day_accum.delivered_kwh == 0.0
    assert coordinator.day_accum.wood_kwh == 0.0
    # Counter baselines survive the reset so the new day's first delta isn't lost.
    assert coordinator.day_accum.last_energy_counters == {"meter": 123.0}

    # A second trigger the same day (00:05 job after the tick already rolled,
    # or another tick) must never capture again.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAMPLE_INTERVAL_S))
    await hass.async_block_till_done()
    assert coordinator.ua_state.sample_count == 1
    assert [r.date for r in coordinator.ua_state.rows] == ["2026-01-10"]


async def test_log_wood_service_energy_kwh_and_units(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(
        hass, [wood_subentry(price_per_unit_eur=0.10, energy_density=1.0, efficiency=1.0)]
    )
    coordinator = entry.runtime_data

    await hass.services.async_call(DOMAIN, SERVICE_LOG_WOOD, {ATTR_ENERGY_KWH: 3.0}, blocking=True)
    assert coordinator.day_accum.wood_kwh == pytest.approx(3.0)

    # units mode: 2 fuel units * density(1.0) * efficiency(1.0) = 2.0 kWh
    await hass.services.async_call(DOMAIN, SERVICE_LOG_WOOD, {ATTR_UNITS: 2.0}, blocking=True)
    assert coordinator.day_accum.wood_kwh == pytest.approx(5.0)
    assert len(coordinator.wood_log) == 2


def _fake_recorder(monkeypatch: pytest.MonkeyPatch, temp_rows, energy_rows, calls=None):
    class _FakeRecorderInstance:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    def _fake_statistics_during_period(hass, start, end, entity_ids, period, units, types):
        if calls is not None:
            calls.append({"start": start, "end": end, "period": period, "units": units})
        if period == "hour":
            return {next(iter(entity_ids)): list(temp_rows)}
        return {eid: list(energy_rows) for eid in entity_ids}

    monkeypatch.setattr(history, "get_instance", lambda hass: _FakeRecorderInstance())
    monkeypatch.setattr(history, "statistics_during_period", _fake_statistics_during_period)


def _fd25_subentry():
    return curve_subentry(
        title="FD25",
        preset="msz_fd25va",
        power_entity="sensor.fd25_power",
        energy_entity="sensor.fd25_energy",
    )


async def test_backfill_ingests_rows_from_recorder_statistics(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_local(2026, 1, 12, 15))
    full_day = dt_util.as_utc(_local(2026, 1, 11))  # yesterday: 24 hourly means
    gappy_day = dt_util.as_utc(_local(2026, 1, 10))  # only 10 hours recorded
    temp_rows = [{"start": full_day + timedelta(hours=h), "mean": -10.0} for h in range(24)] + [
        {"start": gappy_day + timedelta(hours=h), "mean": -10.0} for h in range(10)
    ]
    energy_rows = [{"start": full_day, "change": 20.0}, {"start": gappy_day, "change": 20.0}]
    calls: list[dict] = []
    _fake_recorder(monkeypatch, temp_rows, energy_rows, calls)

    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [_fd25_subentry()])
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data

    # Only the fully observed day teaches: the gappy one would have 10 h of
    # degree-hours against a whole day's energy (UA inflated ~2.4x).
    assert coordinator.ua_state.sample_count == 1
    assert [r.date for r in coordinator.ua_state.rows] == ["2026-01-11"]
    assert coordinator.ua_state.rows[0].backfilled
    assert coordinator.backfill_done is True

    # Whole local days only: the query ends at today's local midnight
    # (today's partial day belongs to the live accumulator) and starts at a
    # local midnight N days back; temperatures are requested in °C.
    backfill_calls = [c for c in calls if c["end"] == dt_util.as_utc(_local(2026, 1, 12))]
    assert backfill_calls, calls
    for call in backfill_calls:
        assert dt_util.as_local(call["start"]).time() == time(0, 0)
    hour_call = next(c for c in backfill_calls if c["period"] == "hour")
    assert hour_call["units"] == {"temperature": "°C"}


async def test_backfill_runs_once_even_when_it_teaches_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    _fake_recorder(monkeypatch, [], [], calls)  # recorder answers, but no history

    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [_fd25_subentry()])
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.ua_state.sample_count == 0
    assert coordinator.backfill_done is True
    backfill_queries = len([c for c in calls if c["period"] == "day"])
    assert backfill_queries == 1

    # A reload restores the flag from the store and does not query again.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.backfill_done is True
    assert len([c for c in calls if c["period"] == "day"]) == backfill_queries


async def test_backfill_without_recorder_is_a_noop(hass: HomeAssistant) -> None:
    # The test harness never sets up the `recorder` component, so
    # get_instance(hass) genuinely fails here — exercised implicitly by every
    # other setup in this file too, asserted explicitly here.
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
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
    )
    coordinator = entry.runtime_data
    assert coordinator.ua_state.sample_count == 0

    ingested = await history.async_backfill(hass, coordinator)
    assert ingested == 0
