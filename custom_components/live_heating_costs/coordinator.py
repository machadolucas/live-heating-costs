"""Hub coordinator: sample sources, compute snapshots, rank, allocate, forecast.

One coordinator per hub config entry (stored in ``entry.runtime_data``). Unlike
Load Scheduler's polling coordinator, this one is event-driven
(``update_interval=None``): a 60 s sampling tick keeps the heat-pump telemetry
deques and the day accumulator fresh, state-change listeners on the watched
entities trigger a debounced recompute, and a periodic + on-change forecast
rebuild refreshes the 24-48h projection. All of that funnels into one
``HubData`` snapshot via :meth:`LiveHeatingCostsCoordinator.build_hub_data`,
published with ``async_set_updated_data``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import CoreState, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from . import blend, demand, history, models, price_source, temp_source, ua_learner
from . import forecast as forecast_mod
from .blend import Sample
from .const import (
    ACTIVITY_MODE_DURATION,
    ACTIVITY_MODE_EVENT,
    CONF_BACKFILL_DAYS,
    CONF_BASE_TEMP,
    CONF_BUY_PRICE_ENTITY,
    CONF_DAY_AHEAD_ENTITY,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_MODE,
    CONF_DEMAND_UNIT,
    CONF_FORECAST_PRICE_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_UA_LEARNING,
    CONF_UA_SEED_W_PER_C,
    CONF_WEATHER_ENTITY,
    DAILY_CAPTURE_HOUR,
    DAILY_CAPTURE_MINUTE,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_BASE_TEMP,
    DEFAULT_UA_LEARNING,
    DEFAULT_UA_SEED_W_PER_C,
    DEMAND_MODE_EXTERNAL,
    DEMAND_MODE_INTERNAL,
    DEMAND_UNIT_KWH_PER_DAY,
    DOMAIN,
    FORECAST_HORIZON_H,
    FORECAST_REBUILD_MIN,
    REASON_BELOW_OPERATING_LIMIT,
    REASON_PRICE_UNAVAILABLE,
    REASON_TEMP_UNAVAILABLE,
    SAMPLE_GAP_REBASELINE_S,
    SAMPLE_INTERVAL_S,
    SAVE_DELAY_TICK_S,
    SOURCE_TYPE_CURVE,
    SOURCE_TYPE_DYNAMIC_FIXED,
    SOURCE_TYPE_STATIC,
    SUBENTRY_TYPE_SOURCE,
    TEMP_MEAN_WINDOW_H,
    WEATHER_CACHE_MIN,
)
from .cost_engine import Allocation, SourceSnapshot, electric_cost, merit_order, rank
from .forecast import SourceSlotCost, TempPoint
from .models import (
    ConfigError,
    CurveSourceConfig,
    DynamicFixedConfig,
    SourceConfig,
    StaticSourceConfig,
)
from .persistence import RuntimeStore
from .shadow import ShadowModel
from .ua_learner import UaState

_LOGGER = logging.getLogger(__name__)

# Debounce window for state-change-triggered recomputes: several watched
# entities (buy price, per-source power/energy, climate) tend to update within
# the same second, so a short collapse avoids redundant snapshot rebuilds.
_RECOMPUTE_DEBOUNCE_S = 2.0

# Forecast rebuilds run on the price-slot grid (:00/:15/:30/:45), a few
# seconds past the boundary so a price entity that flips its "current" slot
# exactly on the boundary has already done so.
_FORECAST_TICK_SECOND = 5

# A recorder hourly mean describes the whole hour; placing it at the hour's
# midpoint is what makes trapezoidal integration over the seeded points
# reproduce that hour's mean instead of skewing it half an hour early.
_HOURLY_MEAN_OFFSET = timedelta(minutes=30)


def _time_weighted_mean(points: Sequence[tuple[datetime, float]]) -> float | None:
    """Trapezoidal time-weighted mean of ``(ts, value)`` points (sorted).

    The history mixes recorder-seeded hourly points with 60 s live samples,
    so a plain arithmetic mean would weight the live hours ~60x the seeded
    ones. A single point is its own mean; no points means no answer.
    """
    if not points:
        return None
    if len(points) == 1:
        return points[0][1]
    area = 0.0
    total_s = 0.0
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        dt_s = (t1 - t0).total_seconds()
        if dt_s <= 0:
            continue
        area += (v0 + v1) / 2.0 * dt_s
        total_s += dt_s
    if total_s <= 0:
        return sum(v for _, v in points) / len(points)
    return area / total_s


@dataclass
class HubData:
    """The coordinator's published snapshot — everything the platforms read."""

    snapshots: dict[str, SourceSnapshot]
    ranked: list[SourceSnapshot]
    excluded: list[SourceSnapshot]
    cheapest: SourceSnapshot | None
    demand_kw: float | None
    demand_covered: bool | None
    allocations: list[Allocation]
    blended_cost: float | None
    degree_hours_24h: float | None
    ua_effective: float
    temp_c: float | None
    temp_24h_mean: float | None
    buy_eur_per_kwh: float | None
    forecast: dict[str, list[SourceSlotCost]]
    forecast_combined: list[dict]
    forecast_updated: datetime | None


