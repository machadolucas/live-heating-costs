"""Binary sensors: per-source activity indicators + hub demand-covered flag."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ACTIVITY_MODE_NONE, SOURCE_TYPE_CURVE
from .coordinator import LiveHeatingCostsConfigEntry, LiveHeatingCostsCoordinator
from .entity import LiveHeatingCostsEntity, LiveHeatingCostsHubEntity
from .models import CurveSourceConfig, StaticSourceConfig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LiveHeatingCostsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up per-source activity binary sensors + the hub demand-covered sensor."""
    coordinator = entry.runtime_data

    for subentry_id, cfg in coordinator.sources.items():
        if isinstance(cfg, CurveSourceConfig):
            async_add_entities(
                [SourceHeatingBinarySensor(coordinator, subentry_id)],
                config_subentry_id=subentry_id,
            )
        elif isinstance(cfg, StaticSourceConfig) and cfg.activity.mode != ACTIVITY_MODE_NONE:
            async_add_entities(
                [SourceActiveBinarySensor(coordinator, subentry_id)],
                config_subentry_id=subentry_id,
            )

    async_add_entities([DemandCoveredBinarySensor(coordinator)])


class SourceHeatingBinarySensor(LiveHeatingCostsEntity, BinarySensorEntity):
    """On while this curve source is actively delivering heat."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "heating")

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    @property
    def is_on(self) -> bool | None:
        snap = self.snapshot
        if snap is None:
            return None
        return snap.source_type == SOURCE_TYPE_CURVE and snap.heating_fraction > 0.05

    def _last_sample(self):
        dq = self.coordinator._samples.get(self._subentry_id)
        return dq[-1] if dq else None

    @property
    def extra_state_attributes(self) -> dict:
        snap = self.snapshot
        if snap is None:
            return {}
        last = self._last_sample()
        return {
            "hvac_action": last.hvac_action if last is not None else None,
            "power_w": last.power_w if last is not None else None,
            "heating_fraction": snap.heating_fraction,
        }


class SourceActiveBinarySensor(LiveHeatingCostsEntity, BinarySensorEntity):
    """On while this static (fuel) source is detected as active."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "active")

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    @property
    def is_on(self) -> bool | None:
        snap = self.snapshot
        return snap.active if snap is not None else None


class DemandCoveredBinarySensor(LiveHeatingCostsHubEntity, BinarySensorEntity):
    """On when the current heat demand is fully covered by allocated sources."""

    def __init__(self, coordinator: LiveHeatingCostsCoordinator) -> None:
        super().__init__(coordinator, "demand_covered")

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and data.demand_covered is not None

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return data.demand_covered if data is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None or data.demand_kw is None:
            return {}
        allocated_total = sum(a.kw for a in data.allocations)
        shortfall_kw = 0.0 if data.demand_covered else max(data.demand_kw - allocated_total, 0.0)
        return {"shortfall_kw": round(shortfall_kw, 2)}
