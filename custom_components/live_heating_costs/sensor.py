"""Sensors: per-source cost/capacity/COP/forecast + hub-level ranking/demand/forecast.

Money is EUR/kWh everywhere in the coordinator; the single ×100 conversion to
the displayed unit (c/kWh) happens here, exactly once per value (see the
"Units rule" in ``const.py``'s module docstring). Display states are rounded
to 2 decimals; attribute costs (c/kWh) to 3 decimals.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SOURCE_TYPE_CURVE
from .coordinator import HubData, LiveHeatingCostsConfigEntry, LiveHeatingCostsCoordinator
from .entity import LiveHeatingCostsEntity, LiveHeatingCostsHubEntity
from .forecast import SourceSlotCost, forecast_attr_list
from .models import CurveSourceConfig, DynamicFixedConfig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LiveHeatingCostsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up per-source and hub-level sensors."""
    coordinator = entry.runtime_data

    for subentry_id, cfg in coordinator.sources.items():
        entities: list[SensorEntity] = [
            SourceHeatCostSensor(coordinator, subentry_id),
            SourceCapacitySensor(coordinator, subentry_id),
        ]
        if isinstance(cfg, (CurveSourceConfig, DynamicFixedConfig)):
            entities.append(SourceCopSensor(coordinator, subentry_id))
            entities.append(SourceCostForecastSensor(coordinator, subentry_id))
        async_add_entities(entities, config_subentry_id=subentry_id)

    async_add_entities(
        [
            CheapestSourceSensor(coordinator),
            HeatingDemandSensor(coordinator),
            UaCoefficientSensor(coordinator),
            HubCostForecastSensor(coordinator),
            ShadowValueSensor(coordinator, "shadow_demand", "total_kw", "kW"),
            ShadowValueSensor(coordinator, "remaining_demand", "remaining_kw", "kW"),
            ShadowValueSensor(coordinator, "expected_delivery", "expected_delivery_kw", "kW"),
            ShadowValueSensor(coordinator, "parmair_supply_airflow", "supply", "L/s"),
            ShadowValueSensor(coordinator, "parmair_extract_airflow", "extract", "L/s"),
            ShadowStatusSensor(coordinator),
        ]
    )


# The displayed cost unit, shared by every c/kWh sensor (live and forecast).
_COST_UNIT = "c/kWh"


def _r(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _entity_id_for(hass: HomeAssistant, unique_id: str) -> str | None:
    """Resolve one of our own sensors' entity id, or ``None`` if not registered.

    The card only ever sees subentry ids in the ranking; handing it the real
    entity id is what lets a row open HA's more-info dialog. Registration can
    legitimately be missing (a disabled entity, or a subentry whose platform
    hasn't been set up yet), so this never raises.
    """
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)


def _has_real_horizon_beyond(slots: list[SourceSlotCost], hours: float) -> bool:
    """True when the real (non-estimated) horizon extends more than ``hours`` out."""
    real_ends = [s.end for s in slots if not s.estimated]
    if not real_ends:
        return False
    return (max(real_ends) - dt_util.utcnow()) > timedelta(hours=hours)


# --------------------------------------------------------------------------- #
# Per-source sensors
# --------------------------------------------------------------------------- #


class SourceHeatCostSensor(LiveHeatingCostsEntity, SensorEntity):
    """This source's live delivered-heat cost, c/kWh."""

    _attr_native_unit_of_measurement = _COST_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "heat_cost")

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    @property
    def native_value(self) -> float | None:
        snap = self.snapshot
        if snap is None or snap.cost_eur_per_kwh is None:
            return None
        return round(snap.cost_eur_per_kwh * 100, 2)

    @property
    def extra_state_attributes(self) -> dict:
        snap = self.snapshot
        if snap is None:
            return {}
        return {
            # Rounded (0.001 c/kWh): the raw float differs in its last digits
            # on nearly every tick, and each difference is a recorder row.
            "cost_eur_per_kwh": _r(snap.cost_eur_per_kwh, 5),
            "unavailable_reason": snap.unavailable_reason,
            "source_type": snap.source_type,
        }