@dataclass
class DayAccum:
    """The running day's accumulated demand/delivered-heat totals.

    Persisted whole (see ``RuntimeStore``); reset by the daily-capture job.
    ``last_energy_counters`` tracks each metered source's last-seen cumulative
    counter so only the *delta* since the previous tick is folded in, which
    also makes a counter reset (delta < 0) safely skippable rather than
    corrupting the accumulator with a huge negative jump.

    ``last_tick_utc`` is when those counters were last read: a counter jump
    across a longer gap (HA restart/outage) is re-baselined rather than
    credited, because the outage's degree-hours were never observed.
    ``temp_covered_s`` counts the seconds of this day that had a known
    outdoor temperature, so the capture can tell a fully observed day from
    one that only saw part of its degree-hours (UA quality gate).
    """

    date: str  # local ISO date, e.g. "2026-07-08"
    degree_hours: float = 0.0
    delivered_kwh: float = 0.0
    electricity_kwh: float = 0.0
    wood_kwh: float = 0.0
    per_source_kwh: dict[str, float] = field(default_factory=dict)
    last_energy_counters: dict[str, float] = field(default_factory=dict)
    last_tick_utc: datetime | None = None
    temp_covered_s: float = 0.0
    heat_covered_s: dict[str, float] = field(default_factory=dict)

    def coverage(self) -> float:
        """Fraction (0-1) of this local day with a known outdoor temperature."""
        try:
            length = history.local_day_length_s(self.date)
        except ValueError:
            return 0.0
        if length <= 0:
            return 0.0
        return min(1.0, self.temp_covered_s / length)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "degree_hours": self.degree_hours,
            "delivered_kwh": self.delivered_kwh,
            "electricity_kwh": self.electricity_kwh,
            "wood_kwh": self.wood_kwh,
            "per_source_kwh": dict(self.per_source_kwh),
            "last_energy_counters": dict(self.last_energy_counters),
            "last_tick_utc": self.last_tick_utc.isoformat() if self.last_tick_utc else None,
            "temp_covered_s": self.temp_covered_s,
            "heat_covered_s": dict(self.heat_covered_s),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DayAccum:
        # Stores written before last_tick_utc/temp_covered_s existed load as
        # "never ticked" (-> first tick re-baselines) and "no coverage" (-> the
        # day in progress can't pass the UA quality gate): both err on the
        # side of teaching the learner nothing rather than something wrong.
        last_tick_raw = d.get("last_tick_utc")
        last_tick = dt_util.parse_datetime(last_tick_raw) if last_tick_raw else None
        if last_tick is not None and last_tick.tzinfo is None:
            last_tick = None
        return cls(
            date=d["date"],
            degree_hours=d.get("degree_hours", 0.0),
            delivered_kwh=d.get("delivered_kwh", 0.0),
            electricity_kwh=d.get("electricity_kwh", 0.0),
            wood_kwh=d.get("wood_kwh", 0.0),
            per_source_kwh=dict(d.get("per_source_kwh", {})),
            last_energy_counters=dict(d.get("last_energy_counters", {})),
            last_tick_utc=last_tick,
            temp_covered_s=float(d.get("temp_covered_s", 0.0)),
            heat_covered_s=dict(d.get("heat_covered_s", {})),
        )


@dataclass(frozen=True)
class _CurveEval:
    """Result of evaluating one curve source's COP/capacity right now."""

    temp: float | None
    temp_source: str | None  # temp_source.TEMP_SOURCE_*; None when unresolved
    modeled_cop: float | None
    capacity_kw: float
    cop: float | None
    measured_cop: float | None
    heating_fraction: float
    window_energy_kwh: float
    reason: str | None


def _float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Parse an entity's numeric state; ``None`` for missing/unknown/unparseable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


