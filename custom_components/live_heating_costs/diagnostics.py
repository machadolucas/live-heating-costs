"""Diagnostics for the Live Heating Costs hub.

No secrets are involved (entity IDs and computed numbers only), but datetimes
and dataclasses still need a small serializer to be JSON-safe.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import LiveHeatingCostsConfigEntry


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LiveHeatingCostsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the hub config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    sources: dict[str, Any] = {
        sub_id: {"title": subentry.title, "data": dict(subentry.data)}
        for sub_id, subentry in entry.subentries.items()
    }

    hub: dict[str, Any] | None = None
    if data is not None:
        hub = {
            "snapshots": {sub_id: _serialize(snap) for sub_id, snap in data.snapshots.items()},
            "ranked": [s.name for s in data.ranked],
            "excluded": [s.name for s in data.excluded],
            "cheapest": data.cheapest.name if data.cheapest else None,
            "demand_kw": data.demand_kw,
            "demand_covered": data.demand_covered,
            "allocations": [_serialize(a) for a in data.allocations],
            "blended_cost": data.blended_cost,
            "degree_hours_24h": data.degree_hours_24h,
            "ua_effective": data.ua_effective,
            "temp_c": data.temp_c,
            "temp_24h_mean": data.temp_24h_mean,
            "buy_eur_per_kwh": data.buy_eur_per_kwh,
            "forecast_updated": _serialize(data.forecast_updated),
            "forecast_combined": _serialize(data.forecast_combined),
        }

    return {
        "hub": dict(entry.data),
        "sources": sources,
        "data": hub,
        "ua_state": coordinator.ua_state.to_dict(),
        "day_accum": coordinator.day_accum.to_dict(),
        "source_meta": coordinator.source_meta,
        "wood_log": coordinator.wood_log[-50:],
    }
