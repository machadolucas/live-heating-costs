"""Normalise heterogeneous price-forecast entities into a common slot list.

Different price integrations expose their forecast completely differently
(Nord Pool, ENTSO-e, the user's ``nordpool_fi_day_ahead`` template, …). This
module auto-detects the shape and returns a uniform list of :class:`ForecastSlot`
(tz-aware ``start``/``end`` + ``buy`` and optional ``sell``), which the
coordinator then turns into :class:`engine.Slot` (adding solar excess).

Design choice mirroring the engine: the parsing core is **pure** and operates on
a plain attributes ``dict`` (no Home Assistant import), so it is unit-testable in
isolation. The thin ``slots_from_state`` wrapper adapts an HA ``State``.

Supported attribute layouts (auto-detected, in order):

* combined buy+sell items under ``data_today`` / ``data_tomorrow``
  (the user's day-ahead template: ``{start, end, buy, sell}``);
* split today/tomorrow lists: ``raw_today``/``raw_tomorrow`` (Nord Pool),
  ``prices_today``/``prices_tomorrow``, ``today_interval_prices``/…;
* a single list under ``prices`` / ``data`` / ``forecast`` (ENTSO-e etc.).

Per-item keys are detected from a small candidate set; ``start`` may be an ISO
string or a ``datetime``; ``end`` is taken from the item, else the next item's
start, else inferred from the dominant slot length.

Prices come out in **EUR/kWh**: a unit implied by the item key itself
(``price_ct_per_kwh``) wins, otherwise the caller passes the entity's
``unit_of_measurement`` scale (:func:`unit_scale`). Parsing is row-tolerant —
one malformed row is dropped, not the whole forecast — and every failure
surfaces as :class:`PriceFormatError`, the only exception callers catch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Attribute names that hold a *today* list and the matching *tomorrow* list.
_SPLIT_ATTR_PAIRS: list[tuple[str, str]] = [
    ("data_today", "data_tomorrow"),
    ("raw_today", "raw_tomorrow"),
    ("prices_today", "prices_tomorrow"),
    ("today_interval_prices", "tomorrow_interval_prices"),
]
# Attribute names that hold a single combined list (today + tomorrow together).
_SINGLE_ATTRS: list[str] = ["prices", "data", "forecast"]

_START_KEYS = ["start", "time", "hour", "start_time", "datetime", "period_start"]
_END_KEYS = ["end", "end_time"]
_BUY_KEYS = ["buy", "value", "price", "price_ct_per_kwh", "electricity_price"]
_SELL_KEYS = ["sell", "sell_price"]
# Item keys whose name fixes the unit, regardless of the entity's
# ``unit_of_measurement`` (multiplier to EUR/kWh).
_KEY_SCALES: dict[str, float] = {"price_ct_per_kwh": 0.01}


@dataclass(frozen=True)
class ForecastSlot:
    """One normalised price slot. ``sell`` is ``None`` when not provided."""

    start: datetime
    end: datetime
    buy: float
    sell: float | None = None


@dataclass(frozen=True)
class FormatSpec:
    """Detected layout of a price entity's attributes."""

    today_attr: str
    tomorrow_attr: str | None
    start_key: str
    buy_key: str
    sell_key: str | None
    end_key: str | None
    # Multiplier to EUR/kWh implied by ``buy_key`` itself (``price_ct_per_kwh``
    # -> 0.01), or ``None`` when the key says nothing and the entity's unit
    # decides. Applies to buy and sell alike (same item, same convention).
    key_scale: float | None = None


class PriceFormatError(ValueError):
    """Raised when a price entity's attributes can't be understood."""


# Price unit -> multiplier to EUR/kWh. Matched case-insensitively with spaces
# removed. Deliberately a closed list: an unrecognised unit keeps the historic
# behaviour (taken as EUR/kWh) rather than guessing at a scale.
_UNIT_SCALES: dict[str, float] = {
    "eur/kwh": 1.0,
    "€/kwh": 1.0,
    "c/kwh": 0.01,
    "ct/kwh": 0.01,
    "¢/kwh": 0.01,
    "snt/kwh": 0.01,
    "cent/kwh": 0.01,
    "cents/kwh": 0.01,
    "eur/mwh": 0.001,
    "€/mwh": 0.001,
}


def unit_scale(unit: object) -> float:
    """Multiplier that converts a price in ``unit`` to EUR/kWh.

    Nord Pool (``price_in_cents: true``), spot-hinta and friends happily
    publish c/kWh or EUR/MWh; read raw, every electric source would be priced
    100x or 1000x off and a wood stove would "win" every hour with nothing
    logged. Missing/unknown units map to ``1.0`` (EUR/kWh, as before).
    """
    if not isinstance(unit, str):
        return 1.0
    return _UNIT_SCALES.get(unit.replace(" ", "").lower(), 1.0)


