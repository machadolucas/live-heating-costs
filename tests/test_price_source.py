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
