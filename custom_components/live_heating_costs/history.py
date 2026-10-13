"""UA-learner backfill from the recorder's long-term statistics.

Best-effort, background-task only: called once from ``async_setup_entry``
after the first refresh, and entirely wrapped so a missing/partial recorder
never breaks setup. Only whole local days before today are mined (today's
partial day is the live accumulator's job), and a day whose hourly
temperature record has gaps is skipped: its heat is all there but its
degree-hours are not, which would inflate the learned UA.

Metered sources are backfilled: curve sources (delivered heat = daily energy
× modeled COP at the day's mean temperature) and dynamic_fixed sources with an
optional ``energy_entity`` (× their fixed COP); static (wood) sources have no
recorder history to mine.

Also home to :func:`async_hourly_temp_means`, which the coordinator uses to
seed its 24 h temperature history after a restart.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import ua_learner
from .const import BACKFILL_MIN_TEMP_HOURS, DEMAND_MODE_INTERNAL, SAVE_DELAY_S
from .models import CurveSourceConfig, DynamicFixedConfig
from .telemetry import validate_counter_delta
from .temp_source import PLAUSIBLE_TEMP_RANGE_C

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
except ImportError:  # pragma: no cover - recorder is normally present in HA
    get_instance = None  # type: ignore[assignment]
    statistics_during_period = None  # type: ignore[assignment]


# Ask the recorder to convert temperature statistics to °C: they are stored
# in the sensor's own unit, which is °F on an imperial instance.
_TEMP_UNITS = {"temperature": UnitOfTemperature.CELSIUS}
# Same for energy counters kept in Wh/MWh.
_ENERGY_UNITS = {"energy": UnitOfEnergy.KILO_WATT_HOUR}


def local_day_length_s(iso_date: str) -> float:
    """Real length of a local calendar day in seconds (23 h / 25 h on DST days).

    Aware-datetime subtraction within one zone is wall-clock arithmetic in
    Python, so both ends are converted to UTC first.
    """
    day = date.fromisoformat(iso_date)
    start = dt_util.as_utc(dt_util.start_of_local_day(day))
    end = dt_util.as_utc(dt_util.start_of_local_day(day + timedelta(days=1)))
    return (end - start).total_seconds()


def _row_start(ts: object) -> datetime:
    """A statistics row's ``start`` (epoch seconds or datetime) as aware UTC."""
    return dt_util.utc_from_timestamp(ts) if isinstance(ts, int | float) else dt_util.as_utc(ts)


def _day_key(ts: object) -> str:
    """Local ISO date for a statistics row's ``start`` (epoch seconds or datetime)."""
    return dt_util.as_local(_row_start(ts)).date().isoformat()


async def async_hourly_temp_means(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """``(hour_start_utc, mean_c)`` for each recorded hour in ``[start, end)``.

    Returns ``[]`` whenever the recorder is absent or the query fails — the
    caller treats seeded history as a bonus, never a requirement.
    """
    if get_instance is None or statistics_during_period is None:
        return []
    try:
        instance = get_instance(hass)
        stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {entity_id},
            "hour",
            _TEMP_UNITS,
            {"mean"},
        )
    except Exception as err:  # noqa: BLE001 - recorder may be unavailable/partial
        _LOGGER.debug("Recorder temperature history unavailable: %s", err)
        return []
    out: list[tuple[datetime, float]] = []
    for row in stats.get(entity_id, []):
        mean = row.get("mean")
        if mean is None:
            continue
        out.append((_row_start(row["start"]), float(mean)))
    return out


