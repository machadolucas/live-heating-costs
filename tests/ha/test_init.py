"""Setup/unload round-trip + frontend/service registration."""

from __future__ import annotations

from conftest import async_setup_hub, resistive_subentry, wood_subentry
from homeassistant.core import HomeAssistant

from custom_components.live_heating_costs.const import DOMAIN, SERVICE_LOG_WOOD


async def test_setup_and_unload_round_trip(hass: HomeAssistant) -> None:
    entry = await async_setup_hub(hass, [wood_subentry(), resistive_subentry()])

    assert entry.state.recoverable or entry.state.value == "loaded"
    coordinator = entry.runtime_data
    assert coordinator is not None
    assert coordinator.data is not None  # first refresh completed
    assert len(coordinator.sources) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_registers_log_wood_service_without_frontend(hass: HomeAssistant) -> None:
    # The test harness has no Lovelace/frontend component loaded, so card
    # registration must soft-fail (try/except) rather than blocking setup.
    entry = await async_setup_hub(hass, [wood_subentry()])

    assert entry.runtime_data is not None
    assert hass.services.has_service(DOMAIN, SERVICE_LOG_WOOD)


async def test_second_hub_does_not_reregister_service(hass: HomeAssistant) -> None:
    entry1 = await async_setup_hub(hass, [wood_subentry()])
    entry2 = await async_setup_hub(hass, [resistive_subentry()])

    assert hass.services.has_service(DOMAIN, SERVICE_LOG_WOOD)
    assert entry1.runtime_data is not entry2.runtime_data

    coordinator1, coordinator2 = entry1.runtime_data, entry2.runtime_data
    coordinators = hass.data[f"{DOMAIN}_coordinators"]
    assert coordinator1 in coordinators
    assert coordinator2 in coordinators

    assert await hass.config_entries.async_unload(entry1.entry_id)
    await hass.async_block_till_done()
    assert coordinator1 not in hass.data[f"{DOMAIN}_coordinators"]
    assert coordinator2 in hass.data[f"{DOMAIN}_coordinators"]


async def test_source_config_error_is_skipped_not_fatal(hass: HomeAssistant) -> None:
    from homeassistant.config_entries import ConfigSubentryData

    from custom_components.live_heating_costs.const import SUBENTRY_TYPE_SOURCE

    broken = ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_SOURCE,
        title="Broken",
        unique_id=None,
        data={"source_type": "not_a_real_type"},
    )
    entry = await async_setup_hub(hass, [broken, wood_subentry()])

    coordinator = entry.runtime_data
    assert len(coordinator.sources) == 1  # the broken one was skipped, not fatal
