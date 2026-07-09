"""Typed views over a source subentry's config data.

Pure module (no Home Assistant import): turns the raw subentry ``data``
mapping the config flow produces into one of the three :data:`SourceConfig`
variants, validating required fields and applying ``const.py`` defaults —
mirrors the pattern in Load Scheduler's ``models.py``. Tested directly.

Curve-source points (``cop_points`` / ``capacity_points``) are stored in the
subentry as JSON-safe ``[[t, v], ...]`` lists (resolved from a preset or typed
in by the user); this module converts them to validated, sorted tuples and
falls back to :data:`curves.PRESETS` when a preset was picked but the points
were not (yet) copied into ``data``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property

from .const import (
    ACTIVITY_MODE_DURATION,
    ACTIVITY_MODE_EVENT,
    ACTIVITY_MODE_NONE,
    ACTIVITY_MODES,
    CONF_ACTIVE_OUTPUT_KW,
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVITY_MODE,
    CONF_BLEND_WINDOW_MIN,
    CONF_CAPACITY_KW,
    CONF_CAPACITY_POINTS,
    CONF_CLIMATE_ENTITY,
    CONF_COP,
    CONF_COP_POINTS,
    CONF_EFFICIENCY,
    CONF_ENERGY_DENSITY,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_PER_EVENT_KWH,
    CONF_EVENT_DEBOUNCE_HOURS,
    CONF_FUEL_UNIT,
    CONF_MAX_COP,
    CONF_MIN_COP,
    CONF_NAME,
    CONF_OPERATING_LIMIT_C,
    CONF_POWER_ENTITY,
    CONF_PRESET,
    CONF_PRICE_PER_UNIT,
    CONF_PUMP_TEMP_ENTITY,
    CONF_SOURCE_TYPE,
    CONF_STANDBY_W,
    CONF_TEMP_ENTITY_OVERRIDE,
    DEFAULT_BLEND_WINDOW_MIN,
    DEFAULT_CAPACITY_KW,
    DEFAULT_EFFICIENCY,
    DEFAULT_ENERGY_DENSITY_KG,
    DEFAULT_ENERGY_DENSITY_M3,
    DEFAULT_ENERGY_DENSITY_M3_LOOSE,
    DEFAULT_EVENT_DEBOUNCE_HOURS,
    DEFAULT_FIXED_COP,
    DEFAULT_STANDBY_W,
    FUEL_UNIT_KG,
    FUEL_UNIT_KWH,
    FUEL_UNIT_M3,
    FUEL_UNIT_M3_LOOSE,
    FUEL_UNITS,
    PRESET_CUSTOM,
    SOURCE_TYPE_CURVE,
    SOURCE_TYPE_DYNAMIC_FIXED,
    SOURCE_TYPE_STATIC,
)
from .cost_engine import wood_cost_eur_per_kwh
from .curves import PRESETS, Curve, validate_points

_ENERGY_DENSITY_DEFAULTS: dict[str, float] = {
    FUEL_UNIT_M3: DEFAULT_ENERGY_DENSITY_M3,
    FUEL_UNIT_M3_LOOSE: DEFAULT_ENERGY_DENSITY_M3_LOOSE,
    FUEL_UNIT_KG: DEFAULT_ENERGY_DENSITY_KG,
    FUEL_UNIT_KWH: 1.0,
}


class ConfigError(ValueError):
    """A subentry field is missing or invalid.

    ``field`` names the offending config key (a ``CONF_*`` constant) so the
    config flow can attach the error to the right form field.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _require_name(data: Mapping) -> str:
    name = data.get(CONF_NAME)
    if not name:
        raise ConfigError(CONF_NAME, "name is required")
    return str(name)


def _require_capacity_kw(data: Mapping) -> float:
    capacity_kw = float(data.get(CONF_CAPACITY_KW, DEFAULT_CAPACITY_KW))
    if capacity_kw < 0:
        raise ConfigError(CONF_CAPACITY_KW, "capacity_kw must be >= 0")
    return capacity_kw


def _points_from_raw(raw: object, field: str) -> tuple[tuple[float, float], ...]:
    try:
        points = [(float(p[0]), float(p[1])) for p in raw]  # type: ignore[union-attr]
    except (TypeError, ValueError, IndexError) as err:
        raise ConfigError(field, f"invalid points for {field}: {err}") from err
    errors = validate_points(points)
    if errors:
        raise ConfigError(field, "; ".join(errors))
    return tuple(sorted(points, key=lambda p: p[0]))


# --------------------------------------------------------------------------- #
# Activity (wood-burner detection)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActivityConfig:
    """How a static (fuel) source's activity/output is detected."""

    mode: str  # one of const.ACTIVITY_MODE_*
    entity: str | None
    active_output_kw: float | None  # duration mode: assumed output while "on"
    energy_per_event_kwh: float | None  # event mode: fixed kWh per off->on
    event_debounce_hours: float