def _first_present(keys: list[str], item: dict) -> str | None:
    return next((k for k in keys if k in item), None)


def detect_format(attributes: dict) -> FormatSpec:
    """Work out where the forecast lives and which keys to read.

    Raises :class:`PriceFormatError` if no known layout matches.
    """
    today_attr: str | None = None
    tomorrow_attr: str | None = None

    for today, tomorrow in _SPLIT_ATTR_PAIRS:
        if today in attributes:
            today_attr, tomorrow_attr = today, tomorrow
            break
    if today_attr is None:
        today_attr = next((a for a in _SINGLE_ATTRS if a in attributes), None)
        tomorrow_attr = None

    if today_attr is None:
        raise PriceFormatError("no recognised forecast attribute found")

    sample = attributes.get(today_attr) or []
    if not isinstance(sample, list):
        raise PriceFormatError(f"attribute {today_attr!r} is not a list of dicts")
    # Tomorrow counts too: right after midnight some integrations briefly
    # publish an empty today list next to a full tomorrow one.
    if tomorrow_attr and isinstance(attributes.get(tomorrow_attr), list):
        sample = sample + attributes[tomorrow_attr]
    if not sample:
        raise PriceFormatError(f"attribute {today_attr!r} is not a list of dicts")

    # Detect from the first row that actually looks like a price row, so one
    # malformed leading row (``None``, or a dict missing its keys) can't make
    # the whole layout unrecognisable — _parse_rows skips it later anyway.
    item = next(
        (
            it
            for it in sample
            if isinstance(it, dict)
            and _first_present(_START_KEYS, it) is not None
            and _first_present(_BUY_KEYS, it) is not None
        ),
        None,
    )
    if item is None:
        raise PriceFormatError("could not find start/value keys in forecast items")
    start_key = _first_present(_START_KEYS, item)
    buy_key = _first_present(_BUY_KEYS, item)
    assert start_key is not None and buy_key is not None

    return FormatSpec(
        today_attr=today_attr,
        tomorrow_attr=tomorrow_attr,
        start_key=start_key,
        buy_key=buy_key,
        sell_key=_first_present(_SELL_KEYS, item),
        end_key=_first_present(_END_KEYS, item),
        key_scale=_KEY_SCALES.get(buy_key),
    )


def _parse_dt(value: object) -> datetime:
    """Parse an ISO string / pass through a ``datetime``, normalised to **UTC**.

    Normalising to UTC here is what keeps the engine DST-correct: all downstream
    arithmetic (``+timedelta``, subtraction) then runs in a zone without DST, so
    a slot that straddles a transition is still its true real-time length.
    Display/actuation converts back to local at the entity layer.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise PriceFormatError(f"unparseable start value: {value!r}")
    if dt.tzinfo is None:
        raise PriceFormatError(f"start time is not timezone-aware: {value!r}")
    return dt.astimezone(UTC)


def _infer_slot_length(starts: list[datetime]) -> timedelta:
    """Most common gap between consecutive starts (fallback 15 min)."""
    gaps: dict[float, int] = {}
    for a, b in zip(starts, starts[1:], strict=False):
        secs = (b - a).total_seconds()
        if secs > 0:
            gaps[secs] = gaps.get(secs, 0) + 1
    if not gaps:
        return timedelta(minutes=15)
    return timedelta(seconds=max(gaps, key=lambda s: gaps[s]))


def _finite_or_none(value: object) -> float | None:
    """``float(value)`` when it is a finite number, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_rows(
    items: list[object], spec: FormatSpec, scale: float
) -> list[tuple[datetime, datetime | None, float | None, float | None]]:
    """Parse ``(start, explicit end | None, buy | None, sell)`` per usable row.

    A row is skipped — not fatal — when it isn't a dict, lacks or has an
    invalid start, has no finite buy price, or carries an explicit end at or
    before its start: price integrations publish ``None`` for not-yet-known
    quarters and the odd malformed row, and one of those must not cost the
    whole forecast. An unparseable explicit end is treated as absent (it gets
    inferred like any other missing end). A bad sell just becomes ``None``.
    """
    rows: list[tuple[datetime, datetime | None, float | None, float | None]] = []
    for item in items:
        if not isinstance(item, dict) or spec.start_key not in item:
            continue
        try:
            start = _parse_dt(item[spec.start_key])
        except (PriceFormatError, TypeError, ValueError):
            continue
        # An unpriced row is kept (buy None) so its start still bounds the
        # previous slot; normalize() drops it only after ends are inferred.
        # Skipping it here would stretch 00:00's price over the unknown 01:00.
        buy = _finite_or_none(item.get(spec.buy_key))
        end: datetime | None = None
        if spec.end_key and item.get(spec.end_key) is not None:
            try:
                end = _parse_dt(item[spec.end_key])
            except (PriceFormatError, TypeError, ValueError):
                end = None
            if end is not None and end <= start:
                continue
        sell = _finite_or_none(item.get(spec.sell_key)) if spec.sell_key else None
        rows.append(
            (
                start,
                end,
                buy * scale if buy is not None else None,
                sell * scale if sell is not None else None,
            )
        )
    return rows


