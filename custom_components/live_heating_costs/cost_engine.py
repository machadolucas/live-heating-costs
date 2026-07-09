"""Pure cost-comparison engine for the Live Heating Costs integration.

Turns a snapshot of each configured heat source (capacity, COP, delivered-heat
cost) into a ranking and a cheapest-first "merit order" allocation of a given
heat demand across sources — the calculation behind the replacement card
described in ``docs/heating-costs.md`` ("Cost math spec").

Money is **EUR/kWh** throughout (never cents — the ×100 for display happens at
the sensor layer). COP is dimensionless. An unknown COP or price propagates as
``None`` cost, which excludes the source from ranking/allocation — there is
**never** a silent fallback to COP = 1 (see the const.py module docstring).
This module has no Home Assistant import and no ``datetime.now()``; it is a
set of deterministic functions over caller-supplied snapshots.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Below this many kW of remaining demand we treat the demand as "covered" —
# guards against float noise leaving an infinitesimal shortfall.
_EPS_KW = 1e-9


@dataclass(frozen=True)
class SourceSnapshot:
    """A heat source's state at one instant, ready for ranking/allocation.

    ``capacity_kw`` is 0.0 when the source is non-dispatchable right now (e.g.
    a heat pump below its operating limit). ``cop`` is ``None`` when not
    applicable (wood) or unknown; ``cost_eur_per_kwh`` is ``None`` whenever the
    cost cannot be honestly computed, which excludes the source from ranking.
    """

    subentry_id: str
    name: str
    source_type: str  # one of const.SOURCE_TYPE_*
    capacity_kw: float
    cop: float | None
    cost_eur_per_kwh: float | None
    available: bool
    unavailable_reason: str | None  # one of const.REASON_* or None
    modeled_cop: float | None = None
    measured_cop: float | None = None
    heating_fraction: float = 0.0
    active: bool = False


def wood_cost_eur_per_kwh(
    price_per_unit_eur: float, energy_density_kwh_per_unit: float, efficiency: float
) -> float:
    """Delivered-heat cost of a fuel source: price / (energy density × efficiency).

    All three inputs must be strictly positive (a non-positive price, density
    or efficiency is a configuration error, not an "unknown cost" case).
    """
    if price_per_unit_eur <= 0:
        raise ValueError(f"price_per_unit_eur must be > 0, got {price_per_unit_eur}")
    if energy_density_kwh_per_unit <= 0:
        raise ValueError(
            f"energy_density_kwh_per_unit must be > 0, got {energy_density_kwh_per_unit}"
        )
    if efficiency <= 0:
        raise ValueError(f"efficiency must be > 0, got {efficiency}")
    return price_per_unit_eur / (energy_density_kwh_per_unit * efficiency)


def electric_cost(buy_eur_per_kwh: float | None, cop: float | None) -> float | None:
    """Delivered-heat cost of an electric source: buy price / COP.

    Unknown propagates: ``None`` in (either argument) or a non-positive COP
    yields ``None`` out. There is **no** fallback to COP = 1.
    """
    if buy_eur_per_kwh is None or cop is None:
        return None
    if cop <= 0:
        return None
    return buy_eur_per_kwh / cop


def rank(
    snapshots: Sequence[SourceSnapshot],
) -> tuple[list[SourceSnapshot], list[SourceSnapshot]]:
    """Split snapshots into (ranked ascending by cost, excluded — cost unknown).

    Capacity-0 sources are still ranked (their cost is comparable even if they
    can't currently be dispatched); only an unknown ``cost_eur_per_kwh`` (None)
    excludes a source. ``excluded`` preserves input order.
    """
    ranked = sorted(
        (s for s in snapshots if s.cost_eur_per_kwh is not None),
        key=lambda s: s.cost_eur_per_kwh,
    )
    excluded = [s for s in snapshots if s.cost_eur_per_kwh is None]
    return ranked, excluded


@dataclass(frozen=True)
class Allocation:
    """A slice of demand assigned to one source in merit-order dispatch."""

    subentry_id: str
    name: str
    kw: float
    fraction: float  # fraction of *this source's* capacity used
    cost_eur_per_kwh: float


def merit_order(
    demand_kw: float, ranked: Sequence[SourceSnapshot]
) -> tuple[list[Allocation], bool, float | None]:
    """Fill ``demand_kw`` cheapest-first from ``ranked`` (ascending by cost).

    Capacity-0 sources and sources with unknown cost are skipped (never
    allocated, even if present in ``ranked``). Stops as soon as demand is
    covered.

    Returns ``(allocations, demand_covered, blended_cost)``: ``blended_cost``
    is the demand-weighted average €/kWh of what was actually allocated, or
    ``None`` if there was no demand or nothing could be allocated.
    """
    if demand_kw <= 0:
        return [], True, None

    remaining = demand_kw
    allocations: list[Allocation] = []
    weighted_cost_sum = 0.0
    allocated_kw = 0.0

    for source in ranked:
        if remaining <= _EPS_KW:
            break
        if source.capacity_kw <= 0 or source.cost_eur_per_kwh is None:
            continue
        take = min(source.capacity_kw, remaining)
        allocations.append(
            Allocation(
                subentry_id=source.subentry_id,
                name=source.name,
                kw=take,
                fraction=take / source.capacity_kw,
                cost_eur_per_kwh=source.cost_eur_per_kwh,
            )
        )
        weighted_cost_sum += take * source.cost_eur_per_kwh
        allocated_kw += take
        remaining -= take

    demand_covered = remaining <= _EPS_KW
    blended_cost = weighted_cost_sum / allocated_kw if allocated_kw > 0 else None
    return allocations, demand_covered, blended_cost
