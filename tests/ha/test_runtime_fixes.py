"""Coordinator/sensor runtime behaviour: units, throttling, forecast rebuilds, caches."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import async_setup_hub, curve_subentry, resistive_subentry
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs import history
from custom_components.live_heating_costs.binary_sensor import SourceHeatingBinarySensor
from custom_components.live_heating_costs.const import (
    CONF_DAY_AHEAD_ENTITY,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    SAMPLE_INTERVAL_S,
)
from custom_components.live_heating_costs.forecast import SourceSlotCost
from custom_components.live_heating_costs.persistence import RuntimeStore
from custom_components.live_heating_costs.sensor import (
    SourceCopSensor,
    SourceCostForecastSensor,
)


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """See test_coordinator._tick: settle, fire mocked time, settle again."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _fd25(**kwargs):
    return curve_subentry(
        title="FD25",
        preset="msz_fd25va",
        power_entity="sensor.fd25_power",
        energy_entity="sensor.fd25_energy",
        **kwargs,
    )


def _snap(coordinator, name):
    return next(s for s in coordinator.data.snapshots.values() if s.name == name)


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #


async def test_fahrenheit_temperature_sensor_is_converted(hass: HomeAssistant) -> None:
    # 23 °F == -5 °C, an exact knot on the FD25 COP curve (2.9). Read raw, 23
    # would be taken as a mild 23 °C and the pump priced at a summer COP.
    hass.states.async_set("sensor.outdoor_temp", "23", {"unit_of_measurement": "°F"})
    hass.states.async_set("sensor.buy_price", "0.10")
    entry = await async_setup_hub(hass, [_fd25()])
    coordinator = entry.runtime_data
    await _tick(hass, SAMPLE_INTERVAL_S + 5)

    assert coordinator.data.temp_c == pytest.approx(-5.0)
    assert _snap(coordinator, "FD25").cop == pytest.approx(2.9)
    assert coordinator.data.temp_24h_mean == pytest.approx(-5.0)


