"""Config flow for Live Heating Costs.

The hub flow selects the shared price/weather/demand sources and supports
reconfigure (two steps: ``user``/``reconfigure`` for the shared sensors, then
``demand`` for the demand-model branch). Heat sources (wood stove, resistive
element, heat pump) are added/edited as ``ConfigSubentry`` instances via
``async_get_supported_subentry_types``, mirroring the hub+subentry pattern
used by Load Scheduler's ``config_flow.py``.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import curves, models, price_source
from .const import (
    ACTIVITY_MODE_NONE,
    ACTIVITY_MODES,
    CONF_ACTIVE_OUTPUT_KW,
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVITY_MODE,
    CONF_BACKFILL_DAYS,
    CONF_BASE_TEMP,
    CONF_BLEND_WINDOW_MIN,
    CONF_BUY_PRICE_ENTITY,
    CONF_CAPACITY_KW,
    CONF_CAPACITY_POINTS,
    CONF_CLIMATE_ENTITY,
    CONF_COP,
    CONF_COP_POINTS,
    CONF_DAY_AHEAD_ENTITY,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_MODE,
    CONF_DEMAND_UNIT,
    CONF_EFFICIENCY,
    CONF_ENERGY_DENSITY,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_PER_EVENT_KWH,
    CONF_EVENT_DEBOUNCE_HOURS,
    CONF_FORECAST_PRICE_ENTITY,
    CONF_FUEL_UNIT,
    CONF_MAX_COP,
    CONF_MIN_COP,
    CONF_NAME,
    CONF_OPERATING_LIMIT_C,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRESET,
    CONF_PRICE_PER_UNIT,
    CONF_PUMP_TEMP_ENTITY,
    CONF_SOURCE_TYPE,
    CONF_STANDBY_W,
    CONF_TEMP_ENTITY_OVERRIDE,
    CONF_UA_LEARNING,
    CONF_UA_SEED_W_PER_C,
    CONF_WEATHER_ENTITY,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_BASE_TEMP,
    DEFAULT_BLEND_WINDOW_MIN,
    DEFAULT_CAPACITY_KW,
    DEFAULT_EFFICIENCY,
    DEFAULT_EVENT_DEBOUNCE_HOURS,
    DEFAULT_FIXED_COP,
    DEFAULT_STANDBY_W,
    DEFAULT_UA_LEARNING,
    DEFAULT_UA_SEED_W_PER_C,
    DEMAND_MODE_EXTERNAL,
    DEMAND_MODE_INTERNAL,
    DEMAND_MODES,
    DEMAND_UNIT_KW,
    DEMAND_UNITS,
    DOMAIN,
    FUEL_UNIT_M3,
    FUEL_UNITS,
    PRESET_CUSTOM,
    SOURCE_TYPE_CURVE,
    SOURCE_TYPE_DYNAMIC_FIXED,
    SOURCE_TYPE_STATIC,
    SOURCE_TYPES,
    SUBENTRY_TYPE_SOURCE,
)

DEFAULT_HUB_NAME = "Live Heating Costs"
DEFAULT_ENERGY_DENSITY_SUGGESTION = 1700

_BOX = selector.NumberSelectorMode.BOX
_DROPDOWN = selector.SelectSelectorMode.DROPDOWN


def _sensor_selector(**kwargs: Any) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", **kwargs))


def _clean(user_input: dict) -> dict:
    """Drop unset optionals so downstream defaults (models.py) apply cleanly."""
    return {k: v for k, v in user_input.items() if v not in (None, "")}


def _entity_with_real_state(hass: HomeAssistant, entity_id: str | None):
    """Return the ``State`` for ``entity_id`` if it has a real (non-unknown) value.

    ``None`` is returned both when the entity doesn't exist yet and when it's
    unknown/unavailable — both cases mean "can't validate yet, allow it" so a
    sensor that hasn't loaded doesn't block setup.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    return state


def _validate_buy_price(hass: HomeAssistant, entity_id: str | None) -> str | None:
    state = _entity_with_real_state(hass, entity_id)
    if state is None:
        return None
    try:
        float(state.state)
    except (TypeError, ValueError):
        return "buy_price_not_numeric"
    return None


