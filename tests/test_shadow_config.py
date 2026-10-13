"""Portable expert configuration rejects ambiguous heat accounting before setup."""

from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys

import pytest

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "live_heating_costs"
    / "shadow_config.py"
)
_spec = importlib.util.spec_from_file_location("ls_shadow_config", _PATH)
cfg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cfg
_spec.loader.exec_module(cfg)


@pytest.mark.parametrize("value", [None, {}])
def test_absent_optional_settings_stay_disabled(value: object) -> None:
    assert cfg.validate_config(value) == {}


def test_normalization_does_not_mutate_user_configuration() -> None:
    raw = {"indoor": [{"entity_id": "sensor.room_temperature"}], "sources": {"pump": {}}}
    before = copy.deepcopy(raw)
    result = cfg.validate_config(raw)
    assert raw == before
    assert result["target_c"] == 21
    assert result["indoor"][0]["weight"] == 1
    assert result["indoor"][0]["offset_c"] == 0
    assert result["parameters"]["fabric_capacity_kwh_per_c"] > 0
    assert result["sources"]["pump"]["max_electric_kw"] == 20


@pytest.mark.parametrize("value", [[], True, "{}", 21])
def test_nonobject_configuration_is_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        cfg.validate_config(value)


@pytest.mark.parametrize(
    "raw",
    [
        {"unexpected": 1},
        {"target_c": 31},
        {"target_c": True},
        {"target_c": float("nan")},
        {"target_c": float("inf")},
        {"target_c": 10**400},
        {"calibrated": "yes"},
        {"activate": 1},
        {"event_coverage_reviewed": "false"},
        {"indoor": [{"entity_id": "sensor.room", "weight": 0}]},
        {"indoor": [{"entity_id": "sensor.room", "offset_c": 11}]},
        {"indoor": [{"entity_id": "sensor.room", "calibrated": 1}]},
        {"indoor": [{"entity_id": "sensor.room"}, {"entity_id": "sensor.room"}]},
        {"parameters": {"legacy_ua_kw_per_c": 0.14}},
        {"parameters": {"fabric_capacity_kwh_per_c": 0}},
        {"parameters": {"coupling_kw_per_c": -1}},
        {"ventilation": {"model_verified": "yes"}},
        {"ventilation": {"supply_resistance_pa_per_ls2": -0.1}},
        {"sources": {"pump": {"max_electric_kw": 0}}},
        {"sources": {"pump": {"response_minutes": -1}}},
        {"sources": {"pump": {"open_house": "yes"}}},
        {"gains": {"max_kw": -1}},
        {"geometry": {"schema_version": 2}},
        {
            "geometry": {
                "schema_version": 1,
                "model_id": "fixture",
                "fingerprint": "abc",
                "conditioned_area_m2": 0,
            }
        },
    ],
)
def test_invalid_driver_fails_before_sampling(raw: dict) -> None:
    with pytest.raises(ValueError):
        cfg.validate_config(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"indoor": [{"entity_id": "bad"}]},
        {"ventilation": {"supply_control_entity": "bad"}},
        {"gains": {"equipment_power_entity": "sensor.bad entity"}},
        {"local_demand_entity": "bad"},
        {"sources": {"pump": {"energy_entities": ["bad"]}}},
        {"sources": {"pump": {"energy_entities": [123]}}},
        {"sources": {"pump": {"delivered_power_entity": "bad"}}},
    ],
)
def test_invalid_entity_mapping_fails_before_watchers_are_registered(raw: dict) -> None:
    with pytest.raises(ValueError):
        cfg.validate_config(raw)


@pytest.mark.parametrize(
    "sources",
    [
        {"pump": {"energy_entities": ["sensor.energy", "sensor.energy"]}},
        {
            "pump": {"energy_entities": ["sensor.energy"]},
            "floor": {"energy_entities": ["sensor.energy"]},
        },
    ],
)
def test_physical_energy_meter_cannot_be_counted_twice(sources: dict) -> None:
    with pytest.raises(ValueError, match="twice"):
        cfg.validate_config({"sources": sources})


