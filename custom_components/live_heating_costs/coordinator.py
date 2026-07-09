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

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import blend, demand, models, price_source, ua_learner
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
    SAMPLE_INTERVAL_S,
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
from .ua_learner import UaState

_LOGGER = logging.getLogger(__name__)

# Debounce window for state-change-triggered recomputes: several watched
# entities (buy price, per-source power/energy, climate) tend to update within
# the same second, so a short collapse avoids redundant snapshot rebuilds.
_RECOMPUTE_DEBOUNCE_S = 2.0


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
    """

    date: str  # local ISO date, e.g. "2026-07-08"
    degree_hours: float = 0.0
    delivered_kwh: float = 0.0
    electricity_kwh: float = 0.0
    wood_kwh: float = 0.0
    per_source_kwh: dict[str, float] = field(default_factory=dict)
    last_energy_counters: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "degree_hours": self.degree_hours,
            "delivered_kwh": self.delivered_kwh,
            "electricity_kwh": self.electricity_kwh,
            "wood_kwh": self.wood_kwh,
            "per_source_kwh": dict(self.per_source_kwh),
            "last_energy_counters": dict(self.last_energy_counters),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DayAccum:
        return cls(
            date=d["date"],
            degree_hours=d.get("degree_hours", 0.0),
            delivered_kwh=d.get("delivered_kwh", 0.0),
            electricity_kwh=d.get("electricity_kwh", 0.0),
            wood_kwh=d.get("wood_kwh", 0.0),
            per_source_kwh=dict(d.get("per_source_kwh", {})),
            last_energy_counters=dict(d.get("last_energy_counters", {})),
        )


@dataclass(frozen=True)
class _CurveEval:
    """Result of evaluating one curve source's COP/capacity right now."""

    temp: float | None
    temp_source: str  # "pump" | "override" | "hub"
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

        self._buy: float | None = None
        self.forecast: dict[str, list[SourceSlotCost]] = {}
        self.forecast_combined: list[dict] = []
        self.forecast_updated: datetime | None = None
        self._weather_cache: list[TempPoint] | None = None
        self._weather_cache_at: datetime | None = None

        self._debounce_unsub: Callable[[], None] | None = None
        self._forecast_dirty = True  # force a first build on setup

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

    def store_snapshot(self) -> dict:
        """The full persisted state as a JSON-safe dict."""
        return {
            "ua": self.ua_state.to_dict(),
            "day_accum": self.day_accum.to_dict(),
            "wood_log": self.wood_log[-500:],
            "fuel_price_overrides": dict(self.fuel_price_overrides),
            "last_event_ts": {k: v.isoformat() for k, v in self.last_event_ts.items()},
        }

    @callback
    def async_setup_listeners(self) -> None:
        """Wire the sampling tick, forecast rebuild, daily capture and state watchers."""
        watched: set[str] = set()
        event_mode_entities: set[str] = set()
        for entity in (self._buy_entity, self._outdoor_temp_entity):
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
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._handle_forecast_tick, timedelta(minutes=FORECAST_REBUILD_MIN)
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

    def _resolve_temp(self, cfg: CurveSourceConfig) -> tuple[float | None, str]:
        """Per-source temperature: pump reading -> per-source override -> hub."""
        if cfg.pump_temp_entity:
            value = self._float(cfg.pump_temp_entity)
            if value is not None:
                return value, "pump"
        if cfg.temp_entity_override:
            value = self._float(cfg.temp_entity_override)
            if value is not None:
                return value, "override"
        return self._float(self._outdoor_temp_entity), "hub"

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

    def _temp_24h_mean(self) -> float | None:
        if not self._temp_history:
            return None
        values = [t for _, t in self._temp_history]
        return sum(values) / len(values)

    def _degree_hours_24h(self) -> float | None:
        if len(self._temp_history) < 2:
            return None
        end = dt_util.utcnow()
        start = end - timedelta(hours=TEMP_MEAN_WINDOW_H)
        return demand.integrate_degree_hours(list(self._temp_history), self._base_temp, start, end)

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
        self._buy = self._float(self._buy_entity)
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
        if demand_kw is None:
            allocations, demand_covered, blended_cost = [], None, None
        else:
            allocations, demand_covered, blended_cost = merit_order(demand_kw, ranked)

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
            temp_c=self._float(self._outdoor_temp_entity),
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

    def _accumulate_curve_energy(
        self, subentry_id: str, cfg: CurveSourceConfig, energy_kwh: float | None
    ) -> None:
        if energy_kwh is None:
            return
        last = self.day_accum.last_energy_counters.get(subentry_id)
        self.day_accum.last_energy_counters[subentry_id] = energy_kwh
        if last is None:
            return
        delta_e = energy_kwh - last
        if delta_e <= 0:
            return  # counter reset or no movement — nothing to fold in
        self.day_accum.electricity_kwh += delta_e
        cop = self._eval_curve(subentry_id, cfg).cop
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

    @callback
    def _handle_sampling_tick(self, _now: datetime) -> None:
        now = dt_util.utcnow()
        hub_temp = self._float(self._outdoor_temp_entity)
        if hub_temp is not None:
            self._temp_history.append((now, hub_temp))
            deficit = max(self._base_temp - hub_temp, 0.0)
            self.day_accum.degree_hours += deficit * (SAMPLE_INTERVAL_S / 3600.0)

        for subentry_id, cfg in self.sources.items():
            if isinstance(cfg, CurveSourceConfig):
                power_w = self._float(cfg.power_entity)
                energy_kwh = self._float(cfg.energy_entity)
                temp, _source = self._resolve_temp(cfg)
                hvac_action = None
                if cfg.climate_entity:
                    state = self.hass.states.get(cfg.climate_entity)
                    if state is not None:
                        hvac_action = state.attributes.get("hvac_action")
                dq = self._samples.setdefault(subentry_id, deque(maxlen=cfg.blend_window_min + 5))
                dq.append(
                    Sample(
                        ts=now,
                        power_w=power_w,
                        energy_kwh=energy_kwh,
                        temp_c=temp,
                        hvac_action=hvac_action,
                    )
                )
                self._accumulate_curve_energy(subentry_id, cfg, energy_kwh)
            elif isinstance(cfg, StaticSourceConfig):
                self._accumulate_static_duration(subentry_id, cfg)

        self.store.schedule_save(self.store_snapshot())
        self.async_set_updated_data(self.build_hub_data())

    # ------------------------------------------------------------------ #
    # State-change-driven recompute (debounced) + wood event accounting
    # ------------------------------------------------------------------ #

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in (self._day_ahead_entity, self._forecast_price_entity):
            self._forecast_dirty = True
        if self._debounce_unsub is not None:
            self._debounce_unsub()
        self._debounce_unsub = async_call_later(
            self.hass, _RECOMPUTE_DEBOUNCE_S, self._debounced_recompute
        )

    @callback
    def _debounced_recompute(self, _now: datetime) -> None:
        self._debounce_unsub = None
        if self._forecast_dirty:
            self._forecast_dirty = False
            self.config_entry.async_create_task(
                self.hass, self.async_rebuild_forecast(), "live_heating_costs_forecast"
            )
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
            points: list[TempPoint] = []
            for item in raw:
                ts = datetime.fromisoformat(item["datetime"])
                if ts.tzinfo is None:
                    raise ValueError(
                        f"weather forecast datetime is not tz-aware: {item['datetime']!r}"
                    )
                points.append(TempPoint(ts=dt_util.as_utc(ts), temp_c=float(item["temperature"])))
        except Exception as err:  # noqa: BLE001 - weather forecast is best-effort
            _LOGGER.debug("Weather forecast unavailable: %s", err)
            return self._weather_cache or []
        self._weather_cache = points
        self._weather_cache_at = now
        return points

    async def async_rebuild_forecast(self) -> None:
        now = dt_util.utcnow()
        price_slots = self._safe_price_slots(self._day_ahead_entity)
        est_slots = self._safe_price_slots(self._forecast_price_entity)
        temps = await self._async_weather_temps()

        forecast: dict[str, list[SourceSlotCost]] = {}
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
            # static sources are constant-cost; the card reads that from the
            # snapshot directly, no forecast needed.

        self.forecast = forecast
        self.forecast_combined = forecast_mod.combined_slots(forecast)
        self.forecast_updated = now
        self._forecast_dirty = False
        self.async_set_updated_data(self.build_hub_data())

    @callback
    def _handle_forecast_tick(self, _now: datetime) -> None:
        self.config_entry.async_create_task(
            self.hass, self.async_rebuild_forecast(), "live_heating_costs_forecast"
        )

    # ------------------------------------------------------------------ #
    # Daily capture (UA-learner ingest + accumulator reset)
    # ------------------------------------------------------------------ #

    @callback
    def async_set_fuel_price_override(self, subentry_id: str, price_per_unit_eur: float) -> None:
        """Persist a per-source fuel-price override and recompute immediately."""
        self.fuel_price_overrides[subentry_id] = price_per_unit_eur
        self.store.schedule_save(self.store_snapshot())
        self.async_set_updated_data(self.build_hub_data())

    @callback
    def _handle_daily_capture(self, _now: datetime) -> None:
        self.config_entry.async_create_task(
            self.hass, self._async_daily_capture(), "live_heating_costs_daily_capture"
        )

    async def _async_daily_capture(self) -> None:
        accum = self.day_accum
        if self._demand_mode == DEMAND_MODE_INTERNAL and self._ua_learning:
            row = ua_learner.make_row(
                date=accum.date,
                degree_hours=accum.degree_hours,
                delivered_kwh=accum.delivered_kwh,
                electricity_kwh=accum.electricity_kwh,
                wood_kwh=accum.wood_kwh,
            )
            self.ua_state = ua_learner.ingest(self.ua_state, row)
            predicted = ua_learner.predict_daily_kwh(self.ua_state, row.degree_hours)
            self.ua_state = ua_learner.note_prediction(self.ua_state, predicted)
        self.day_accum = DayAccum(date=dt_util.now().date().isoformat())
        self.store.schedule_save(self.store_snapshot())
        self.async_set_updated_data(self.build_hub_data())


type LiveHeatingCostsConfigEntry = ConfigEntry[LiveHeatingCostsCoordinator]
