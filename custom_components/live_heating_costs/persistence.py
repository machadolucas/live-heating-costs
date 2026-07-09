"""Store-backed persistence for the hub's runtime state.

The UA-learner state, the running day accumulator, the wood log and a couple
of small overrides all live in a single ``Store`` per hub config entry (so
they are part of Home Assistant backups). The coordinator treats this as the
source of truth, restored at setup — mirrors Load Scheduler's
``persistence.py``.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import SAVE_DELAY_S, STORAGE_KEY_TEMPLATE, STORAGE_VERSION


class RuntimeStore:
    """Thin wrapper over ``Store`` keyed per hub config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry_id)
        )

    async def async_load(self) -> dict:
        """Return the saved state mapping (empty if none saved yet)."""
        return await self._store.async_load() or {}

    @callback
    def schedule_save(self, data: dict) -> None:
        """Debounced save of ``data``, delayed by ``SAVE_DELAY_S``."""
        self._store.async_delay_save(lambda: data, SAVE_DELAY_S)

    async def async_save_now(self, data: dict) -> None:
        """Flush ``data`` to storage immediately (e.g. on unload)."""
        await self._store.async_save(data)
