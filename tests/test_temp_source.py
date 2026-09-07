"""Unit tests for the outdoor-temperature resolution chain (pure, no HA)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_TS_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "live_heating_costs"
    / "temp_source.py"
)
_spec = importlib.util.spec_from_file_location("ls_temp_source", _TS_PATH)
ts = importlib.util.module_from_spec(_spec)
sys.modules["ls_temp_source"] = ts
_spec.loader.exec_module(ts)


# --------------------------------------------------------------------------- #
# is_plausible
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [-89.9, -30.0, 0.0, 17.3, 59.9])
def test_plausible_accepts_real_temperatures(value: float) -> None:
    assert ts.is_plausible(value) is True


@pytest.mark.parametrize(
    "value",
    [None, -999.0, -90.1, 60.1, 273.15 + 20, float("nan"), float("inf")],
)
def test_plausible_rejects_sentinels_and_nonfinite(value: float | None) -> None:
    """-999 sentinels and an unconverted Kelvin value must not reach a curve."""
    assert ts.is_plausible(value) is False


# --------------------------------------------------------------------------- #
# weather_temp_c
# --------------------------------------------------------------------------- #


def test_weather_temp_celsius_passthrough() -> None:
    assert ts.weather_temp_c({"temperature": 17.3, "temperature_unit": "°C"}) == pytest.approx(17.3)


def test_weather_temp_converts_fahrenheit() -> None:
    """A °F instance would otherwise feed Fahrenheit into a °C COP curve."""
    got = ts.weather_temp_c({"temperature": 14.0, "temperature_unit": "°F"})
    assert got == pytest.approx(-10.0)


def test_weather_temp_accepts_numeric_string() -> None:
    assert ts.weather_temp_c({"temperature": "-4.5"}) == pytest.approx(-4.5)


@pytest.mark.parametrize(
    "attributes",
    [
        None,
        {},
        {"temperature": None},
        {"temperature": "unknown"},
        {"temperature": True},
        {"temperature": -999},
        {"humidity": 80},
    ],
)
def test_weather_temp_rejects_unusable_attributes(attributes: dict | None) -> None:
    assert ts.weather_temp_c(attributes) is None


def test_weather_temp_missing_unit_assumes_celsius() -> None:
    assert ts.weather_temp_c({"temperature": 3.0}) == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #


def test_resolve_prefers_the_first_usable_link() -> None:
    reading = ts.resolve(
        [
            (ts.TEMP_SOURCE_PUMP, -6.0),
            (ts.TEMP_SOURCE_HUB, -5.0),
            (ts.TEMP_SOURCE_WEATHER, -4.0),
        ]
    )
    assert reading == ts.TempReading(temp_c=-6.0, source="pump")


def test_resolve_falls_through_to_weather_when_sensors_are_down() -> None:
    """The whole point: a dead outdoor sensor degrades, it does not blank out."""
    reading = ts.resolve(
        [
            (ts.TEMP_SOURCE_PUMP, None),
            (ts.TEMP_SOURCE_OVERRIDE, None),
            (ts.TEMP_SOURCE_HUB, None),
            (ts.TEMP_SOURCE_WEATHER, 17.3),
        ]
    )
    assert reading is not None
    assert reading.source == ts.TEMP_SOURCE_WEATHER
    assert reading.temp_c == pytest.approx(17.3)


def test_resolve_skips_an_implausible_reading_and_keeps_going() -> None:
    reading = ts.resolve([(ts.TEMP_SOURCE_HUB, -999.0), (ts.TEMP_SOURCE_WEATHER, 2.0)])
    assert reading is not None
    assert reading.source == ts.TEMP_SOURCE_WEATHER


def test_resolve_returns_none_when_every_link_is_unusable() -> None:
    """No invented fallback: unknown stays unknown, so the source is excluded."""
    assert ts.resolve([(ts.TEMP_SOURCE_HUB, None), (ts.TEMP_SOURCE_WEATHER, None)]) is None


def test_resolve_on_an_empty_chain_is_none() -> None:
    assert ts.resolve([]) is None
