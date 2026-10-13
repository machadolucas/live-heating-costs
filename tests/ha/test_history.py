"""Optional-profile backfill never teaches unknown heat or corrupt aggregates."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.live_heating_costs import history
from custom_components.live_heating_costs.const import DEMAND_MODE_INTERNAL
from custom_components.live_heating_costs.models import source_from_subentry
from custom_components.live_heating_costs.ua_learner import UaState


def coordinator(sources, mapping):
    return SimpleNamespace(
        _demand_mode=DEMAND_MODE_INTERNAL,
        _ua_learning=True,
        _backfill_days=2,
        backfill_done=False,
        _outdoor_temp_entity="sensor.outdoor",
        _base_temp=17,
        sources={sid: source_from_subentry(cfg["data"]) for sid, cfg in sources.items()},
        shadow=SimpleNamespace(config={"sources": mapping}),
        ua_state=UaState(0.14),
        store=SimpleNamespace(schedule_save=Mock()),
        store_snapshot=lambda: {},
    )


def fake_recorder(monkeypatch, hass, energy, *, missing=None, spike=None):
    end = dt_util.as_utc(dt_util.start_of_local_day(dt_util.now().date()))
    start = end - timedelta(days=1)
    temperatures = [{"start": start + timedelta(hours=i), "mean": 0} for i in range(24)]
    energy_stats = {}
    for entity, value in energy.items():
        energy_stats[entity] = [
            {
                "start": start + timedelta(hours=i),
                "change": 8381.205455 if spike == (entity, i) else value,
            }
            for i in range(24)
            if missing != (entity, i)
        ]
    calls = []

    def stats(_hass, _start, _end, ids, period, units, types):
        calls.append((ids, period, units))
        if "mean" in types:
            return {"sensor.outdoor": temperatures}
        return {eid: energy_stats.get(eid, []) for eid in ids}

    class Recorder:
        async def async_add_executor_job(self, function, *args):
            return function(*args)

    monkeypatch.setattr(history, "get_instance", lambda _hass: Recorder())
    monkeypatch.setattr(history, "statistics_during_period", stats)
    return calls


async def test_individual_floor_override_avoids_corrupt_aggregate(hass: HomeAssistant, monkeypatch):
    ctx = coordinator(
        {"floor": resistive_subentry(energy_entity="sensor.bad_aggregate")},
        {"floor": {"energy_entities": ["sensor.floor_a", "sensor.floor_b"], "max_electric_kw": 2}},
    )
    calls = fake_recorder(
        monkeypatch,
        hass,
        {"sensor.floor_a": 0.2, "sensor.floor_b": 0.2, "sensor.bad_aggregate": 8381},
    )
    assert await history.async_backfill(hass, ctx) == 1
    assert ctx.ua_state.sample_count == 1
    assert ctx.ua_state.rows[-1].delivered_kwh == pytest.approx(9.6)
    energy_call = next(call for call in calls if call[2] == {"energy": "kWh"})
    assert energy_call[0] == {"sensor.floor_a", "sensor.floor_b"}
    assert energy_call[1] == "hour"


@pytest.mark.parametrize("source", [resistive_subentry(), wood_subentry()])
async def test_unknown_radiator_or_fuel_blocks_backfill_learning(hass, monkeypatch, source):
    ctx = coordinator(
        {"floor": resistive_subentry(energy_entity="sensor.floor"), "unknown": source}, {}
    )
    fake_recorder(monkeypatch, hass, {"sensor.floor": 0.4})
    assert await history.async_backfill(hass, ctx) == 1
    assert ctx.ua_state.sample_count == 0
    assert not ctx.ua_state.rows[-1].quality
    assert ctx.ua_state.rows[-1].delivered_kwh == pytest.approx(9.6)


@pytest.mark.parametrize("problem", ["missing", "spike"])
async def test_one_missing_or_impossible_hour_does_not_teach_partial_heat(
    hass, monkeypatch, problem
):
    ctx = coordinator(
        {"floor": resistive_subentry(energy_entity="sensor.floor")},
        {"floor": {"max_electric_kw": 2}},
    )
    fake_recorder(monkeypatch, hass, {"sensor.floor": 0.4}, **{problem: ("sensor.floor", 12)})
    assert await history.async_backfill(hass, ctx) == 1
    assert ctx.ua_state.sample_count == 0
    assert not ctx.ua_state.rows[-1].quality
    assert ctx.ua_state.rows[-1].delivered_kwh == pytest.approx(9.2)


async def test_live_capture_missing_source_coverage_is_not_quality(hass, monkeypatch):
    # The coordinator's guard is exercised without starting background sampler jobs.
    from custom_components.live_heating_costs.coordinator import (
        DayAccum,
        LiveHeatingCostsCoordinator,
    )

    capture = LiveHeatingCostsCoordinator._capture_day
    ctx = coordinator(
        {
            "floor": resistive_subentry(energy_entity="sensor.floor"),
            "radiator": resistive_subentry(),
        },
        {},
    )
    day = dt_util.now().date().isoformat()
    accum = DayAccum(
        date=day,
        degree_hours=100,
        delivered_kwh=30,
        electricity_kwh=30,
        temp_covered_s=history.local_day_length_s(day),
        heat_covered_s={"floor": history.local_day_length_s(day)},
    )
    capture(ctx, accum)
    assert ctx.ua_state.sample_count == 0
    assert not ctx.ua_state.rows[-1].quality


async def test_duplicate_source_meter_cannot_teach_double_heat(hass, monkeypatch):
    ctx = coordinator(
        {
            "a": resistive_subentry(energy_entity="sensor.shared"),
            "b": resistive_subentry(energy_entity="sensor.shared"),
        },
        {},
    )
    calls = fake_recorder(monkeypatch, hass, {"sensor.shared": 0.4})
    assert await history.async_backfill(hass, ctx) == 0
    assert not calls
    assert ctx.ua_state.sample_count == 0