def _activity_from_subentry(data: Mapping) -> ActivityConfig:
    mode = data.get(CONF_ACTIVITY_MODE, ACTIVITY_MODE_NONE)
    if mode not in ACTIVITY_MODES:
        raise ConfigError(CONF_ACTIVITY_MODE, f"unknown activity_mode: {mode!r}")

    entity = data.get(CONF_ACTIVITY_ENTITY) or None
    raw_output = data.get(CONF_ACTIVE_OUTPUT_KW)
    raw_event = data.get(CONF_ENERGY_PER_EVENT_KWH)
    active_output_kw = float(raw_output) if raw_output is not None else None
    energy_per_event_kwh = float(raw_event) if raw_event is not None else None
    debounce_hours = float(data.get(CONF_EVENT_DEBOUNCE_HOURS, DEFAULT_EVENT_DEBOUNCE_HOURS))

    if mode == ACTIVITY_MODE_DURATION:
        if not entity:
            raise ConfigError(CONF_ACTIVITY_ENTITY, "duration mode requires activity_entity")
        if active_output_kw is None or active_output_kw <= 0:
            raise ConfigError(CONF_ACTIVE_OUTPUT_KW, "duration mode requires active_output_kw > 0")
    elif mode == ACTIVITY_MODE_EVENT:
        if not entity:
            raise ConfigError(CONF_ACTIVITY_ENTITY, "event mode requires activity_entity")
        if energy_per_event_kwh is None or energy_per_event_kwh <= 0:
            raise ConfigError(
                CONF_ENERGY_PER_EVENT_KWH, "event mode requires energy_per_event_kwh > 0"
            )

    return ActivityConfig(
        mode=mode,
        entity=entity,
        active_output_kw=active_output_kw,
        energy_per_event_kwh=energy_per_event_kwh,
        event_debounce_hours=debounce_hours,
    )


# --------------------------------------------------------------------------- #
# static (fuel) sources — wood
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StaticSourceConfig:
    """A fixed fuel-derived cost source (wood burner / stove)."""

    name: str
    capacity_kw: float
    fuel_unit: str  # one of const.FUEL_UNIT_*
    price_per_unit_eur: float
    energy_density_kwh_per_unit: float
    efficiency: float
    activity: ActivityConfig

    def cost_eur_per_kwh(self, price_override: float | None = None) -> float:
        """Delivered-heat cost, optionally overriding the configured price."""
        price = price_override if price_override is not None else self.price_per_unit_eur
        return wood_cost_eur_per_kwh(price, self.energy_density_kwh_per_unit, self.efficiency)


def _static_from_subentry(data: Mapping) -> StaticSourceConfig:
    name = _require_name(data)
    capacity_kw = _require_capacity_kw(data)

    fuel_unit = data.get(CONF_FUEL_UNIT, FUEL_UNIT_M3)
    if fuel_unit not in FUEL_UNITS:
        raise ConfigError(CONF_FUEL_UNIT, f"unknown fuel_unit: {fuel_unit!r}")

    raw_price = data.get(CONF_PRICE_PER_UNIT)
    if raw_price is None:
        raise ConfigError(CONF_PRICE_PER_UNIT, "price_per_unit_eur is required")
    price = float(raw_price)
    if price <= 0:
        raise ConfigError(CONF_PRICE_PER_UNIT, "price_per_unit_eur must be > 0")

    raw_density = data.get(CONF_ENERGY_DENSITY)
    density = float(raw_density) if raw_density is not None else _ENERGY_DENSITY_DEFAULTS[fuel_unit]
    if density <= 0:
        raise ConfigError(CONF_ENERGY_DENSITY, "energy_density_kwh_per_unit must be > 0")

    efficiency = float(data.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY))
    if not (0 < efficiency <= 1):
        raise ConfigError(CONF_EFFICIENCY, "efficiency must be within (0, 1]")

    activity = _activity_from_subentry(data)

    return StaticSourceConfig(
        name=name,
        capacity_kw=capacity_kw,
        fuel_unit=fuel_unit,
        price_per_unit_eur=price,
        energy_density_kwh_per_unit=density,
        efficiency=efficiency,
        activity=activity,
    )


# --------------------------------------------------------------------------- #
# dynamic_fixed sources — resistive (or any fixed-COP electric source)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DynamicFixedConfig:
    """An electric source with a fixed COP (resistive heating = 1.0).

    ``energy_entity`` (optional) is a cumulative kWh counter for the source;
    when set, its consumption feeds the day accumulator and the UA learner
    (delivered heat = energy × COP), same as a heat pump's Shelly counter.
    """

    name: str
    capacity_kw: float
    cop: float
    energy_entity: str | None = None


def _dynamic_fixed_from_subentry(data: Mapping) -> DynamicFixedConfig:
    name = _require_name(data)
    capacity_kw = _require_capacity_kw(data)
    cop = float(data.get(CONF_COP, DEFAULT_FIXED_COP))
    if cop <= 0:
        raise ConfigError(CONF_COP, "cop must be > 0")
    return DynamicFixedConfig(
        name=name,
        capacity_kw=capacity_kw,
        cop=cop,
        energy_entity=data.get(CONF_ENERGY_ENTITY) or None,
    )