async def async_backfill(hass: HomeAssistant, coordinator) -> int:
    """Backfill the UA learner from recorder history; returns rows ingested.

    A no-op (returns 0) unless: demand mode is internal, UA learning is
    enabled, ``backfill_days > 0``, no backfill has completed before (the
    persisted ``backfill_done`` flag — ``sample_count`` alone stays 0 when
    the history taught nothing, which re-ran the import on every reload), the
    UA state has no samples yet, an outdoor temperature entity is configured,
    and at least one metered source exists.
    """
    if coordinator._demand_mode != DEMAND_MODE_INTERNAL or not coordinator._ua_learning:
        return 0
    if coordinator.backfill_done:
        return 0
    if coordinator._backfill_days <= 0 or coordinator.ua_state.sample_count != 0:
        return 0
    if get_instance is None or statistics_during_period is None:
        _LOGGER.info("Recorder not available; skipping UA backfill")
        return 0
    if not coordinator._outdoor_temp_entity:
        return 0

    shadow_config = getattr(getattr(coordinator, "shadow", None), "config", {})
    if shadow_config:
        return await _async_backfill_strict(hass, coordinator, shadow_config)

    metered_sources: dict[str, CurveSourceConfig | DynamicFixedConfig] = {
        sub_id: cfg
        for sub_id, cfg in coordinator.sources.items()
        if isinstance(cfg, CurveSourceConfig)
        or (isinstance(cfg, DynamicFixedConfig) and cfg.energy_entity)
    }
    if not metered_sources:
        return 0

    # Whole local days only: ``end`` is today's local midnight (today is
    # still being accumulated live), ``start`` the local midnight N days back
    # — so every "day" row and every hourly row falls into a complete day.
    today = dt_util.now().date()
    end = dt_util.as_utc(dt_util.start_of_local_day(today))
    start = dt_util.as_utc(
        dt_util.start_of_local_day(today - timedelta(days=coordinator._backfill_days))
    )

    try:
        instance = get_instance(hass)
        temp_stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {coordinator._outdoor_temp_entity},
            "hour",
            _TEMP_UNITS,
            {"mean"},
        )
        energy_stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {cfg.energy_entity for cfg in metered_sources.values()},
            "day",
            _ENERGY_UNITS,
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
        if not start <= _row_start(row["start"]) < end:
            continue  # defensive: only whole days inside the queried window
        day = _day_key(row["start"])
        degree_hours_by_day[day] += max(base_temp - mean, 0.0)
        temp_sum_by_day[day] += mean
        temp_n_by_day[day] += 1

    delivered_by_day: dict[str, float] = defaultdict(float)
    electricity_by_day: dict[str, float] = defaultdict(float)
    for cfg in metered_sources.values():
        for row in energy_stats.get(cfg.energy_entity, []):
            change = row.get("change")
            if change is None or change < 0:
                continue
            if not start <= _row_start(row["start"]) < end:
                continue
            day = _day_key(row["start"])
            electricity_by_day[day] += change
            if isinstance(cfg, DynamicFixedConfig):
                delivered_by_day[day] += change * cfg.cop
                continue
            n = temp_n_by_day.get(day, 0)
            if n == 0:
                continue  # no temperature that day -> can't model a COP
            mean_temp = temp_sum_by_day[day] / n
            delivered_by_day[day] += change * cfg.cop_curve.value_at(mean_temp)

    state = coordinator.ua_state
    ingested = 0
    for day in sorted(degree_hours_by_day):
        if temp_n_by_day[day] < BACKFILL_MIN_TEMP_HOURS:
            # Missing hours would leave this day's degree-hours short while
            # its energy is complete -> an inflated UA ratio. Skip it.
            continue
        row = ua_learner.make_row(
            date=day,
            degree_hours=degree_hours_by_day[day],
            delivered_kwh=delivered_by_day.get(day, 0.0),
            electricity_kwh=electricity_by_day.get(day, 0.0),
            wood_kwh=0.0,
            backfilled=True,
            # Same DST-aware gate as a live day: 22 hourly means is plenty for
            # a 24 h day but only 88 % of the 25 h autumn day.
            coverage=min(1.0, temp_n_by_day[day] * 3600.0 / local_day_length_s(day)),
        )
        state = ua_learner.ingest(state, row)
        ingested += 1

    coordinator.ua_state = state
    # The recorder answered, so this history has been mined — even when it
    # taught nothing, re-querying it on every reload won't change that.
    coordinator.backfill_done = True
    coordinator.store.schedule_save(coordinator.store_snapshot(), SAVE_DELAY_S)
    return ingested


