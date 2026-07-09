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
"""

from __future__ import annotations

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


class PriceFormatError(ValueError):
    """Raised when a price entity's attributes can't be understood."""


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
    if not isinstance(sample, list) or not sample or not isinstance(sample[0], dict):
        raise PriceFormatError(f"attribute {today_attr!r} is not a list of dicts")

    item = sample[0]
    start_key = _first_present(_START_KEYS, item)
    buy_key = _first_present(_BUY_KEYS, item)
    if start_key is None or buy_key is None:
        raise PriceFormatError("could not find start/value keys in forecast items")

    return FormatSpec(
        today_attr=today_attr,
        tomorrow_attr=tomorrow_attr,
        start_key=start_key,
        buy_key=buy_key,
        sell_key=_first_present(_SELL_KEYS, item),
        end_key=_first_present(_END_KEYS, item),
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


def _parse_list(items: list[dict], spec: FormatSpec) -> list[ForecastSlot]:
    """Turn one raw item list into ForecastSlots (end inferred if absent)."""
    starts = [_parse_dt(it[spec.start_key]) for it in items]
    slot_len = _infer_slot_length(starts)
    slots: list[ForecastSlot] = []
    for i, it in enumerate(items):
        start = starts[i]
        if spec.end_key and spec.end_key in it:
            end = _parse_dt(it[spec.end_key])
        elif i + 1 < len(starts):
            end = starts[i + 1]
        else:
            end = start + slot_len
        sell = (
            float(it[spec.sell_key])
            if spec.sell_key and it.get(spec.sell_key) is not None
            else None
        )
        slots.append(ForecastSlot(start=start, end=end, buy=float(it[spec.buy_key]), sell=sell))
    return slots


def normalize(attributes: dict, spec: FormatSpec | None = None) -> list[ForecastSlot]:
    """Normalise a price entity's attributes into time-ordered ForecastSlots.

    Concatenates the today and tomorrow lists (tomorrow may be missing), parses
    each item, sorts by start and drops exact-duplicate start times (keeping the
    first), which guards against overlapping today/tomorrow tails.
    """
    spec = spec or detect_format(attributes)
    raw: list[dict] = list(attributes.get(spec.today_attr) or [])
    if spec.tomorrow_attr and spec.tomorrow_attr != spec.today_attr:
        raw += list(attributes.get(spec.tomorrow_attr) or [])

    slots = _parse_list(raw, spec)
    slots.sort(key=lambda s: s.start)

    deduped: list[ForecastSlot] = []
    seen: set[datetime] = set()
    for slot in slots:
        if slot.start in seen:
            continue
        seen.add(slot.start)
        deduped.append(slot)
    return deduped


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
    """
    if state is None:
        raise PriceFormatError("price entity is unavailable (no state)")
    return normalize(dict(state.attributes))
