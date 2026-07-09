"""The Live Heating Costs integration.

A hub config entry holds the shared price/weather/demand config; one config
*subentry* per heat source (wood stove, resistive heater, heat pump, …) feeds
the cost-comparison + merit-order engine in :mod:`coordinator`.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from . import history
from .const import ATTR_ENERGY_KWH, ATTR_SOURCE, ATTR_UNITS, DOMAIN, PLATFORMS, SERVICE_LOG_WOOD
from .coordinator import LiveHeatingCostsConfigEntry, LiveHeatingCostsCoordinator
from .models import StaticSourceConfig
from .persistence import RuntimeStore

_LOGGER = logging.getLogger(__name__)
_CARD_FILE = "live-heating-costs-card.js"
_CARD_URL = f"/{DOMAIN}/{_CARD_FILE}"


def _card_version(path: pathlib.Path) -> str:
    """A short content hash of the card file, for cache-busting its URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add/refresh the card in the Lovelace resource registry (storage mode).

    Preferred over ``add_extra_js_url``: the resource registry is fetched by the
    frontend at runtime over WebSocket, so the card survives a stale app shell —
    a CDN edge or the service worker serving cached index HTML that omits an
    injected ``<script>`` (the failure mode where the browser never even requests
    the card). This is how HACS registers its cards. Returns ``True`` when
    handled, ``False`` when the registry isn't usable (YAML resource mode, or
    Lovelace not ready) so the caller can fall back to ``add_extra_js_url``.

    Idempotent across restarts: it matches on the URL path and updates the version
    in place, so the resource list never accumulates duplicates.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import ResourceStorageCollection
    except ImportError:
        return False
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return False
    resources = data.resources
    if not isinstance(resources, ResourceStorageCollection):
        return False  # YAML resource mode — can't be edited programmatically
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    base = url.split("?", 1)[0]
    existing = [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).split("?", 1)[0] == base
    ]
    if existing:
        keep, *dupes = existing
        if keep.get("url") != url:
            await resources.async_update_item(keep["id"], {"url": url})
        for dupe in dupes:  # collapse duplicates from older registrations
            await resources.async_delete_item(dupe["id"])
    else:
        await resources.async_create_item({"res_type": "module", "url": url})
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card (best-effort).

    The card is added to the Lovelace **resource registry** (like HACS) rather
    than injected into the index HTML via ``add_extra_js_url``: an injected
    ``<script>`` is dropped whenever a cached app shell — a CDN edge or the
    frontend service worker serving stale index HTML — is used, which makes the
    card vanish ("Custom element doesn't exist") until a hard refresh. The
    resource registry is fetched by the frontend at runtime, so it is immune;
    ``add_extra_js_url`` remains a fallback for YAML resource mode.

    The file is served with long-lived cache headers (``cache_headers=True``) and
    the URL carries a ``?v=<content-hash>`` query so a changed card refetches
    while unchanged files stay cached. The static route matches on path only, so
    the query is ignored server-side; the bare path is what's registered.

    Skipped silently when the frontend/http components aren't available (e.g. in
    the test harness), or the card file doesn't exist yet; the integration works
    without the card.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    try:
        from homeassistant.components.http import StaticPathConfig

        path = pathlib.Path(__file__).parent / "frontend" / _CARD_FILE
        # Register the static route *first*: serving the card must never depend on
        # the cache-buster below. Computing the hash reads the file, and any
        # failure there must not leave the card unregistered (a silent 404).
        await hass.http.async_register_static_paths([StaticPathConfig(_CARD_URL, str(path), True)])
        url = _CARD_URL
        try:
            version = await hass.async_add_executor_job(_card_version, path)
            url = f"{_CARD_URL}?v={version}"
        except Exception as err:  # noqa: BLE001 - cache-bust is best-effort
            _LOGGER.debug("Live Heating Costs card cache-buster skipped: %s", err)
        # Prefer the resource registry; fall back to extra-JS for YAML mode.
        if not await _async_register_resource(hass, url):
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, url)
        hass.data[f"{DOMAIN}_card"] = True
    except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
        _LOGGER.debug("Live Heating Costs card not registered: %s", err)


def _exactly_one_energy_field(value: dict) -> dict:
    if (ATTR_ENERGY_KWH in value) == (ATTR_UNITS in value):
        raise vol.Invalid(f"specify exactly one of '{ATTR_ENERGY_KWH}' or '{ATTR_UNITS}'")
    return value


