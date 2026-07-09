"""Project each heat source's delivered-heat cost 24-48 h ahead.

Combines a normalised day-ahead price series (:mod:`price_source`), an
optional beyond-horizon "estimated" price series, and an hourly weather
forecast into a per-source, per-slot cost. Emits the result in the same
``{start, end, buy}`` slot shape :mod:`price_source` itself parses, so a Load
Scheduler-style consumer can read this integration's forecast the same way it
reads a price entity's.

Pure module — no Home Assistant imports, no ``datetime.now()``. All datetimes
are timezone-aware; naive input raises ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .price_source import ForecastSlot


@dataclass(frozen=True)
class TempPoint:
    """One hourly weather-forecast point, UTC."""

    ts: datetime
    temp_c: float


@dataclass(frozen=True)
class SourceSlotCost:
    """One source's projected delivered-heat cost for one price slot.

    ``buy`` is EUR/kWh of *delivered heat* for this source in this slot, or
    ``None`` when it can't be priced (no temperature forecast that far out
    for a curve source, or below the source's operating limit). ``estimated``
    is ``True`` when the slot's price came from the beyond-horizon estimated
    series rather than the real day-ahead one.
    """

    start: datetime
    end: datetime
    buy: float | None
    cop: float | None
    capacity_kw: float
    estimated: bool


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{label} is not timezone-aware: {value!r}")


def interp_temp(points: Sequence[TempPoint], t: datetime) -> float | None:
    """Linearly interpolate the forecast temperature at ``t``.

    Only interpolates *inside* the points' covered time range; outside it
    (before the first point or after the last) returns ``None`` — no
    extrapolation, since a modeled COP/capacity that far beyond the weather
    forecast's horizon would be fabricated. Points are sorted defensively.
    """
    if not points:
        return None
    pts = sorted(points, key=lambda p: p.ts)
    if t < pts[0].ts or t > pts[-1].ts:
        return None
    for a, b in zip(pts, pts[1:], strict=False):
        if a.ts <= t <= b.ts:
            if b.ts == a.ts:
                return a.temp_c
            frac = (t - a.ts).total_seconds() / (b.ts - a.ts).total_seconds()
            return a.temp_c + frac * (b.temp_c - a.temp_c)
    return pts[-1].temp_c  # t exactly equals the last point's timestamp


def _window_filter(
    slots: Sequence[ForecastSlot], now: datetime, horizon_end: datetime
) -> list[ForecastSlot]:
    return [s for s in slots if s.end > now and s.start < horizon_end]


def build_source_forecast(
    price_slots: Sequence[ForecastSlot],
    est_price_slots: Sequence[ForecastSlot],
    temps: Sequence[TempPoint],
    now: datetime,
    horizon_h: int,
    *,
    cop_at: Callable[[float], float] | None,
    capacity_at: Callable[[float], float] | None,
    fixed_cop: float | None,
    fixed_capacity_kw: float | None,
    operating_limit_c: float | None,
) -> list[SourceSlotCost]:
    """Build one source's per-slot cost forecast over ``[now, now + horizon_h)``.

    Two source shapes are supported (mirroring ``SOURCE_TYPE_CURVE`` /
    ``SOURCE_TYPE_DYNAMIC_FIXED`` in ``const.py``):

    * **curve** (``cop_at`` given): COP/capacity depend on the slot's
      interpolated temperature. A slot whose midpoint temperature can't be
      interpolated (beyond the weather forecast's horizon) is *skipped
      entirely* — there's nothing honest to say about it. A slot at or below
      ``operating_limit_c`` is *kept* but priced as unavailable
      (``buy=None``, ``cop=None``, ``capacity_kw=0.0``).
    * **fixed** (``fixed_cop`` given): constant COP/capacity, temperature
      irrelevant — every slot in the window is priced, never skipped.

    ``est_price_slots`` extends the real ``price_slots`` series strictly
    *after* the last real slot's end (real slots always win on overlap), so
    a downstream card can show a rougher-but-present forecast beyond the
    day-ahead horizon.
    """
    _require_aware(now, "now")
    horizon_end = now + timedelta(hours=horizon_h)

    last_real_end = max((s.end for s in price_slots), default=None)
    real = _window_filter(price_slots, now, horizon_end)
    est_candidates = (
        est_price_slots
        if last_real_end is None
        else [s for s in est_price_slots if s.start >= last_real_end]
    )
    est = _window_filter(est_candidates, now, horizon_end)

    tagged: list[tuple[ForecastSlot, bool]] = [(s, False) for s in real] + [(s, True) for s in est]
    tagged.sort(key=lambda pair: pair[0].start)

    if cop_at is None and fixed_cop is None:
        raise ValueError("build_source_forecast requires either cop_at or fixed_cop")

    out: list[SourceSlotCost] = []
    for slot, estimated in tagged:
        midpoint = slot.start + (slot.end - slot.start) / 2
        if cop_at is not None:
            temp = interp_temp(temps, midpoint)
            if temp is None:
                continue  # no weather forecast that far out for a curve source
            if operating_limit_c is not None and temp < operating_limit_c:
                out.append(
                    SourceSlotCost(
                        start=slot.start,
                        end=slot.end,
                        buy=None,
                        cop=None,
                        capacity_kw=0.0,
                        estimated=estimated,
                    )
                )
                continue
            cop = cop_at(temp)
            capacity_kw = capacity_at(temp) if capacity_at is not None else 0.0
            buy = slot.buy / cop if cop else None
        else:
            cop = fixed_cop
            capacity_kw = fixed_capacity_kw if fixed_capacity_kw is not None else 0.0
            buy = slot.buy / cop if cop else None
        out.append(
            SourceSlotCost(
                start=slot.start,
                end=slot.end,
                buy=buy,
                cop=cop,
                capacity_kw=capacity_kw,
                estimated=estimated,
            )
        )

    out.sort(key=lambda s: s.start)
    return out


def forecast_attr_list(slots: Sequence[SourceSlotCost]) -> list[dict]:
    """Render priced slots in the ``{start, end, buy}`` shape ``price_source`` parses.

    Slots with unknown ``buy`` (unpriceable) are dropped — a downstream
    consumer parsing this via :func:`price_source.normalize` must never see
    an unknown cost pretending to be a number.
    """
    return [
        {
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "buy": s.buy,
            "estimated": s.estimated,
        }
        for s in slots
        if s.buy is not None
    ]


def combined_slots(per_source: Mapping[str, Sequence[SourceSlotCost]]) -> list[dict]:
    """Union per-source forecasts by exact ``(start, end)`` boundary.

    Source slot grids are aligned (15-min, all derived from the same price
    series), so matching on exact start/end is sufficient — no interval-tree
    needed. Per boundary: ``costs`` maps every source that has a slot there to
    its cost (``None`` when unpriceable); ``cheapest`` is the lowest-cost
    source among those with a known cost *and* nonzero capacity (a source
    that can't currently deliver any heat is never "cheapest" even if its
    theoretical cost is lowest); ``estimated`` is true if any contributing
    slot came from the estimated series.
    """
    by_key: dict[tuple[datetime, datetime], dict[str, SourceSlotCost]] = {}
    for slug, slots in per_source.items():
        for s in slots:
            by_key.setdefault((s.start, s.end), {})[slug] = s

    result: list[dict] = []
    for (start, end), sources in sorted(by_key.items(), key=lambda kv: kv[0][0]):
        costs = {slug: s.buy for slug, s in sources.items()}
        cheapest: str | None = None
        best: float | None = None
        for slug, s in sources.items():
            if s.buy is None or s.capacity_kw <= 0:
                continue
            if best is None or s.buy < best:
                best = s.buy
                cheapest = slug
        result.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "costs": costs,
                "cheapest": cheapest,
                "estimated": any(s.estimated for s in sources.values()),
            }
        )
    return result
