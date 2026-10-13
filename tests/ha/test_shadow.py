"""Optional thermal model: HA boundaries, continuity and validation remain explicit."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import async_setup_hub, resistive_subentry
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.live_heating_costs import calibration
from custom_components.live_heating_costs.const import DOMAIN
from custom_components.live_heating_costs.sensor import CheapestSourceSensor
from custom_components.live_heating_costs.shadow_config import CONF_SHADOW

_NOW = datetime(2026, 1, 15, 12, tzinfo=UTC)


def _source(title: str, sid: str, energy_entity: str | None = None):
    result = resistive_subentry(title=title, energy_entity=energy_entity)
    result["subentry_id"] = sid
    return result


def _config(*, verified: bool = False, sources: dict | None = None, **overrides) -> dict:
    return {
        "indoor": [{"entity_id": "sensor.room_temperature", "calibrated": True}],
        "calibrated": verified,
        "ventilation": {
            "model_verified": verified,
            "controller_verified": verified,
            "supply_control_entity": "number.supply_control",
            "extract_control_entity": "number.extract_control",
            "supply_resistance_pa_per_ls2": 122.23009762664459 / 48.82366458342551**2,
            "extract_resistance_pa_per_ls2": 126.88519720649228 / 65.67172067228347**2,
            "supply_temp_entity": "sensor.supply_temperature",
            "power_entity": "sensor.ventilation_power",
            "auxiliary_power_w": 0,
            "preheater_entity": "number.preheater",
            "postheater_entity": "number.postheater",
        },
        "sources": sources or {},
        "local_demand_entity": "sensor.local_demand",
        **overrides,
    }


def _initial_states(hass: HomeAssistant) -> None:
    for eid, value, unit in (
        ("sensor.outdoor_temp", -5, "°C"),
        ("sensor.buy_price", 0.1, "EUR/kWh"),
        ("sensor.room_temperature", 20, "°C"),
        ("sensor.supply_temperature", 17, "°C"),
        ("sensor.energy", 100, "kWh"),
        ("sensor.local_demand", 0, "W"),
        ("number.supply_control", 60, "%"),
        ("number.extract_control", 60, "%"),
        ("number.preheater", 0, "%"),
        ("number.postheater", 0, "%"),
        ("sensor.ventilation_power", 87.53514808912324, "W"),
    ):
        hass.states.async_set(eid, str(value), {"unit_of_measurement": unit})


async def _sample(hass, coordinator, freezer, *, seconds=60, states=None) -> None:
    """Controlled sampling with actual clock advancement and settled HA listeners."""
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=seconds))
    for eid, value, unit in states or []:
        hass.states.async_set(eid, str(value), {"unit_of_measurement": unit})
    if seconds == 0:
        coordinator._handle_sampling_tick(dt_util.utcnow())
    else:
        async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry, suffix: str, platform: str = "sensor") -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    assert entity_id is not None
    return entity_id


async def test_disabled_shadow_preserves_legacy_demand_and_allocations(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(hass, [_source("Floor", "floor", "sensor.energy")])
    coordinator = entry.runtime_data
    await _sample(hass, coordinator, freezer)
    assert coordinator.shadow.result["status"] == "disabled"
    assert coordinator.shadow.result["total_kw"] is None
    assert coordinator.data.demand_kw == pytest.approx(0.14 * 22)
    assert [(row.subentry_id, row.kw) for row in coordinator.data.allocations] == [
        ("floor", pytest.approx(3.08))
    ]
    assert hass.states.get(_entity_id(hass, entry, "shadow_status")).state == "disabled"


async def test_unverified_parmair_exposes_measured_bounds_without_airflow(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config()}
    )
    coordinator = entry.runtime_data
    await _sample(hass, coordinator, freezer)
    result = coordinator.shadow.result
    assert result["indoor"]["temp_c"] == 20
    assert result["total_kw"] is not None
    assert result["active"] is False
    for side in ("supply", "extract"):
        fan = result["ventilation"][side]
        assert fan["flow_ls"] is None
        assert 0 < fan["flow_low_ls"] < fan["flow_high_ls"]
        assert (
            hass.states.get(_entity_id(hass, entry, f"parmair_{side}_airflow")).state == "unknown"
        )
    assert coordinator.data.demand_kw == pytest.approx(3.08)


async def test_missing_indoor_reading_does_not_turn_into_zero_temperature(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    hass.states.async_set("sensor.room_temperature", "unavailable")
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config()}
    )
    await _sample(hass, entry.runtime_data, freezer)
    result = entry.runtime_data.shadow.result
    assert result["status"] == "inputs_unavailable"
    assert result["total_kw"] is result["remaining_kw"] is None
    assert result["indoor"]["temp_c"] is None
    assert entry.runtime_data.data.demand_kw == pytest.approx(3.08)


async def test_comfort_target_number_restores_after_reload(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config(target_c=20.5)}
    )
    target_id = _entity_id(hass, entry, "comfort_target", "number")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": target_id, "value": 23}, blocking=True
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(target_id).state) == 23
    assert entry.runtime_data.shadow.target_c == 23
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.shadow.target_c == 23
    assert float(hass.states.get(target_id).state) == 23
    assert entry.runtime_data.shadow.counters == {}
    assert entry.runtime_data.shadow.calibration.pending == ()


async def test_restore_rejects_validation_from_changed_configuration(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config(target_c=20.5)}
    )
    shadow = entry.runtime_data.shadow
    old = shadow.to_dict()
    old.update(signature="different-configuration", target_c=25)
    shadow.restore(old)
    assert shadow.target_c == 20.5
    assert shadow.calibration.features == ()


async def test_target_edit_discards_pending_predictions_and_previous_scores(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config()}
    )
    shadow = entry.runtime_data.shadow
    pending = calibration.ShadowPrediction(_NOW, _NOW + timedelta(hours=1), 20, 19, 20)
    score = calibration.ShadowScore(_NOW, 0.1, 1, 1, 0.1, 1, 1, "measured", shadow.signature, True)
    shadow.calibration = calibration.CalibrationState(pending=(pending,), scores=(score,))
    shadow.set_target(23)
    assert shadow.calibration.pending == ()
    assert shadow.calibration.scores == ()
    assert not shadow.validation()["ready"]


async def test_independent_source_meters_reset_without_erasing_other_source(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    for eid in ("sensor.meter_a", "sensor.meter_b", "sensor.meter_c"):
        hass.states.async_set(eid, "10", {"unit_of_measurement": "kWh"})
    source_cfg = {
        "floor_a": {"energy_entities": ["sensor.meter_a", "sensor.meter_b"], "max_electric_kw": 5},
        "floor_b": {"energy_entities": ["sensor.meter_c"], "max_electric_kw": 5},
    }
    entry = await async_setup_hub(
        hass,
        [_source("Floor A", "floor_a"), _source("Floor B", "floor_b")],
        **{CONF_SHADOW: _config(sources=source_cfg)},
    )
    co = entry.runtime_data
    await _sample(hass, co, freezer, seconds=0)
    await _sample(
        hass,
        co,
        freezer,
        states=[
            ("sensor.meter_a", 10.01, "kWh"),
            ("sensor.meter_b", 10.02, "kWh"),
            ("sensor.meter_c", 10.01, "kWh"),
        ],
    )
    rows = co.shadow.result["sources"]
    assert rows["floor_a"]["heat_kw"] == pytest.approx(1.8)
    assert rows["floor_b"]["heat_kw"] == pytest.approx(0.6)
    assert rows["floor_a"]["provenance"] == "measured"
    await _sample(
        hass,
        co,
        freezer,
        states=[
            ("sensor.meter_a", 0, "kWh"),
            ("sensor.meter_b", 10.03, "kWh"),
            ("sensor.meter_c", 10.02, "kWh"),
        ],
    )
    rows = co.shadow.result["sources"]
    assert rows["floor_a"]["heat_kw"] is None
    assert "sensor.meter_a:counter_reset" in rows["floor_a"]["reasons"]
    assert rows["floor_b"]["heat_kw"] == pytest.approx(0.6)
    assert co.shadow.result["remaining_kw"] is None
    await _sample(
        hass,
        co,
        freezer,
        states=[
            ("sensor.meter_a", 0.01, "kWh"),
            ("sensor.meter_b", 10.04, "kWh"),
            ("sensor.meter_c", 10.03, "kWh"),
        ],
    )
    assert co.shadow.result["sources"]["floor_a"]["heat_kw"] == pytest.approx(1.2)


async def test_sampling_outage_rebaselines_without_crediting_unobserved_heat(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config()}
    )
    co = entry.runtime_data
    await _sample(hass, co, freezer, seconds=0)
    await _sample(hass, co, freezer, seconds=600, states=[("sensor.energy", 100.5, "kWh")])
    row = co.shadow.result["sources"]["floor"]
    assert row["heat_kw"] is row["electric_kwh"] is None
    assert "sensor.energy:sampling_gap" in row["reasons"]
    await _sample(hass, co, freezer, states=[("sensor.energy", 100.51, "kWh")])
    assert co.shadow.result["sources"]["floor"]["heat_kw"] == pytest.approx(0.6)


async def test_stale_energy_entity_cannot_teach_zero_heat(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config()}
    )
    co = entry.runtime_data
    await _sample(hass, co, freezer, seconds=0)
    for _ in range(16):
        await _sample(hass, co, freezer, states=[("sensor.room_temperature", 20, "°C")])
    row = co.shadow.result["sources"]["floor"]
    assert row["heat_kw"] is None
    assert "sensor.energy:missing_counter" in row["reasons"]
    assert "heating_input_incomplete" in co.shadow.result["quality_reasons"]


async def test_calibrated_ventilation_can_capture_a_valid_feature(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config(verified=True)}
    )
    co = entry.runtime_data
    await _sample(hass, co, freezer, seconds=0)
    for i in range(1, 6):
        await _sample(hass, co, freezer, states=[("sensor.energy", 100 + i * 0.01, "kWh")])
    result = co.shadow.result
    assert result["ventilation"]["calibrated"]
    assert "ventilation_calibrated" not in result["quality_reasons"]
    assert not any(reason.startswith("ventilation_") for reason in result["quality_reasons"])
    assert co.shadow.calibration.features[-1].complete
    assert co.shadow.calibration.features[-1].electric_kwh == pytest.approx(0.05)


async def test_activation_request_cannot_bypass_chronological_validation(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass,
        [_source("Floor", "floor", "sensor.energy")],
        **{CONF_SHADOW: _config(verified=True, activate=True, event_coverage_reviewed=True)},
    )
    co = entry.runtime_data
    await _sample(hass, co, freezer, seconds=0)
    await _sample(hass, co, freezer, states=[("sensor.energy", 100.01, "kWh")])
    assert co.shadow.result["active"] is False
    assert "insufficient_distributed_days" in co.shadow.validation()["reasons"]
    assert co.data.demand_kw == pytest.approx(3.08)


async def test_card_shadow_link_resolves_registered_entity_without_changing_rank_keys(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass, [_source("Floor", "floor", "sensor.energy")], **{CONF_SHADOW: _config()}
    )
    await _sample(hass, entry.runtime_data, freezer)
    attrs = CheapestSourceSensor(entry.runtime_data).extra_state_attributes
    assert attrs["shadow_entity_id"] == _entity_id(hass, entry, "shadow_demand")
    assert hass.states.get(attrs["shadow_entity_id"]) is not None
    assert attrs["shadow"]["active"] is False
    assert attrs["ranking"][0]["entity_id"] == er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "floor_heat_cost"
    )
    assert attrs["demand_kw"] == pytest.approx(3.08)


async def _active_allocation_hub(hass, freezer):
    """Isolate repricing after promotion; chronology is checked separately above."""
    freezer.move_to(_NOW)
    _initial_states(hass)
    entry = await async_setup_hub(
        hass,
        [_source("Floor", "floor", "sensor.energy")],
        **{CONF_SHADOW: _config(sources={"floor": {"open_house": True}})},
    )
    co = entry.runtime_data
    await _sample(hass, co, freezer, seconds=0)
    co.shadow.result.update(
        active=True,
        total_kw=4,
        remaining_kw=4,
        quality_reasons=[],
        sources={"floor": {"heat_kw": 0}},
    )
    return co


async def test_active_dispatch_drops_unknown_cost_on_immediate_recompute(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    co = await _active_allocation_hub(hass, freezer)
    before = co.build_hub_data()
    assert sum(row.kw for row in before.allocations) == 4
    counters = dict(co.shadow.counters)
    hass.states.async_set("sensor.buy_price", "unavailable")
    after = co.build_hub_data()
    assert after.demand_kw == 4
    assert after.snapshots["floor"].cost_eur_per_kwh is None
    assert after.allocations == []
    assert after.demand_covered is False
    assert co.shadow.result["total_allocation"]["uncovered_kw"] == 4
    assert co.shadow.counters == counters


async def test_active_dispatch_recomputes_source_capacity_without_meter_resampling(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    co = await _active_allocation_hub(hass, freezer)
    assert sum(row.kw for row in co.build_hub_data().allocations) == 4
    counters = dict(co.shadow.counters)
    co.sources["floor"] = replace(co.sources["floor"], capacity_kw=0.5)
    after = co.build_hub_data()
    assert sum(row.kw for row in after.allocations) == 0.5
    assert co.shadow.result["total_allocation"]["uncovered_kw"] == 3.5
    assert after.demand_covered is False
    assert co.shadow.counters == counters


async def test_active_model_reverts_to_legacy_when_local_requirement_disappears(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    co = await _active_allocation_hub(hass, freezer)
    assert co.build_hub_data().demand_kw == 4
    hass.states.async_set("sensor.local_demand", "unavailable")
    after = co.build_hub_data()
    assert co.shadow.result["active"] is False
    assert "local_requirement_unknown" in co.shadow.result["quality_reasons"]
    assert after.demand_kw == pytest.approx(3.08)