def _as_list(value: object) -> list[object]:
    # A non-list attribute (``None`` before tomorrow's prices publish, or a
    # string) contributes nothing rather than being iterated character-wise.
    return list(value) if isinstance(value, list | tuple) else []


def normalize(
    attributes: dict, spec: FormatSpec | None = None, *, unit_scale_factor: float = 1.0
) -> list[ForecastSlot]:
    """Normalise a price entity's attributes into time-ordered ForecastSlots.

    Concatenates the today and tomorrow lists (tomorrow may be missing), parses
    each row, sorts by start and drops exact-duplicate start times (keeping the
    first), which guards against overlapping today/tomorrow tails — and only
    *then* infers missing ends, so "the next start" really is the next slot in
    time, not the next row of an unordered list.

    ``unit_scale_factor`` converts the entity's unit to EUR/kWh (see
    :func:`unit_scale`); a unit implied by the item key (``spec.key_scale``)
    overrides it so a ``price_ct_per_kwh`` list is never scaled twice.

    Raises :class:`PriceFormatError` — and only that — when nothing usable can
    be read: callers catch exactly this, so a stray ``KeyError``/``TypeError``
    from a malformed payload must never escape.
    """
    try:
        spec = spec or detect_format(attributes)
        scale = spec.key_scale if spec.key_scale is not None else unit_scale_factor
        raw = _as_list(attributes.get(spec.today_attr))
        if spec.tomorrow_attr and spec.tomorrow_attr != spec.today_attr:
            raw += _as_list(attributes.get(spec.tomorrow_attr))

        rows = _parse_rows(raw, spec, scale)
        if raw and not any(r[2] is not None for r in rows):
            raise PriceFormatError("no usable rows in the price forecast")

        # Stable sort, priced rows first within a start: the first duplicate
        # wins below, and a priced duplicate beats an unpriced one.
        rows.sort(key=lambda r: (r[0], r[2] is None))
        deduped: list[tuple[datetime, datetime | None, float | None, float | None]] = []
        seen: set[datetime] = set()
        for row in rows:
            if row[0] in seen:
                continue
            seen.add(row[0])
            deduped.append(row)

        slot_len = _infer_slot_length([r[0] for r in deduped])
        slots: list[ForecastSlot] = []
        for i, (start, end, buy, sell) in enumerate(deduped):
            if end is None:
                end = deduped[i + 1][0] if i + 1 < len(deduped) else start + slot_len
            if buy is None:
                continue  # its start bounded the previous slot; it has no price
            slots.append(ForecastSlot(start=start, end=end, buy=buy, sell=sell))
        return slots
    except PriceFormatError:
        raise
    except (TypeError, KeyError, ValueError, AttributeError, IndexError) as err:
        raise PriceFormatError(f"could not parse price forecast: {err}") from err


def merge_sell(buy_slots: list[ForecastSlot], sell_slots: list[ForecastSlot]) -> list[ForecastSlot]:
    """Attach sell prices from a *separate* sell-forecast entity by start time.

    Used when buy and sell come from two different entities; the sell entity is
    normalised the same way (its ``buy`` field carries the sell value).
    """
    sell_by_start = {s.start: s.buy for s in sell_slots}
    return [
        ForecastSlot(
            start=b.start,
            end=b.end,
            buy=b.buy,
            sell=sell_by_start.get(b.start, b.sell),
        )
        for b in buy_slots
    ]


def slots_from_state(state) -> list[ForecastSlot]:
    """Normalise a Home Assistant ``State`` (duck-typed: anything with
    ``.attributes``). Kept here, not in the coordinator, so the only HA-aware
    surface is this one-liner and the rest stays unit-testable.

    The entity's ``unit_of_measurement`` sets the scale to EUR/kWh (a c/kWh
    Nord Pool sensor would otherwise price every electric source 100x high).
    """
    if state is None:
        raise PriceFormatError("price entity is unavailable (no state)")
    attributes = dict(state.attributes)
    return normalize(
        attributes, unit_scale_factor=unit_scale(attributes.get("unit_of_measurement"))
    )