async def test_wh_energy_and_kw_power_are_converted(hass: HomeAssistant) -> None:
    # A Wh counter read as kWh would credit 1000x the electricity (and heat);
    # a kW power reading would never clear the W standby threshold.
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "0.10")
    hass.states.async_set("sensor.fd25_power", "1.2", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.fd25_energy", "100000", {"unit_of_measurement": "Wh"})
    entry = await async_setup_hub(hass, [_fd25()])
    coordinator = entry.runtime_data
    sub_id = next(iter(coordinator.sources))
    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    await _tick(hass, SAMPLE_INTERVAL_S)
    hass.states.async_set("sensor.fd25_energy", "100500", {"unit_of_measurement": "Wh"})
    await _tick(hass, SAMPLE_INTERVAL_S)

    sample = coordinator._samples[sub_id][-1]
    assert sample.power_w == pytest.approx(1200.0)
    assert sample.energy_kwh == pytest.approx(100.5)
    assert coordinator.day_accum.electricity_kwh == pytest.approx(0.5)


async def test_meter_unavailable_across_a_gap_is_rebaselined(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "0.10")
    hass.states.async_set("sensor.fd25_energy", "100.0")
    entry = await async_setup_hub(hass, [_fd25()])
    coordinator = entry.runtime_data
    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    await _tick(hass, SAMPLE_INTERVAL_S)
    # Outage: no ticks for an hour, and the meter is still down on the first
    # tick after it; it comes back one tick later with the outage's energy.
    coordinator.day_accum.last_tick_utc = dt_util.utcnow() - timedelta(hours=1)
    hass.states.async_set("sensor.fd25_energy", "unavailable")
    await _tick(hass, SAMPLE_INTERVAL_S)
    hass.states.async_set("sensor.fd25_energy", "103.0")
    await _tick(hass, SAMPLE_INTERVAL_S)
    assert coordinator.day_accum.electricity_kwh == pytest.approx(0.0)


async def test_buy_price_in_cents_is_scaled(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "5.3", {"unit_of_measurement": "c/kWh"})
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data

    assert coordinator.data.buy_eur_per_kwh == pytest.approx(0.053)
    assert _snap(coordinator, "Resistive").cost_eur_per_kwh == pytest.approx(0.053)


async def test_weather_forecast_units_and_implausible_points(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("weather.home", "cloudy", {"temperature_unit": "°F", "temperature": 23})
    now = dt_util.utcnow().replace(microsecond=0)
    items = [
        {"datetime": (now + timedelta(hours=1)).isoformat(), "temperature": 23.0},
        {"datetime": (now + timedelta(hours=2)).isoformat(), "temperature": float("nan")},
        {"datetime": (now + timedelta(hours=3)).isoformat(), "temperature": -999.0},
        {"datetime": (now + timedelta(hours=4)).isoformat(), "temperature": None},
        {"datetime": (now + timedelta(hours=5)).isoformat(), "temperature": 32.0},
    ]

    async def _fake_get_forecasts(call):
        return {call.data["entity_id"]: {"forecast": items}}

    hass.services.async_register(
        "weather", "get_forecasts", _fake_get_forecasts, supports_response=SupportsResponse.ONLY
    )
    entry = await async_setup_hub(hass, [_fd25()], **{CONF_WEATHER_ENTITY: "weather.home"})
    coordinator = entry.runtime_data
    coordinator._weather_cache = None

    temps = await coordinator._async_weather_temps()
    assert [p.temp_c for p in temps] == [pytest.approx(-5.0), pytest.approx(0.0)]


# --------------------------------------------------------------------------- #
# Recompute throttle
# --------------------------------------------------------------------------- #


async def test_state_changes_throttle_rather_than_postpone(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    event = SimpleNamespace(data={"entity_id": "sensor.fd25_power"})

    coordinator._handle_state_change(event)
    first = coordinator._debounce_unsub
    assert first is not None
    # A 1 Hz sensor must not keep re-arming (and so postponing) the timer.
    for _ in range(5):
        coordinator._handle_state_change(event)
    assert coordinator._debounce_unsub is first

    await _tick(hass, 3)
    assert coordinator._debounce_unsub is None


# --------------------------------------------------------------------------- #
# Forecast rebuild concurrency
# --------------------------------------------------------------------------- #


async def test_forecast_rebuild_reads_prices_after_the_weather_await(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    hass.states.async_set("sensor.buy_price", "0.10")
    now = dt_util.utcnow().replace(microsecond=0)

    def _prices(buy: float) -> dict:
        return {
            "data_today": [
                {"start": (now + timedelta(minutes=15 * i)).isoformat(), "buy": buy}
                for i in range(8)
            ]
        }

    hass.states.async_set("sensor.day_ahead", "0.10", _prices(0.10))
    release = asyncio.Event()

    async def _slow_get_forecasts(call):
        await release.wait()
        return {call.data["entity_id"]: {"forecast": []}}

    hass.services.async_register(
        "weather", "get_forecasts", _slow_get_forecasts, supports_response=SupportsResponse.ONLY
    )
    release.set()  # let setup's own rebuild through
    entry = await async_setup_hub(
        hass,
        [resistive_subentry(cop=1.0)],
        **{CONF_DAY_AHEAD_ENTITY: "sensor.day_ahead", CONF_WEATHER_ENTITY: "weather.home"},
    )
    coordinator = entry.runtime_data
    sub_id = next(iter(coordinator.sources))

    release.clear()
    coordinator._weather_cache = None
    task = hass.async_create_task(coordinator.async_rebuild_forecast())
    await asyncio.sleep(0)
    # New prices (and their invalidation) land while the rebuild is awaiting.
    hass.states.async_set("sensor.day_ahead", "0.30", _prices(0.30))
    coordinator._forecast_dirty = True
    release.set()
    await task

    assert coordinator.forecast[sub_id][0].buy == pytest.approx(0.30)
    # The invalidation raised mid-rebuild must survive it.
    assert coordinator._forecast_dirty is True


async def test_forecast_requests_are_coalesced(hass: HomeAssistant) -> None:
    """While one rebuild runs, any number of requests queue exactly one more."""
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    calls = 0
    release = asyncio.Event()
    original = coordinator._async_rebuild_forecast_locked

    async def _slow():
        nonlocal calls
        calls += 1
        await release.wait()
        await original()

    coordinator._async_rebuild_forecast_locked = _slow
    for _ in range(5):
        coordinator._request_forecast_rebuild()
    await asyncio.sleep(0)
    release.set()
    await hass.async_block_till_done()
    assert calls == 2  # the running one + one queued for everything after it
    assert coordinator._forecast_queued is False


async def test_forecast_rebuild_runs_on_the_slot_grid(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Rebuilds land just after :00/:15/:30/:45, not 15 min after setup time."""
    freezer.move_to(dt_util.parse_datetime("2026-01-10T12:07:00+00:00"))
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    requests: list = []
    coordinator._request_forecast_rebuild = lambda: requests.append(dt_util.utcnow())

    for minute, second in ((14, 59), (15, 5)):
        freezer.move_to(dt_util.parse_datetime(f"2026-01-10T12:{minute}:{second:02d}+00:00"))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert [(t.minute, t.second) for t in requests] == [(15, 5)]


# --------------------------------------------------------------------------- #
# Forecast sensors
# --------------------------------------------------------------------------- #


async def test_source_forecast_state_is_the_slot_containing_now(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    sub_id = next(iter(coordinator.sources))
    now = dt_util.utcnow()
    past = SourceSlotCost(
        now - timedelta(minutes=30), now - timedelta(minutes=15), 0.5, 1.0, 9, False
    )
    current = SourceSlotCost(
        now - timedelta(minutes=15), now + timedelta(minutes=1), 0.2, 1.0, 9, False
    )
    coordinator.data = replace(
        coordinator.data, forecast={sub_id: [past, current]}, forecast_updated=now
    )

    sensor = SourceCostForecastSensor(coordinator, sub_id)
    assert sensor.native_value == pytest.approx(20.0)

    # Attribute list is cached per rebuild: same forecast_updated, same object.
    first = sensor.extra_state_attributes["forecast"]
    assert sensor.extra_state_attributes["forecast"] is first
    coordinator.data = replace(coordinator.data, forecast_updated=now + timedelta(seconds=1))
    assert sensor.extra_state_attributes["forecast"] is not first


async def test_forecast_sensor_stays_unitless(hass: HomeAssistant) -> None:
    # Adding a unit would raise a units-changed statistics repair on existing
    # installs and make price_source rescale the EUR/kWh forecast attribute.
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    sub_id = next(iter(entry.subentries))
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{sub_id}_cost_forecast")
    assert "unit_of_measurement" not in hass.states.get(entity_id).attributes


def test_per_tick_attributes_are_not_recorded() -> None:
    assert {"power_w", "heating_fraction", "hvac_action"} <= (
        SourceHeatingBinarySensor._unrecorded_attributes
    )
    assert {"modeled_cop", "measured_cop", "heating_fraction", "window_energy_kwh"} <= (
        SourceCopSensor._unrecorded_attributes
    )


# --------------------------------------------------------------------------- #
# 24 h temperature history
# --------------------------------------------------------------------------- #


async def test_temp_stats_ignore_stale_samples_and_never_extrapolate(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    coordinator._temp_history = deque(
        [
            (now - timedelta(hours=30), 40.0),  # outside the 24 h window
            (now - timedelta(hours=2), -3.0),
            (now, -3.0),
        ],
        maxlen=coordinator._temp_history.maxlen,
    )
    coordinator._refresh_temp_stats(now)

    assert coordinator._temp_24h_mean() == pytest.approx(-3.0)
    # base 17 °C: 20 °C deficit over the 2 h actually observed — not 24 h's
    # worth (480) from clamping the first sample back to the window start.
    assert coordinator._degree_hours_24h() == pytest.approx(40.0)


async def test_temp_history_is_seeded_from_recorder_hourly_means(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.states.async_set("sensor.outdoor_temp", "-5.0")
    entry = await async_setup_hub(hass, [resistive_subentry()])
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    hour0 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)

    async def _fake_means(hass, entity_id, start, end):
        assert entity_id == "sensor.outdoor_temp"
        assert end - start == timedelta(hours=24)
        return [(hour0 + timedelta(hours=h), -3.0) for h in range(22)]

    monkeypatch.setattr(history, "async_hourly_temp_means", _fake_means)
    await coordinator.async_seed_temp_history()

    assert len(coordinator._temp_history) == 22
    assert coordinator.data.temp_24h_mean == pytest.approx(-3.0)
    assert coordinator.data.degree_hours_24h == pytest.approx(20.0 * 21, rel=1e-6)

    # Live samples keep precedence; seeding never duplicates them.
    await _tick(hass, SAMPLE_INTERVAL_S + 5)
    assert coordinator._temp_history[-1][1] == pytest.approx(-5.0)


async def test_hourly_temp_means_without_recorder_is_empty(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    assert await history.async_hourly_temp_means(hass, "sensor.x", now, now) == []


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


async def test_tick_save_does_not_postpone_a_pending_write(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    store = RuntimeStore(hass, "entry123")
    key = "live_heating_costs.entry123"

    store.schedule_save({"n": 1}, 10)  # e.g. a wood log
    store.schedule_save({"n": 2}, 300)  # the sampling tick right after it
    await _tick(hass, 11)
    # Written at the 10 s deadline (Store alone would have dragged it to
    # 300 s), carrying the newest data.
    assert hass_storage[key]["data"] == {"n": 2}


class _FakeHandle:
    def __init__(self, when, cb):
        self._when, self.cb, self.cancelled = when, cb, False

    def when(self):
        return self._when

    def cancel(self):
        self.cancelled = True


async def test_schedule_save_only_ever_pulls_the_deadline_earlier(hass: HomeAssistant) -> None:
    store = RuntimeStore(hass, "entry456")
    clock = [1000.0]
    handles: list[_FakeHandle] = []

    def _call_at(when, cb):
        handles.append(_FakeHandle(when, cb))
        return handles[-1]

    store._hass = SimpleNamespace(loop=SimpleNamespace(time=lambda: clock[0], call_at=_call_at))
    delays: list[float] = []
    store._store = SimpleNamespace(async_delay_save=lambda func, delay: delays.append(delay))

    # A steady 60 s tick asking for 300 s schedules one write, not a new
    # (later) one per tick — so it is due 300 s after the first tick.
    for _ in range(4):
        store.schedule_save({"tick": clock[0]}, 300)
        clock[0] += 60
    assert delays == [300]
    assert handles[-1].when() == pytest.approx(1300.0)

    # A significant event pulls it earlier...
    store.schedule_save({"event": True}, 10)
    assert delays == [300, 10]
    assert handles[0].cancelled
    assert store._data_to_save() == {"event": True}

    # ...and once that deadline has passed, the next tick starts a new cycle.
    handles[-1].cb()
    store.schedule_save({"tick": "next"}, 300)
    assert delays == [300, 10, 300]
