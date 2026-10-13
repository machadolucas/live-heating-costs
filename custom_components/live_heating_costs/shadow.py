"""Home Assistant boundary for the optional, independently validated thermal model.

Sampling is bounded and passive. Missing heat inputs remain visible instead of
teaching a low-loss house; neither a good temperature fit nor elapsed calendar
time is permission to activate an uncalibrated model.
"""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from . import allocation, calibration, thermal, ventilation
from .models import CurveSourceConfig, DynamicFixedConfig, StaticSourceConfig
from .shadow_config import CONF_SHADOW, entity_ids, fingerprint, validate_config
from .telemetry import validate_counter_delta


class ShadowModel:
    """Own optional runtime state while leaving the legacy UA untouched."""

    VERSION = 1

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.config = validate_config(coordinator.config_entry.data.get(CONF_SHADOW))
        self.target_c = self.config.get("target_c", 21.0)
        self.signature = fingerprint(self.config)
        self.state = None
        self.last_tick = None
        self.last_inputs = None
        self.last_members = ()
        self.counters = {}
        self.calibration = calibration.CalibrationState()
        self.interval = []
        self.last_bucket = None
        self._serialized_state = None
        self._serialized_calibration = {}
        self._validation_key = None
        self._validation_value = {}
        self.result = {"status": "disabled", "total_kw": None, "remaining_kw": None}

    @property
    def watched(self):
        return entity_ids(self.config)

    def set_target(self, value):
        if not math.isfinite(value) or not 5 <= value <= 30:
            raise ValueError("comfort target must be 5–30 °C")
        if value != self.target_c:
            self.calibration = replace(self.calibration, pending=(), scores=())
            self.result.update(active=False, total_kw=None, remaining_kw=None, target_c=value)
        self.target_c = value

    def restore(self, data):
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return
        if data.get("signature") != self.signature:
            return
        target = data.get("target_c", self.target_c)
        if isinstance(target, (int, float)) and math.isfinite(target) and 5 <= target <= 30:
            self.target_c = target
        # Never bridge an outage with old meter readings or pending predictions.
        try:
            self.calibration = replace(
                calibration.CalibrationState.from_dict(data.get("calibration", {})), pending=()
            )
        except (ValueError, TypeError, KeyError):
            self.calibration = calibration.CalibrationState()

    def to_dict(self):
        # The immutable calibration changes at five-minute boundaries, not on
        # each source/state update. Avoid rebuilding 90 days of rows every tick.
        if self._serialized_state is not self.calibration:
            self._serialized_state = self.calibration
            self._serialized_calibration = self.calibration.to_dict()
        return {
            "version": self.VERSION,
            "signature": self.signature,
            "target_c": self.target_c,
            "calibration": self._serialized_calibration,
        }

    def _value(self, entity_id, kind=None, now=None):
        if not entity_id:
            return None
        state = self.coordinator.hass.states.get(entity_id)
        if state is None:
            return None
        if now is not None:
            age = (now - state.last_reported).total_seconds()
            if age < 0 or age > 900:
                return None
        if kind == "temperature":
            return self.coordinator._temp_c(entity_id)
        if kind == "power":
            return self.coordinator._power_w(entity_id)
        if kind == "energy":
            return self.coordinator._energy_kwh(entity_id)
        return self.coordinator._float(entity_id)

    def _indoor(self, now):
        readings = []
        for item in self.config.get("indoor", []):
            eid = item["entity_id"]
            state = self.coordinator.hass.states.get(eid)
            readings.append(
                thermal.WeightedIndoorReading(
                    entity_id=eid,
                    temp_c=self._value(eid, "temperature"),
                    weight=item["weight"],
                    offset_c=item["offset_c"],
                    observed_at=state.last_reported if state else None,
                    calibrated=item.get("calibrated", False),
                )
            )
        return thermal.aggregate_indoor(readings, now)

    def _ventilation(self, indoor, outdoor, now):
        cfg = self.config.get("ventilation", {})
        if not cfg:
            return None
        auxiliary = cfg.get("auxiliary_power_w")
        # Unknown/on heaters make whole-device watts unsuitable for a fan-only check.
        for key in ("preheater_entity", "postheater_entity"):
            value = self._value(cfg.get(key), now=now)
            if value is None or value > 0:
                auxiliary = None
        return ventilation.estimate_ventilation(
            supply_control_percent=self._value(cfg.get("supply_control_entity"), now=now),
            extract_control_percent=self._value(cfg.get("extract_control_entity"), now=now),
            supply_resistance_pa_per_ls2=cfg.get("supply_resistance_pa_per_ls2"),
            extract_resistance_pa_per_ls2=cfg.get("extract_resistance_pa_per_ls2"),
            supply_temp_c=self._value(cfg.get("supply_temp_entity"), "temperature", now),
            indoor_temp_c=indoor,
            outdoor_temp_c=outdoor,
            measured_power_w=self._value(cfg.get("power_entity"), "power", now),
            auxiliary_power_w=auxiliary,
            model_verified=cfg.get("model_verified", False),
            controller_verified=cfg.get("controller_verified", False),
        )

    def _heat(self, now, snapshots):
        rows = {}
        for sid, cfg in self.coordinator.sources.items():
            mapping = self.config.get("sources", {}).get(sid, {})
            meters = mapping.get("energy_entities") or (
                [cfg.energy_entity]
                if isinstance(cfg, (CurveSourceConfig, DynamicFixedConfig)) and cfg.energy_entity
                else []
            )
            reasons, deltas = [], []
            for eid in meters:
                current = self._value(eid, "energy", now)
                previous, start = self.counters.get(eid, (None, now))
                self.counters[eid] = (current, now)
                check = validate_counter_delta(
                    previous,
                    current,
                    start,
                    now,
                    mapping.get("max_electric_kw", 20),
                    maximum_gap_s=180,
                )
                if not check.valid:
                    reasons.append(f"{eid}:{check.reason}")
                else:
                    deltas.append(check.delta_kwh)
            elapsed = (now - self.last_tick).total_seconds() if self.last_tick else 0
            electric = sum(deltas) if meters and not reasons else None
            heat, provenance = None, "modelled"
            if isinstance(cfg, StaticSourceConfig):
                electric = 0.0  # Fuel sources consume no metered heating electricity.
                activity = (
                    self.coordinator.hass.states.get(cfg.activity.entity)
                    if cfg.activity.entity
                    else None
                )
                if activity and activity.state == "off" and cfg.activity.mode == "duration":
                    heat = 0.0
                else:
                    reasons.append("fuel_heat_unquantified")
            elif electric is not None and elapsed > 0:
                snap = snapshots.get(sid)
                cop = snap.cop if snap else None
                if cop is not None and cop > 0:
                    heat = electric * cop * 3600 / elapsed
                    if isinstance(cfg, DynamicFixedConfig) and cfg.cop == 1:
                        provenance = "measured"
                    if isinstance(cfg, CurveSourceConfig):
                        samples = self.coordinator._samples.get(sid)
                        if samples:
                            from .blend import is_producing_heat

                            producing = is_producing_heat(samples[-1], cfg.standby_w)
                            prior = (
                                is_producing_heat(samples[-2], cfg.standby_w)
                                if len(samples) > 1
                                else None
                            )
                            if prior is None or producing != prior:
                                heat = None
                                reasons.append("pump_state_transition")
                            elif not producing:
                                heat = 0.0
                else:
                    reasons.append("cop_unavailable")
            elif not meters:
                reasons.append("energy_meter_missing")
            # A direct heat meter takes precedence; its provenance is independent of COP.
            if mapping.get("delivered_power_entity"):
                measured = self._value(mapping["delivered_power_entity"], "power", now)
                heat = measured / 1000 if measured is not None and measured >= 0 else None
                provenance = "measured"
                reasons = [] if heat is not None else ["heat_meter_unavailable"]
            rows[sid] = {
                "heat_kw": heat,
                "electric_kwh": electric,
                "provenance": provenance,
                "reasons": reasons,
                "response_minutes": mapping.get("response_minutes", 0),
            }
        return rows

    def _gains(self, now):
        cfg = self.config.get("gains", {})
        gain, missing = 0.0, []
        for key, factor in (
            ("people_entity", cfg.get("watts_per_person", 75) / 1000),
            ("equipment_power_entity", 0.001),
            ("solar_power_entity", cfg.get("solar_factor", 0) / 1000),
        ):
            if not cfg.get(key):
                continue
            value = self._value(cfg[key], "power" if "power" in key else None, now)
            if value is None or value < 0:
                missing.append(key)
                continue
            if key == "people_entity":
                value = min(value, cfg.get("max_people", 10))
            gain += value * factor
        return min(gain, cfg.get("max_kw", 3)), missing

    def sample(self, now, snapshots, legacy_ua, outdoor):
        if not self.config:
            return
        indoor = self._indoor(now)
        vent = self._ventilation(indoor.temp_c, outdoor, now)
        heat = self._heat(now, snapshots)
        gains, missing_gains = self._gains(now)
        problems = list(missing_gains)
        if not indoor.calibrated:
            problems.append("indoor_calibration_incomplete")
        if not self.config.get("calibrated", False):
            problems.append("thermal_parameters_unverified")
        if vent is None or vent.net_sensible_kw is None:
            problems.append("ventilation_unquantified")
        elif vent.flags:
            problems.extend("ventilation_" + flag for flag in vent.flags)
        complete_heat = bool(heat) and all(r["heat_kw"] is not None for r in heat.values())
        if not complete_heat:
            problems.append("heating_input_incomplete")
        if any(r["electric_kwh"] is None for r in heat.values()):
            problems.append("electricity_input_incomplete")
        result = {
            "status": "shadow",
            "total_kw": None,
            "remaining_kw": None,
            "target_c": self.target_c,
            "horizon_minutes": 60,
            "indoor": asdict(indoor),
            "ventilation": asdict(vent) if vent else None,
            "sources": heat,
            "gains_kw": gains,
            "quality_reasons": problems,
            "signature": self.signature,
            "active": False,
        }
        if indoor.temp_c is None or outdoor is None:
            self.state = None
            self.last_inputs = None
            self.calibration = replace(self.calibration, pending=())
            result["status"] = "inputs_unavailable"
        else:
            params = thermal.ThermalParameters(**self.config["parameters"])
            gap = (now - self.last_tick).total_seconds() if self.last_tick else 0
            if self.state is None or not 0 < gap <= 180 or indoor.used != self.last_members:
                self.state = thermal.ThermalState(indoor.temp_c, indoor.temp_c)
                self.calibration = replace(self.calibration, pending=())
            elif self.last_inputs is not None:
                evolved = thermal.advance(self.state, params, self.last_inputs, self.last_tick, now)
                self.state = thermal.ThermalState(indoor.temp_c, evolved.state.fabric_c)
            else:
                self.state = thermal.ThermalState(indoor.temp_c, self.state.fabric_c)
            direct = sum(r["heat_kw"] or 0 for r in heat.values() if r["response_minutes"] == 0)
            fabric = sum(r["heat_kw"] or 0 for r in heat.values() if r["response_minutes"] > 0)
            current = direct if complete_heat else None
            inputs = thermal.ThermalInputs(
                outdoor_c=outdoor,
                gains_kw=gains,
                fabric_heat_kw=fabric if complete_heat else 0,
                ventilation_kw=vent.net_sensible_kw
                if vent and vent.net_sensible_kw is not None
                else 0,
            )
            prediction = thermal.target_demand(
                self.state, params, inputs, now, target_c=self.target_c, current_heat_kw=current
            )
            result.update(asdict(prediction))
            result["expected_delivery_kw"] = direct + fabric if complete_heat else None
            result["fabric_c"] = self.state.fabric_c
            result["estimated"] = True
            result["uncertainty"] = "unvalidated_priors" if problems else "awaiting_validation"
            self.last_inputs = thermal.ThermalInputs(
                outdoor_c=outdoor,
                heat_kw=current or 0,
                fabric_heat_kw=fabric if complete_heat else 0,
                gains_kw=gains,
                ventilation_kw=inputs.ventilation_kw,
            )
            self._capture(now, indoor, heat, result, params, legacy_ua, problems)
        self.last_tick = now
        self.last_members = indoor.used
        self.result = result
        result["validation"] = self.validation()
        self.refresh_allocations(snapshots, now)
        result["active"] = bool(
            self.config.get("activate")
            and result["validation"]["ready"]
            and not result["quality_reasons"]
            and result.get("total_kw") is not None
            and result.get("target_reachable")
        )
        if result["active"]:
            result["status"] = "validated"

    def _local_heat(self, mapping, now, source_heat):
        value = self._value(mapping.get("delivered_local_power_entity"), "power", now)
        if value is None or value < 0:
            return None
        if source_heat is None or value / 1000 > source_heat:
            reasons = self.result.get("quality_reasons", [])
            if "local_heat_inconsistent" not in reasons:
                reasons.append("local_heat_inconsistent")
            self.result["active"] = False
            self.result["status"] = "shadow"
            return None
        return value / 1000

    def refresh_allocations(self, snapshots, now):
        """Reprice current capacity without resampling counters or old heat inputs."""
        result = self.result
        local = self._value(self.config.get("local_demand_entity"), "power", now)
        local = local / 1000 if local is not None and local >= 0 else None
        eligible = []
        for sid, snapshot in snapshots.items():
            mapping = self.config.get("sources", {}).get(sid, {})
            eligible.append(
                allocation.EligibleSource(
                    sid,
                    snapshot.capacity_kw,
                    min(mapping.get("local_capacity_kw", 0), snapshot.capacity_kw),
                    snapshot.cost_eur_per_kwh if snapshot.available else None,
                    result.get("sources", {}).get(sid, {}).get("heat_kw"),
                    mapping.get(
                        "open_house", isinstance(self.coordinator.sources[sid], CurveSourceConfig)
                    ),
                    mapping.get("response_minutes", 0),
                    self._local_heat(
                        mapping, now, result.get("sources", {}).get(sid, {}).get("heat_kw")
                    ),
                )
            )
        if result.get("total_kw") is not None:
            result["total_allocation"] = allocation.allocate(result["total_kw"], local, eligible)
            if result.get("remaining_kw") is not None:
                result["additional_allocation"] = allocation.allocate(
                    result["remaining_kw"], local, eligible, additional=True
                )
        if local is None:
            if "local_requirement_unknown" not in result["quality_reasons"]:
                result["quality_reasons"].append("local_requirement_unknown")
        if local is None or self.last_tick is None or (now - self.last_tick).total_seconds() > 180:
            result["active"] = False
            result["status"] = "shadow"

    def _capture(self, now, indoor, heat, result, params, legacy_ua, problems):
        """Retain real five-minute energy totals, and issue forecasts before outcomes."""
        input_ok = (
            indoor.temp_c is not None
            and indoor.coverage >= 0.9
            and not any(
                p not in {"indoor_calibration_incomplete", "thermal_parameters_unverified"}
                for p in problems
            )
        )
        elapsed = (now - self.last_tick).total_seconds() if self.last_tick else 0
        self.interval.append((heat, elapsed, input_ok and 0 < elapsed <= 180))
        bucket = int(now.timestamp()) // 300
        if bucket == self.last_bucket:
            return
        self.last_bucket = bucket
        at = datetime.fromtimestamp(bucket * 300, tz=now.tzinfo)
        duration = sum(row[1] for row in self.interval)
        valid = all(row[2] for row in self.interval) and 240 <= duration <= 360
        electric = measured = modelled = fabric = 0.0
        for rows, seconds, _ in self.interval:
            for row in rows.values():
                electric += row["electric_kwh"] or 0
                heat_kwh = (row["heat_kw"] or 0) * seconds / 3600
                if row["response_minutes"] > 0:
                    fabric += heat_kwh
                if row["provenance"] == "measured":
                    measured += heat_kwh
                else:
                    modelled += heat_kwh
        self.interval.clear()
        events = []
        vent_config = self.config.get("ventilation", {})
        mode_id = vent_config.get("mode_entity")
        mode = self.coordinator.hass.states.get(mode_id) if mode_id else None
        if mode and mode.state not in {"unknown", "unavailable"}:
            events.append("ventilation_mode:" + mode.state[:80])
        for heater in ("preheater", "postheater"):
            output = self._value(vent_config.get(heater + "_entity"), now=now)
            if output is not None and output > 0:
                events.append("ventilation_" + heater)
        if self.last_inputs.outdoor_c < 0:
            events.append("cold")
        if self.last_inputs.outdoor_c > 5:
            events.append("mild")
        if result["gains_kw"] > 0.5:
            events.append("gains")
        feature = calibration.FeatureRow(
            at,
            indoor.temp_c,
            self.last_inputs.outdoor_c,
            indoor.coverage,
            electric if valid else None,
            measured if valid else None,
            modelled if valid else None,
            self.last_inputs.ventilation_kw if "ventilation_unquantified" not in problems else None,
            self.last_inputs.gains_kw,
            valid,
            tuple(events),
            fabric_heat_kwh=fabric if valid else None,
        )
        if self.calibration.features and self.calibration.features[-1].at >= at:
            return
        self.calibration = calibration.record_feature(self.calibration, feature)
        window = [r for r in self.calibration.features if at - timedelta(hours=1) < r.at <= at]
        valid_hour = len(window) == 12 and all(r.complete for r in window)
        heat_kwh = (
            sum((r.measured_heat_kwh or 0) + (r.modelled_heat_kwh or 0) for r in window)
            if valid_hour
            else None
        )
        has_measured = any((r.measured_heat_kwh or 0) > 0 for r in window)
        has_modelled = any((r.modelled_heat_kwh or 0) > 0 for r in window)
        provenance = (
            "mixed" if has_measured and has_modelled else "modelled" if has_modelled else "measured"
        )
        self.calibration = calibration.score_observation(
            self.calibration,
            at,
            indoor.temp_c,
            heat_kwh=heat_kwh,
            provenance=provenance,
            valid=valid_hour,
        )
        end = at + timedelta(hours=1)
        predicted = thermal.advance(self.state, params, self.last_inputs, at, end).state.air_c
        base_params = thermal.ThermalParameters(
            legacy_ua,
            params.air_capacity_kwh_per_c,
            params.fabric_capacity_kwh_per_c,
            params.coupling_kw_per_c,
        )
        base_inputs = thermal.ThermalInputs(
            outdoor_c=self.last_inputs.outdoor_c,
            heat_kw=self.last_inputs.heat_kw,
            fabric_heat_kw=self.last_inputs.fabric_heat_kw,
        )
        base = thermal.advance(self.state, base_params, base_inputs, at, end).state.air_c
        self.calibration = calibration.record_prediction(
            self.calibration,
            calibration.ShadowPrediction(
                at,
                end,
                predicted,
                base,
                indoor.temp_c,
                result["total_kw"],
                legacy_ua * max(self.coordinator._base_temp - self.last_inputs.outdoor_c, 0),
                self.last_inputs.heat_kw + self.last_inputs.fabric_heat_kw,
                self.signature,
            ),
        )
        result["validation"] = self.validation()

    def validation(self):
        vent = self.result.get("ventilation") or {}
        flags = calibration.CalibrationFlags(
            indoor_offsets=bool(self.config.get("indoor"))
            and all(r.get("calibrated", False) for r in self.config.get("indoor", [])),
            envelope=self.config.get("calibrated", False),
            thermal_capacities=self.config.get("calibrated", False),
            ventilation_airflow=bool(vent.get("calibrated")),
            heat_accounting="heating_input_incomplete"
            not in self.result.get("quality_reasons", ["heating_input_incomplete"]),
            event_coverage=self.config.get("event_coverage_reviewed", False),
        )
        key = (id(self.calibration), flags, self.signature)
        if key == self._validation_key:
            return self._validation_value
        result = asdict(
            calibration.readiness(self.calibration, flags, model_revision=self.signature)
        )
        result["feature_count"] = len(self.calibration.features)
        self._validation_key, self._validation_value = key, result
        return result
