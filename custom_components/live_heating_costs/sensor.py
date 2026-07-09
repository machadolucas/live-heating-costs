"""Sensors: per-source cost/capacity/COP/forecast + hub-level ranking/demand/forecast.

Money is EUR/kWh everywhere in the coordinator; the single ×100 conversion to
the displayed unit (c/kWh) happens here, exactly once per value (see the
"Units rule" in ``const.py``'s module docstring). Display states are rounded
to 2 decimals; attribute costs (c/kWh) to 3 decimals.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
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
        ]
    )


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

    _attr_native_unit_of_measurement = "c/kWh"
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
            "cost_eur_per_kwh": snap.cost_eur_per_kwh,
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
            "modeled_cop": snap.modeled_cop,
            "measured_cop": snap.measured_cop,
            "heating_fraction": snap.heating_fraction,
            "temp_used_c": meta.get("temp_used_c"),
            "temp_source": meta.get("temp_source"),
            "window_energy_kwh": meta.get("window_energy_kwh"),
        }


class SourceCostForecastSensor(LiveHeatingCostsEntity, SensorEntity):
    """This source's projected delivered-heat cost, 24-48h ahead."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"forecast", "horizon_end", "has_tomorrow", "updated"})

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "cost_forecast")

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
        slots = self._slots()
        if not slots or slots[0].buy is None:
            return None
        return round(slots[0].buy * 100, 2)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        slots = self._slots()
        return {
            "forecast": forecast_attr_list(slots),
            "horizon_end": slots[-1].end.isoformat() if slots else None,
            "has_tomorrow": _has_real_horizon_beyond(slots, 12),
            "updated": data.forecast_updated.isoformat() if data.forecast_updated else None,
        }


# --------------------------------------------------------------------------- #
# Hub sensors
# --------------------------------------------------------------------------- #


class CheapestSourceSensor(LiveHeatingCostsHubEntity, SensorEntity):
    """State = cheapest dispatchable source's name; attributes = card contract."""

    _unrecorded_attributes = frozenset({"ranking", "excluded"})

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
                }
            )

        excluded = [{"name": s.name, "reason": s.unavailable_reason} for s in data.excluded]

        shortfall_kw: float | None = None
        if demand_kw is not None:
            allocated_total = sum(a.kw for a in data.allocations)
            shortfall_kw = 0.0 if data.demand_covered else max(demand_kw - allocated_total, 0.0)

        return {
            "integration": DOMAIN,
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

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.forecast_updated is None:
            return None
        return data.forecast_updated.isoformat()

    def _has_tomorrow(self, combined: list[dict]) -> bool:
        now = dt_util.utcnow()
        real_ends = [
            end
            for item in combined
            if not item["estimated"] and (end := dt_util.parse_datetime(item["end"])) is not None
        ]
        if not real_ends:
            return False
        return (max(real_ends) - now) > timedelta(hours=12)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        combined = data.forecast_combined
        slots = [
            {
                "start": item["start"],
                "end": item["end"],
                "costs": {
                    sid: (round(cost * 100, 3) if cost is not None else None)
                    for sid, cost in item["costs"].items()
                },
                "cheapest": item["cheapest"],
                "estimated": item["estimated"],
            }
            for item in combined
        ]
        names = {sid: cfg.name for sid, cfg in self.coordinator.sources.items()}
        return {
            "integration": DOMAIN,
            "slots": slots,
            "names": names,
            "horizon_end": combined[-1]["end"] if combined else None,
            "has_tomorrow": self._has_tomorrow(combined),
        }
