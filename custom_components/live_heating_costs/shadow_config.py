"""Optional, portable shadow-model configuration; no household identifiers baked in.

The envelope coefficient is independent of the legacy fitted UA: that UA
already includes average ventilation and gains, so reusing it would count
those losses twice. Unverified priors may run in shadow but cannot be promoted.
"""

from __future__ import annotations

import hashlib
import json
import math

CONF_SHADOW = "shadow_model"


def _number(value, name, low=0.0, high=1e6):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def _known_fields(value: dict, allowed: set[str], context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {context} fields: " + ", ".join(sorted(unknown)))


def validate_config(raw: object) -> dict:
    """Validate optional expert configuration before persisting or sampling it."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("shadow_model must be an object")
    data = json.loads(json.dumps(raw, allow_nan=False))
    allowed = {
        "indoor",
        "parameters",
        "ventilation",
        "sources",
        "gains",
        "geometry",
        "calibrated",
        "activate",
        "local_demand_entity",
        "target_c",
        "event_coverage_reviewed",
    }
    if set(data) - allowed:
        raise ValueError("Unknown shadow model fields: " + ", ".join(sorted(set(data) - allowed)))
    for key in ("calibrated", "activate", "event_coverage_reviewed"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{key} must be boolean")
    data["target_c"] = _number(data.get("target_c", 21), "target_c", 5, 30)
    indoor = data.get("indoor", [])
    if not isinstance(indoor, list) or len(indoor) > 100:
        raise ValueError("indoor must be a list of at most 100 sensors")
    seen = set()
    for item in indoor:
        if not isinstance(item, dict) or not isinstance(item.get("entity_id"), str):
            raise ValueError("Each indoor sensor needs entity_id")
        _known_fields(item, {"entity_id", "weight", "offset_c", "calibrated"}, "indoor")
        if item["entity_id"] in seen:
            raise ValueError("Duplicate indoor sensor")
        seen.add(item["entity_id"])
        item["weight"] = _number(item.get("weight", 1), "indoor weight", 0.001, 10000)
        item["offset_c"] = _number(item.get("offset_c", 0), "offset_c", -10, 10)
        if not isinstance(item.get("calibrated", False), bool):
            raise ValueError("indoor calibrated must be boolean")
    params = data.setdefault("parameters", {})
    if not isinstance(params, dict):
        raise ValueError("parameters must be an object")
    defaults = {
        "envelope_kw_per_c": 0.14,
        "air_capacity_kwh_per_c": 0.5,
        "fabric_capacity_kwh_per_c": 10.0,
        "coupling_kw_per_c": 0.5,
    }
    if set(params) - set(defaults):
        raise ValueError("Unknown thermal parameter")
    for key, default in defaults.items():
        params[key] = _number(params.get(key, default), key, 0.001, 1000)
    vent = data.get("ventilation", {})
    if not isinstance(vent, dict):
        raise ValueError("ventilation must be an object")
    _known_fields(
        vent,
        {
            "supply_control_entity",
            "extract_control_entity",
            "supply_temp_entity",
            "power_entity",
            "preheater_entity",
            "postheater_entity",
            "mode_entity",
            "supply_resistance_pa_per_ls2",
            "extract_resistance_pa_per_ls2",
            "model_verified",
            "controller_verified",
            "auxiliary_power_w",
        },
        "ventilation",
    )
    for side in ("supply", "extract"):
        key = f"{side}_resistance_pa_per_ls2"
        if key in vent:
            vent[key] = _number(vent[key], key, 0, 100)
    for key in ("model_verified", "controller_verified"):
        if key in vent and not isinstance(vent[key], bool):
            raise ValueError(f"{key} must be boolean")
    if "auxiliary_power_w" in vent:
        vent["auxiliary_power_w"] = _number(
            vent["auxiliary_power_w"], "auxiliary_power_w", 0, 10000
        )
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("sources must map subentry IDs to measurement settings")
    meters = set()
    for sid, item in sources.items():
        if not isinstance(item, dict):
            raise ValueError(f"source {sid} must be an object")
        if not isinstance(sid, str) or not sid:
            raise ValueError("source mapping needs nonempty subentry IDs")
        _known_fields(
            item,
            {
                "energy_entities",
                "max_electric_kw",
                "local_capacity_kw",
                "open_house",
                "response_minutes",
                "delivered_power_entity",
                "delivered_local_power_entity",
            },
            "source",
        )
        ids = item.get("energy_entities", [])
        if not isinstance(ids, list) or any(not isinstance(e, str) for e in ids):
            raise ValueError("energy_entities must be an entity list")
        for eid in ids:
            if "." not in eid or " " in eid:
                raise ValueError("energy_entities must contain entity IDs")
            if eid in meters:
                raise ValueError("Energy meters must not be counted twice")
            meters.add(eid)
        item["max_electric_kw"] = _number(
            item.get("max_electric_kw", 20), "max_electric_kw", 0.01, 100
        )
        item["local_capacity_kw"] = _number(
            item.get("local_capacity_kw", 0), "local_capacity_kw", 0, 100
        )
        if "open_house" in item and not isinstance(item["open_house"], bool):
            raise ValueError("open_house must be boolean")
        item["response_minutes"] = _number(
            item.get("response_minutes", 0), "response_minutes", 0, 1440
        )
    gains = data.get("gains", {})
    if not isinstance(gains, dict):
        raise ValueError("gains must be an object")
    _known_fields(
        gains,
        {
            "people_entity",
            "equipment_power_entity",
            "solar_power_entity",
            "watts_per_person",
            "max_people",
            "solar_factor",
            "max_kw",
        },
        "gains",
    )
    for key, default, high in (
        ("watts_per_person", 75, 200),
        ("max_people", 10, 100),
        ("solar_factor", 0, 10),
        ("max_kw", 3, 20),
    ):
        gains[key] = _number(gains.get(key, default), key, 0, high)
    if gains:
        data["gains"] = gains
    geometry = data.get("geometry")
    if geometry is not None:
        if not isinstance(geometry, dict) or geometry.get("schema_version") != 1:
            raise ValueError("geometry needs schema_version 1")
        _known_fields(
            geometry,
            {"schema_version", "model_id", "fingerprint", "conditioned_area_m2"},
            "geometry",
        )
        if isinstance(geometry["schema_version"], bool):
            raise ValueError("geometry schema_version must be an integer")
        for key in ("model_id", "fingerprint"):
            if not isinstance(geometry.get(key), str) or not geometry[key]:
                raise ValueError(f"geometry needs {key}")
        _number(geometry.get("conditioned_area_m2"), "conditioned_area_m2", 1, 10000)

    # Reject malformed entity mappings rather than failing inside a sampling callback.
    def check_entities(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_entity") or key == "entity_id":
                    if not isinstance(child, str) or "." not in child or " " in child:
                        raise ValueError(f"{key} must be an entity ID")
                check_entities(child)
        elif isinstance(value, list):
            for child in value:
                check_entities(child)

    check_entities(data)
    return data


def fingerprint(config: dict) -> str:
    """Changing inputs/parameters invalidates old validation, never silently reuses it."""
    data = {k: v for k, v in config.items() if k not in {"activate", "event_coverage_reviewed"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def entity_ids(config: dict) -> set[str]:
    result = set()

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (key.endswith("_entity") or key == "entity_id") and isinstance(child, str):
                    result.add(child)
                elif key == "energy_entities":
                    result.update(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(config)
    return result
