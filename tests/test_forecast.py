"""Unit tests for the per-source cost forecast (pure, no Home Assistant)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.live_heating_costs import price_source as ps
from custom_components.live_heating_costs.forecast import (
    SourceSlotCost,
    TempPoint,
    anchor_temps,
    build_source_forecast,
    combined_slots,
    forecast_attr_list,
    interp_temp,
)
from custom_components.live_heating_costs.price_source import ForecastSlot

NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)


def _price_slots(
    n: int, start: datetime = NOW, step_min: int = 15, base: float = 0.05
) -> list[ForecastSlot]:
    return [
        ForecastSlot(
            start=start + timedelta(minutes=step_min * i),
            end=start + timedelta(minutes=step_min * (i + 1)),
            buy=base + i * 0.001,
        )
        for i in range(n)
    ]


def _hourly_temps(
    hours: int, start: datetime = NOW, t_start: float = 10.0, t_end: float = -20.0
) -> list[TempPoint]:
    span = t_end - t_start
    return [
        TempPoint(ts=start + timedelta(hours=i), temp_c=t_start + span * i / hours)
        for i in range(hours + 1)
    ]


def cop_curve(t: float) -> float:
    return 3.0 + t / 10.0


def cap_curve(t: float) -> float:
    return max(0.0, 5.0 + t * 0.1)


# --------------------------------------------------------------------------- #
# interp_temp
# --------------------------------------------------------------------------- #


def test_interp_temp_linear_inside_range():
    pts = [TempPoint(NOW, 0.0), TempPoint(NOW + timedelta(hours=2), 10.0)]
    assert interp_temp(pts, NOW + timedelta(hours=1)) == pytest.approx(5.0)


def test_interp_temp_none_outside_range():
    pts = [TempPoint(NOW, 0.0), TempPoint(NOW + timedelta(hours=2), 10.0)]
    assert interp_temp(pts, NOW - timedelta(minutes=1)) is None
    assert interp_temp(pts, NOW + timedelta(hours=2, minutes=1)) is None


def test_interp_temp_empty_points():
    assert interp_temp([], NOW) is None


def test_interp_temp_sorts_defensively():
    pts = [TempPoint(NOW + timedelta(hours=2), 10.0), TempPoint(NOW, 0.0)]
    assert interp_temp(pts, NOW + timedelta(hours=1)) == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# curve source: exact per-slot buy
# --------------------------------------------------------------------------- #


def test_curve_source_spot_slots_match_cop_at_temp():
    price_slots = _price_slots(96)  # 24h of 15-min slots
    temps = _hourly_temps(24)

    result = build_source_forecast(
        price_slots,
        [],
        temps,
        NOW,
        horizon_h=24,
        cop_at=cop_curve,
        capacity_at=cap_curve,
        fixed_cop=None,
        fixed_capacity_kw=None,
        operating_limit_c=None,
    )
    by_start = {s.start: s for s in result}

    for i in (0, 50, 95):
        slot = price_slots[i]
        midpoint = slot.start + (slot.end - slot.start) / 2
        t_mid = interp_temp(temps, midpoint)
        assert t_mid is not None
        expected_cop = cop_curve(t_mid)
        expected_cap = cap_curve(t_mid)
        out = by_start[slot.start]
        assert out.cop == pytest.approx(expected_cop)
        assert out.capacity_kw == pytest.approx(expected_cap)
        assert out.buy == pytest.approx(slot.buy / expected_cop)
        assert out.estimated is False


def test_operating_limit_gates_slot_to_unavailable():
    price_slots = _price_slots(96)
    temps = _hourly_temps(24)
    limit = -5.0

    result = build_source_forecast(
        price_slots,
        [],
        temps,
        NOW,
        horizon_h=24,
        cop_at=cop_curve,
        capacity_at=cap_curve,
        fixed_cop=None,
        fixed_capacity_kw=None,
        operating_limit_c=limit,
    )
    by_start = {s.start: s for s in result}

    gated_any = False
    for slot in price_slots:
        midpoint = slot.start + (slot.end - slot.start) / 2
        t_mid = interp_temp(temps, midpoint)
        out = by_start[slot.start]
        if t_mid is not None and t_mid < limit:
            gated_any = True
            assert out.buy is None
            assert out.cop is None
            assert out.capacity_kw == 0.0
        elif t_mid is not None:
            assert out.buy is not None
    assert gated_any  # the ramp does cross the limit within the 24h window


# --------------------------------------------------------------------------- #
# no-extrapolation: curve skips beyond temp horizon, fixed source doesn't
# --------------------------------------------------------------------------- #


def test_curve_source_skips_slots_beyond_temp_horizon_but_fixed_keeps_them():
    price_slots_48h = _price_slots(192)  # 48h of 15-min slots
    temps_24h = _hourly_temps(24)  # weather forecast only covers the first 24h

    curve_result = build_source_forecast(
        price_slots_48h,
        [],
        temps_24h,
        NOW,
        horizon_h=48,
        cop_at=cop_curve,
        capacity_at=cap_curve,
        fixed_cop=None,
        fixed_capacity_kw=None,
        operating_limit_c=None,
    )
    assert len(curve_result) < len(price_slots_48h)
    assert all(s.end <= temps_24h[-1].ts for s in curve_result)

    fixed_result = build_source_forecast(
        price_slots_48h,
        [],
        temps_24h,
        NOW,
        horizon_h=48,
        cop_at=None,
        capacity_at=None,
        fixed_cop=1.0,
        fixed_capacity_kw=10.0,
        operating_limit_c=None,
    )
    assert len(fixed_result) == len(price_slots_48h)


# --------------------------------------------------------------------------- #
# estimated extension
# --------------------------------------------------------------------------- #


def test_estimated_slots_extend_past_real_horizon_only():
    real = _price_slots(96, base=0.05)  # covers [NOW, NOW+24h)
    # "estimated" series covers the *same* 48h span, at a different price, so
    # overlap with the real series is unambiguous if it leaks through.
    est_full = _price_slots(192, base=0.90)  # covers [NOW, NOW+48h)
    temps_48h = _hourly_temps(48)

    result = build_source_forecast(
        real,
        est_full,
        temps_48h,
        NOW,
        horizon_h=48,
        cop_at=None,
        capacity_at=None,
        fixed_cop=1.0,
        fixed_capacity_kw=5.0,
        operating_limit_c=None,
    )

    real_horizon_end = NOW + timedelta(hours=24)
    real_part = [s for s in result if s.start < real_horizon_end]
    est_part = [s for s in result if s.start >= real_horizon_end]

    assert len(real_part) == 96
    assert all(not s.estimated for s in real_part)
    # none of the real-horizon buys came from the (much higher) estimated price.
    assert all(s.buy < 0.5 for s in real_part)

    assert len(est_part) == 96
    assert all(s.estimated for s in est_part)
    assert all(s.buy > 0.5 for s in est_part)

    # no duplicate boundaries.
    starts = [s.start for s in result]
    assert len(starts) == len(set(starts))


# --------------------------------------------------------------------------- #
# fixed source
# --------------------------------------------------------------------------- #


def test_fixed_source_cop_one_equals_raw_price():
    price_slots = _price_slots(96)
    result = build_source_forecast(
        price_slots,
        [],
        [],
        NOW,
        horizon_h=24,
        cop_at=None,
        capacity_at=None,
        fixed_cop=1.0,
        fixed_capacity_kw=10.0,
        operating_limit_c=None,
    )
    assert len(result) == len(price_slots)
    by_start = {s.start: s for s in result}
    for slot in price_slots:
        out = by_start[slot.start]
        assert out.buy == pytest.approx(slot.buy)
        assert out.cop == pytest.approx(1.0)
        assert out.capacity_kw == pytest.approx(10.0)
        assert out.estimated is False


# --------------------------------------------------------------------------- #
# horizon truncation + naive datetime
# --------------------------------------------------------------------------- #


def test_horizon_h_truncates_output():
    price_slots = _price_slots(192)  # 48h
    result = build_source_forecast(
        price_slots,
        [],
        [],
        NOW,
        horizon_h=6,
        cop_at=None,
        capacity_at=None,
        fixed_cop=1.0,
        fixed_capacity_kw=10.0,
        operating_limit_c=None,
    )
    horizon_end = NOW + timedelta(hours=6)
    assert result
    assert all(s.start < horizon_end for s in result)
    assert len(result) == 24  # 6h of 15-min slots


def test_naive_now_raises():
    price_slots = _price_slots(4)
    with pytest.raises(ValueError):
        build_source_forecast(
            price_slots,
            [],
            [],
            datetime(2026, 1, 10, 0, 0),  # naive
            horizon_h=24,
            cop_at=None,
            capacity_at=None,
            fixed_cop=1.0,
            fixed_capacity_kw=10.0,
            operating_limit_c=None,
        )


def test_requires_a_cop_source():
    price_slots = _price_slots(4)
    with pytest.raises(ValueError):
        build_source_forecast(
            price_slots,
            [],
            [],
            NOW,
            horizon_h=24,
            cop_at=None,
            capacity_at=None,
            fixed_cop=None,
            fixed_capacity_kw=None,
            operating_limit_c=None,
        )


# --------------------------------------------------------------------------- #
# forecast_attr_list round-trip via price_source
# --------------------------------------------------------------------------- #


def test_forecast_attr_list_round_trips_through_price_source():
    price_slots = _price_slots(96)
    temps = _hourly_temps(24)
    limit = -5.0  # guarantees some None-buy slots to confirm they're dropped

    slots = build_source_forecast(
        price_slots,
        [],
        temps,
        NOW,
        horizon_h=24,
        cop_at=cop_curve,
        capacity_at=cap_curve,
        fixed_cop=None,
        fixed_capacity_kw=None,
        operating_limit_c=limit,
    )
    priced = [s for s in slots if s.buy is not None]
    assert 0 < len(priced) < len(slots)  # some gated to None, some priced

    attrs = {"forecast": forecast_attr_list(slots)}
    assert len(attrs["forecast"]) == len(priced)

    spec = ps.detect_format(attrs)
    normalized = ps.normalize(attrs, spec)

    assert len(normalized) == len(priced)
    got = {(n.start, n.buy) for n in normalized}
    want = {(s.start, s.buy) for s in priced}
    assert got == want


# --------------------------------------------------------------------------- #
# combined_slots
# --------------------------------------------------------------------------- #


def _slot_cost(start, end, buy, cop, capacity_kw, estimated=False) -> SourceSlotCost:
    return SourceSlotCost(
        start=start, end=end, buy=buy, cop=cop, capacity_kw=capacity_kw, estimated=estimated
    )


def test_combined_slots_cheapest_capacity_and_missing_cost():
    t = [NOW + timedelta(minutes=15 * i) for i in range(5)]

    resistive = [
        _slot_cost(t[0], t[1], 0.10, 1.0, 10.0),
        _slot_cost(t[1], t[2], 0.09, 1.0, 10.0),
        _slot_cost(t[2], t[3], 0.08, 1.0, 10.0),
        _slot_cost(t[3], t[4], 0.07, 1.0, 10.0),
    ]
    pump = [
        _slot_cost(t[0], t[1], 0.05, 4.0, 5.0),  # cheaper, usable -> cheapest
        _slot_cost(t[1], t[2], 0.03, 4.0, 0.0),  # cheaper but zero capacity -> excluded
        _slot_cost(t[2], t[3], None, None, 5.0),  # unpriceable -> present as None
        _slot_cost(t[3], t[4], 0.02, 4.0, 5.0, estimated=True),  # cheapest, estimated
    ]

    result = combined_slots({"resistive": resistive, "pump": pump})
    assert len(result) == 4
    by_start = {r["start"]: r for r in result}

    slot0 = by_start[t[0].isoformat()]
    assert slot0["cheapest"] == "pump"
    assert slot0["costs"] == {"resistive": pytest.approx(0.10), "pump": pytest.approx(0.05)}
    assert slot0["estimated"] is False

    slot1 = by_start[t[1].isoformat()]
    assert slot1["cheapest"] == "resistive"  # pump's zero capacity disqualifies it
    assert slot1["costs"]["pump"] == pytest.approx(0.03)

    slot2 = by_start[t[2].isoformat()]
    assert slot2["cheapest"] == "resistive"
    assert slot2["costs"]["pump"] is None  # missing-cost source still present

    slot3 = by_start[t[3].isoformat()]
    assert slot3["cheapest"] == "pump"
    assert slot3["estimated"] is True


def test_combined_slots_empty():
    assert combined_slots({}) == []


# --------------------------------------------------------------------------- #
# anchor_temps
# --------------------------------------------------------------------------- #


def test_anchor_temps_empty_points_yields_single_anchor():
    assert anchor_temps([], NOW, -3.5) == [TempPoint(ts=NOW, temp_c=-3.5)]


def test_anchor_temps_prepends_when_series_starts_after_now():
    # A weather integration's hourly series starts at the next full hour, so
    # everything between now and it would otherwise be un-interpolatable.
    points = [TempPoint(ts=NOW + timedelta(hours=h), temp_c=-5.0) for h in (2, 1)]
    result = anchor_temps(points, NOW, -3.5)

    assert result[0] == TempPoint(ts=NOW, temp_c=-3.5)
    assert [p.ts for p in result] == [NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)]
    # The gap is now ordinary interpolation from a real reading.
    assert interp_temp(result, NOW + timedelta(minutes=30)) == pytest.approx(-4.25)


def test_anchor_temps_leaves_series_that_already_covers_now():
    before = [TempPoint(ts=NOW - timedelta(minutes=10), temp_c=-1.0)]
    assert anchor_temps(before, NOW, -3.5) == before

    exactly = [TempPoint(ts=NOW, temp_c=-1.0)]
    assert anchor_temps(exactly, NOW, -3.5) == exactly


def test_anchor_temps_without_a_reading_is_a_no_op():
    points = [TempPoint(ts=NOW + timedelta(hours=1), temp_c=-5.0)]
    assert anchor_temps(points, NOW, None) == points
    assert anchor_temps([], NOW, None) == []


def test_anchor_temps_requires_aware_now():
    with pytest.raises(ValueError):
        anchor_temps([], datetime(2026, 1, 10, 0, 0), -3.5)


# --------------------------------------------------------------------------- #
# combined_slots: static sources + partial slots
# --------------------------------------------------------------------------- #


def _two_slot_pump(t, buy=0.05, capacity_kw=5.0) -> list[SourceSlotCost]:
    return [
        _slot_cost(t[0], t[1], buy, 4.0, capacity_kw),
        _slot_cost(t[1], t[2], buy, 4.0, capacity_kw),
    ]


def test_combined_slots_static_costs_present_and_can_win():
    t = [NOW + timedelta(minutes=15 * i) for i in range(3)]
    result = combined_slots(
        {"pump": _two_slot_pump(t)},
        static_costs={"wood": (0.02, 8.0)},
    )

    assert len(result) == 2
    for item in result:
        assert item["costs"]["wood"] == pytest.approx(0.02)
        assert item["cheapest"] == "wood"


def test_combined_slots_static_zero_capacity_never_cheapest():
    t = [NOW + timedelta(minutes=15 * i) for i in range(3)]
    result = combined_slots(
        {"pump": _two_slot_pump(t)},
        static_costs={"wood": (0.001, 0.0)},  # cheapest by far, but delivers nothing
    )

    assert result[0]["costs"]["wood"] == pytest.approx(0.001)
    assert result[0]["cheapest"] == "pump"


def test_combined_slots_static_unknown_cost_tolerated():
    t = [NOW + timedelta(minutes=15 * i) for i in range(3)]
    result = combined_slots({"pump": _two_slot_pump(t)}, static_costs={"wood": (None, 8.0)})

    assert result[0]["costs"]["wood"] is None
    assert result[0]["cheapest"] == "pump"


def test_combined_slots_partial_flags_a_missing_source():
    t = [NOW + timedelta(minutes=15 * i) for i in range(3)]
    resistive = [
        _slot_cost(t[0], t[1], 0.10, 1.0, 10.0),
        _slot_cost(t[1], t[2], 0.10, 1.0, 10.0),
    ]
    pump = [_slot_cost(t[1], t[2], 0.05, 4.0, 5.0)]  # no temperature that early

    result = combined_slots({"resistive": resistive, "pump": pump})

    assert [item["partial"] for item in result] == [True, False]
    # cheapest is still decided from what is known in the partial slot
    assert result[0]["cheapest"] == "resistive"
    assert result[1]["cheapest"] == "pump"


def test_combined_slots_empty_source_list_does_not_make_slots_partial():
    t = [NOW + timedelta(minutes=15 * i) for i in range(3)]
    result = combined_slots({"resistive": _two_slot_pump(t), "pump": []})

    assert all(item["partial"] is False for item in result)
