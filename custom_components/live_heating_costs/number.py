"""`number.<source>_fuel_price` — editable per-unit price for static (fuel) sources.

The coordinator's ``fuel_price_overrides`` (persisted in the Store) is the
source of truth, so the value survives restarts without ``RestoreEntity`` —
mirrors Load Scheduler's target number.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import FUEL_UNIT_KG, FUEL_UNIT_KWH, FUEL_UNIT_M3, FUEL_UNIT_M3_LOOSE
from .coordinator import LiveHeatingCostsConfigEntry, LiveHeatingCostsCoordinator
from .entity import LiveHeatingCostsEntity
from .models import StaticSourceConfig

_FUEL_UNIT_SUFFIX = {
    FUEL_UNIT_M3: "/m³",
    FUEL_UNIT_M3_LOOSE: "/m³",
    FUEL_UNIT_KG: "/kg",
    FUEL_UNIT_KWH: "/kWh",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LiveHeatingCostsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the fuel-price number for each static (fuel) source subentry."""
    coordinator = entry.runtime_data
    for subentry_id, cfg in coordinator.sources.items():
        if isinstance(cfg, StaticSourceConfig):
            async_add_entities(
                [FuelPriceNumber(coordinator, subentry_id, cfg)],
                config_subentry_id=subentry_id,
            )


class FuelPriceNumber(LiveHeatingCostsEntity, NumberEntity):
    """The fuel's price per unit; an override once changed from the config default."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0.01
    _attr_native_max_value = 10000.0
    _attr_native_step = 0.01

    def __init__(
        self,
        coordinator: LiveHeatingCostsCoordinator,
        subentry_id: str,
        cfg: StaticSourceConfig,
    ) -> None:
        super().__init__(coordinator, subentry_id, "fuel_price")
        self._cfg = cfg
        self._attr_native_unit_of_measurement = "€" + _FUEL_UNIT_SUFFIX.get(cfg.fuel_unit, "")

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None

    @property
    def native_value(self) -> float:
        override = self.coordinator.fuel_price_overrides.get(self._subentry_id)
        return override if override is not None else self._cfg.price_per_unit_eur

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_fuel_price_override(self._subentry_id, value)
