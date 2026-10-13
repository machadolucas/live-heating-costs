"""Source coverage and committed heat constrain additional dispatch exactly once."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "live_heating_costs"
    / "allocation.py"
)
_spec = importlib.util.spec_from_file_location("ls_allocation", _PATH)
allocation = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = allocation
_spec.loader.exec_module(allocation)
Source = allocation.EligibleSource


def test_total_allocation_reserves_local_demand_before_cheaper_pump() -> None:
    sources = [Source("pump", 6, 0, 0.02), Source("floor", 3, 3, 0.1, open_house=False)]
    result = allocation.allocate(5, 2, sources)
    assert result["allocations"] == [
        {"source_id": "floor", "group": "local", "kw": 2},
        {"source_id": "pump", "group": "open_house", "kw": 3},
    ]
    assert result["uncovered_kw"] == 0
    assert result["total_kw"] == 5


def test_running_local_heat_reduces_local_requirement_not_global_remainder_again() -> None:
    """The supplied 3 kW already excludes 1 kW floor + 1 kW pump continuation."""
    sources = [
        Source("pump", 5, 0, 0.02, current_kw=1),
        Source("floor", 3, 3, 0.1, current_kw=1, open_house=False),
    ]
    result = allocation.allocate(3, 2, sources, additional=True)
    assert result["local_kw"] == 1
    assert result["local_requirement_kw"] == 2
    assert result["committed_local_kw"] == 1
    assert result["allocations"] == [
        {"source_id": "floor", "group": "local", "kw": 1},
        {"source_id": "pump", "group": "open_house", "kw": 2},
    ]
    assert sum(row["kw"] for row in result["allocations"]) == 3
    assert result["uncovered_kw"] == 0


def test_satisfied_local_load_does_not_request_it_a_second_time() -> None:
    sources = [
        Source("pump", 5, 0, 0.02),
        Source("floor", 2, 2, 0.1, current_kw=2, open_house=False),
    ]
    result = allocation.allocate(1, 2, sources, additional=True)
    assert result["local_kw"] == 0
    assert result["total_kw"] == 1
    assert result["allocations"] == [{"source_id": "pump", "group": "open_house", "kw": 1}]
    assert result["uncovered_kw"] == 0


def test_existing_output_reduces_total_source_headroom_once() -> None:
    sources = [Source("pump", 5, 0, 0.02, current_kw=4)]
    result = allocation.allocate(2, 0, sources, additional=True)
    assert result["allocations"] == [{"source_id": "pump", "group": "open_house", "kw": 1}]
    assert result["uncovered_kw"] == 1


def test_dual_purpose_existing_heat_requires_an_identified_local_split() -> None:
    sources = [Source("shared", 5, 2, 0.02, current_kw=2)]
    result = allocation.allocate(3, 2, sources, additional=True)
    assert result["reason"] == "local_remaining_unknown"
    assert result["allocations"] == []
    assert result["uncovered_kw"] is None


def test_explicit_dual_purpose_split_preserves_local_and_total_headroom() -> None:
    sources = [Source("shared", 5, 2, 0.02, current_kw=2, current_local_kw=0.5)]
    result = allocation.allocate(3, 2, sources, additional=True)
    assert result["allocations"] == [
        {"source_id": "shared", "group": "local", "kw": 1.5},
        {"source_id": "shared", "group": "open_house", "kw": 1.5},
    ]
    assert result["uncovered_kw"] == 0


def test_unknown_current_heat_cannot_supply_additional_headroom() -> None:
    sources = [Source("unknown", 10, 0, 0.01, current_kw=None), Source("known", 1, 0, 0.1)]
    result = allocation.allocate(3, 0, sources, additional=True)
    assert result["allocations"] == [{"source_id": "known", "group": "open_house", "kw": 1}]
    assert result["uncovered_kw"] == 2


def test_unknown_local_current_heat_blocks_local_remaining_claim() -> None:
    sources = [Source("floor", 3, 3, 0.1, current_kw=None, open_house=False)]
    result = allocation.allocate(2, 2, sources, additional=True)
    assert result["reason"] == "local_remaining_unknown"
    assert result["uncovered_kw"] is None


def test_slow_heat_input_is_not_assumed_to_be_current_local_release() -> None:
    sources = [Source("slab", 3, 3, 0.1, current_kw=1, open_house=False, response_minutes=60)]
    result = allocation.allocate(2, 2, sources, additional=True)
    assert result["reason"] == "local_remaining_unknown"


def test_slow_source_never_receives_new_short_horizon_dispatch() -> None:
    sources = [Source("slab", 3, 3, 0.1, response_minutes=60), Source("pump", 4, 0, 0.02)]
    result = allocation.allocate(3, 1, sources, additional=True)
    assert result["allocations"] == [{"source_id": "pump", "group": "open_house", "kw": 2}]
    assert result["uncovered_kw"] == 1


def test_unknown_cost_excludes_new_dispatch_but_not_known_committed_local_heat() -> None:
    sources = [
        Source("floor", 2, 2, None, current_kw=2, open_house=False),
        Source("pump", 4, 0, 0.02),
    ]
    result = allocation.allocate(1, 2, sources, additional=True)
    assert result["local_kw"] == 0
    assert result["allocations"] == [{"source_id": "pump", "group": "open_house", "kw": 1}]


def test_zero_future_capacity_does_not_discard_measured_local_continuation() -> None:
    sources = [
        Source("stopping", 0, 0, None, current_kw=1, current_local_kw=1),
        Source("pump", 4, 0, 0.02),
    ]
    result = allocation.allocate(1, 2, sources, additional=True)
    assert result["local_kw"] == 1
    assert result["committed_local_kw"] == 1
    assert result["allocations"] == []
    assert result["uncovered_kw"] == 1


def test_local_requirement_larger_than_global_estimate_exposes_consistent_shortfall() -> None:
    result = allocation.allocate(1, 2, [Source("floor", 0.5, 0.5, 0.1, open_house=False)])
    assert result["total_kw"] == 2
    assert result["uncovered_kw"] == 1.5
    assert (
        sum(row["kw"] for row in result["allocations"]) + result["uncovered_kw"]
        == result["total_kw"]
    )


def test_missing_local_requirement_keeps_coverage_unknown() -> None:
    result = allocation.allocate(5, None, [Source("pump", 5, 0, 0.02)])
    assert result["uncovered_kw"] is None
    assert result["reason"] == "local_requirement_unknown"


@pytest.mark.parametrize(
    "source",
    [
        Source("invalid", -1, 0, 0.1),
        Source("invalid", 1, 2, 0.1),
        Source("invalid", 1, 0, 0.1, current_kw=-1),
        Source("invalid", 1, 0, 0.1, response_minutes=-1),
        Source("invalid", 2, 1, 0.1, current_kw=1, current_local_kw=2),
        Source("invalid", 2, 1, 0.1, current_kw=None, current_local_kw=1),
        Source("invalid", float("nan"), 0, 0.1),
    ],
)
def test_invalid_capacity_or_local_accounting_is_rejected(source) -> None:
    with pytest.raises(ValueError):
        allocation.allocate(1, 0, [source], additional=True)


def test_duplicate_source_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        allocation.allocate(1, 0, [Source("same", 1, 0, 0.1), Source("same", 1, 0, 0.2)])
