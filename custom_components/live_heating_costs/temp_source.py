"""Resolve "the outdoor temperature right now" from a chain of candidates.

Every curve source's COP and capacity are looked up from an outdoor
temperature, so a single dead sensor used to blank out the whole integration:
the curve sources reported ``temp_unavailable`` and dropped out of the
ranking, the degree-hour accumulator stopped counting, and
``forecast.anchor_temps`` lost the reading it prepends to the weather series.
A RuuviTag with a flat battery is enough to do all three at once.

The hub already knows a *second* live source of the same quantity — the
configured ``weather`` entity, whose current conditions carry a temperature —
so this module makes the fallback order explicit and testable:

    pump-reported -> per-source override -> hub sensor -> weather entity

A fallback is only ever another *real* measurement of the same quantity, and
the winning link is reported back as :attr:`TempReading.source` so the sensor
attributes (and diagnostics) say which one was used. Nothing here invents a
number: when every candidate is unusable the answer stays ``None`` and the
source is still excluded, exactly as before.

Degrading to the weather entity also protects UA learning. A partial outage —
sensor down for half a day — is worse than a total one: the day would keep
its full ``delivered_kwh`` but only count half its degree-hours, teaching the
learner a UA roughly twice the truth. A slightly-off temperature for those
hours is a far smaller error than a missing one.

Pure module: no Home Assistant imports, so the chain order and the weather
attribute parsing are unit-testable without a ``hass`` fixture.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Chain labels, in preference order. Published as the ``temp_source``
# attribute on each curve source's COP sensor.
TEMP_SOURCE_PUMP = "pump"
TEMP_SOURCE_OVERRIDE = "override"
TEMP_SOURCE_HUB = "hub"
TEMP_SOURCE_WEATHER = "weather"

# Sanity band for any accepted reading (°C). Wide enough to cover the whole
# inhabited planet; narrow enough to reject a sentinel like -999 or an
# unconverted Kelvin value leaking in from a misconfigured entity.
PLAUSIBLE_TEMP_RANGE_C = (-90.0, 60.0)

# Weather-entity attribute names (HA's standard weather entity contract).
_ATTR_TEMPERATURE = "temperature"
_ATTR_TEMPERATURE_UNIT = "temperature_unit"

_FAHRENHEIT_UNITS = {"°F", "F"}
_KELVIN_UNITS = {"K"}


def to_celsius(value: float | None, unit: object) -> float | None:
    """Convert a reading in ``unit`` (an entity's unit string) to °C.

    Sensors and weather forecasts report in the instance's configured unit,
    so on a °F instance ``23`` means -5 °C — and 23 °C is perfectly plausible,
    so the sanity band alone would never catch it. Unknown or missing units
    are taken as °C (the unit-less sensors this used to accept unchanged).
    """
    if value is None:
        return None
    if isinstance(unit, str):
        u = unit.strip()
        if u in _FAHRENHEIT_UNITS:
            return (value - 32.0) * 5.0 / 9.0
        if u in _KELVIN_UNITS:
            return value - 273.15
    return value


@dataclass(frozen=True)
class TempReading:
    """One accepted temperature and which link of the chain produced it."""

    temp_c: float
    source: str


def is_plausible(value: float | None) -> bool:
    """True when ``value`` is a finite temperature inside the sanity band."""
    if value is None or not math.isfinite(value):
        return False
    low, high = PLAUSIBLE_TEMP_RANGE_C
    return low <= value <= high


def weather_temp_c(attributes: Mapping[str, object] | None) -> float | None:
    """Current temperature from a weather entity's attributes, in °C.

    HA weather entities report in the instance's configured unit and name it
    in ``temperature_unit``, so a °F instance would otherwise feed Fahrenheit
    straight into a COP curve calibrated in °C. ``None`` for a missing,
    non-numeric or implausible reading.
    """
    if not attributes:
        return None
    raw = attributes.get(_ATTR_TEMPERATURE)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    value = to_celsius(value, attributes.get(_ATTR_TEMPERATURE_UNIT))
    return value if is_plausible(value) else None


def resolve(candidates: Iterable[tuple[str, float | None]]) -> TempReading | None:
    """First plausible reading in the chain, or ``None`` when all are unusable.

    ``candidates`` is an ordered ``(source_label, value)`` sequence; the caller
    builds it so the chain order lives at exactly one call site.
    """
    for source, value in candidates:
        if is_plausible(value):
            return TempReading(temp_c=float(value), source=source)  # type: ignore[arg-type]
    return None
