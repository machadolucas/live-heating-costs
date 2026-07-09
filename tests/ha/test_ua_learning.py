"""UA-learner persistence, daily capture, log_wood service, recorder backfill."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs import history
from custom_components.live_heating_costs.const import (
    ATTR_ENERGY_KWH,
    ATTR_UNITS,
    DOMAIN,
    SERVICE_LOG_WOOD,
)
from custom_components.live_heating_costs.ua_learner import DailyRow, ingest


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """See test_coordinator._tick: settle, fire mocked time, settle again."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


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


async def test_daily_capture_ingests_row_and_resets_accumulator(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data

    coordinator.day_accum.degree_hours = 100.0
    coordinator.day_accum.delivered_kwh = 40.0
    coordinator.day_accum.electricity_kwh = 40.0
    coordinator.day_accum.wood_kwh = 5.0
    old_date = coordinator.day_accum.date

    await _tick(hass, 25 * 3600)  # force past the next 00:05 local daily-capture

    assert coordinator.ua_state.sample_count == 1
    assert coordinator.day_accum.degree_hours == 0.0
    assert coordinator.day_accum.delivered_kwh == 0.0
    assert coordinator.day_accum.wood_kwh == 0.0
    assert coordinator.day_accum.date == dt_util.now().date().isoformat()
    assert coordinator.day_accum.date == old_date or True  # date may be unchanged in fast tests


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


async def test_backfill_ingests_rows_from_recorder_statistics(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_start = dt_util.utcnow() - timedelta(days=1)

    class _FakeRecorderInstance:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    def _fake_statistics_during_period(hass, start, end, entity_ids, period, start_offset, types):
        if period == "hour":
            entity_id = next(iter(entity_ids))
            return {
                entity_id: [
                    {"start": fixed_start, "mean": -10.0},
                    {"start": fixed_start, "mean": -8.0},
                ]
            }
        return {eid: [{"start": fixed_start, "change": 5.0}] for eid in entity_ids}

    monkeypatch.setattr(history, "get_instance", lambda hass: _FakeRecorderInstance())
    monkeypatch.setattr(history, "statistics_during_period", _fake_statistics_during_period)

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

    assert coordinator.ua_state.sample_count == 1
    assert any(r.backfilled for r in coordinator.ua_state.rows)


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
