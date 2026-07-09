"""Constants for the Live Heating Costs integration.

Pure module — no Home Assistant imports (config-flow keys and defaults are
shared with the HA-free engine modules and their bare-pytest tests).

Unit conventions (enforced across the integration):

* Money is **EUR/kWh everywhere internally**; the single conversion to the
  display unit (c/kWh) happens in ``sensor.py``.
* Power is kW, energy kWh, temperature °C.
* COP is dimensionless. An unknown COP propagates to an **unknown** cost —
  there is no silent COP=1 fallback anywhere.
"""

from __future__ import annotations

DOMAIN = "live_heating_costs"

PLATFORMS = ["binary_sensor", "number", "sensor"]

# --------------------------------------------------------------------------- #
# Subentry
# --------------------------------------------------------------------------- #

SUBENTRY_TYPE_SOURCE = "source"

# Source types
SOURCE_TYPE_CURVE = "curve"  # heat pump: COP(T)/capacity(T) lookup tables
SOURCE_TYPE_DYNAMIC_FIXED = "dynamic_fixed"  # electric with fixed COP (resistive = 1.0)
SOURCE_TYPE_STATIC = "static"  # fixed fuel-derived cost (wood)
SOURCE_TYPES = [SOURCE_TYPE_CURVE, SOURCE_TYPE_DYNAMIC_FIXED, SOURCE_TYPE_STATIC]

# Wood/static activity detection modes
ACTIVITY_MODE_NONE = "none"
ACTIVITY_MODE_DURATION = "duration"  # integrate active_output_kw while entity is on
ACTIVITY_MODE_EVENT = "event"  # fixed kWh per off->on transition (debounced)
ACTIVITY_MODES = [ACTIVITY_MODE_NONE, ACTIVITY_MODE_DURATION, ACTIVITY_MODE_EVENT]

# --------------------------------------------------------------------------- #
# Config keys — hub entry
# --------------------------------------------------------------------------- #

CONF_NAME = "name"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"  # live all-in buy price, EUR/kWh
CONF_DAY_AHEAD_ENTITY = "day_ahead_entity"  # slot attrs parseable by price_source
CONF_FORECAST_PRICE_ENTITY = "forecast_price_entity"  # optional beyond-horizon estimate
CONF_WEATHER_ENTITY = "weather_entity"
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"

CONF_DEMAND_MODE = "demand_mode"
DEMAND_MODE_INTERNAL = "internal_model"
DEMAND_MODE_EXTERNAL = "external_entity"
DEMAND_MODES = [DEMAND_MODE_INTERNAL, DEMAND_MODE_EXTERNAL]

CONF_DEMAND_ENTITY = "demand_entity"
CONF_DEMAND_UNIT = "demand_unit"
DEMAND_UNIT_KW = "kw"
DEMAND_UNIT_KWH_PER_DAY = "kwh_per_day"
DEMAND_UNITS = [DEMAND_UNIT_KW, DEMAND_UNIT_KWH_PER_DAY]

CONF_BASE_TEMP = "base_temp"
CONF_UA_SEED_W_PER_C = "ua_seed_w_per_c"  # W/°C in config; engine works in kW/°C
CONF_UA_LEARNING = "ua_learning"
CONF_BACKFILL_DAYS = "backfill_days"

DEFAULT_BASE_TEMP = 17.0
DEFAULT_UA_SEED_W_PER_C = 140.0  # reproduces the legacy 140/1000 heat-loss constant
DEFAULT_UA_LEARNING = True
DEFAULT_BACKFILL_DAYS = 60

# --------------------------------------------------------------------------- #
# Config keys — source subentry (shared)
# --------------------------------------------------------------------------- #

CONF_SOURCE_TYPE = "source_type"
CONF_CAPACITY_KW = "capacity_kw"
DEFAULT_CAPACITY_KW = 10.0

