"""Measured/modeled COP blending for heat-pump sources.

The legacy card computed an "instant COP" as ``modeled_capacity / max(power,
floor)``. When a pump is idle, ``power`` collapses to (near) zero, the floor
takes over, and COP is pinned to a meaningless ~20-22 — which then makes the
delivered-heat cost look absurdly cheap (~0.24 c/kWh) while the pump isn't
running at all.

This module fixes that by keeping the *modeled* COP(T) curve as the answer for
"what would running now cost", and only blending in a *measured* COP — derived
by reconciling the modeled curve against metered electricity over a rolling
window — weighted by how much of that window the pump was actually active. An
idle window has a heating fraction of 0, so the blend collapses to the pure
modeled COP; a window spent entirely heating collapses to the pure measured
COP.

The asymmetry in :func:`window_cop` is the point: defrost and standby
electricity add to the metered-energy denominator with zero modeled-heat
numerator, dragging the measured COP below the modeled one — that drag *is*
the defrost/cycling penalty the legacy floor hack could never see.

No Home Assistant imports; all datetimes must be timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .const import MIN_WINDOW_ENERGY_KWH

_ACTIVE_HVAC_ACTIONS = {"heating", "defrosting"}


@dataclass(frozen=True)
class Sample:
    """One telemetry sample for a heat-pump source.

    ``hvac_action`` is ``None`` when the source has no climate telemetry at
    all (e.g. the Sensibo-driven old ILP) — in that case activity/heat
    detection falls back to the metered power.
    """

    ts: datetime
    power_w: float | None
    energy_kwh: float | None
    temp_c: float | None
    hvac_action: str | None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError(f"Sample.ts is not timezone-aware: {self.ts!r}")


@dataclass(frozen=True)
class WindowCop:
    """Result of reconciling a window of samples into a measured COP.

    ``measured_cop`` is ``None`` when the window is unusable: too little
    metered energy, a counter reset, fewer than 2 usable samples, or a
    heating segment whose temperature (hence modeled heat) is unknown.
    """

    measured_cop: float | None
    heating_fraction: float
    energy_kwh: float


def is_active(sample: Sample, standby_w: float) -> bool:
    """True if the pump is doing work (heating *or* defrosting)."""
    if sample.hvac_action is not None:
        return sample.hvac_action in _ACTIVE_HVAC_ACTIONS
    return sample.power_w is not None and sample.power_w > standby_w


def is_producing_heat(sample: Sample, standby_w: float) -> bool:
    """True if the pump is actually delivering heat (defrost excluded).

    Without climate telemetry there is no way to distinguish defrosting from
    heating, so the power-only fallback is identical to :func:`is_active`.
    """
    if sample.hvac_action is not None:
        return sample.hvac_action == "heating"
    return sample.power_w is not None and sample.power_w > standby_w


def window_cop(
    samples: Sequence[Sample],
    cop_at: Callable[[float], float],
    standby_w: float,
    min_energy_kwh: float = MIN_WINDOW_ENERGY_KWH,
) -> WindowCop:
    """Reconcile a modeled COP(T) curve against metered energy over a window.

    Samples need not be pre-sorted (sorted defensively). Per consecutive pair,
    the *leading* sample's activity/temperature is used to attribute that
    segment's energy delta — a reasonable approximation at the ~1-minute
    sampling granularity this is designed for.

    Zero numerator is only right for energy *known* not to make heat
    (defrost/standby). Energy spent heating at an unknown temperature is heat
    we just can't quantify; counting it as zero would read as a spurious COP
    collapse, so such a window yields no measurement at all and the blend
    falls back to the modeled COP.
    """
    ordered = sorted(samples, key=lambda s: s.ts)
    if len(ordered) < 2:
        return WindowCop(measured_cop=None, heating_fraction=0.0, energy_kwh=0.0)

    active_seconds = 0.0
    total_seconds = 0.0
    numerator = 0.0
    total_delta_e = 0.0
    counter_reset = False
    unquantified_heat = False

    for a, b in zip(ordered, ordered[1:], strict=False):
        seg_seconds = (b.ts - a.ts).total_seconds()
        if seg_seconds > 0:
            total_seconds += seg_seconds
            if is_active(a, standby_w):
                active_seconds += seg_seconds

        if a.energy_kwh is None or b.energy_kwh is None:
            continue
        delta_e = b.energy_kwh - a.energy_kwh
        if delta_e < 0:
            counter_reset = True
            continue
        total_delta_e += delta_e
        if delta_e > 0 and is_producing_heat(a, standby_w):
            if a.temp_c is None:
                unquantified_heat = True
            else:
                numerator += cop_at(a.temp_c) * delta_e

    heating_fraction = active_seconds / total_seconds if total_seconds > 0 else 0.0

    if counter_reset:
        return WindowCop(measured_cop=None, heating_fraction=heating_fraction, energy_kwh=0.0)
    if unquantified_heat or total_delta_e < min_energy_kwh:
        return WindowCop(
            measured_cop=None, heating_fraction=heating_fraction, energy_kwh=total_delta_e
        )
    return WindowCop(
        measured_cop=numerator / total_delta_e,
        heating_fraction=heating_fraction,
        energy_kwh=total_delta_e,
    )


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def blend_cop(
    modeled: float | None,
    measured: float | None,
    heating_fraction: float,
    min_cop: float,
    max_cop: float,
) -> float | None:
    """Blend modeled and measured COP, weighted by the measured heating fraction.

    No modeled COP (below the pump's operating limit) means no answer at all,
    regardless of measurement. No usable measurement falls back to the pure
    modeled COP (clamped) — this is what makes idle cost sensible instead of a
    floored-power artifact.
    """
    if modeled is None:
        return None
    if measured is None:
        return _clamp(modeled, min_cop, max_cop)
    f = _clamp(heating_fraction, 0.0, 1.0)
    blended = f * measured + (1.0 - f) * modeled
    return _clamp(blended, min_cop, max_cop)