class LiveHeatingCostsCoordinator(DataUpdateCoordinator[HubData]):
    """Compute and hold the hub's cost/ranking/forecast snapshot."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: RuntimeStore) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None, config_entry=entry)
        self.store = store

        data = entry.data
        self._buy_entity: str | None = data.get(CONF_BUY_PRICE_ENTITY)
        self._day_ahead_entity: str | None = data.get(CONF_DAY_AHEAD_ENTITY)
        self._forecast_price_entity: str | None = data.get(CONF_FORECAST_PRICE_ENTITY)
        self._weather_entity: str | None = data.get(CONF_WEATHER_ENTITY)
        self._outdoor_temp_entity: str | None = data.get(CONF_OUTDOOR_TEMP_ENTITY)
        self._demand_mode: str = data.get(CONF_DEMAND_MODE, DEMAND_MODE_INTERNAL)
        self._demand_entity: str | None = data.get(CONF_DEMAND_ENTITY)
        self._demand_unit: str = data.get(CONF_DEMAND_UNIT, DEMAND_UNIT_KWH_PER_DAY)
        self._base_temp: float = float(data.get(CONF_BASE_TEMP, DEFAULT_BASE_TEMP))
        self._ua_seed_kw_per_c: float = (
            float(data.get(CONF_UA_SEED_W_PER_C, DEFAULT_UA_SEED_W_PER_C)) / 1000.0
        )
        self._ua_learning: bool = bool(data.get(CONF_UA_LEARNING, DEFAULT_UA_LEARNING))
        self._backfill_days: int = int(data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))

        # Parsed source configs, keyed by subentry_id; a source that fails to
        # parse is skipped (logged) rather than breaking hub setup.
        self.sources: dict[str, SourceConfig] = {}
        self._parse_sources()
        self.shadow = ShadowModel(self)

        # Per-source extras the sensor platform will want (temp used, modeled
        # capacity, window energy, …) that don't belong on SourceSnapshot.
        self.source_meta: dict[str, dict] = {}

        # Rolling telemetry. Curve-source sample deques hold enough history for
        # their blend window (+ a small buffer); the hub temperature deque
        # holds a full TEMP_MEAN_WINDOW_H at the sampling cadence.
        self._samples: dict[str, deque[Sample]] = {
            sub_id: deque(maxlen=cfg.blend_window_min + 5)
            for sub_id, cfg in self.sources.items()
            if isinstance(cfg, CurveSourceConfig)
        }
        temp_maxlen = int(TEMP_MEAN_WINDOW_H * 3600 / SAMPLE_INTERVAL_S) + 10
        self._temp_history: deque[tuple[datetime, float]] = deque(maxlen=temp_maxlen)

        # Runtime state restored by async_load_runtime(); sane empty defaults
        # so the coordinator is usable even before that runs.
        self.ua_state: UaState = UaState(seed=self._ua_seed_kw_per_c)
        self.day_accum: DayAccum = DayAccum(date=dt_util.now().date().isoformat())
        self.wood_log: list[dict] = []
        self.fuel_price_overrides: dict[str, float] = {}
        self.last_event_ts: dict[str, datetime] = {}
        # Set once a recorder backfill has run to completion, so a hub whose
        # history teaches nothing (sample_count stays 0) doesn't re-query the
        # recorder on every reload.
        self.backfill_done: bool = False

        # Hub temperature aggregates, recomputed when _temp_history changes
        # (sampling tick, recorder seeding) rather than on every debounced
        # recompute — the 24 h integral walks ~1,440 samples.
        self._temp_mean_cache: float | None = None
        self._degree_hours_cache: float | None = None

        self._buy: float | None = None
        self.forecast: dict[str, list[SourceSlotCost]] = {}
        self.forecast_combined: list[dict] = []
        self.forecast_updated: datetime | None = None
        self._weather_cache: list[TempPoint] | None = None
        self._weather_cache_at: datetime | None = None

        self._debounce_unsub: Callable[[], None] | None = None
        self._forecast_dirty = True  # force a first build on setup
        # Rebuilds are serialized: two overlapping ones could finish out of
        # order and publish the older prices last. ``_forecast_queued``
        # coalesces requests that arrive while one is already waiting.
        self._forecast_lock = asyncio.Lock()
        self._forecast_queued = False
        # Entity ids already reported as absent, so the warning fires once each.
        self._warned_missing: set[str] = set()

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def _parse_sources(self) -> None:
        self.sources = {}
        for subentry_id, subentry in self.config_entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_SOURCE:
                continue
            try:
                self.sources[subentry_id] = models.source_from_subentry(dict(subentry.data))
            except (ConfigError, ValueError, TypeError, KeyError) as err:
                _LOGGER.error("Skipping source %r (%s): %s", subentry.title, subentry_id, err)

    async def async_load_runtime(self) -> None:
        """Restore UA state / day accumulator / wood log from the Store."""
        data = await self.store.async_load()
        self.shadow.restore(data.get("shadow", {}))
        ua_raw = data.get("ua")
        self.ua_state = (
            replace(UaState.from_dict(ua_raw), seed=self._ua_seed_kw_per_c)
            if ua_raw
            else UaState(seed=self._ua_seed_kw_per_c)
        )
        accum_raw = data.get("day_accum")
        self.day_accum = (
            DayAccum.from_dict(accum_raw)
            if accum_raw
            else DayAccum(date=dt_util.now().date().isoformat())
        )
        self.wood_log = list(data.get("wood_log", []))
        self.fuel_price_overrides = dict(data.get("fuel_price_overrides", {}))
        self.last_event_ts = {}
        for sub_id, iso in data.get("last_event_ts", {}).items():
            parsed = dt_util.parse_datetime(iso)
            if parsed is not None:
                self.last_event_ts[sub_id] = parsed
        self.backfill_done = bool(data.get("backfill_done", False))
        # HA may have been down across one or more midnights: capture the
        # stale day now rather than folding today's samples into it.
        self._maybe_roll_day()

    def store_snapshot(self) -> dict:
        """The full persisted state as a JSON-safe dict."""
        return {
            "shadow": self.shadow.to_dict(),
            "ua": self.ua_state.to_dict(),
            "day_accum": self.day_accum.to_dict(),
            "wood_log": self.wood_log[-500:],
            "fuel_price_overrides": dict(self.fuel_price_overrides),
            "last_event_ts": {k: v.isoformat() for k, v in self.last_event_ts.items()},
            "backfill_done": self.backfill_done,
        }

    @callback
    def async_setup_listeners(self) -> None:
        """Wire the sampling tick, forecast rebuild, daily capture and state watchers."""
        watched: set[str] = set(self.shadow.watched)
        event_mode_entities: set[str] = set()
        # The weather entity is watched too: while the outdoor sensor is down
        # it *is* the temperature source, so its updates must drive a recompute.
        for entity in (self._buy_entity, self._outdoor_temp_entity, self._weather_entity):
            if entity:
                watched.add(entity)
        for entity in (self._day_ahead_entity, self._forecast_price_entity):
            if entity:
                watched.add(entity)
        for cfg in self.sources.values():
            if isinstance(cfg, CurveSourceConfig):
                for entity in (cfg.power_entity, cfg.energy_entity, cfg.climate_entity):
                    if entity:
                        watched.add(entity)
            elif isinstance(cfg, StaticSourceConfig) and cfg.activity.entity:
                watched.add(cfg.activity.entity)
                if cfg.activity.mode == ACTIVITY_MODE_EVENT:
                    event_mode_entities.add(cfg.activity.entity)

        if watched:
            self.config_entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, sorted(watched), self._handle_state_change
                )
            )
        if event_mode_entities:
            self.config_entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, sorted(event_mode_entities), self._handle_activity_event
                )
            )

        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._handle_sampling_tick, timedelta(seconds=SAMPLE_INTERVAL_S)
            )
        )
        # Aligned to the slot grid rather than an interval from setup time, so
        # the "current slot" the forecast sensors report flips on the slot
        # boundary instead of up to 15 min late.
        self.config_entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_forecast_tick,
                minute=list(range(0, 60, FORECAST_REBUILD_MIN)),
                second=_FORECAST_TICK_SECOND,
            )
        )
        self.config_entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_daily_capture,
                hour=DAILY_CAPTURE_HOUR,
                minute=DAILY_CAPTURE_MINUTE,
                second=0,
            )
        )

    async def async_shutdown(self) -> None:
        """Cancel any pending debounce timer, then the base shutdown."""
        if self._debounce_unsub is not None:
            self._debounce_unsub()
            self._debounce_unsub = None
        await super().async_shutdown()

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #

    def _float(self, entity_id: str | None) -> float | None:
        return _float_state(self.hass, entity_id)

    def _unit(self, entity_id: str | None) -> object:
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state is not None else None

    def _temp_c(self, entity_id: str | None) -> float | None:
        """A temperature entity's reading converted to °C by its own unit.

        On a °F instance every temperature sensor reports °F, and 23 °F is a
        perfectly plausible-looking 23 °C to the sanity band — only the unit
        tells them apart. Plausibility is left to ``temp_source.resolve``.
        """
        return temp_source.to_celsius(self._float(entity_id), self._unit(entity_id))

    def _converted(self, entity_id: str | None, converter, target: str) -> float | None:
        """A reading converted to ``target`` by the entity's own unit.

        The engine assumes W and kWh, but plenty of meters publish kW or Wh:
        read raw, a Wh counter credits 1000x the heat and a kW power reading
        never clears the standby threshold. An unknown or missing unit is
        taken as already being ``target`` (the historic behaviour).
        """
        value = self._float(entity_id)
        unit = self._unit(entity_id)
        if value is None or not isinstance(unit, str) or unit == target:
            return value
        try:
            return converter.convert(value, unit, target)
        except HomeAssistantError:
            return value

    def _energy_kwh(self, entity_id: str | None) -> float | None:
        return self._converted(entity_id, EnergyConverter, UnitOfEnergy.KILO_WATT_HOUR)

    def _power_w(self, entity_id: str | None) -> float | None:
        return self._converted(entity_id, PowerConverter, UnitOfPower.WATT)

    def _buy_eur_per_kwh(self) -> float | None:
        """The live buy price scaled to EUR/kWh by the entity's unit.

        Nord Pool & co. commonly publish c/kWh or EUR/MWh; read raw, every
        electric source would be priced 100x/1000x off.
        """
        value = self._float(self._buy_entity)
        if value is None:
            return None
        return value * price_source.unit_scale(self._unit(self._buy_entity))

    def _weather_temp_c(self) -> float | None:
        """Current temperature reported by the configured weather entity."""
        if not self._weather_entity:
            return None
        state = self.hass.states.get(self._weather_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
            return None
        return temp_source.weather_temp_c(state.attributes)

    def _resolve_hub_temp(self) -> temp_source.TempReading | None:
        """Hub-wide outdoor temperature: configured sensor -> weather entity.

        Every consumer of "the outdoor temperature" (degree-hour accumulation,
        the hub temperature sensor, the forecast anchor) goes through here, so
        one dead sensor degrades to the weather entity instead of blanking the
        integration out.
        """
        return temp_source.resolve(
            [
                (temp_source.TEMP_SOURCE_HUB, self._temp_c(self._outdoor_temp_entity)),
                (temp_source.TEMP_SOURCE_WEATHER, self._weather_temp_c()),
            ]
        )

    def _hub_temp_c(self) -> float | None:
        reading = self._resolve_hub_temp()
        return reading.temp_c if reading is not None else None

    def _resolve_temp(self, cfg: CurveSourceConfig) -> tuple[float | None, str | None]:
        """Per-source temperature: pump -> override -> hub sensor -> weather."""
        reading = temp_source.resolve(
            [
                (temp_source.TEMP_SOURCE_PUMP, self._temp_c(cfg.pump_temp_entity)),
                (temp_source.TEMP_SOURCE_OVERRIDE, self._temp_c(cfg.temp_entity_override)),
                (temp_source.TEMP_SOURCE_HUB, self._temp_c(self._outdoor_temp_entity)),
                (temp_source.TEMP_SOURCE_WEATHER, self._weather_temp_c()),
            ]
        )
        if reading is None:
            return None, None
        return reading.temp_c, reading.source

    def _recent_samples(self, subentry_id: str, window_min: int) -> list[Sample]:
        dq = self._samples.get(subentry_id)
        if not dq:
            return []
        cutoff = dt_util.utcnow() - timedelta(minutes=window_min)
        return [s for s in dq if s.ts >= cutoff]

    def _eval_curve(self, subentry_id: str, cfg: CurveSourceConfig) -> _CurveEval:
        temp, temp_source = self._resolve_temp(cfg)
        if temp is None:
            return _CurveEval(
                temp, temp_source, None, 0.0, None, None, 0.0, 0.0, REASON_TEMP_UNAVAILABLE
            )
        if temp < cfg.operating_limit_c:
            return _CurveEval(
                temp, temp_source, None, 0.0, None, None, 0.0, 0.0, REASON_BELOW_OPERATING_LIMIT
            )
        modeled_cop = cfg.cop_curve.value_at(temp)
        capacity_kw = cfg.capacity_curve.value_at(temp)
        samples = self._recent_samples(subentry_id, cfg.blend_window_min)
        wc = blend.window_cop(samples, cfg.cop_curve.value_at, cfg.standby_w)
        cop = blend.blend_cop(
            modeled_cop, wc.measured_cop, wc.heating_fraction, cfg.min_cop, cfg.max_cop
        )
        return _CurveEval(
            temp,
            temp_source,
            modeled_cop,
            capacity_kw,
            cop,
            wc.measured_cop,
            wc.heating_fraction,
            wc.energy_kwh,
            None,
        )

    def _refresh_temp_stats(self, now: datetime) -> None:
        """Recompute the 24 h mean / degree-hours from ``_temp_history``.

        Both only look at what was actually observed inside the window: the
        deque can hold samples older than 24 h (it is sized by count, and
        seeding prepends), and ``integrate_degree_hours`` clamps outside its
        samples' span — integrating from the window start with only two hours
        of history would extrapolate the first reading across the other 22.
        """
        cutoff = now - timedelta(hours=TEMP_MEAN_WINDOW_H)
        window = sorted((p for p in self._temp_history if p[0] >= cutoff), key=lambda p: p[0])
        self._temp_mean_cache = _time_weighted_mean(window)
        if len(window) < 2:
            self._degree_hours_cache = None
            return
        self._degree_hours_cache = demand.integrate_degree_hours(
            window, self._base_temp, window[0][0], window[-1][0]
        )

    def _temp_24h_mean(self) -> float | None:
        return self._temp_mean_cache

    def _degree_hours_24h(self) -> float | None:
        return self._degree_hours_cache

    async def async_seed_temp_history(self) -> None:
        """Seed the 24 h temperature history from the recorder's hourly means.

        Without this, every restart starts the 24 h mean (and so the internal
        demand model) from a single reading. Best-effort background task: a
        missing/partial recorder just leaves the live samples to fill in.
        """
        if not self._outdoor_temp_entity:
            return
        end = dt_util.utcnow()
        start = end - timedelta(hours=TEMP_MEAN_WINDOW_H)
        means = await history.async_hourly_temp_means(
            self.hass, self._outdoor_temp_entity, start, end
        )
        if not means:
            return
        live = sorted(self._temp_history, key=lambda p: p[0])
        first_live = live[0][0] if live else None
        seeded = [
            (hour_start + _HOURLY_MEAN_OFFSET, mean)
            for hour_start, mean in means
            if temp_source.is_plausible(mean)
            and (first_live is None or hour_start + _HOURLY_MEAN_OFFSET < first_live)
        ]
        if not seeded:
            return
        self._temp_history = deque(
            sorted(seeded, key=lambda p: p[0]) + live, maxlen=self._temp_history.maxlen
        )
        self._refresh_temp_stats(dt_util.utcnow())
        self.async_set_updated_data(self.build_hub_data())

    def _compute_demand_kw(self, ua_effective: float, temp_mean: float | None) -> float | None:
        if self._demand_mode == DEMAND_MODE_EXTERNAL:
            value = self._float(self._demand_entity)
            if value is None:
                return None
            if self._demand_unit == DEMAND_UNIT_KWH_PER_DAY:
                return value / 24.0
            return value
        if temp_mean is None:
            return None
        return demand.instant_demand_kw(ua_effective, temp_mean, self._base_temp)

    # ------------------------------------------------------------------ #
    # Snapshot computation
    # ------------------------------------------------------------------ #

    def _snapshot_static(self, subentry_id: str, cfg: StaticSourceConfig) -> SourceSnapshot:
        cost = cfg.cost_eur_per_kwh(self.fuel_price_overrides.get(subentry_id))
        active = False
        if cfg.activity.mode == ACTIVITY_MODE_DURATION and cfg.activity.entity:
            state = self.hass.states.get(cfg.activity.entity)
            active = state is not None and state.state == "on"
        return SourceSnapshot(
            subentry_id=subentry_id,
            name=cfg.name,
            source_type=SOURCE_TYPE_STATIC,
            capacity_kw=cfg.capacity_kw,
            cop=None,
            cost_eur_per_kwh=cost,
            available=True,
            unavailable_reason=None,
            active=active,
        )

    def _snapshot_dynamic_fixed(self, subentry_id: str, cfg: DynamicFixedConfig) -> SourceSnapshot:
        cost = electric_cost(self._buy, cfg.cop)
        return SourceSnapshot(
            subentry_id=subentry_id,
            name=cfg.name,
            source_type=SOURCE_TYPE_DYNAMIC_FIXED,
            capacity_kw=cfg.capacity_kw,
            cop=cfg.cop,
            cost_eur_per_kwh=cost,
            available=self._buy is not None,
            unavailable_reason=None if self._buy is not None else REASON_PRICE_UNAVAILABLE,
        )

    def _snapshot_curve(self, subentry_id: str, cfg: CurveSourceConfig) -> SourceSnapshot:
        ev = self._eval_curve(subentry_id, cfg)
        self.source_meta[subentry_id] = {
            "temp_source": ev.temp_source,
            "temp_used_c": ev.temp,
            "modeled_cop": ev.modeled_cop,
            "modeled_capacity_kw": ev.capacity_kw,
            "measured_cop": ev.measured_cop,
            "heating_fraction": ev.heating_fraction,
            "window_energy_kwh": ev.window_energy_kwh,
        }
        if ev.reason is not None:
            return SourceSnapshot(
                subentry_id=subentry_id,
                name=cfg.name,
                source_type=SOURCE_TYPE_CURVE,
                capacity_kw=0.0,
                cop=None,
                cost_eur_per_kwh=None,
                available=False,
                unavailable_reason=ev.reason,
            )
        cost = electric_cost(self._buy, ev.cop)
        return SourceSnapshot(
            subentry_id=subentry_id,
            name=cfg.name,
            source_type=SOURCE_TYPE_CURVE,
            capacity_kw=ev.capacity_kw,
            cop=ev.cop,
            cost_eur_per_kwh=cost,
            available=self._buy is not None,
            unavailable_reason=None if self._buy is not None else REASON_PRICE_UNAVAILABLE,
            modeled_cop=ev.modeled_cop,
            measured_cop=ev.measured_cop,
            heating_fraction=ev.heating_fraction,
        )

    def build_hub_data(self) -> HubData:
        """Compute the full published snapshot from current entity states."""
        self._buy = self._buy_eur_per_kwh()
        snapshots: dict[str, SourceSnapshot] = {}
        for subentry_id, cfg in self.sources.items():
            if isinstance(cfg, StaticSourceConfig):
                snapshots[subentry_id] = self._snapshot_static(subentry_id, cfg)
            elif isinstance(cfg, DynamicFixedConfig):
                snapshots[subentry_id] = self._snapshot_dynamic_fixed(subentry_id, cfg)
            else:
                snapshots[subentry_id] = self._snapshot_curve(subentry_id, cfg)

        ranked, excluded = rank(list(snapshots.values()))
        cheapest = next((s for s in ranked if s.capacity_kw > 0), None)

        temp_24h_mean = self._temp_24h_mean()
        ua_effective = ua_learner.effective_ua(self.ua_state)
        demand_kw = self._compute_demand_kw(ua_effective, temp_24h_mean)
        if self.shadow.result.get("active"):
            self.shadow.refresh_allocations(snapshots, dt_util.utcnow())
        advanced = self.shadow.result if self._demand_mode == DEMAND_MODE_INTERNAL else {}
        if advanced.get("active"):
            demand_kw = advanced["total_allocation"]["total_kw"]
        if demand_kw is None:
            allocations, demand_covered, blended_cost = [], None, None
        else:
            allocations, demand_covered, blended_cost = merit_order(demand_kw, ranked)
            if advanced.get("active"):
                from .cost_engine import Allocation

                amounts = {}
                for row in advanced["total_allocation"]["allocations"]:
                    amounts[row["source_id"]] = amounts.get(row["source_id"], 0) + row["kw"]
                allocations = [
                    Allocation(
                        s.subentry_id,
                        s.name,
                        amounts[s.subentry_id],
                        amounts[s.subentry_id] / s.capacity_kw if s.capacity_kw else 0,
                        s.cost_eur_per_kwh,
                    )
                    for s in ranked
                    if amounts.get(s.subentry_id, 0) > 0
                ]
                demand_covered = advanced["total_allocation"]["uncovered_kw"] <= 1e-6
                supplied = sum(a.kw for a in allocations)
                blended_cost = (
                    sum(a.kw * a.cost_eur_per_kwh for a in allocations) / supplied
                    if supplied
                    else None
                )

        return HubData(
            snapshots=snapshots,
            ranked=ranked,
            excluded=excluded,
            cheapest=cheapest,
            demand_kw=demand_kw,
            demand_covered=demand_covered,
            allocations=allocations,
            blended_cost=blended_cost,
            degree_hours_24h=self._degree_hours_24h(),
            ua_effective=ua_effective,
            temp_c=self._hub_temp_c(),
            temp_24h_mean=temp_24h_mean,
            buy_eur_per_kwh=self._buy,
            forecast=self.forecast,
            forecast_combined=self.forecast_combined,
            forecast_updated=self.forecast_updated,
        )

    async def _async_update_data(self) -> HubData:
        return self.build_hub_data()

    # ------------------------------------------------------------------ #
    # Sampling tick / day accumulator
    # ------------------------------------------------------------------ #

    def _accumulate_metered_energy(
        self,
        subentry_id: str,
        energy_kwh: float | None,
        cop: float | None,
        *,
        rebaseline: bool = False,
    ) -> None:
        """Fold a cumulative counter reading into the day accumulator.

        ``cop`` converts the electricity delta into delivered heat (blended
        COP for curve sources, the fixed COP for dynamic_fixed ones); with an
        unknown COP the electricity still counts but no heat is credited.
        ``rebaseline`` stores the reading without crediting the delta — used
        after a sampling gap, whose energy was spent while no degree-hours
        (and no COP) were being observed.
        """
        if energy_kwh is None or not math.isfinite(energy_kwh) or energy_kwh < 0:
            self.day_accum.last_energy_counters.pop(subentry_id, None)
            return
        last = self.day_accum.last_energy_counters.get(subentry_id)
        self.day_accum.last_energy_counters[subentry_id] = energy_kwh
        if last is None or rebaseline:
            return
        delta_e = energy_kwh - last
        if delta_e <= 0:
            return  # counter reset or no movement — nothing to fold in
        # A physical upper bound catches plausible-looking reset artifacts before
        # they reach the daily learner. Per-source settings may tighten the bound.
        max_kw = (
            self.shadow.config.get("sources", {}).get(subentry_id, {}).get("max_electric_kw", 20.0)
        )
        elapsed = (
            (dt_util.utcnow() - self.day_accum.last_tick_utc).total_seconds()
            if self.day_accum.last_tick_utc
            else SAMPLE_INTERVAL_S
        )
        if delta_e > max_kw * max(elapsed, 0) / 3600 * 1.05:
            return
        self.day_accum.electricity_kwh += delta_e
        if cop is not None and cop > 0:
            delivered = delta_e * cop
            self.day_accum.delivered_kwh += delivered
            self.day_accum.per_source_kwh[subentry_id] = (
                self.day_accum.per_source_kwh.get(subentry_id, 0.0) + delivered
            )

    def _accumulate_static_duration(self, subentry_id: str, cfg: StaticSourceConfig) -> None:
        act = cfg.activity
        if act.mode != ACTIVITY_MODE_DURATION or not act.entity:
            return
        state = self.hass.states.get(act.entity)
        if state is None or state.state != "on":
            return
        kwh = act.active_output_kw * (SAMPLE_INTERVAL_S / 3600.0)
        self.day_accum.wood_kwh += kwh
        self.day_accum.delivered_kwh += kwh
        self.day_accum.per_source_kwh[subentry_id] = (
            self.day_accum.per_source_kwh.get(subentry_id, 0.0) + kwh
        )

    def _configured_entities(self) -> list[str]:
        """Every entity id this hub and its sources were configured to read."""
        ids = [
            self._buy_entity,
            self._day_ahead_entity,
            self._forecast_price_entity,
            self._weather_entity,
            self._outdoor_temp_entity,
            self._demand_entity,
        ]
        for cfg in self.sources.values():
            if isinstance(cfg, CurveSourceConfig):
                ids += [
                    cfg.power_entity,
                    cfg.energy_entity,
                    cfg.climate_entity,
                    cfg.pump_temp_entity,
                    cfg.temp_entity_override,
                ]
            elif isinstance(cfg, DynamicFixedConfig):
                ids.append(cfg.energy_entity)
            elif isinstance(cfg, StaticSourceConfig):
                ids.append(cfg.activity.entity)
        return [e for e in ids if e] + sorted(self.shadow.watched)

    def _warn_missing_entities(self) -> None:
        """Log once per configured entity id that does not exist at all.

        An entity that was renamed or mistyped looks exactly like one that is
        temporarily unavailable — both just yield ``None`` — so a source can
        sit at "temperature unavailable" for months with nothing in the log to
        say why. Absent from the state machine is a different, permanent
        failure, and it deserves to be said out loud.

        Skipped until HA has finished starting, so an integration that simply
        has not been set up yet is never mistaken for a missing one.
        """
        if self.hass.state is not CoreState.running:
            return
        for entity_id in self._configured_entities():
            if entity_id in self._warned_missing:
                continue
            if self.hass.states.get(entity_id) is None:
                self._warned_missing.add(entity_id)
                _LOGGER.warning(
                    "Configured entity %s does not exist; anything depending on it "
                    "will stay unavailable until the id is corrected",
                    entity_id,
                )

    def _maybe_roll_day(self) -> bool:
        """Capture and reset the accumulator if its day is already over.

        Called on load, at the top of every sampling tick, and by the 00:05
        job, so a midnight missed while HA was down (or a capture job that
        simply didn't fire) is still captured — before any of today's samples
        can be folded into yesterday. Returns ``True`` when a day was rolled.
        """
        today = dt_util.now().date()
        try:
            accum_date = date.fromisoformat(self.day_accum.date)
        except ValueError:
            accum_date = None
        if accum_date is not None and accum_date >= today:
            return False
        if accum_date is not None:
            self._capture_day(self.day_accum)
        # Carry the counter baselines (and when they were read) across the
        # reset: without them the first minute of the new day is lost, and a
        # restored-after-outage baseline is still judged by its real age.
        self.day_accum = DayAccum(
            date=today.isoformat(),
            last_energy_counters=dict(self.day_accum.last_energy_counters),
            last_tick_utc=self.day_accum.last_tick_utc,
        )
        self.store.schedule_save(self.store_snapshot())
        return True

    def _capture_day(self, accum: DayAccum) -> None:
        """Log a finished day into the UA learner (internal demand mode only)."""
        if self._demand_mode != DEMAND_MODE_INTERNAL or not self._ua_learning:
            return
        coverage = accum.coverage()
        if self.shadow.config:
            length = history.local_day_length_s(accum.date)
            coverage = min(
                [coverage, *(accum.heat_covered_s.get(sid, 0) / length for sid in self.sources)]
            )
        row = ua_learner.make_row(
            date=accum.date,
            degree_hours=accum.degree_hours,
            delivered_kwh=accum.delivered_kwh,
            electricity_kwh=accum.electricity_kwh,
            wood_kwh=accum.wood_kwh,
            coverage=coverage,
        )
        # Predict *before* ingesting: scoring the model after it has already
        # learned from this very day would be an in-sample (flattering) MAE.
        predicted = ua_learner.predict_daily_kwh(self.ua_state, row.degree_hours)
        self.ua_state = ua_learner.note_prediction(self.ua_state, predicted)
        self.ua_state = ua_learner.ingest(self.ua_state, row)

    @callback
    def _handle_sampling_tick(self, _now: datetime) -> None:
        now = dt_util.utcnow()
        self._warn_missing_entities()
        self._maybe_roll_day()
        accum = self.day_accum
        last_tick = accum.last_tick_utc
        rebaseline = (
            last_tick is None or (now - last_tick).total_seconds() > SAMPLE_GAP_REBASELINE_S
        )
        if rebaseline:
            # Forget *every* baseline, not just those read this tick: a meter
            # that is still unavailable right after the outage would otherwise
            # keep its pre-outage baseline and credit the whole outage's energy
            # on whichever later tick it comes back.
            accum.last_energy_counters.clear()

        hub_temp = self._hub_temp_c()
        if hub_temp is not None:
            self._temp_history.append((now, hub_temp))
            deficit = max(self._base_temp - hub_temp, 0.0)
            accum.degree_hours += deficit * (SAMPLE_INTERVAL_S / 3600.0)
            accum.temp_covered_s += SAMPLE_INTERVAL_S
        self._refresh_temp_stats(now)

        for subentry_id, cfg in self.sources.items():
            if not isinstance(cfg, CurveSourceConfig):
                continue
            hvac_action = None
            if cfg.climate_entity:
                state = self.hass.states.get(cfg.climate_entity)
                if state is not None:
                    hvac_action = state.attributes.get("hvac_action")
            dq = self._samples.setdefault(subentry_id, deque(maxlen=cfg.blend_window_min + 5))
            dq.append(
                Sample(
                    ts=now,
                    power_w=self._power_w(cfg.power_entity),
                    energy_kwh=self._energy_kwh(cfg.energy_entity),
                    temp_c=self._resolve_temp(cfg)[0],
                    hvac_action=hvac_action,
                )
            )

        # One evaluation per tick: the snapshot's blended COP (computed with
        # the sample just appended) is also what converts this tick's energy
        # delta into delivered heat.
        data = self.build_hub_data()
        self.shadow.sample(now, data.snapshots, data.ua_effective, hub_temp)
        for subentry_id, cfg in self.sources.items():
            mapping = self.shadow.config.get("sources", {}).get(subentry_id, {})
            if mapping.get("energy_entities"):
                row = self.shadow.result.get("sources", {}).get(subentry_id, {})
                electric = row.get("electric_kwh")
                snap = data.snapshots.get(subentry_id)
                if electric is not None and snap and snap.cop is not None:
                    accum.electricity_kwh += electric
                    delivered = electric * snap.cop
                    accum.delivered_kwh += delivered
                    accum.per_source_kwh[subentry_id] = (
                        accum.per_source_kwh.get(subentry_id, 0) + delivered
                    )
                continue
            if isinstance(cfg, CurveSourceConfig):
                snap = data.snapshots.get(subentry_id)
                self._accumulate_metered_energy(
                    subentry_id,
                    self._energy_kwh(cfg.energy_entity),
                    snap.cop if snap is not None else None,
                    rebaseline=rebaseline,
                )
            elif isinstance(cfg, DynamicFixedConfig):
                if cfg.energy_entity:
                    self._accumulate_metered_energy(
                        subentry_id,
                        self._energy_kwh(cfg.energy_entity),
                        cfg.cop,
                        rebaseline=rebaseline,
                    )
            elif isinstance(cfg, StaticSourceConfig):
                self._accumulate_static_duration(subentry_id, cfg)
        if self.shadow.config and last_tick is not None and not rebaseline:
            elapsed = max(0, (now - last_tick).total_seconds())
            for sid, row in self.shadow.result.get("sources", {}).items():
                if row.get("heat_kw") is not None and not row.get("reasons"):
                    accum.heat_covered_s[sid] = accum.heat_covered_s.get(sid, 0) + elapsed
        accum.last_tick_utc = now

        self.store.schedule_save(self.store_snapshot(), SAVE_DELAY_TICK_S)
        self.async_set_updated_data(
            self.build_hub_data() if self.shadow.result.get("active") else data
        )

    # ------------------------------------------------------------------ #
    # State-change-driven recompute (debounced) + wood event accounting
    # ------------------------------------------------------------------ #

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in (self._day_ahead_entity, self._forecast_price_entity):
            self._forecast_dirty = True
        # A throttle, not a sliding debounce: re-arming the timer on every
        # event would let a 1 Hz power sensor postpone the recompute forever.
        # The first event arms it; later ones ride along until it fires.
        if self._debounce_unsub is None:
            self._debounce_unsub = async_call_later(
                self.hass, _RECOMPUTE_DEBOUNCE_S, self._debounced_recompute
            )

    @callback
    def _debounced_recompute(self, _now: datetime) -> None:
        self._debounce_unsub = None
        if self._forecast_dirty:
            # The rebuild itself clears the flag once it has started reading
            # prices, so an invalidation that lands mid-rebuild survives it.
            self._request_forecast_rebuild()
        self.async_set_updated_data(self.build_hub_data())

    @callback
    def _handle_activity_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or new_state.state != "on":
            return
        if old_state is not None and old_state.state == "on":
            return  # not an off->on transition

        subentry_id = next(
            (
                sid
                for sid, cfg in self.sources.items()
                if isinstance(cfg, StaticSourceConfig) and cfg.activity.entity == entity_id
            ),
            None,
        )
        if subentry_id is None:
            return
        cfg = self.sources[subentry_id]
        assert isinstance(cfg, StaticSourceConfig)
        if cfg.activity.mode != ACTIVITY_MODE_EVENT:
            return

        now = dt_util.utcnow()
        last = self.last_event_ts.get(subentry_id)
        debounce_s = cfg.activity.event_debounce_hours * 3600.0
        if last is not None and (now - last).total_seconds() < debounce_s:
            return

        self.last_event_ts[subentry_id] = now
        kwh = cfg.activity.energy_per_event_kwh or 0.0
        self.day_accum.wood_kwh += kwh
        self.day_accum.delivered_kwh += kwh
        self.day_accum.per_source_kwh[subentry_id] = (
            self.day_accum.per_source_kwh.get(subentry_id, 0.0) + kwh
        )
        self.wood_log.append(
            {"ts": now.isoformat(), "subentry_id": subentry_id, "kwh": kwh, "mode": "event"}
        )
        self.store.schedule_save(self.store_snapshot())
        self.async_set_updated_data(self.build_hub_data())

    # ------------------------------------------------------------------ #
    # Forecast rebuild
    # ------------------------------------------------------------------ #

    def _safe_price_slots(self, entity_id: str | None) -> list[price_source.ForecastSlot]:
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        try:
            return price_source.slots_from_state(state)
        except price_source.PriceFormatError:
            return []

    def _weather_temp_unit(self) -> object:
        state = self.hass.states.get(self._weather_entity) if self._weather_entity else None
        return state.attributes.get("temperature_unit") if state is not None else None

    async def _async_weather_temps(self) -> list[TempPoint]:
        if not self._weather_entity:
            return []
        now = dt_util.utcnow()
        if (
            self._weather_cache is not None
            and self._weather_cache_at is not None
            and (now - self._weather_cache_at).total_seconds() < WEATHER_CACHE_MIN * 60
        ):
            return self._weather_cache
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": self._weather_entity},
                blocking=True,
                return_response=True,
            )
            raw = response[self._weather_entity]["forecast"]
            # Forecast items are in the weather entity's own unit (°F on an
            # imperial instance), named by its ``temperature_unit`` attribute.
            unit = self._weather_temp_unit()
            points: list[TempPoint] = []
            for item in raw:
                ts = datetime.fromisoformat(item["datetime"])
                if ts.tzinfo is None:
                    raise ValueError(
                        f"weather forecast datetime is not tz-aware: {item['datetime']!r}"
                    )
                raw_temp = item.get("temperature")
                if raw_temp is None or isinstance(raw_temp, bool):
                    continue
                try:
                    temp_c = temp_source.to_celsius(float(raw_temp), unit)
                except (TypeError, ValueError):
                    continue
                # One NaN or sentinel point would otherwise be interpolated
                # into every slot around it.
                if temp_c is None or not math.isfinite(temp_c):
                    continue
                if not temp_source.is_plausible(temp_c):
                    continue
                points.append(TempPoint(ts=dt_util.as_utc(ts), temp_c=temp_c))
        except Exception as err:  # noqa: BLE001 - weather forecast is best-effort
            _LOGGER.debug("Weather forecast unavailable: %s", err)
            return self._weather_cache or []
        self._weather_cache = points
        self._weather_cache_at = now
        return points

    @callback
    def _request_forecast_rebuild(self) -> None:
        """Schedule a rebuild unless one is already waiting to start."""
        if self._forecast_queued:
            return
        self._forecast_queued = True
        self.config_entry.async_create_task(
            self.hass, self._async_queued_rebuild(), "live_heating_costs_forecast"
        )

    async def _async_queued_rebuild(self) -> None:
        async with self._forecast_lock:
            # Cleared on *entering* the lock: a request arriving while this
            # rebuild runs queues exactly one more, which will see its data.
            self._forecast_queued = False
            await self._async_rebuild_forecast_locked()

    async def async_rebuild_forecast(self) -> None:
        """Rebuild the per-source + combined forecast now (serialized)."""
        async with self._forecast_lock:
            await self._async_rebuild_forecast_locked()

    async def _async_rebuild_forecast_locked(self) -> None:
        # Cleared before reading anything: a price update that arrives during
        # the weather await below sets it again, so the next recompute
        # rebuilds rather than this run silently swallowing the invalidation.
        self._forecast_dirty = False
        temps = await self._async_weather_temps()
        # Prices and "now" are read *after* the await so a slow weather call
        # never publishes prices older than the ones already in the state.
        now = dt_util.utcnow()
        price_slots = self._safe_price_slots(self._day_ahead_entity)
        est_slots = self._safe_price_slots(self._forecast_price_entity)
        # The hourly forecast's first point is the *next* full hour; anchoring
        # it with the measured outdoor temperature keeps curve sources priced
        # from now on instead of dropping out of the leading slots.
        temps = forecast_mod.anchor_temps(temps, now, self._hub_temp_c())

        forecast: dict[str, list[SourceSlotCost]] = {}
        static_costs: dict[str, tuple[float | None, float]] = {}
        for subentry_id, cfg in self.sources.items():
            if isinstance(cfg, CurveSourceConfig):
                forecast[subentry_id] = forecast_mod.build_source_forecast(
                    price_slots,
                    est_slots,
                    temps,
                    now,
                    FORECAST_HORIZON_H,
                    cop_at=cfg.cop_curve.value_at,
                    capacity_at=cfg.capacity_curve.value_at,
                    fixed_cop=None,
                    fixed_capacity_kw=None,
                    operating_limit_c=cfg.operating_limit_c,
                    # Same clamp the live blend applies, so a forecast slot
                    # can't price the pump at a COP the live cost never would.
                    min_cop=cfg.min_cop,
                    max_cop=cfg.max_cop,
                )
            elif isinstance(cfg, DynamicFixedConfig):
                forecast[subentry_id] = forecast_mod.build_source_forecast(
                    price_slots,
                    est_slots,
                    temps,
                    now,
                    FORECAST_HORIZON_H,
                    cop_at=None,
                    capacity_at=None,
                    fixed_cop=cfg.cop,
                    fixed_capacity_kw=cfg.capacity_kw,
                    operating_limit_c=None,
                )
            elif isinstance(cfg, StaticSourceConfig):
                # Constant cost, no weather/price dependence -> no per-slot
                # forecast (and no SourceCostForecastSensor). It's still folded
                # into the combined slots below as a flat line, so a cheap
                # wood stove can win the comparison at any hour.
                static_costs[subentry_id] = (
                    cfg.cost_eur_per_kwh(self.fuel_price_overrides.get(subentry_id)),
                    cfg.capacity_kw,
                )

        self.forecast = forecast
        self.forecast_combined = forecast_mod.combined_slots(forecast, static_costs=static_costs)
        self.forecast_updated = now
        self.async_set_updated_data(self.build_hub_data())

    @callback
    def _handle_forecast_tick(self, _now: datetime) -> None:
        self._request_forecast_rebuild()

    # ------------------------------------------------------------------ #
    # Fuel-price override + daily capture
    # ------------------------------------------------------------------ #

    @callback
    def async_set_fuel_price_override(self, subentry_id: str, price_per_unit_eur: float) -> None:
        """Persist a per-source fuel-price override and recompute immediately."""
        self.fuel_price_overrides[subentry_id] = price_per_unit_eur
        self.store.schedule_save(self.store_snapshot())
        # The static source's flat line in the combined forecast is built from
        # this price, so redraw it now rather than at the next 15-min rebuild.
        self._request_forecast_rebuild()
        self.async_set_updated_data(self.build_hub_data())

    @callback
    def _handle_daily_capture(self, _now: datetime) -> None:
        # Normally a no-op: the first sampling tick after midnight has already
        # rolled the day. Kept as a safety net (e.g. the tick listener was
        # never set up), sharing the same date-guarded capture so a day can
        # never be captured twice.
        if self._maybe_roll_day():
            self.async_set_updated_data(self.build_hub_data())


type LiveHeatingCostsConfigEntry = ConfigEntry[LiveHeatingCostsCoordinator]