def _validate_price_format(hass: HomeAssistant, entity_id: str | None) -> str | None:
    state = _entity_with_real_state(hass, entity_id)
    if state is None:
        return None
    try:
        price_source.detect_format(dict(state.attributes))
    except price_source.PriceFormatError:
        return "invalid_price_format"
    return None


def _validate_hub_fields(hass: HomeAssistant, user_input: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    buy_error = _validate_buy_price(hass, user_input.get(CONF_BUY_PRICE_ENTITY))
    if buy_error:
        errors[CONF_BUY_PRICE_ENTITY] = buy_error
    for key in (CONF_DAY_AHEAD_ENTITY, CONF_FORECAST_PRICE_ENTITY):
        error = _validate_price_format(hass, user_input.get(key))
        if error:
            errors[key] = error
    return errors


def _hub_schema(defaults: dict) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_HUB_NAME)): (
                selector.TextSelector()
            ),
            vol.Required(
                CONF_BUY_PRICE_ENTITY, description=suggest(CONF_BUY_PRICE_ENTITY)
            ): _sensor_selector(),
            vol.Optional(
                CONF_DAY_AHEAD_ENTITY, description=suggest(CONF_DAY_AHEAD_ENTITY)
            ): _sensor_selector(),
            vol.Optional(
                CONF_FORECAST_PRICE_ENTITY, description=suggest(CONF_FORECAST_PRICE_ENTITY)
            ): _sensor_selector(),
            vol.Optional(
                CONF_WEATHER_ENTITY, description=suggest(CONF_WEATHER_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            vol.Required(
                CONF_OUTDOOR_TEMP_ENTITY, description=suggest(CONF_OUTDOOR_TEMP_ENTITY)
            ): _sensor_selector(device_class="temperature"),
            vol.Optional(
                CONF_DEMAND_MODE, default=defaults.get(CONF_DEMAND_MODE, DEMAND_MODE_INTERNAL)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=DEMAND_MODES, translation_key="demand_mode", mode=_DROPDOWN
                )
            ),
        }
    )


def _demand_schema(mode: str, defaults: dict) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    if mode == DEMAND_MODE_EXTERNAL:
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEMAND_ENTITY, description=suggest(CONF_DEMAND_ENTITY)
                ): _sensor_selector(),
                vol.Optional(
                    CONF_DEMAND_UNIT, default=defaults.get(CONF_DEMAND_UNIT, DEMAND_UNIT_KW)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=DEMAND_UNITS, translation_key="demand_unit", mode=_DROPDOWN
                    )
                ),
            }
        )

    return vol.Schema(
        {
            vol.Optional(
                CONF_BASE_TEMP, default=defaults.get(CONF_BASE_TEMP, DEFAULT_BASE_TEMP)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5, max=25, step=0.5, unit_of_measurement="°C", mode=_BOX
                )
            ),
            vol.Optional(
                CONF_UA_SEED_W_PER_C,
                default=defaults.get(CONF_UA_SEED_W_PER_C, DEFAULT_UA_SEED_W_PER_C),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=20, max=1000, step=5, unit_of_measurement="W/°C", mode=_BOX
                )
            ),
            vol.Optional(
                CONF_UA_LEARNING, default=defaults.get(CONF_UA_LEARNING, DEFAULT_UA_LEARNING)
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_BACKFILL_DAYS,
                default=defaults.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=90, step=1, mode=_BOX)
            ),
        }
    )


class LiveHeatingCostsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the hub config flow."""

    VERSION = 1

    _hub_data: dict[str, Any]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the shared price/weather sensors and pick the demand model."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_hub_fields(self.hass, user_input)
            if not errors:
                self._hub_data = dict(user_input)
                return await self.async_step_demand()
        return self.async_show_form(
            step_id="user", data_schema=_hub_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the hub's shared sensors (demand step follows, same as add)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_hub_fields(self.hass, user_input)
            if not errors:
                self._hub_data = {**entry.data, **user_input}
                return await self.async_step_demand()
        defaults = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure", data_schema=_hub_schema(defaults), errors=errors
        )

    async def async_step_demand(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect demand-model fields, branching on the chosen mode."""
        mode = self._hub_data.get(CONF_DEMAND_MODE, DEMAND_MODE_INTERNAL)
        if user_input is not None:
            data = {**self._hub_data, **_clean(user_input)}
            if self.source == SOURCE_RECONFIGURE:
                entry = self._get_reconfigure_entry()
                return self.async_update_reload_and_abort(
                    entry, data=data, title=data.get(CONF_NAME, entry.title)
                )
            await self.async_set_unique_id(data[CONF_BUY_PRICE_ENTITY])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=data.get(CONF_NAME, DEFAULT_HUB_NAME), data=data)
        return self.async_show_form(
            step_id="demand", data_schema=_demand_schema(mode, self._hub_data), errors={}
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Heat sources are added as subentries of the hub."""
        return {SUBENTRY_TYPE_SOURCE: SourceSubentryFlowHandler}


# --------------------------------------------------------------------------- #
# Source subentry flow
# --------------------------------------------------------------------------- #

_STATIC_FIELDS = {
    CONF_NAME,
    CONF_FUEL_UNIT,
    CONF_PRICE_PER_UNIT,
    CONF_ENERGY_DENSITY,
    CONF_EFFICIENCY,
    CONF_CAPACITY_KW,
    CONF_ACTIVITY_MODE,
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVE_OUTPUT_KW,
    CONF_ENERGY_PER_EVENT_KWH,
    CONF_EVENT_DEBOUNCE_HOURS,
}
_DYNAMIC_FIXED_FIELDS = {CONF_NAME, CONF_COP, CONF_CAPACITY_KW, CONF_ENERGY_ENTITY}
_CURVE_TABLES_FIELDS = {
    CONF_PRESET,
    CONF_COP_POINTS,
    CONF_CAPACITY_POINTS,
    CONF_OPERATING_LIMIT_C,
    CONF_MIN_COP,
    CONF_MAX_COP,
}


def _init_schema(defaults: dict) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Required(CONF_NAME, description=suggest(CONF_NAME)): selector.TextSelector(),
            vol.Optional(
                CONF_SOURCE_TYPE, default=defaults.get(CONF_SOURCE_TYPE, SOURCE_TYPE_CURVE)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=SOURCE_TYPES, translation_key="source_type", mode=_DROPDOWN
                )
            ),
        }
    )


def _static_schema(defaults: dict, include_name: bool) -> vol.Schema:
    def suggest(key: str, fallback: Any = None) -> dict:
        return {"suggested_value": defaults.get(key, fallback)}

    schema: dict[Any, Any] = {}
    if include_name:
        schema[vol.Required(CONF_NAME, description=suggest(CONF_NAME))] = selector.TextSelector()
    schema.update(
        {
            vol.Optional(
                CONF_FUEL_UNIT, default=defaults.get(CONF_FUEL_UNIT, FUEL_UNIT_M3)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=FUEL_UNITS, translation_key="fuel_unit", mode=_DROPDOWN
                )
            ),
            vol.Required(
                CONF_PRICE_PER_UNIT, description=suggest(CONF_PRICE_PER_UNIT)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.01, step=0.01, unit_of_measurement="€", mode=_BOX
                )
            ),
            vol.Optional(
                CONF_ENERGY_DENSITY,
                description=suggest(CONF_ENERGY_DENSITY, DEFAULT_ENERGY_DENSITY_SUGGESTION),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.01, step=0.1, mode=_BOX)
            ),
            vol.Optional(
                CONF_EFFICIENCY, default=defaults.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.3, max=1.0, step=0.05, mode=_BOX)
            ),
            vol.Optional(
                CONF_CAPACITY_KW, default=defaults.get(CONF_CAPACITY_KW, DEFAULT_CAPACITY_KW)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=0.1, unit_of_measurement="kW", mode=_BOX
                )
            ),
            vol.Optional(
                CONF_ACTIVITY_MODE,
                default=defaults.get(CONF_ACTIVITY_MODE, ACTIVITY_MODE_NONE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=ACTIVITY_MODES, translation_key="activity_mode", mode=_DROPDOWN
                )
            ),
            vol.Optional(
                CONF_ACTIVITY_ENTITY, description=suggest(CONF_ACTIVITY_ENTITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "binary_sensor", "input_boolean"])
            ),
            vol.Optional(
                CONF_ACTIVE_OUTPUT_KW, description=suggest(CONF_ACTIVE_OUTPUT_KW)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(unit_of_measurement="kW", mode=_BOX)
            ),
            vol.Optional(
                CONF_ENERGY_PER_EVENT_KWH, description=suggest(CONF_ENERGY_PER_EVENT_KWH)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(unit_of_measurement="kWh", mode=_BOX)
            ),
            vol.Optional(
                CONF_EVENT_DEBOUNCE_HOURS,
                default=defaults.get(CONF_EVENT_DEBOUNCE_HOURS, DEFAULT_EVENT_DEBOUNCE_HOURS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(unit_of_measurement="h", mode=_BOX)
            ),
        }
    )
    return vol.Schema(schema)


def _dynamic_fixed_schema(defaults: dict, include_name: bool) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    schema: dict[Any, Any] = {}
    if include_name:
        schema[vol.Required(CONF_NAME, description=suggest(CONF_NAME))] = selector.TextSelector()
    schema.update(
        {
            vol.Optional(
                CONF_COP, default=defaults.get(CONF_COP, DEFAULT_FIXED_COP)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.5, max=10, step=0.05, mode=_BOX)
            ),
            vol.Optional(
                CONF_CAPACITY_KW, default=defaults.get(CONF_CAPACITY_KW, DEFAULT_CAPACITY_KW)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=0.1, unit_of_measurement="kW", mode=_BOX
                )
            ),
            vol.Optional(
                CONF_ENERGY_ENTITY, description=suggest(CONF_ENERGY_ENTITY)
            ): _sensor_selector(device_class="energy"),
        }
    )
    return vol.Schema(schema)


def _curve_entities_schema(defaults: dict, include_name: bool) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    schema: dict[Any, Any] = {}
    if include_name:
        schema[vol.Required(CONF_NAME, description=suggest(CONF_NAME))] = selector.TextSelector()
    schema.update(
        {
            vol.Required(
                CONF_POWER_ENTITY, description=suggest(CONF_POWER_ENTITY)
            ): _sensor_selector(device_class="power"),
            vol.Required(
                CONF_ENERGY_ENTITY, description=suggest(CONF_ENERGY_ENTITY)
            ): _sensor_selector(device_class="energy"),
            vol.Optional(
                CONF_CLIMATE_ENTITY, description=suggest(CONF_CLIMATE_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
            vol.Optional(
                CONF_PUMP_TEMP_ENTITY, description=suggest(CONF_PUMP_TEMP_ENTITY)
            ): _sensor_selector(),
            vol.Optional(
                CONF_TEMP_ENTITY_OVERRIDE, description=suggest(CONF_TEMP_ENTITY_OVERRIDE)
            ): _sensor_selector(),
            vol.Optional(
                CONF_STANDBY_W, default=defaults.get(CONF_STANDBY_W, DEFAULT_STANDBY_W)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(unit_of_measurement="W", mode=_BOX)
            ),
            vol.Optional(
                CONF_BLEND_WINDOW_MIN,
                default=defaults.get(CONF_BLEND_WINDOW_MIN, DEFAULT_BLEND_WINDOW_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=15, max=120, step=5, unit_of_measurement="min", mode=_BOX
                )
            ),
        }
    )
    return vol.Schema(schema)


def _curve_tables_schema(defaults: dict) -> vol.Schema:
    def suggest(key: str) -> dict:
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Optional(
                CONF_PRESET, default=defaults.get(CONF_PRESET, "msz_fd25va")
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["msz_fd25va", "msz_rw35vg", PRESET_CUSTOM],
                    translation_key="preset",
                    mode=_DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_COP_POINTS, description=suggest(CONF_COP_POINTS)
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_CAPACITY_POINTS, description=suggest(CONF_CAPACITY_POINTS)
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_OPERATING_LIMIT_C, description=suggest(CONF_OPERATING_LIMIT_C)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(unit_of_measurement="°C", mode=_BOX)
            ),
            vol.Optional(CONF_MIN_COP, description=suggest(CONF_MIN_COP)): selector.NumberSelector(
                selector.NumberSelectorConfig(mode=_BOX)
            ),
            vol.Optional(CONF_MAX_COP, description=suggest(CONF_MAX_COP)): selector.NumberSelector(
                selector.NumberSelectorConfig(mode=_BOX)
            ),
        }
    )


def _curve_table_prefill(preset_key: str, existing: dict) -> dict:
    """Suggested values for the ``curve_tables`` form for ``preset_key``.

    Named presets resolve to their canonical points/bounds. ``custom`` falls
    back to whatever points/bounds are already in ``existing`` (a reconfigure
    of a previously-custom source), or blank text for a brand new one.
    """
    preset = curves.PRESETS.get(preset_key)
    if preset is not None:
        return {
            CONF_PRESET: preset_key,
            CONF_COP_POINTS: curves.points_to_text(preset.cop_points),
            CONF_CAPACITY_POINTS: curves.points_to_text(preset.capacity_points),
            CONF_OPERATING_LIMIT_C: preset.operating_limit_c,
            CONF_MIN_COP: preset.min_cop,
            CONF_MAX_COP: preset.max_cop,
        }
    cop_points = existing.get(CONF_COP_POINTS)
    capacity_points = existing.get(CONF_CAPACITY_POINTS)
    return {
        CONF_PRESET: PRESET_CUSTOM,
        CONF_COP_POINTS: curves.points_to_text(cop_points) if cop_points else "",
        CONF_CAPACITY_POINTS: curves.points_to_text(capacity_points) if capacity_points else "",
        CONF_OPERATING_LIMIT_C: existing.get(CONF_OPERATING_LIMIT_C),
        CONF_MIN_COP: existing.get(CONF_MIN_COP),
        CONF_MAX_COP: existing.get(CONF_MAX_COP),
    }


class SourceSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure a heat source.

    ``init`` (name + type) is only shown when adding — reconfigure jumps
    straight to the type-specific step(s) since the type can't change, and
    carries ``name`` into that step instead so it stays editable.
    """

    _defaults: dict[str, Any]
    _curve_prefill_preset: str
    _curve_prefill: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        return self.source == SOURCE_USER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        self._defaults = {}
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._defaults = dict(self._get_reconfigure_subentry().data)
        return await self._route_to_type_step(self._defaults.get(CONF_SOURCE_TYPE), None)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            self._defaults = {**self._defaults, **user_input}
            return await self._route_to_type_step(user_input[CONF_SOURCE_TYPE], None)
        return self.async_show_form(step_id="init", data_schema=_init_schema(self._defaults))

    async def _route_to_type_step(
        self, source_type: str | None, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        if source_type == SOURCE_TYPE_STATIC:
            return await self.async_step_static(user_input)
        if source_type == SOURCE_TYPE_DYNAMIC_FIXED:
            return await self.async_step_dynamic_fixed(user_input)
        if source_type == SOURCE_TYPE_CURVE:
            return await self.async_step_curve_entities(user_input)
        raise ValueError(f"unknown source_type: {source_type!r}")

    def _finish(self, data: dict[str, Any]) -> SubentryFlowResult:
        name = data.get(CONF_NAME) or "Heat source"
        if self._is_new:
            return self.async_create_entry(title=name, data=data)
        return self.async_update_and_abort(
            self._get_entry(), self._get_reconfigure_subentry(), title=name, data=data
        )

    @staticmethod
    def _map_config_error(
        err: models.ConfigError, step_fields: set[str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        field = err.field if err.field in step_fields else "base"
        return {field: "invalid_config"}, {"error": str(err)}

    # ------------------------------------------------------------------ #
    # static (fixed-cost fuel) sources
    # ------------------------------------------------------------------ #

    async def async_step_static(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        include_name = not self._is_new
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None
        data = self._defaults
        if user_input is not None:
            data = {**self._defaults, **_clean(user_input)}
            try:
                models.source_from_subentry(data)
            except models.ConfigError as err:
                errors, placeholders = self._map_config_error(err, _STATIC_FIELDS)
            else:
                return self._finish(data)
        return self.async_show_form(
            step_id="static",
            data_schema=_static_schema(data, include_name),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------ #
    # dynamic_fixed (electric, fixed COP) sources
    # ------------------------------------------------------------------ #

    async def async_step_dynamic_fixed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        include_name = not self._is_new
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None
        data = self._defaults
        if user_input is not None:
            data = {**self._defaults, **_clean(user_input)}
            try:
                models.source_from_subentry(data)
            except models.ConfigError as err:
                errors, placeholders = self._map_config_error(err, _DYNAMIC_FIXED_FIELDS)
            else:
                return self._finish(data)
        return self.async_show_form(
            step_id="dynamic_fixed",
            data_schema=_dynamic_fixed_schema(data, include_name),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------ #
    # curve (heat pump) sources — two steps: entities, then COP/capacity tables
    # ------------------------------------------------------------------ #

    async def async_step_curve_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        include_name = not self._is_new
        if user_input is not None:
            self._defaults = {**self._defaults, **_clean(user_input)}
            return await self.async_step_curve_tables()
        return self.async_show_form(
            step_id="curve_entities",
            data_schema=_curve_entities_schema(self._defaults, include_name),
            errors={},
        )

    async def async_step_curve_tables(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is None:
            preset = self._defaults.get(CONF_PRESET, "msz_fd25va")
            prefill = _curve_table_prefill(preset, self._defaults)
            self._curve_prefill_preset = prefill[CONF_PRESET]
            self._curve_prefill = prefill
            return self.async_show_form(
                step_id="curve_tables", data_schema=_curve_tables_schema(prefill), errors={}
            )

        submitted_preset = user_input.get(CONF_PRESET, PRESET_CUSTOM)
        if submitted_preset != self._curve_prefill_preset:
            # The dropdown changed since the last render: refresh the prefill
            # from the newly-chosen preset instead of finishing — any text the
            # user had (still reflecting the old preset) is stale.
            new_prefill = _curve_table_prefill(submitted_preset, self._defaults)
            self._curve_prefill_preset = new_prefill[CONF_PRESET]
            self._curve_prefill = new_prefill
            return self.async_show_form(
                step_id="curve_tables", data_schema=_curve_tables_schema(new_prefill), errors={}
            )

        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None

        if submitted_preset != PRESET_CUSTOM:
            preset_obj = curves.PRESETS[submitted_preset]
            cop_points = [list(p) for p in preset_obj.cop_points]
            capacity_points = [list(p) for p in preset_obj.capacity_points]
            operating_limit_c = preset_obj.operating_limit_c
            min_cop = preset_obj.min_cop
            max_cop = preset_obj.max_cop
        else:
            cop_points = capacity_points = None
            try:
                cop_points = [
                    list(p) for p in curves.parse_curve_text(user_input.get(CONF_COP_POINTS, ""))
                ]
            except curves.CurveParseError:
                errors[CONF_COP_POINTS] = "invalid_curve_format"
            try:
                capacity_points = [
                    list(p)
                    for p in curves.parse_curve_text(user_input.get(CONF_CAPACITY_POINTS, ""))
                ]
            except curves.CurveParseError:
                errors[CONF_CAPACITY_POINTS] = "invalid_curve_format"

            if not errors:
                for field, points in (
                    (CONF_COP_POINTS, cop_points),
                    (CONF_CAPACITY_POINTS, capacity_points),
                ):
                    problems = curves.validate_points(points)
                    if problems:
                        errors[field] = "invalid_curve_points"

            operating_limit_c = user_input.get(CONF_OPERATING_LIMIT_C)
            min_cop = user_input.get(CONF_MIN_COP)
            max_cop = user_input.get(CONF_MAX_COP)

        if errors:
            self._curve_prefill = dict(user_input)
            return self.async_show_form(
                step_id="curve_tables",
                data_schema=_curve_tables_schema(user_input),
                errors=errors,
            )

        data = {
            **self._defaults,
            CONF_PRESET: submitted_preset,
            CONF_COP_POINTS: cop_points,
            CONF_CAPACITY_POINTS: capacity_points,
            CONF_OPERATING_LIMIT_C: operating_limit_c,
            CONF_MIN_COP: min_cop,
            CONF_MAX_COP: max_cop,
        }
        try:
            models.source_from_subentry(data)
        except models.ConfigError as err:
            errors, placeholders = self._map_config_error(err, _CURVE_TABLES_FIELDS)
            return self.async_show_form(
                step_id="curve_tables",
                data_schema=_curve_tables_schema(data),
                errors=errors,
                description_placeholders=placeholders,
            )
        return self._finish(data)
