"""Heat-demand modeling: degree-hours and UA-based energy/power.

Replaces the legacy ``heating_energy_needed`` template, which multiplied an
*instantaneous* temperature deficit by a hand-tuned constant instead of
integrating over time. Here, demand is ``E = UA * degree_hours`` where
``degree_hours`` is a proper time integral of ``max(base_temp - T, 0)`` and
``UA`` (kW/°C) is the house's heat-loss coefficient, supplied by the caller
(either seeded or learned elsewhere).

No Home Assistant imports; all datetimes must be timezone-aware. Duration math
is done directly on ``timedelta``s so DST transitions are a non-issue (a
23h/25h day integrates its true real-time length).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None:
        raise ValueError(f"datetime is not timezone-aware: {dt!r}")


def _interp(ordered: Sequence[tuple[datetime, float]], t: datetime) -> float:
    """Linearly interpolate temperature at ``t``; clamps outside the samples' span."""
    if t <= ordered[0][0]:
        return ordered[0][1]
    if t >= ordered[-1][0]:
        return ordered[-1][1]
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:], strict=False):
        if t0 <= t <= t1:
            if t1 == t0:
                return v0
            frac = (t - t0).total_seconds() / (t1 - t0).total_seconds()
            return v0 + frac * (v1 - v0)
    return ordered[-1][1]


def _segment_area(d0: float, d1: float, dt_h: float) -> float:
    """°C·h under ``max(deficit, 0)`` over a segment, deficit linear in time.

    ``d0``/``d1`` are the (signed) deficits at the segment's start/end; a sign
    change mid-segment is solved for exactly and only the positive triangle is
    counted.
    """
    if d0 >= 0 and d1 >= 0:
        return (d0 + d1) / 2.0 * dt_h
    if d0 <= 0 and d1 <= 0:
        return 0.0
    # Sign change: solve for the zero crossing as a fraction of the segment.
    frac = d0 / (d0 - d1)
    if d0 > 0:
        return 0.5 * d0 * frac * dt_h
    return 0.5 * d1 * (1.0 - frac) * dt_h


def integrate_degree_hours(
    samples: Sequence[tuple[datetime, float]],
    base_temp: float,
    start: datetime,
    end: datetime,
) -> float:
    """°C·h of ``max(base_temp - T(t), 0)`` over ``[start, end]``.

    Trapezoidal integration between samples; samples outside the window are
    used only to interpolate the boundary values at ``start``/``end``, then
    clipped. Fewer than 2 input samples (or a degenerate/empty window) yields
    0.0.
    """
    _require_aware(start)
    _require_aware(end)
    if len(samples) < 2 or end <= start:
        return 0.0

    ordered = sorted(samples, key=lambda s: s[0])
    for ts, _ in ordered:
        _require_aware(ts)

    points: list[tuple[datetime, float]] = [(t, v) for t, v in ordered if start <= t <= end]
    if not points or points[0][0] > start:
        points.insert(0, (start, _interp(ordered, start)))
    if points[-1][0] < end:
        points.append((end, _interp(ordered, end)))

    if len(points) < 2:
        return 0.0

    total = 0.0
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        dt_h = (t1 - t0).total_seconds() / 3600.0
        if dt_h <= 0:
            continue
        total += _segment_area(base_temp - v0, base_temp - v1, dt_h)
    return total


def instant_demand_kw(ua_kw_per_c: float, temp_c: float, base_temp: float) -> float:
    """Instantaneous heat demand, kW: ``UA * max(base_temp - T, 0)``."""
    return ua_kw_per_c * max(base_temp - temp_c, 0.0)


def daily_energy_kwh(ua_kw_per_c: float, degree_hours: float) -> float:
    """Delivered-heat energy, kWh, implied by degree-hours: ``UA * DH``."""
    return ua_kw_per_c * degree_hours
