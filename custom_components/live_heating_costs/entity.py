"""Shared base entities for Live Heating Costs.

Mirrors Load Scheduler's ``entity.py``: one device per source subentry (a
heat source — wood stove, resistive element, heat pump), plus a hub-level
device for whole-house sensors (blended cost, demand, forecast).
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LiveHeatingCostsCoordinator
from .cost_engine import SourceSnapshot


class LiveHeatingCostsEntity(CoordinatorEntity[LiveHeatingCostsCoordinator]):
    """Base entity for one heat source (one subentry = one device)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: LiveHeatingCostsCoordinator, subentry_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._subentry_id = subentry_id
        subentry = coordinator.config_entry.subentries.get(subentry_id)
        title = subentry.title if subentry is not None else subentry_id
        self._attr_unique_id = f"{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=title,
            manufacturer="Live Heating Costs",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def snapshot(self) -> SourceSnapshot | None:
        """This source's current snapshot (``None`` before the first refresh)."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.snapshots.get(self._subentry_id)

    @property
    def source_meta(self) -> dict:
        """Extra per-source data the coordinator tracks outside the snapshot."""
        return self.coordinator.source_meta.get(self._subentry_id, {})


class LiveHeatingCostsHubEntity(CoordinatorEntity[LiveHeatingCostsCoordinator]):
    """Base entity for a hub-level (whole-house) sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LiveHeatingCostsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Live Heating Costs",
            entry_type=DeviceEntryType.SERVICE,
        )
