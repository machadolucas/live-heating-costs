"""Unit tests for the price-source normalisation (pure, no Home Assistant)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest

_PS_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "live_heating_costs"
    / "price_source.py"
)
_spec = importlib.util.spec_from_file_location("ls_price_source", _PS_PATH)
ps = importlib.util.module_from_spec(_spec)
sys.modules["ls_price_source"] = ps
_spec.loader.exec_module(ps)

FI = timezone(timedelta(hours=3))  # Europe/Helsinki summer (EEST), like the live data


# --------------------------------------------------------------------------- #
# user's day-ahead format: data_today / data_tomorrow with buy + sell
# --------------------------------------------------------------------------- #


def test_user_day_ahead_buy_sell_string_starts():
    t0 = datetime(2026, 5, 28, 22, 0, tzinfo=FI)
    today = [
        {
            "start": (t0 + timedelta(minutes=15 * i)).isoformat(),
            "end": (t0 + timedelta(minutes=15 * (i + 1))).isoformat(),
            "buy": 0.05 + i * 0.01,
            "sell": 0.002 + i * 0.001,
        }
        for i in range(4)
    ]
    attrs = {"data_today": today, "data_tomorrow": [], "tomorrow_valid": False}

    slots = ps.normalize(attrs)
    assert len(slots) == 4
    assert slots[0].buy == pytest.approx(0.05)
    assert slots[0].sell == pytest.approx(0.002)
    assert slots[0].end - slots[0].start == timedelta(minutes=15)
    # time-ordered
    assert [s.start for s in slots] == sorted(s.start for s in slots)


def test_user_day_ahead_concatenates_tomorrow():
    t0 = datetime(2026, 5, 28, 22, 0, tzinfo=FI)
    today = [{"start": t0, "end": t0 + timedelta(minutes=15), "buy": 0.05, "sell": 0.002}]
    t1 = datetime(2026, 5, 29, 0, 0, tzinfo=FI)
    tomorrow = [{"start": t1, "end": t1 + timedelta(minutes=15), "buy": 0.10, "sell": 0.01}]
    attrs = {"data_today": today, "data_tomorrow": tomorrow}
    slots = ps.normalize(attrs)  # datetime objects, not strings
    assert len(slots) == 2
    assert slots[1].buy == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# Nord Pool raw_today / raw_tomorrow with {start, end, value}
# --------------------------------------------------------------------------- #


def test_nordpool_raw_value_no_sell():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    today = [
        {
            "start": t0 + timedelta(hours=i),
            "end": t0 + timedelta(hours=i + 1),
            "value": float(i),
        }
        for i in range(3)
    ]
    slots = ps.normalize({"raw_today": today, "raw_tomorrow": []})
    assert len(slots) == 3
    assert slots[0].buy == 0.0
    assert slots[0].sell is None
    assert slots[0].end - slots[0].start == timedelta(hours=1)


# --------------------------------------------------------------------------- #
# ENTSO-e: single 'prices' list, string time, no end => inferred
# --------------------------------------------------------------------------- #


def test_entsoe_prices_infers_end_from_next_start():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    prices = [{"time": (t0 + timedelta(hours=i)).isoformat(), "price": 0.1 + i} for i in range(3)]
    slots = ps.normalize({"prices": prices})
    assert len(slots) == 3
    # end inferred from the next start...
    assert slots[0].end == slots[1].start
    # ...and the last slot falls back to the inferred slot length (1h).
    assert slots[-1].end - slots[-1].start == timedelta(hours=1)


# --------------------------------------------------------------------------- #
# generic + edge cases
# --------------------------------------------------------------------------- #


def test_dedupes_overlapping_starts():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    today = [{"start": t0, "end": t0 + timedelta(minutes=15), "value": 1.0}]
    # tomorrow list repeats the same start (overlapping tail) — must be dropped.
    tomorrow = [{"start": t0, "end": t0 + timedelta(minutes=15), "value": 9.0}]
    slots = ps.normalize({"raw_today": today, "raw_tomorrow": tomorrow})
    assert len(slots) == 1
    assert slots[0].buy == 1.0  # first one kept


def test_merge_sell_from_separate_entity():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    buy = ps.normalize(
        {"raw_today": [{"start": t0, "end": t0 + timedelta(hours=1), "value": 0.20}]}
    )
    sell = ps.normalize(
        {"raw_today": [{"start": t0, "end": t0 + timedelta(hours=1), "value": 0.05}]}
    )
    merged = ps.merge_sell(buy, sell)
    assert merged[0].buy == pytest.approx(0.20)
    assert merged[0].sell == pytest.approx(0.05)


def test_detect_format_raises_on_unknown():
    with pytest.raises(ps.PriceFormatError):
        ps.normalize({"state": "nonsense", "unrelated": 5})


def test_naive_datetime_rejected():
    naive = datetime(2026, 1, 1, 0, 0)  # no tzinfo
    with pytest.raises(ps.PriceFormatError):
        ps.normalize({"prices": [{"time": naive.isoformat(), "price": 1.0}]})


def test_normalises_to_utc_across_dst_spring_forward():
    # Real day-ahead ISO data straddling spring-forward: 02:45+02:00 and
    # 04:00+03:00 are 15 *real* minutes apart. After UTC normalisation the slots
    # must stay uniformly 15 min apart — no spurious 1h15m wall-clock gap.
    prices = [
        {"time": "2026-03-29T02:30:00+02:00", "price": 1.0},
        {"time": "2026-03-29T02:45:00+02:00", "price": 2.0},
        {"time": "2026-03-29T04:00:00+03:00", "price": 3.0},
        {"time": "2026-03-29T04:15:00+03:00", "price": 4.0},
    ]
    slots = ps.normalize({"prices": prices})
    assert all(s.start.utcoffset() == timedelta(0) for s in slots)  # all UTC now
    gaps = [b.start - a.start for a, b in zip(slots, slots[1:], strict=False)]
    assert all(g == timedelta(minutes=15) for g in gaps)


# --------------------------------------------------------------------------- #
# units: unit_scale, key-implied cents, entity unit_of_measurement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("unit", "scale"),
    [
        ("€/kWh", 1.0),
        ("EUR/kWh", 1.0),
        ("c/kWh", 0.01),
        ("snt/kWh", 0.01),
        ("ct / kWh", 0.01),
        ("EUR/MWh", 0.001),
        ("€/MWh", 0.001),
        (None, 1.0),
        ("", 1.0),
        ("bananas", 1.0),
        (42, 1.0),
    ],
)
def test_unit_scale(unit, scale):
    assert ps.unit_scale(unit) == pytest.approx(scale)


class _State:
    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


def _hourly(key: str, values: list[float]) -> list[dict]:
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return [{"start": (t0 + timedelta(hours=i)).isoformat(), key: v} for i, v in enumerate(values)]


def test_price_ct_per_kwh_key_is_cents():
    slots = ps.normalize({"prices": _hourly("price_ct_per_kwh", [12.0, 5.0])})
    assert [s.buy for s in slots] == pytest.approx([0.12, 0.05])


def test_slots_from_state_applies_entity_unit():
    state = _State({"prices": _hourly("price", [12.0]), "unit_of_measurement": "c/kWh"})
    assert ps.slots_from_state(state)[0].buy == pytest.approx(0.12)
    state = _State({"prices": _hourly("price", [120.0]), "unit_of_measurement": "EUR/MWh"})
    assert ps.slots_from_state(state)[0].buy == pytest.approx(0.12)


def test_slots_from_state_scales_sell_too():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    items = [{"start": t0.isoformat(), "buy": 20.0, "sell": 4.0}]
    state = _State({"data_today": items, "unit_of_measurement": "c/kWh"})
    slot = ps.slots_from_state(state)[0]
    assert slot.buy == pytest.approx(0.20)
    assert slot.sell == pytest.approx(0.04)


def test_ct_key_ignores_entity_unit_no_double_scale():
    state = _State({"prices": _hourly("price_ct_per_kwh", [12.0]), "unit_of_measurement": "c/kWh"})
    assert ps.slots_from_state(state)[0].buy == pytest.approx(0.12)


def test_slots_from_state_without_unit_is_eur():
    state = _State({"prices": _hourly("price", [0.12])})
    assert ps.slots_from_state(state)[0].buy == pytest.approx(0.12)


# --------------------------------------------------------------------------- #
# robustness: bad rows are dropped, everything else is PriceFormatError
# --------------------------------------------------------------------------- #


def test_rows_with_missing_or_bad_buy_are_skipped():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    prices = [
        {"time": (t0 + timedelta(hours=i)).isoformat(), "price": v}
        for i, v in enumerate([0.1, None, "n/a", float("nan"), float("inf"), 0.2])
    ]
    slots = ps.normalize({"prices": prices})
    assert [s.buy for s in slots] == pytest.approx([0.1, 0.2])
    assert slots[1].start == t0 + timedelta(hours=5)


def test_rows_with_missing_or_invalid_start_are_skipped():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    prices = [
        {"time": t0.isoformat(), "price": 0.1},
        {"price": 0.5},  # no start key
        {"time": "not a timestamp", "price": 0.6},
        {"time": "2026-01-01T02:00:00", "price": 0.7},  # naive
        {"time": 12345, "price": 0.8},
        "not even a dict",
        {"time": (t0 + timedelta(hours=1)).isoformat(), "price": 0.2},
    ]
    slots = ps.normalize({"prices": prices})
    assert [s.buy for s in slots] == pytest.approx([0.1, 0.2])


def test_all_rows_unusable_raises_price_format_error():
    with pytest.raises(ps.PriceFormatError):
        ps.normalize({"prices": [{"time": "garbage", "price": 1.0}, {"time": None, "price": 2}]})
    with pytest.raises(ps.PriceFormatError):
        ps.normalize({"prices": [{"time": "2026-01-01T00:00:00+00:00", "price": None}]})


@pytest.mark.parametrize(
    "attributes",
    [None, 5, "prices", {"prices": "oops"}, {"prices": [1, 2]}, {"raw_today": [{"foo": 1}]}],
)
def test_malformed_payloads_only_raise_price_format_error(attributes):
    with pytest.raises(ps.PriceFormatError):
        ps.normalize(attributes)


def test_non_list_tomorrow_is_ignored():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    today = [{"start": t0, "end": t0 + timedelta(hours=1), "value": 1.0}]
    slots = ps.normalize({"raw_today": today, "raw_tomorrow": None})
    assert len(slots) == 1
    slots = ps.normalize({"raw_today": today, "raw_tomorrow": "unavailable"})
    assert len(slots) == 1


def test_unsorted_rows_infer_ends_in_time_order():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    order = [2, 0, 3, 1]
    prices = [{"time": (t0 + timedelta(hours=i)).isoformat(), "price": float(i)} for i in order]
    slots = ps.normalize({"prices": prices})
    assert [s.buy for s in slots] == [0.0, 1.0, 2.0, 3.0]
    for s in slots:
        assert s.end - s.start == timedelta(hours=1)


def test_duplicate_start_does_not_poison_end_inference():
    # Dedupe must run before end inference: a duplicate start would otherwise
    # hand its twin a zero-length slot (end = the "next" start = its own start).
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    prices = [
        {"time": t0.isoformat(), "price": 1.0},
        {"time": t0.isoformat(), "price": 9.0},
        {"time": (t0 + timedelta(hours=1)).isoformat(), "price": 2.0},
    ]
    slots = ps.normalize({"prices": prices})
    assert [s.buy for s in slots] == [1.0, 2.0]
    assert slots[0].end == t0 + timedelta(hours=1)


def test_explicit_end_not_after_start_is_dropped():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    today = [
        {"start": t0, "end": t0 + timedelta(hours=1), "value": 1.0},
        {"start": t0 + timedelta(hours=1), "end": t0 + timedelta(hours=1), "value": 2.0},
        {"start": t0 + timedelta(hours=2), "end": t0 + timedelta(hours=1), "value": 3.0},
        {"start": t0 + timedelta(hours=3), "end": t0 + timedelta(hours=4), "value": 4.0},
    ]
    slots = ps.normalize({"raw_today": today})
    assert [s.buy for s in slots] == [1.0, 4.0]


def test_bad_sell_becomes_none_row_kept():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    items = [{"start": t0, "end": t0 + timedelta(hours=1), "buy": 0.1, "sell": "n/a"}]
    slots = ps.normalize({"data_today": items})
    assert slots[0].sell is None
    assert slots[0].buy == pytest.approx(0.1)


def test_unpriced_row_still_bounds_the_previous_slot():
    # 01:00 has no price yet: 00:00's slot must end at 01:00 (not stretch to
    # 02:00 at 00:00's price), and the unknown hour is simply absent.
    t0 = datetime(2026, 7, 8, 0, 0, tzinfo=UTC)
    attrs = {
        "prices": [
            {"time": t0.isoformat(), "price": 0.10},
            {"time": (t0 + timedelta(hours=1)).isoformat(), "price": None},
            {"time": (t0 + timedelta(hours=2)).isoformat(), "price": 0.30},
        ]
    }
    slots = ps.normalize(attrs)
    assert [(s.start, s.end, s.buy) for s in slots] == [
        (t0, t0 + timedelta(hours=1), 0.10),
        (t0 + timedelta(hours=2), t0 + timedelta(hours=3), 0.30),
    ]


def test_priced_duplicate_beats_unpriced_one():
    t0 = datetime(2026, 7, 8, 0, 0, tzinfo=UTC)
    attrs = {
        "prices": [
            {"time": t0.isoformat(), "price": None},
            {"time": t0.isoformat(), "price": 0.12},
        ]
    }
    assert [s.buy for s in ps.normalize(attrs)] == [0.12]


@pytest.mark.parametrize("first", [None, {"time": "x"}, {"price": 0.1}])
def test_malformed_first_row_does_not_break_format_detection(first):
    t0 = datetime(2026, 7, 8, 0, 0, tzinfo=UTC)
    attrs = {"prices": [first, {"time": t0.isoformat(), "price": 0.12}]}
    slots = ps.normalize(attrs)
    assert [(s.start, s.buy) for s in slots] == [(t0, 0.12)]


def test_empty_today_with_tomorrow_rows_still_parses():
    t0 = datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
    attrs = {"raw_today": [], "raw_tomorrow": [{"start": t0.isoformat(), "value": 0.2}]}
    assert [(s.start, s.buy) for s in ps.normalize(attrs)] == [(t0, 0.2)]
