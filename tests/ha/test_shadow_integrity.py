"""Read-only HA-boundary regressions for interval heat and operation evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import curve_subentry, resistive_subentry
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.live_heating_costs.blend import Sample
from custom_components.live_heating_costs.models import source_from_subentry
from custom_components.live_heating_costs.shadow import ShadowModel
from custom_components.live_heating_costs.shadow_config import CONF_SHADOW

T0 = datetime(2026, 1, 15, 12, tzinfo=UTC)


def _coordinator(hass, config, sources):
    def value(entity_id):
        state = hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    return SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(data={CONF_SHADOW: config}),
        sources={sid: source_from_subentry(data["data"]) for sid, data in sources.items()},
        _samples={},
        _base_temp=17,
        _float=value,
        _temp_c=value,
        _power_w=value,
        _energy_kwh=value,
    )


def _snapshot(cop=3):
    return SimpleNamespace(cop=cop, capacity_kw=5, cost_eur_per_kwh=0.1, available=True)


@pytest.mark.parametrize("last_action", ["idle", "defrosting"])
async def test_pump_transition_interval_is_unknown_instead_of_erased_heat(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    last_action: str,
):
    now = T0 + timedelta(seconds=60)
    freezer.move_to(now)
    hass.states.async_set("sensor.pump_energy", "1.01")
    pump = curve_subentry(
        title="Pump",
        preset="msz_fd25va",
        power_entity="sensor.pump_power",
        energy_entity="sensor.pump_energy",
    )
    co = _coordinator(hass, {"sources": {}}, {"pump": pump})
    co._samples["pump"] = [Sample(T0, 600, 1, 0, "heating"), Sample(now, 10, 1.01, 0, last_action)]
    shadow = ShadowModel(co)
    shadow.last_tick = T0
    shadow.counters["sensor.pump_energy"] = (1, T0)
    row = shadow._heat(now, {"pump": _snapshot()})["pump"]
    assert row["electric_kwh"] == pytest.approx(0.01)
    assert row["heat_kw"] is None
    assert "pump_state_transition" in row["reasons"]


async def test_single_pump_state_cannot_certify_entire_interval(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
):
    now = T0 + timedelta(seconds=60)
    freezer.move_to(now)
    hass.states.async_set("sensor.pump_energy", "1.01")
    pump = curve_subentry(
        title="Pump",
        preset="msz_fd25va",
        power_entity="sensor.pump_power",
        energy_entity="sensor.pump_energy",
    )
    co = _coordinator(hass, {"sources": {}}, {"pump": pump})
    co._samples["pump"] = [Sample(now, 600, 1.01, 0, "heating")]
    shadow = ShadowModel(co)
    shadow.last_tick = T0
    shadow.counters["sensor.pump_energy"] = (1, T0)
    row = shadow._heat(now, {"pump": _snapshot()})["pump"]
    assert row["heat_kw"] is None
    assert "pump_state_transition" in row["reasons"]


async def test_independent_heat_meter_does_not_fabricate_missing_electricity(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
):
    freezer.move_to(T0)
    hass.states.async_set("sensor.room", "20")
    hass.states.async_set("sensor.heat", "600")
    hass.states.async_set("sensor.local", "0")
    config = {
        "indoor": [{"entity_id": "sensor.room", "calibrated": True}],
        "sources": {"heater": {"delivered_power_entity": "sensor.heat"}},
        "local_demand_entity": "sensor.local",
    }
    co = _coordinator(hass, config, {"heater": resistive_subentry()})
    shadow = ShadowModel(co)
    shadow.sample(T0, {"heater": _snapshot(1)}, 0.14, 0)
    row = shadow.result["sources"]["heater"]
    assert row["heat_kw"] == pytest.approx(0.6)
    assert row["provenance"] == "measured"
    assert row["electric_kwh"] is None
    assert "electricity_input_incomplete" in shadow.result["quality_reasons"]
    assert shadow.calibration.features[-1].electric_kwh is None
    assert not shadow.calibration.features[-1].valid
    assert not shadow.result["active"]


@pytest.mark.parametrize("mode", ["recovery", "summer", "defrost"])
async def test_operating_mode_and_heater_events_are_evidence_not_readiness(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mode: str,
):
    freezer.move_to(T0)
    hass.states.async_set("sensor.room", "20")
    hass.states.async_set("sensor.vent_mode", mode)
    hass.states.async_set("sensor.preheater", "50")
    hass.states.async_set("sensor.postheater", "0")
    config = {
        "indoor": [{"entity_id": "sensor.room"}],
        "ventilation": {
            "mode_entity": "sensor.vent_mode",
            "preheater_entity": "sensor.preheater",
            "postheater_entity": "sensor.postheater",
        },
    }
    shadow = ShadowModel(_coordinator(hass, config, {}))
    shadow.sample(T0, {}, 0.14, 0)
    events = shadow.calibration.features[-1].events
    assert "ventilation_mode:" + mode in events
    assert "ventilation_preheater" in events
    assert "ventilation_postheater" not in events
    assert not shadow.result["validation"]["ready"]
    assert not shadow.result["active"]
