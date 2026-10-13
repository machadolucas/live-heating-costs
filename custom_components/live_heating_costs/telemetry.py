"""Electrical-counter quality gates with explicit energy provenance.

Reject resets, long gaps and impossible jumps before COP conversion. A rejected
interval must rebaseline the caller's counter; it must not be caught up later.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .thermal import require_utc

PROVENANCES = frozenset({"measured", "modelled"})


@dataclass(frozen=True)
class CounterDelta:
    delta_kwh: float | None
    valid: bool
    reason: str | None
    provenance: str = "measured"


def validate_counter_delta(
    previous_kwh: float | None,
    current_kwh: float | None,
    start: datetime,
    end: datetime,
    max_power_kw: float,
    *,
    maximum_gap_s: float = 600.0,
    tolerance: float = 1.05,
) -> CounterDelta:
    """Validate an independent cumulative meter over the supplied interval."""
    require_utc(start)
    require_utc(end)
    if not all(math.isfinite(v) for v in (max_power_kw, maximum_gap_s, tolerance)):
        raise ValueError("counter limits must be finite")
    if max_power_kw <= 0 or maximum_gap_s <= 0 or tolerance < 1:
        raise ValueError("invalid counter limits")
    elapsed = (end - start).total_seconds()
    reason = None
    if elapsed <= 0:
        reason = "nonpositive_interval"
    elif elapsed > maximum_gap_s:
        reason = "sampling_gap"
    elif previous_kwh is None or current_kwh is None:
        reason = "missing_counter"
    elif not all(math.isfinite(v) and v >= 0 for v in (previous_kwh, current_kwh)):
        reason = "invalid_counter"
    elif current_kwh < previous_kwh:
        reason = "counter_reset"
    elif current_kwh - previous_kwh > max_power_kw * elapsed / 3600 * tolerance:
        reason = "implausible_increment"
    if reason is not None:
        return CounterDelta(None, False, reason)
    return CounterDelta(current_kwh - previous_kwh, True, None)


@dataclass(frozen=True)
class EnergyContribution:
    meter_id: str
    electric_kwh: float | None
    delivered_kwh: float | None
    provenance: str
    valid: bool = True

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise ValueError("unknown delivered-heat provenance")
        for value in (self.electric_kwh, self.delivered_kwh):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("energy must be finite and nonnegative")


def delivered_energy(
    meter_id: str, delta: CounterDelta, cop: float | None, *, resistive: bool = False
) -> EnergyContribution:
    """COP-derived pump heat stays modelled; resistance uses measured electricity."""
    provenance = "measured" if resistive else "modelled"
    if cop is None or not math.isfinite(cop) or cop <= 0 or (resistive and cop != 1):
        return EnergyContribution(meter_id, delta.delta_kwh, None, provenance, False)
    valid = delta.valid and delta.delta_kwh is not None
    return EnergyContribution(
        meter_id, delta.delta_kwh, delta.delta_kwh * cop if valid else None, provenance, valid
    )


@dataclass(frozen=True)
class EnergyAggregate:
    electric_kwh: float | None
    delivered_kwh: float | None
    measured_heat_kwh: float
    modelled_heat_kwh: float
    coverage: float
    valid: bool
    missing: tuple[str, ...]


def aggregate_energy(
    contributions: Sequence[EnergyContribution], expected_meter_ids: Sequence[str]
) -> EnergyAggregate:
    """Aggregate independent meters once; require the whole expected set for totals."""
    expected = set(expected_meter_ids)
    if len(expected) != len(expected_meter_ids):
        raise ValueError("duplicate expected meter")
    by_id = {c.meter_id: c for c in contributions}
    if len(by_id) != len(contributions) or not set(by_id) <= expected:
        raise ValueError("duplicate or unexpected meter contribution")
    good = [
        c
        for c in contributions
        if c.valid and c.electric_kwh is not None and c.delivered_kwh is not None
    ]
    covered = {c.meter_id for c in good}
    missing = tuple(sorted(expected - covered))
    valid = bool(expected) and not missing
    measured = sum(c.delivered_kwh for c in good if c.provenance == "measured")
    modelled = sum(c.delivered_kwh for c in good if c.provenance == "modelled")
    return EnergyAggregate(
        sum(c.electric_kwh for c in good) if valid else None,
        measured + modelled if valid else None,
        measured,
        modelled,
        len(covered) / len(expected) if expected else 0.0,
        valid,
        missing,
    )