def _positive_float(value: object) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise vol.Invalid("must be > 0")
    return parsed


LOG_WOOD_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_ENERGY_KWH): _positive_float,
            vol.Optional(ATTR_UNITS): _positive_float,
            vol.Optional(ATTR_SOURCE): cv.string,
        }
    ),
    _exactly_one_energy_field,
)


def _find_static_source(
    hass: HomeAssistant, source_ref: str | None
) -> tuple[LiveHeatingCostsCoordinator, str, StaticSourceConfig] | None:
    """Locate a ``(coordinator, subentry_id, cfg)`` static source across all hubs.

    Matches ``source_ref`` against either the subentry id or its title; with no
    ``source_ref`` the first static source found (across every loaded hub) wins.
    """
    coordinators: set[LiveHeatingCostsCoordinator] = hass.data.get(f"{DOMAIN}_coordinators", set())
    candidates: list[tuple[LiveHeatingCostsCoordinator, str, StaticSourceConfig]] = [
        (coordinator, sub_id, cfg)
        for coordinator in coordinators
        for sub_id, cfg in coordinator.sources.items()
        if isinstance(cfg, StaticSourceConfig)
    ]
    if not source_ref:
        return candidates[0] if candidates else None
    for coordinator, sub_id, cfg in candidates:
        subentry = coordinator.config_entry.subentries.get(sub_id)
        title = subentry.title if subentry is not None else None
        if source_ref in (sub_id, title):
            return coordinator, sub_id, cfg
    return None


async def _async_handle_log_wood(hass: HomeAssistant, call: ServiceCall) -> None:
    """Manually log delivered wood heat (kWh or fuel units) for a static source."""
    found = _find_static_source(hass, call.data.get(ATTR_SOURCE))
    if found is None:
        raise ServiceValidationError("No static (wood) source is configured")
    coordinator, subentry_id, cfg = found

    if ATTR_ENERGY_KWH in call.data:
        kwh = float(call.data[ATTR_ENERGY_KWH])
    else:
        kwh = float(call.data[ATTR_UNITS]) * cfg.energy_density_kwh_per_unit * cfg.efficiency

    accum = coordinator.day_accum
    accum.wood_kwh += kwh
    accum.delivered_kwh += kwh
    accum.per_source_kwh[subentry_id] = accum.per_source_kwh.get(subentry_id, 0.0) + kwh
    coordinator.wood_log.append(
        {
            "ts": dt_util.utcnow().isoformat(),
            "subentry_id": subentry_id,
            "kwh": kwh,
            "mode": "service",
        }
    )
    coordinator.store.schedule_save(coordinator.store_snapshot())
    coordinator.async_set_updated_data(coordinator.build_hub_data())


async def async_setup_entry(hass: HomeAssistant, entry: LiveHeatingCostsConfigEntry) -> bool:
    """Set up Live Heating Costs from the hub config entry."""
    store = RuntimeStore(hass, entry.entry_id)
    coordinator = LiveHeatingCostsCoordinator(hass, entry, store)
    await coordinator.async_load_runtime()
    coordinator.async_setup_listeners()
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_rebuild_forecast()
    entry.runtime_data = coordinator

    coordinators: set[LiveHeatingCostsCoordinator] = hass.data.setdefault(
        f"{DOMAIN}_coordinators", set()
    )
    coordinators.add(coordinator)
    entry.async_on_unload(
        lambda: hass.data.get(f"{DOMAIN}_coordinators", set()).discard(coordinator)
    )

    await _async_register_frontend(hass)

    if not hass.services.has_service(DOMAIN, SERVICE_LOG_WOOD):

        async def _handle_log_wood(call: ServiceCall) -> None:
            await _async_handle_log_wood(hass, call)

        hass.services.async_register(
            DOMAIN, SERVICE_LOG_WOOD, _handle_log_wood, schema=LOG_WOOD_SCHEMA
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # UA-learner recorder backfill: best-effort, so it runs as a detached
    # background task after the first refresh rather than blocking setup.
    entry.async_create_task(
        hass, history.async_backfill(hass, coordinator), "live_heating_costs_backfill"
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: LiveHeatingCostsConfigEntry) -> bool:
    """Unload the hub config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()
        await coordinator.store.async_save_now(coordinator.store_snapshot())
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: LiveHeatingCostsConfigEntry) -> None:
    """Reload on options/subentry changes (picks up added/removed sources)."""
    await hass.config_entries.async_reload(entry.entry_id)