# --------------------------------------------------------------------------- #
# curve sources — heat pumps
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CurveSourceConfig:
    """A heat pump modelled by COP(T)/capacity(T) lookup tables."""

    name: str
    power_entity: str
    energy_entity: str
    climate_entity: str | None
    pump_temp_entity: str | None
    temp_entity_override: str | None
    standby_w: float
    blend_window_min: int
    preset_key: str | None  # None => custom (not one of curves.PRESETS)
    cop_points: tuple[tuple[float, float], ...]
    capacity_points: tuple[tuple[float, float], ...]
    operating_limit_c: float
    min_cop: float
    max_cop: float

    @cached_property
    def cop_curve(self) -> Curve:
        return Curve.from_points(self.cop_points)

    @cached_property
    def capacity_curve(self) -> Curve:
        return Curve.from_points(self.capacity_points)


def _resolve_points(
    data: Mapping, field: str, fallback: tuple[tuple[float, float], ...] | None
) -> tuple[tuple[float, float], ...]:
    raw = data.get(field)
    if raw is None:
        if fallback is None:
            raise ConfigError(field, f"{field} is required when no preset is selected")
        return fallback
    return _points_from_raw(raw, field)


def _resolve_float(data: Mapping, field: str, fallback: float | None) -> float:
    raw = data.get(field)
    if raw is None:
        if fallback is None:
            raise ConfigError(field, f"{field} is required when no preset is selected")
        return float(fallback)
    return float(raw)


def _curve_from_subentry(data: Mapping) -> CurveSourceConfig:
    name = _require_name(data)

    power_entity = data.get(CONF_POWER_ENTITY)
    if not power_entity:
        raise ConfigError(CONF_POWER_ENTITY, "power_entity is required")
    energy_entity = data.get(CONF_ENERGY_ENTITY)
    if not energy_entity:
        raise ConfigError(CONF_ENERGY_ENTITY, "energy_entity is required")

    climate_entity = data.get(CONF_CLIMATE_ENTITY) or None
    pump_temp_entity = data.get(CONF_PUMP_TEMP_ENTITY) or None
    temp_entity_override = data.get(CONF_TEMP_ENTITY_OVERRIDE) or None
    standby_w = float(data.get(CONF_STANDBY_W, DEFAULT_STANDBY_W))
    blend_window_min = int(data.get(CONF_BLEND_WINDOW_MIN, DEFAULT_BLEND_WINDOW_MIN))

    raw_preset = data.get(CONF_PRESET)
    preset_key = raw_preset if raw_preset and raw_preset != PRESET_CUSTOM else None
    preset = None
    if preset_key is not None:
        preset = PRESETS.get(preset_key)
        if preset is None:
            raise ConfigError(CONF_PRESET, f"unknown preset: {preset_key!r}")

    cop_points = _resolve_points(data, CONF_COP_POINTS, preset.cop_points if preset else None)
    capacity_points = _resolve_points(
        data, CONF_CAPACITY_POINTS, preset.capacity_points if preset else None
    )
    operating_limit_c = _resolve_float(
        data, CONF_OPERATING_LIMIT_C, preset.operating_limit_c if preset else None
    )
    min_cop = _resolve_float(data, CONF_MIN_COP, preset.min_cop if preset else None)
    max_cop = _resolve_float(data, CONF_MAX_COP, preset.max_cop if preset else None)

    if min_cop <= 0:
        raise ConfigError(CONF_MIN_COP, "min_cop must be > 0")
    if max_cop <= min_cop:
        raise ConfigError(CONF_MAX_COP, "max_cop must be > min_cop")

    return CurveSourceConfig(
        name=name,
        power_entity=power_entity,
        energy_entity=energy_entity,
        climate_entity=climate_entity,
        pump_temp_entity=pump_temp_entity,
        temp_entity_override=temp_entity_override,
        standby_w=standby_w,
        blend_window_min=blend_window_min,
        preset_key=preset_key,
        cop_points=cop_points,
        capacity_points=capacity_points,
        operating_limit_c=operating_limit_c,
        min_cop=min_cop,
        max_cop=max_cop,
    )


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

SourceConfig = StaticSourceConfig | DynamicFixedConfig | CurveSourceConfig


def source_from_subentry(data: Mapping) -> SourceConfig:
    """Build the right :data:`SourceConfig` variant from a subentry ``data`` mapping."""
    source_type = data.get(CONF_SOURCE_TYPE)
    if source_type == SOURCE_TYPE_STATIC:
        return _static_from_subentry(data)
    if source_type == SOURCE_TYPE_DYNAMIC_FIXED:
        return _dynamic_fixed_from_subentry(data)
    if source_type == SOURCE_TYPE_CURVE:
        return _curve_from_subentry(data)
    raise ConfigError(CONF_SOURCE_TYPE, f"unknown source_type: {source_type!r}")
