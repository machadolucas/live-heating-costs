"""Store-backed persistence for the hub's runtime state.

The UA-learner state, the running day accumulator, the wood log and a couple
of small overrides all live in a single ``Store`` per hub config entry (so
they are part of Home Assistant backups). The coordinator treats this as the
source of truth, restored at setup — mirrors Load Scheduler's
``persistence.py``.
"""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import SAVE_DELAY_S, STORAGE_KEY_TEMPLATE, STORAGE_VERSION


class RuntimeStore:
    """Thin wrapper over ``Store`` keyed per hub config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry_id)
        )
        self._latest: dict = {}
        # Marks the currently scheduled write's deadline; cleared when that
        # deadline passes. Only used to decide whether a new request can ride
        # along on the pending write or needs an earlier one.
        self._due_handle: asyncio.TimerHandle | None = None

    async def async_load(self) -> dict:
        """Return the saved state mapping (empty if none saved yet)."""
        return await self._store.async_load() or {}

    @callback
    def schedule_save(self, data: dict, delay: float = SAVE_DELAY_S) -> None:
        """Save ``data`` within ``delay`` seconds, never later than already due.

        ``Store.async_delay_save`` *postpones* a pending write to the newest
        call's deadline, so a 60 s tick asking for a 300 s delay would push the
        write out forever (only the shutdown hook would ever save), and would
        also drag a pending 10 s save of a wood log out to 300 s. Here a write
        that is already due sooner is left alone; it just picks up the newest
        data (the data function reads ``_latest`` at write time). The Store
        still owns the actual write, so HA's final-write-on-shutdown hook
        flushes whatever is pending.
        """
        self._latest = data
        loop = self._hass.loop
        due = loop.time() + delay
        if self._due_handle is not None and self._due_handle.when() <= due:
            return
        if self._due_handle is not None:
            self._due_handle.cancel()
        self._store.async_delay_save(self._data_to_save, delay)
        self._due_handle = loop.call_at(due, self._clear_due)

    @callback
    def _clear_due(self) -> None:
        self._due_handle = None

    def _data_to_save(self) -> dict:
        return self._latest

    async def async_save_now(self, data: dict) -> None:
        """Flush ``data`` to storage immediately (e.g. on unload)."""
        self._latest = data
        if self._due_handle is not None:
            self._due_handle.cancel()
            self._due_handle = None
        await self._store.async_save(data)
