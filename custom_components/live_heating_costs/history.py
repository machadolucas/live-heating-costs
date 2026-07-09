"""UA-learner backfill from the recorder's long-term statistics.

Best-effort, background-task only: called once from ``async_setup_entry``
after the first refresh, and entirely wrapped so a missing/partial recorder
never breaks setup. Only curve sources are backfilled here — they are the
only source type with a metered ``energy_entity`` in this build (see the
``models.DynamicFixedConfig`` deviation noted in the coordinator/tests);
static (wood) sources have no recorder history to mine.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import ua_learner
from .const import DEMAND_MODE_INTERNAL
from .models import CurveSourceConfig

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
except ImportError:  # pragma: no cover - recorder is normally present in HA
    get_instance = None  # type: ignore[assignment]
    statistics_during_period = None  # type: ignore[assignment]


def _day_key(ts: object) -> str:
    """Local ISO date for a statistics row's ``start`` (epoch seconds or datetime)."""
    when = dt_util.utc_from_timestamp(ts) if isinstance(ts, int | float) else dt_util.as_utc(ts)
    return dt_util.as_local(when).date().isoformat()


async def async_backfill(hass: HomeAssistant, coordinator) -> int:
    """Backfill the UA learner from recorder history; returns rows ingested.

    A no-op (returns 0) unless: demand mode is internal, UA learning is
    enabled, ``backfill_days > 0``, the UA state has no samples yet (so a
    reload never double-imports), an outdoor temperature entity is
    configured, and at least one curve source exists.
    """
    if coordinator._demand_mode != DEMAND_MODE_INTERNAL or not coordinator._ua_learning:
        return 0
    if coordinator._backfill_days <= 0 or coordinator.ua_state.sample_count != 0:
        return 0
    if get_instance is None or statistics_during_period is None:
        _LOGGER.info("Recorder not available; skipping UA backfill")
        return 0
    if not coordinator._outdoor_temp_entity:
        return 0

    curve_sources: dict[str, CurveSourceConfig] = {
        sub_id: cfg
        for sub_id, cfg in coordinator.sources.items()
        if isinstance(cfg, CurveSourceConfig)
    }
    if not curve_sources:
        return 0

    end = dt_util.utcnow()
    start = end - timedelta(days=coordinator._backfill_days)

    try:
        instance = get_instance(hass)
        temp_stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {coordinator._outdoor_temp_entity},
            "hour",
            None,
            {"mean"},
        )
        energy_stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {cfg.energy_entity for cfg in curve_sources.values()},
            "day",
            None,
            {"change"},
        )
    except Exception as err:  # noqa: BLE001 - recorder may be unavailable/partial
        _LOGGER.info("UA backfill unavailable: %s", err)
        return 0

    base_temp = coordinator._base_temp
    degree_hours_by_day: dict[str, float] = defaultdict(float)
    temp_sum_by_day: dict[str, float] = defaultdict(float)
    temp_n_by_day: dict[str, int] = defaultdict(int)
    for row in temp_stats.get(coordinator._outdoor_temp_entity, []):
        mean = row.get("mean")
        if mean is None:
            continue
        day = _day_key(row["start"])
        degree_hours_by_day[day] += max(base_temp - mean, 0.0)
        temp_sum_by_day[day] += mean
        temp_n_by_day[day] += 1

    delivered_by_day: dict[str, float] = defaultdict(float)
    electricity_by_day: dict[str, float] = defaultdict(float)
    for cfg in curve_sources.values():
        for row in energy_stats.get(cfg.energy_entity, []):
            change = row.get("change")
            if change is None or change < 0:
                continue
            day = _day_key(row["start"])
            electricity_by_day[day] += change
            n = temp_n_by_day.get(day, 0)
            if n == 0:
                continue  # no temperature that day -> can't model a COP
            mean_temp = temp_sum_by_day[day] / n
            delivered_by_day[day] += change * cfg.cop_curve.value_at(mean_temp)

    state = coordinator.ua_state
    ingested = 0
    for day in sorted(degree_hours_by_day):
        row = ua_learner.make_row(
            date=day,
            degree_hours=degree_hours_by_day[day],
            delivered_kwh=delivered_by_day.get(day, 0.0),
            electricity_kwh=electricity_by_day.get(day, 0.0),
            wood_kwh=0.0,
            backfilled=True,
        )
        state = ua_learner.ingest(state, row)
        ingested += 1

    coordinator.ua_state = state
    if ingested:
        coordinator.store.schedule_save(coordinator.store_snapshot())
    return ingested