# static (fuel) sources
CONF_FUEL_UNIT = "fuel_unit"
FUEL_UNIT_M3 = "m3"
FUEL_UNIT_KG = "kg"
FUEL_UNIT_KWH = "kwh"
FUEL_UNITS = [FUEL_UNIT_M3, FUEL_UNIT_KG, FUEL_UNIT_KWH]
CONF_PRICE_PER_UNIT = "price_per_unit_eur"
CONF_ENERGY_DENSITY = "energy_density_kwh_per_unit"
CONF_EFFICIENCY = "efficiency"
DEFAULT_ENERGY_DENSITY_M3 = 1700.0  # stacked birch, kWh/m³
DEFAULT_ENERGY_DENSITY_KG = 4.1  # birch, kWh/kg
DEFAULT_EFFICIENCY = 0.75

CONF_ACTIVITY_MODE = "activity_mode"
CONF_ACTIVITY_ENTITY = "activity_entity"
CONF_ACTIVE_OUTPUT_KW = "active_output_kw"
CONF_ENERGY_PER_EVENT_KWH = "energy_per_event_kwh"
CONF_EVENT_DEBOUNCE_HOURS = "event_debounce_hours"
DEFAULT_EVENT_DEBOUNCE_HOURS = 4.0

# dynamic_fixed sources
CONF_COP = "cop"
DEFAULT_FIXED_COP = 1.0

# curve (heat pump) sources
CONF_POWER_ENTITY = "power_entity"  # live electrical power, W
CONF_ENERGY_ENTITY = "energy_entity"  # cumulative energy counter, kWh (total_increasing)
CONF_CLIMATE_ENTITY = "climate_entity"  # optional; hvac_action gates "heating"
CONF_PUMP_TEMP_ENTITY = "pump_temp_entity"  # optional pump-reported outdoor temp
CONF_TEMP_ENTITY_OVERRIDE = "temp_entity_override"  # optional per-source override
CONF_STANDBY_W = "standby_w"
CONF_BLEND_WINDOW_MIN = "blend_window_min"
CONF_PRESET = "preset"
CONF_COP_POINTS = "cop_points"  # resolved [[t, cop], ...] stored in subentry data
CONF_CAPACITY_POINTS = "capacity_points"  # resolved [[t, kw], ...]
CONF_OPERATING_LIMIT_C = "operating_limit_c"
CONF_MIN_COP = "min_cop"
CONF_MAX_COP = "max_cop"
DEFAULT_STANDBY_W = 50.0
DEFAULT_BLEND_WINDOW_MIN = 30
PRESET_CUSTOM = "custom"

# --------------------------------------------------------------------------- #
# Engine / coordinator tuning
# --------------------------------------------------------------------------- #

SAMPLE_INTERVAL_S = 60  # coordinator sampling tick
TEMP_MEAN_WINDOW_H = 24  # rolling-mean window for the demand temperature
MIN_WINDOW_ENERGY_KWH = 0.02  # below this counter delta a window COP is meaningless
FORECAST_HORIZON_H = 48
FORECAST_REBUILD_MIN = 15
WEATHER_CACHE_MIN = 30
DAILY_CAPTURE_HOUR = 0
DAILY_CAPTURE_MINUTE = 5

# UA learner
UA_EWMA_ALPHA_LIVE = 0.15
UA_EWMA_ALPHA_BACKFILL = 0.05
UA_LEARNED_CLAMP = (0.3, 3.0)  # × seed
UA_SEED_BLEND_SAMPLES = 14  # learned weight = min(n, 14) / 14
UA_MIN_DEGREE_HOURS = 24.0  # quality gate: below this a day teaches nothing
UA_DELIVERED_RANGE_KWH = (0.5, 200.0)  # quality gate on daily delivered heat
UA_ROWS_CAP = 120

# Snapshot unavailability reasons (sensor attributes + card display)
REASON_BELOW_OPERATING_LIMIT = "below_operating_limit"
REASON_PRICE_UNAVAILABLE = "price_unavailable"
REASON_TEMP_UNAVAILABLE = "temp_unavailable"

# Storage
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = f"{DOMAIN}.{{entry_id}}"
SAVE_DELAY_S = 10

# Services
SERVICE_LOG_WOOD = "log_wood"
ATTR_ENERGY_KWH = "energy_kwh"
ATTR_UNITS = "units"
ATTR_SOURCE = "source"