async def _async_backfill_strict(hass: HomeAssistant, coordinator, shadow_config: dict) -> int:
    """Optional-profile backfill requires all configured heat inputs, not a subtotal.

    Hourly energy statistics permit physical increment checks and independent
    floor-meter mappings. Missing intervals or unquantified fuel/radiators keep
    rows visible but ineligible; this history never certifies shadow readiness.
    """
    mappings = shadow_config.get("sources", {})
    source_meters = {}
    for sid, cfg in coordinator.sources.items():
        if isinstance(cfg, CurveSourceConfig | DynamicFixedConfig):
            source_meters[sid] = mappings.get(sid, {}).get("energy_entities") or (
                [cfg.energy_entity] if cfg.energy_entity else []
            )
        else:
            # Activity timers/events cannot certify historical delivered heat.
            source_meters[sid] = []
    all_meters = [eid for meters in source_meters.values() for eid in meters]
    ids = set(all_meters)
    if len(all_meters) != len(ids):
        _LOGGER.info("Strict recorder backfill refused duplicate source meter assignments")
        return 0
    if not ids:
        return 0
    end = dt_util.as_utc(dt_util.start_of_local_day(dt_util.now().date()))
    start = dt_util.as_utc(
        dt_util.start_of_local_day(
            dt_util.as_local(end).date() - timedelta(days=min(coordinator._backfill_days, 90))
        )
    )
    try:
        instance = get_instance(hass)
        temp_stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {coordinator._outdoor_temp_entity},
            "hour",
            _TEMP_UNITS,
            {"mean"},
        )
        energy_stats = await instance.async_add_executor_job(
            statistics_during_period, hass, start, end, ids, "hour", _ENERGY_UNITS, {"change"}
        )
    except Exception as err:  # noqa: BLE001 - recorder is best-effort
        _LOGGER.info("Strict recorder backfill unavailable: %s", err)
        return 0
    temperatures = {}
    for row in temp_stats.get(coordinator._outdoor_temp_entity, []):
        at, mean = _row_start(row["start"]), row.get("mean")
        if (
            mean is not None
            and math.isfinite(mean)
            and PLAUSIBLE_TEMP_RANGE_C[0] <= mean <= PLAUSIBLE_TEMP_RANGE_C[1]
            and start <= at < end
        ):
            temperatures[at] = float(mean)
    changes = {
        eid: {
            _row_start(row["start"]): row.get("change")
            for row in energy_stats.get(eid, [])
            if start <= _row_start(row["start"]) < end
        }
        for eid in ids
    }
    # No synthesised historical temperatures or energy: only observed hours.
    degree_hours, electricity, delivered = (
        defaultdict(float),
        defaultdict(float),
        defaultdict(float),
    )
    temp_hours, covered = defaultdict(set), defaultdict(lambda: defaultdict(set))
    for at, temp in sorted(temperatures.items()):
        day = _day_key(at)
        degree_hours[day] += max(coordinator._base_temp - temp, 0)
        temp_hours[day].add(at)
    for sid, meters in source_meters.items():
        if not meters:
            continue
        cfg = coordinator.sources[sid]
        maximum = mappings.get(sid, {}).get("max_electric_kw", 20)
        hours = set.intersection(*(set(changes[eid]) for eid in meters))
        for at in sorted(hours):
            values = [changes[eid][at] for eid in meters]
            if any(value is None or value < 0 for value in values):
                continue
            change = sum(values)
            # Recorder change is already a normalised increment; use its zero
            # origin only to apply the same physical delta gate as live meters.
            check = validate_counter_delta(
                0, change, at, at + timedelta(hours=1), maximum, maximum_gap_s=3600
            )
            if not check.valid:
                continue
            if isinstance(cfg, CurveSourceConfig):
                temp = temperatures.get(at)
                if temp is None or temp < cfg.operating_limit_c:
                    continue
                cop = min(cfg.max_cop, max(cfg.min_cop, cfg.cop_curve.value_at(temp)))
            else:
                cop = cfg.cop
            day = _day_key(at)
            electricity[day] += change
            delivered[day] += change * cop
            covered[sid][day].add(at)
    state, ingested = coordinator.ua_state, 0
    for day in sorted(degree_hours):
        if len(temp_hours[day]) < BACKFILL_MIN_TEMP_HOURS:
            continue
        length = local_day_length_s(day)
        # A missing energy hour is an unknown heat subtotal, never a full day.
        complete_heat = all(
            source_meters[sid] and len(covered[sid][day]) * 3600 == length
            for sid in coordinator.sources
        )
        coverage = min(1.0, len(temp_hours[day]) * 3600 / length) if complete_heat else 0.0
        row = ua_learner.make_row(
            date=day,
            degree_hours=degree_hours[day],
            delivered_kwh=delivered[day],
            electricity_kwh=electricity[day],
            wood_kwh=0,
            backfilled=True,
            coverage=coverage,
        )
        state = ua_learner.ingest(state, row)
        ingested += 1
    coordinator.ua_state = state
    coordinator.backfill_done = True
    coordinator.store.schedule_save(coordinator.store_snapshot(), SAVE_DELAY_S)
    return ingested
