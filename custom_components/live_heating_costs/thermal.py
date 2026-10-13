"""Bounded two-state thermal shadow model, independent of Home Assistant.

Heat enters the air node; the fabric exchanges heat with it. Implicit substeps
preserve stability and account for storage in the energy balance exactly once.
Parameters are priors until independently calibrated, never a claim of measured heat.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta


def require_utc(value: datetime) -> None:
    """Reject wall-clock inputs; elapsed-time calculations require aware UTC."""
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be timezone-aware UTC")


def _finite(*values: float) -> None:
    if not all(math.isfinite(v) for v in values):
        raise ValueError("thermal inputs must be finite")


class _Serializable:
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


@dataclass(frozen=True)
class ThermalParameters(_Serializable):
    envelope_kw_per_c: float
    air_capacity_kwh_per_c: float
    fabric_capacity_kwh_per_c: float
    coupling_kw_per_c: float

    def __post_init__(self) -> None:
        _finite(
            self.envelope_kw_per_c,
            self.air_capacity_kwh_per_c,
            self.fabric_capacity_kwh_per_c,
            self.coupling_kw_per_c,
        )
        if self.envelope_kw_per_c < 0 or self.coupling_kw_per_c < 0:
            raise ValueError("conductances must be nonnegative")
        if self.air_capacity_kwh_per_c <= 0 or self.fabric_capacity_kwh_per_c <= 0:
            raise ValueError("thermal capacities must be positive")


@dataclass(frozen=True)
class ThermalState(_Serializable):
    air_c: float
    fabric_c: float

    def __post_init__(self) -> None:
        _finite(self.air_c, self.fabric_c)


@dataclass(frozen=True)
class ThermalInputs(_Serializable):
    outdoor_c: float
    heat_kw: float = 0.0
    gains_kw: float = 0.0
    ventilation_kw_per_c: float = 0.0
    ventilation_kw: float = 0.0
    fabric_heat_kw: float = 0.0

    def __post_init__(self) -> None:
        _finite(
            self.outdoor_c,
            self.heat_kw,
            self.gains_kw,
            self.ventilation_kw_per_c,
            self.ventilation_kw,
            self.fabric_heat_kw,
        )
        if min(self.heat_kw, self.gains_kw, self.ventilation_kw_per_c, self.fabric_heat_kw) < 0:
            raise ValueError("heat, gains and ventilation conductance must be nonnegative")


@dataclass(frozen=True)
class ThermalStep:
    state: ThermalState
    heat_kwh: float
    gains_kwh: float
    loss_kwh: float
    storage_change_kwh: float
    ventilation_kwh: float = 0.0


def advance(
    state: ThermalState,
    parameters: ThermalParameters,
    inputs: ThermalInputs,
    start: datetime,
    end: datetime,
) -> ThermalStep:
    """Integrate at most six hours, in stable substeps of at most one minute."""
    require_utc(start)
    require_utc(end)
    seconds = (end - start).total_seconds()
    if seconds < 0 or seconds > 21600:
        raise ValueError("thermal window must be between zero and six hours")
    if seconds == 0:
        return ThermalStep(state, 0.0, 0.0, 0.0, 0.0)
    n = math.ceil(seconds / 60)
    h = seconds / n / 3600
    ca, cm = parameters.air_capacity_kwh_per_c, parameters.fabric_capacity_kwh_per_c
    coupling = parameters.coupling_kw_per_c
    outside = parameters.envelope_kw_per_c + inputs.ventilation_kw_per_c
    a, b = 1 + h * (outside + coupling) / ca, -h * coupling / ca
    c, d = -h * coupling / cm, 1 + h * coupling / cm
    det = a * d - b * c
    ta, tm, loss = state.air_c, state.fabric_c, 0.0
    for _ in range(n):
        rhs = (
            ta
            + h
            * (
                outside * inputs.outdoor_c
                + inputs.heat_kw
                + inputs.gains_kw
                + inputs.ventilation_kw
            )
            / ca
        )
        fabric_rhs = tm + h * inputs.fabric_heat_kw / cm
        ta, tm = (d * rhs - b * fabric_rhs) / det, (a * fabric_rhs - c * rhs) / det
        loss += h * outside * (ta - inputs.outdoor_c)
    storage = ca * (ta - state.air_c) + cm * (tm - state.fabric_c)
    hours = seconds / 3600
    return ThermalStep(
        ThermalState(ta, tm),
        (inputs.heat_kw + inputs.fabric_heat_kw) * hours,
        inputs.gains_kw * hours,
        loss,
        storage,
        inputs.ventilation_kw * hours,
    )


@dataclass(frozen=True)
class ThermalDemand:
    total_kw: float
    continuation_kw: float | None
    remaining_kw: float | None
    storage_kw: float
    predicted_air_c: float
    target_reachable: bool
    fabric_heat_kw: float = 0.0
    air_heat_kw: float = 0.0


def target_demand(
    state: ThermalState,
    parameters: ThermalParameters,
    inputs: ThermalInputs,
    now: datetime,
    *,
    target_c: float = 21.0,
    horizon_s: float = 3600.0,
    current_heat_kw: float | None = None,
    max_heat_kw: float = 100.0,
) -> ThermalDemand:
    """Constant delivered heat required to reach target at the horizon.

    Total includes committed fabric heat plus the solved air heat. Continuation
    and remaining refer only to direct air heating; committed fabric heat is
    exposed separately. Unknown current heat keeps both those values unknown.
    Storage is explanatory and must never be added to total a second time.
    """
    _finite(target_c, horizon_s, max_heat_kw)
    if current_heat_kw is not None:
        _finite(current_heat_kw)
        if current_heat_kw < 0:
            raise ValueError("current heat must be nonnegative")
    if not 0 < horizon_s <= 21600 or max_heat_kw < 0:
        raise ValueError("invalid demand horizon or heat bounds")
    end = now + timedelta(seconds=horizon_s)

    def simulate(heat: float) -> ThermalStep:
        return advance(
            state,
            parameters,
            ThermalInputs(
                inputs.outdoor_c,
                heat,
                inputs.gains_kw,
                inputs.ventilation_kw_per_c,
                inputs.ventilation_kw,
                inputs.fabric_heat_kw,
            ),
            now,
            end,
        )

    lo, hi = 0.0, max_heat_kw
    passive, upper = simulate(lo), simulate(hi)
    reachable = upper.state.air_c >= target_c
    if passive.state.air_c >= target_c:
        hi = 0.0
    elif reachable:
        # This fixed-parameter RC step is affine in heat: two trajectories
        # determine the required input without an iterative optimiser.
        hi *= (target_c - passive.state.air_c) / (upper.state.air_c - passive.state.air_c)
    result = simulate(hi)
    continuation = min(hi, current_heat_kw) if current_heat_kw is not None else None
    remaining = max(0.0, hi - continuation) if continuation is not None else None
    return ThermalDemand(
        hi + inputs.fabric_heat_kw,
        continuation,
        remaining,
        result.storage_change_kwh / (horizon_s / 3600),
        result.state.air_c,
        reachable,
        inputs.fabric_heat_kw,
        hi,
    )


@dataclass(frozen=True)
class WeightedIndoorReading:
    entity_id: str
    temp_c: float | None
    weight: float = 1.0
    offset_c: float = 0.0
    observed_at: datetime | None = None
    calibrated: bool = False

    def __post_init__(self) -> None:
        _finite(self.weight, self.offset_c)
        if self.weight <= 0:
            raise ValueError("indoor weights must be positive")
        if self.observed_at is not None:
            require_utc(self.observed_at)


@dataclass(frozen=True)
class IndoorEstimate:
    temp_c: float | None
    coverage: float
    used: tuple[str, ...]
    missing: tuple[str, ...]
    calibrated: bool


def aggregate_indoor(
    readings: Sequence[WeightedIndoorReading],
    now: datetime,
    *,
    minimum_coverage: float = 0.75,
    maximum_age_s: float = 900.0,
) -> IndoorEstimate:
    """Weighted calibrated temperatures; missing rooms reduce coverage, never read zero."""
    require_utc(now)
    _finite(minimum_coverage, maximum_age_s)
    if not 0 <= minimum_coverage <= 1 or maximum_age_s <= 0:
        raise ValueError("invalid indoor coverage/age limits")
    if len({r.entity_id for r in readings}) != len(readings):
        raise ValueError("duplicate indoor sensor")
    total = sum(r.weight for r in readings)
    used, missing, weight, numerator = [], [], 0.0, 0.0
    calibrated = bool(readings)
    for r in readings:
        age = (now - r.observed_at).total_seconds() if r.observed_at is not None else 0.0
        value = None if r.temp_c is None else r.temp_c + r.offset_c
        if (
            value is None
            or not math.isfinite(value)
            or not 0 <= value <= 45
            or not 0 <= age <= maximum_age_s
        ):
            missing.append(r.entity_id)
            continue
        used.append(r.entity_id)
        weight += r.weight
        numerator += value * r.weight
        calibrated = calibrated and r.calibrated and r.observed_at is not None
    coverage = weight / total if total else 0.0
    temp = numerator / weight if weight and coverage >= minimum_coverage else None
    return IndoorEstimate(
        temp, coverage, tuple(used), tuple(missing), calibrated and not missing and temp is not None
    )


@dataclass(frozen=True)
class DispatchSource:
    source_id: str
    capacity_kw: float
    cost_eur_per_kwh: float | None
    reserved_kw: float = 0.0
    eligible: bool = True

    def __post_init__(self) -> None:
        _finite(self.capacity_kw, self.reserved_kw)
        if min(self.capacity_kw, self.reserved_kw) < 0 or self.reserved_kw > self.capacity_kw:
            raise ValueError("invalid source capacity/reservation")
        if self.cost_eur_per_kwh is not None:
            _finite(self.cost_eur_per_kwh)


@dataclass(frozen=True)
class HeadroomAllocation:
    allocations: tuple[tuple[str, float], ...]
    uncovered_kw: float


def allocate_headroom(demand_kw: float, sources: Sequence[DispatchSource]) -> HeadroomAllocation:
    """Reserve local loads before allocating the additional common-zone demand."""
    _finite(demand_kw)
    if demand_kw < 0 or len({s.source_id for s in sources}) != len(sources):
        raise ValueError("invalid demand or duplicate source")
    remaining, allocations = demand_kw, []
    ranked = sorted(
        (s for s in sources if s.eligible and s.cost_eur_per_kwh is not None),
        key=lambda s: (s.cost_eur_per_kwh, s.source_id),
    )
    for source in ranked:
        assigned = min(remaining, source.capacity_kw - source.reserved_kw)
        if assigned > 0:
            allocations.append((source.source_id, assigned))
            remaining -= assigned
    return HeadroomAllocation(tuple(allocations), remaining)
