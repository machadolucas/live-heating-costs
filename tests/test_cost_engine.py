"""Unit tests for cost_engine.py (pure, no Home Assistant)."""

from __future__ import annotations

import pytest

from custom_components.live_heating_costs.cost_engine import (
    Allocation,
    SourceSnapshot,
    electric_cost,
    merit_order,
    rank,
    wood_cost_eur_per_kwh,
)


def _snapshot(
    subentry_id: str,
    name: str,
    capacity_kw: float,
    cost_eur_per_kwh: float | None,
    *,
    cop: float | None = None,
    source_type: str = "curve",
) -> SourceSnapshot:
    return SourceSnapshot(
        subentry_id=subentry_id,
        name=name,
        source_type=source_type,
        capacity_kw=capacity_kw,
        cop=cop,
        cost_eur_per_kwh=cost_eur_per_kwh,
        available=True,
        unavailable_reason=None,
    )


# --------------------------------------------------------------------------- #
# wood_cost_eur_per_kwh
# --------------------------------------------------------------------------- #


def test_wood_cost_matches_spec_example():
    # 132 EUR/m3, density 1700 kWh/m3, efficiency 0.75
    cost = wood_cost_eur_per_kwh(132, 1700, 0.75)
    assert cost == pytest.approx(0.10353, abs=1e-4)


@pytest.mark.parametrize(
    "price,density,efficiency",
    [
        (0.0, 1700.0, 0.75),
        (-1.0, 1700.0, 0.75),
        (132.0, 0.0, 0.75),
        (132.0, -1700.0, 0.75),
        (132.0, 1700.0, 0.0),
        (132.0, 1700.0, -0.75),
    ],
)
def test_wood_cost_raises_on_non_positive_inputs(price, density, efficiency):
    with pytest.raises(ValueError):
        wood_cost_eur_per_kwh(price, density, efficiency)


# --------------------------------------------------------------------------- #
# electric_cost — never a silent COP=1 fallback
# --------------------------------------------------------------------------- #


def test_electric_cost_none_buy_is_none():
    assert electric_cost(None, 3.0) is None


def test_electric_cost_none_cop_is_none():
    # The core anti-hack rule: unknown COP must NOT default to 1.
    assert electric_cost(0.05, None) is None


def test_electric_cost_zero_cop_is_none():
    assert electric_cost(0.05, 0.0) is None


def test_electric_cost_negative_cop_is_none():
    assert electric_cost(0.05, -1.0) is None


def test_electric_cost_normal_division():
    assert electric_cost(0.10, 4.0) == pytest.approx(0.025)


# --------------------------------------------------------------------------- #
# rank
# --------------------------------------------------------------------------- #


def test_rank_orders_ascending_by_cost():
    a = _snapshot("a", "A", 5.0, 0.05)
    b = _snapshot("b", "B", 5.0, 0.02)
    c = _snapshot("c", "C", 5.0, 0.08)
    ranked, excluded = rank([a, b, c])
    assert [s.subentry_id for s in ranked] == ["b", "a", "c"]
    assert excluded == []


def test_rank_excludes_unknown_cost_preserving_input_order():
    known = _snapshot("known", "Known", 5.0, 0.03)
    unknown1 = _snapshot("u1", "Unknown 1", 5.0, None)
    unknown2 = _snapshot("u2", "Unknown 2", 0.0, None)
    ranked, excluded = rank([unknown1, known, unknown2])
    assert [s.subentry_id for s in ranked] == ["known"]
    assert [s.subentry_id for s in excluded] == ["u1", "u2"]


def test_rank_includes_zero_capacity_sources():
    zero_cap = _snapshot("zero", "Zero capacity", 0.0, 0.01)
    normal = _snapshot("normal", "Normal", 5.0, 0.05)
    ranked, _excluded = rank([normal, zero_cap])
    assert [s.subentry_id for s in ranked] == ["zero", "normal"]


# --------------------------------------------------------------------------- #
# merit_order
# --------------------------------------------------------------------------- #


def test_merit_order_zero_demand():
    ranked, _ = rank([_snapshot("a", "A", 5.0, 0.05)])
    allocations, covered, blended = merit_order(0.0, ranked)
    assert allocations == []
    assert covered is True
    assert blended is None


def test_merit_order_negative_demand_treated_as_zero():
    ranked, _ = rank([_snapshot("a", "A", 5.0, 0.05)])
    allocations, covered, blended = merit_order(-3.0, ranked)
    assert allocations == []
    assert covered is True
    assert blended is None


def test_merit_order_exact_split_across_two_sources():
    cheap = _snapshot("cheap", "Cheap", 5.0, 0.02)
    mid = _snapshot("mid", "Mid", 10.0, 0.05)
    ranked, _ = rank([mid, cheap])  # rank() will sort cheap first

    allocations, covered, blended = merit_order(8.0, ranked)

    assert covered is True
    assert len(allocations) == 2
    cheap_alloc, mid_alloc = allocations
    assert cheap_alloc == Allocation("cheap", "Cheap", 5.0, 1.0, 0.02)
    assert mid_alloc.subentry_id == "mid"
    assert mid_alloc.kw == pytest.approx(3.0)
    assert mid_alloc.fraction == pytest.approx(0.3)
    assert mid_alloc.cost_eur_per_kwh == pytest.approx(0.05)

    expected_blend = (5.0 * 0.02 + 3.0 * 0.05) / 8.0
    assert blended == pytest.approx(expected_blend)


def test_merit_order_shortfall_when_capacity_insufficient():
    cheap = _snapshot("cheap", "Cheap", 5.0, 0.02)
    mid = _snapshot("mid", "Mid", 10.0, 0.05)
    ranked, _ = rank([mid, cheap])

    allocations, covered, blended = merit_order(20.0, ranked)

    assert covered is False
    total_kw = sum(a.kw for a in allocations)
    assert total_kw == pytest.approx(15.0)
    expected_blend = (5.0 * 0.02 + 10.0 * 0.05) / 15.0
    assert blended == pytest.approx(expected_blend)


def test_merit_order_skips_zero_capacity_source():
    zero_cap = _snapshot("zero", "Zero capacity", 0.0, 0.01)  # cheapest but undispatchable
    normal = _snapshot("normal", "Normal", 5.0, 0.05)
    ranked, _ = rank([normal, zero_cap])
    assert ranked[0].subentry_id == "zero"  # ranked, but...

    allocations, covered, blended = merit_order(3.0, ranked)

    assert covered is True
    assert len(allocations) == 1
    assert allocations[0].subentry_id == "normal"
    assert blended == pytest.approx(0.05)


def test_merit_order_no_dispatchable_capacity_leaves_blended_none():
    zero_cap = _snapshot("zero", "Zero capacity", 0.0, 0.01)
    allocations, covered, blended = merit_order(3.0, [zero_cap])
    assert allocations == []
    assert covered is False
    assert blended is None