class SourceCapacitySensor(LiveHeatingCostsEntity, SensorEntity):
    """This source's current dispatchable capacity, kW."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge-full"

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "capacity")

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    @property
    def native_value(self) -> float | None:
        snap = self.snapshot
        if snap is None:
            return None
        return round(snap.capacity_kw, 2)


class SourceCopSensor(LiveHeatingCostsEntity, SensorEntity):
    """This source's current (blended) COP — dimensionless, no fallback to 1.0."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sine-wave"
    # These move on every 60 s tick (the blend window slides); recording them
    # would write a state row per source per minute. The rounded COP state is
    # the long-term record; the live attributes stay visible in more-info.
    _unrecorded_attributes = frozenset(
        {"modeled_cop", "measured_cop", "heating_fraction", "window_energy_kwh"}
    )

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "cop")

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    @property
    def native_value(self) -> float | None:
        snap = self.snapshot
        if snap is None or snap.cop is None:
            return None
        return round(snap.cop, 2)

    @property
    def extra_state_attributes(self) -> dict:
        snap = self.snapshot
        if snap is None:
            return {}
        meta = self.source_meta
        return {
            "modeled_cop": _r(snap.modeled_cop, 3),
            "measured_cop": _r(snap.measured_cop, 3),
            "heating_fraction": _r(snap.heating_fraction, 3),
            "temp_used_c": _r(meta.get("temp_used_c"), 2),
            "temp_source": meta.get("temp_source"),
            "window_energy_kwh": _r(meta.get("window_energy_kwh"), 4),
        }


class SourceCostForecastSensor(LiveHeatingCostsEntity, SensorEntity):
    """This source's projected delivered-heat cost, 24-48h ahead.

    State = the cost of the slot containing *now* (same unit as the live
    cost sensor); attributes = the whole projection.
    """

    # Deliberately unit-less: existing installs recorded statistics without a
    # unit (adding one now raises a units-changed repair), and the forecast
    # attribute is in EUR/kWh, so a c/kWh unit would make price_source scale
    # it a second time if this sensor is fed back in as a price forecast.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"forecast", "horizon_end", "has_tomorrow", "updated"})

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "cost_forecast")
        # The coordinator republishes every minute but the forecast only
        # changes on a rebuild, so the (large) attribute list is rebuilt only
        # when ``forecast_updated`` moves.
        self._attr_cache_key: datetime | None = None
        self._attr_cache: list[dict] = []

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    def _slots(self) -> list[SourceSlotCost]:
        data: HubData | None = self.coordinator.data
        if data is None:
            return []
        return data.forecast.get(self._subentry_id, [])

    @property
    def native_value(self) -> float | None:
        # The slot containing now, not slots[0]: between rebuilds the first
        # slot can already be in the past.
        now = dt_util.utcnow()
        current = next((s for s in self._slots() if s.start <= now < s.end), None)
        if current is None or current.buy is None:
            return None
        return round(current.buy * 100, 2)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        slots = self._slots()
        if self._attr_cache_key is None or self._attr_cache_key != data.forecast_updated:
            self._attr_cache = forecast_attr_list(slots)
            self._attr_cache_key = data.forecast_updated
        return {
            "forecast": self._attr_cache,
            "horizon_end": slots[-1].end.isoformat() if slots else None,
            "has_tomorrow": _has_real_horizon_beyond(slots, 12),
            "updated": data.forecast_updated.isoformat() if data.forecast_updated else None,
        }


# --------------------------------------------------------------------------- #
# Hub sensors
# --------------------------------------------------------------------------- #


class CheapestSourceSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """State = cheapest dispatchable source's name; attributes = card contract."""

    _unrecorded_attributes = frozenset({"ranking", "excluded", "shadow"})

    def __init__(self, coordinator: LiveHeatingCostsCoordinator) -> None:
        super().__init__(coordinator, "cheapest_source")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.cheapest is None:
            return None
        return data.cheapest.name

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}

        allocated_by_id = {a.subentry_id: a for a in data.allocations}
        demand_kw = data.demand_kw
        # Not ``self.hass``: the card-contract test builds this sensor directly,
        # without adding it to hass, so only the coordinator's reference is safe.
        hass = self.coordinator.hass
        ranking = []
        for snap in data.ranked:
            alloc = allocated_by_id.get(snap.subentry_id)
            allocated_kw = alloc.kw if alloc is not None else 0.0
            allocated_fraction = allocated_kw / demand_kw if demand_kw and demand_kw > 0 else 0.0
            ranking.append(
                {
                    "name": snap.name,
                    "subentry_id": snap.subentry_id,
                    "source_type": snap.source_type,
                    "cost_c_per_kwh": (
                        round(snap.cost_eur_per_kwh * 100, 3)
                        if snap.cost_eur_per_kwh is not None
                        else None
                    ),
                    "cop": round(snap.cop, 2) if snap.cop is not None else None,
                    "capacity_kw": round(snap.capacity_kw, 2),
                    "can_meet_demand": bool(
                        demand_kw is not None and snap.capacity_kw >= demand_kw
                    ),
                    "allocated_kw": round(allocated_kw, 3),
                    "allocated_fraction": round(allocated_fraction, 3),
                    "active": snap.active,
                    "heating": snap.source_type == SOURCE_TYPE_CURVE
                    and snap.heating_fraction > 0.05,
                    "measured_blend": snap.measured_cop is not None,
                    "entity_id": _entity_id_for(hass, f"{snap.subentry_id}_heat_cost"),
                }
            )

        excluded = [
            {
                "name": s.name,
                # The card keys its colours and legend chips by subentry id,
                # for excluded sources as much as ranked ones.
                "subentry_id": s.subentry_id,
                "reason": s.unavailable_reason,
                "entity_id": _entity_id_for(hass, f"{s.subentry_id}_heat_cost"),
            }
            for s in data.excluded
        ]

        shortfall_kw: float | None = None
        if demand_kw is not None:
            allocated_total = sum(a.kw for a in data.allocations)
            shortfall_kw = 0.0 if data.demand_covered else max(demand_kw - allocated_total, 0.0)

        return {
            "integration": DOMAIN,
            "shadow": self.coordinator.shadow.result,
            "shadow_entity_id": _entity_id_for(
                hass, f"{self.coordinator.config_entry.entry_id}_shadow_demand"
            ),
            "ranking": ranking,
            "excluded": excluded,
            "demand_kw": round(demand_kw, 2) if demand_kw is not None else None,
            "demand_covered": data.demand_covered,
            "shortfall_kw": round(shortfall_kw, 2) if shortfall_kw is not None else None,
            "blended_cost_c_per_kwh": (
                round(data.blended_cost * 100, 3) if data.blended_cost is not None else None
            ),
            "buy_price_c_per_kwh": (
                round(data.buy_eur_per_kwh * 100, 3) if data.buy_eur_per_kwh is not None else None
            ),
            "demand_entity_id": _entity_id_for(
                hass, f"{self.coordinator.config_entry.entry_id}_heating_demand"
            ),
        }


class HeatingDemandSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """The house's current heat demand, kW (internal model or external entity)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LiveHeatingCostsCoordinator) -> None:
        super().__init__(coordinator, "heating_demand")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.demand_kw is None:
            return None
        return round(data.demand_kw, 2)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        energy_24h = None
        if data.degree_hours_24h is not None:
            energy_24h = data.ua_effective * data.degree_hours_24h
        return {
            "energy_24h_kwh": round(energy_24h, 2) if energy_24h is not None else None,
            "degree_hours_24h": (
                round(data.degree_hours_24h, 2) if data.degree_hours_24h is not None else None
            ),
            "base_temp": self.coordinator._base_temp,
            "ua_effective_w_per_c": round(data.ua_effective * 1000, 1),
            "demand_mode": self.coordinator._demand_mode,
            "temp_24h_mean": (
                round(data.temp_24h_mean, 2) if data.temp_24h_mean is not None else None
            ),
        }


class UaCoefficientSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """The house's effective heat-loss coefficient (UA), W/°C."""

    _attr_native_unit_of_measurement = "W/°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LiveHeatingCostsCoordinator) -> None:
        super().__init__(coordinator, "ua_coefficient")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        return round(data.ua_effective * 1000, 1)

    @property
    def extra_state_attributes(self) -> dict:
        state = self.coordinator.ua_state
        rows = state.rows
        return {
            "seed_w_per_c": round(state.seed * 1000, 1),
            "learned_w_per_c": round(state.learned * 1000, 1)
            if state.learned is not None
            else None,
            "sample_count": state.sample_count,
            "rolling_mae_kwh": (
                round(state.rolling_mae_kwh, 3) if state.rolling_mae_kwh is not None else None
            ),
            "quality_rows": sum(1 for r in rows if r.quality),
            "last_row": rows[-1].to_dict() if rows else None,
        }


class HubCostForecastSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """The combined per-source forecast, all sources unioned by time slot."""

    _unrecorded_attributes = frozenset({"slots", "names"})

    def __init__(self, coordinator: LiveHeatingCostsCoordinator) -> None:
        super().__init__(coordinator, "cost_forecast")
        # Rebuilt only when the forecast itself changes (see
        # SourceCostForecastSensor): (key, slots, latest real slot end).
        self._attr_cache_key: datetime | None = None
        self._attr_cache: list[dict] = []
        self._real_end_cache: datetime | None = None

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.forecast_updated is None:
            return None
        return data.forecast_updated.isoformat()

    @staticmethod
    def _latest_real_end(combined: list[dict]) -> datetime | None:
        real_ends = [
            end
            for item in combined
            if not item["estimated"] and (end := dt_util.parse_datetime(item["end"])) is not None
        ]
        return max(real_ends) if real_ends else None

    def _refresh_cache(self, data: HubData) -> None:
        if self._attr_cache_key is not None and self._attr_cache_key == data.forecast_updated:
            return
        combined = data.forecast_combined
        self._attr_cache = [
            {
                "start": item["start"],
                "end": item["end"],
                "costs": {
                    sid: (round(cost * 100, 3) if cost is not None else None)
                    for sid, cost in item["costs"].items()
                },
                "cheapest": item["cheapest"],
                "estimated": item["estimated"],
                "partial": item["partial"],
            }
            for item in combined
        ]
        self._real_end_cache = self._latest_real_end(combined)
        self._attr_cache_key = data.forecast_updated

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        self._refresh_cache(data)
        combined = data.forecast_combined
        names = {sid: cfg.name for sid, cfg in self.coordinator.sources.items()}
        real_end = self._real_end_cache
        return {
            "integration": DOMAIN,
            "slots": self._attr_cache,
            "names": names,
            "horizon_end": combined[-1]["end"] if combined else None,
            # Time-dependent, so evaluated on every write (cheaply, from the cache).
            "has_tomorrow": real_end is not None
            and (real_end - dt_util.utcnow()) > timedelta(hours=12),
        }


class ShadowValueSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """Estimated thermal/airflow telemetry, distinct from the active demand sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"model"})

    def __init__(self, coordinator, key, field, unit):
        super().__init__(coordinator, key)
        self._field = field
        self._attr_native_unit_of_measurement = unit
        if unit == "kW":
            self._attr_device_class = SensorDeviceClass.POWER

    @property
    def native_value(self):
        result = self.coordinator.shadow.result
        if self._field in ("supply", "extract"):
            value = ((result.get("ventilation") or {}).get(self._field) or {}).get("flow_ls")
        else:
            value = result.get(self._field)
        return round(value, 3) if value is not None else None

    @property
    def extra_state_attributes(self):
        return {"model": self.coordinator.shadow.result, "measurement_type": "estimated"}


class ShadowStatusSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """Validation evidence; time alone never enables the new allocation."""

    _unrecorded_attributes = frozenset({"validation", "quality_reasons"})

    def __init__(self, coordinator):
        super().__init__(coordinator, "shadow_status")

    @property
    def native_value(self):
        return self.coordinator.shadow.result["status"]

    @property
    def extra_state_attributes(self):
        return {
            "validation": self.coordinator.shadow.validation(),
            "quality_reasons": self.coordinator.shadow.result.get("quality_reasons", []),
        }