def test_watch_set_covers_indoor_ventilation_gains_and_source_mappings() -> None:
    raw = {
        "indoor": [{"entity_id": "sensor.room"}],
        "ventilation": {
            "supply_control_entity": "number.supply",
            "power_entity": "sensor.fan_power",
        },
        "sources": {
            "pump": {
                "energy_entities": ["sensor.phase_one", "sensor.phase_two"],
                "delivered_power_entity": "sensor.heat_power",
            }
        },
        "gains": {"people_entity": "sensor.people"},
        "local_demand_entity": "sensor.local_demand",
    }
    assert cfg.entity_ids(cfg.validate_config(raw)) == {
        "sensor.room",
        "number.supply",
        "sensor.fan_power",
        "sensor.phase_one",
        "sensor.phase_two",
        "sensor.heat_power",
        "sensor.people",
        "sensor.local_demand",
    }


def test_validation_revision_changes_with_inputs_but_not_activation_request() -> None:
    base = cfg.validate_config({"indoor": [{"entity_id": "sensor.room"}]})
    active = cfg.validate_config({**base, "activate": True, "event_coverage_reviewed": True})
    changed_target = cfg.validate_config({**base, "target_c": 23})
    changed_sensor = cfg.validate_config({**base, "indoor": [{"entity_id": "sensor.other_room"}]})
    assert cfg.fingerprint(active) == cfg.fingerprint(base)
    assert cfg.fingerprint(changed_target) != cfg.fingerprint(base)
    assert cfg.fingerprint(changed_sensor) != cfg.fingerprint(base)


def test_geometry_provenance_does_not_assert_thermal_calibration() -> None:
    result = cfg.validate_config(
        {
            "geometry": {
                "schema_version": 1,
                "model_id": "synthetic-house",
                "fingerprint": "0123456789abcdef",
                "conditioned_area_m2": 150,
            }
        }
    )
    assert not result.get("calibrated", False)
    assert not result.get("activate", False)


@pytest.mark.parametrize(
    "raw",
    [
        {"indoor": [{"entity_id": "sensor.room", "offset": 0}]},
        {"parameters": {"fabric_capacity": 10}},
        {"ventilation": {"supply_control_percent": 50}},
        {"sources": {"floor": {"maximum_electric_kw": 2}}},
        {"sources": {"pump": {"delivered_local_power_entiy": "sensor.local_heat"}}},
        {"gains": {"watts_per_occupant": 75}},
        {
            "geometry": {
                "schema_version": 1,
                "model_id": "fixture",
                "fingerprint": "abc",
                "conditioned_area_m2": 100,
                "ua_from_area": 0.2,
            }
        },
    ],
)
def test_unknown_nested_configuration_fields_are_rejected(raw: dict) -> None:
    with pytest.raises(ValueError, match="Unknown"):
        cfg.validate_config(raw)


def test_local_delivery_and_optional_ventilation_mode_are_watched() -> None:
    result = cfg.validate_config(
        {
            "sources": {
                "floor": {
                    "delivered_power_entity": "sensor.total_heat",
                    "delivered_local_power_entity": "sensor.local_heat",
                    "open_house": False,
                }
            },
            "ventilation": {"mode_entity": "sensor.ventilation_mode"},
        }
    )
    assert result["sources"]["floor"]["delivered_local_power_entity"] == "sensor.local_heat"
    assert cfg.entity_ids(result) == {
        "sensor.total_heat",
        "sensor.local_heat",
        "sensor.ventilation_mode",
    }


@pytest.mark.parametrize("entity", [None, 1, "bad", "sensor.bad entity"])
def test_local_delivery_and_mode_entity_mapping_must_be_valid(entity) -> None:
    with pytest.raises(ValueError):
        cfg.validate_config({"sources": {"pump": {"delivered_local_power_entity": entity}}})
    with pytest.raises(ValueError):
        cfg.validate_config({"ventilation": {"mode_entity": entity}})
