"""Calibrated ventilation flow and signed sensible heat at the house boundary.

Control is not airflow. Manufacturer measurements describe a fan's pressure
and electrical power at several flows; an installation's duct resistance
selects its operating point. Both the installed model and controller mapping
must be verified before a calibrated curve may produce an airflow. One total
watt reading cannot determine the two fans' flows, so it is only a residual
check and is never inverted to manufacture a second airflow.

The shipped numeric table comes from the manufacturer's laboratory workbook.
Interpolation stays inside the common measured flow range of adjacent control
curves, including the 40 percent lower limit. Bounds describe that table's
domain, not a confidence interval for an uncalibrated installation.

Heat is positive into the house. Supply air exchanges heat relative to indoor
air; excess extract requires cold outdoor makeup air. Heat recovered inside
the appliance is not added as another source, and appliance electricity is
not automatically counted as an indoor gain. Pure, HA-free; no clock access.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

FanSide = Literal["supply", "extract"]
_AIR_HEAT_W_PER_LS_K = 1.2 * 1006.0 / 1000.0


@dataclass(frozen=True)
class FanEstimate:
    """Installation flow or an explicit reason why it remains unknown."""

    side: FanSide
    control_percent: float | None
    flow_ls: float | None = None
    flow_low_ls: float | None = None
    flow_high_ls: float | None = None
    power_w: float | None = None
    power_low_w: float | None = None
    power_high_w: float | None = None
    calibrated: bool = False
    reason: str = "control_unavailable"


@dataclass(frozen=True)
class VentilationEstimate:
    """Separate heat, power, and calibration diagnostics; serializable with asdict."""

    supply: FanEstimate
    extract: FanEstimate
    sensible_kw: float | None
    makeup_loss_kw: float | None
    net_sensible_kw: float | None
    expected_fan_power_w: float | None
    electricity_residual_w: float | None
    calibrated: bool
    flags: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class _Curve:
    control_percent: float
    # Flow (l/s), external pressure (Pa), fan electricity (W).
    points: tuple[tuple[float, float, float], ...]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


@lru_cache(maxsize=1)
def _manufacturer() -> tuple[float, tuple[_Curve, ...], tuple[_Curve, ...]]:
    """Freeze the local table so callers cannot mutate shared measurements."""
    path = Path(__file__).with_name("data") / "parmair120.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported fan table schema")
    idle = _finite(data.get("idle_power_w"))
    if idle is None or idle < 0:
        raise ValueError("invalid manufacturer idle power")
    sides = []
    for side in ("supply", "extract"):
        curves = []
        for raw in data["curves"][side]:
            control = _finite(raw["control_percent"])
            points = tuple(
                tuple(_finite(point[key]) for key in ("flow_ls", "pressure_pa", "power_w"))
                for point in raw["points"]
            )
            if (
                control is None
                or not 40 <= control <= 100
                or len(points) < 2
                or any(value is None or value < 0 for point in points for value in point)
                or any(
                    b[0] <= a[0] or b[1] >= a[1] for a, b in zip(points, points[1:], strict=False)
                )
            ):
                raise ValueError("invalid manufacturer pressure curve")
            curves.append(_Curve(control, points))
        if (
            len(curves) < 2
            or curves[0].control_percent != 40
            or curves[-1].control_percent != 100
            or any(
                b.control_percent <= a.control_percent
                for a, b in zip(curves, curves[1:], strict=False)
            )
        ):
            raise ValueError("invalid manufacturer control domain")
        sides.append(tuple(curves))
    return idle, sides[0], sides[1]


# HA imports integration modules through its import executor. Preload there so
# the first sampling tick never opens the measurement file on the event loop.
_manufacturer()


def _value(curve: _Curve, flow: float, column: int) -> float:
    """Piecewise-linear raw measurement interpolation, with no extrapolation."""
    if not curve.points[0][0] <= flow <= curve.points[-1][0]:
        raise ValueError("flow outside measured curve")
    for a, b in zip(curve.points, curve.points[1:], strict=False):
        if a[0] <= flow <= b[0]:
            weight = (flow - a[0]) / (b[0] - a[0])
            return a[column] + weight * (b[column] - a[column])
    raise ValueError("flow outside measured curve")


def estimate_fan(
    side: FanSide,
    control_percent: float | None,
    resistance_pa_per_ls2: float | None = None,
    *,
    model_verified: bool = False,
    controller_verified: bool = False,
) -> FanEstimate:
    """Intersect the measured fan curve with calibrated ``pressure = k * flow²``.

    ``control_percent`` is the actual fan command, not the appliance's mode
    number or nominal mode speed. Zero explicitly means off. Nonzero commands
    below 40 percent have no measurements and never extrapolate. Resistance
    includes the installation's external duct/terminal/filter condition; a
    zero-resistance free-flow point also needs to lie in the measured domain.
    """
    if side not in ("supply", "extract"):
        raise ValueError("fan side must be supply or extract")
    control = _finite(control_percent)
    if control is None:
        return FanEstimate(side, None)
    if control == 0:
        return FanEstimate(side, control, 0, 0, 0, 0, 0, 0, False, "off")
    if not 40 <= control <= 100:
        return FanEstimate(side, control, reason="control_outside_measured_domain")

    _, supply, extract = _manufacturer()
    curves = supply if side == "supply" else extract
    below = max(
        (curve for curve in curves if curve.control_percent <= control),
        key=lambda c: c.control_percent,
    )
    above = min(
        (curve for curve in curves if curve.control_percent >= control),
        key=lambda c: c.control_percent,
    )
    weight = (
        (control - below.control_percent) / (above.control_percent - below.control_percent)
        if below != above
        else 0.0
    )
    low = max(below.points[0][0], above.points[0][0])
    high = min(below.points[-1][0], above.points[-1][0])
    if low >= high:
        return FanEstimate(side, control, reason="no_common_measured_domain")

    def interpolated(flow: float, column: int) -> float:
        return _value(below, flow, column) * (1 - weight) + _value(above, flow, column) * weight

    knots = {low, high}
    knots.update(
        point[0] for curve in (below, above) for point in curve.points if low <= point[0] <= high
    )
    powers = [interpolated(flow, 2) for flow in knots]
    common = {
        "side": side,
        "control_percent": control,
        "flow_low_ls": low,
        "flow_high_ls": high,
        "power_low_w": min(powers),
        "power_high_w": max(powers),
    }
    if model_verified is not True:
        return FanEstimate(**common, reason="model_unverified")
    if controller_verified is not True:
        return FanEstimate(**common, reason="controller_unverified")
    if resistance_pa_per_ls2 is None:
        return FanEstimate(**common, reason="resistance_missing")
    resistance = _finite(resistance_pa_per_ls2)
    if resistance is None or resistance < 0:
        return FanEstimate(**common, reason="resistance_invalid")

    def residual(flow: float) -> float:
        return interpolated(flow, 1) - resistance * flow * flow

    left, right = low, high
    if residual(left) < 0 or residual(right) > 0:
        return FanEstimate(**common, reason="operating_point_outside_measured_domain")
    # Raw pressure is strictly decreasing; the nonnegative duct curve is
    # increasing, so a bracketed crossing is unique without choosing roots.
    for _ in range(60):
        mid = (left + right) / 2
        if residual(mid) > 0:
            left = mid
        else:
            right = mid
    flow = (left + right) / 2
    return FanEstimate(
        **common, flow_ls=flow, power_w=interpolated(flow, 2), calibrated=True, reason="calibrated"
    )


def estimate_ventilation(
    *,
    supply_control_percent: float | None,
    extract_control_percent: float | None,
    supply_resistance_pa_per_ls2: float | None = None,
    extract_resistance_pa_per_ls2: float | None = None,
    supply_temp_c: float | None = None,
    indoor_temp_c: float | None = None,
    outdoor_temp_c: float | None = None,
    measured_power_w: float | None = None,
    auxiliary_power_w: float | None = None,
    model_verified: bool = False,
    controller_verified: bool = False,
) -> VentilationEstimate:
    """Estimate signed sensible exchange, keeping unexplained watts explicit.

    Supply temperature is the measured stream entering the house, including
    any fan or post-heater contribution already in that reading. Auxiliary
    electricity must be known before computing a residual; ``None`` never
    means zero. Expected fan electricity includes the lab's measured idle
    load once. A positive residual does not establish an indoor heat gain.
    """
    options = {"model_verified": model_verified, "controller_verified": controller_verified}
    supply = estimate_fan("supply", supply_control_percent, supply_resistance_pa_per_ls2, **options)
    extract = estimate_fan(
        "extract", extract_control_percent, extract_resistance_pa_per_ls2, **options
    )
    flags = []
    if model_verified is not True:
        flags.append("model_unverified")
    if controller_verified is not True:
        flags.append("controller_unverified")
    for fan in (supply, extract):
        if fan.flow_ls is None:
            flags.append(f"{fan.side}_{fan.reason}")
    qs, qe = supply.flow_ls, extract.flow_ls
    ts, ti, to = (_finite(value) for value in (supply_temp_c, indoor_temp_c, outdoor_temp_c))
    sensible = makeup = net = None
    if qs is not None and qe is not None:
        deficit = 0.0 if math.isclose(qe, qs, abs_tol=1e-9) else max(qe - qs, 0.0)
        sensible = (
            0.0
            if qs == 0
            else (
                _AIR_HEAT_W_PER_LS_K * qs * (ts - ti) / 1000
                if ts is not None and ti is not None
                else None
            )
        )
        # Signed: makeup warms the building when outdoors is warmer.
        makeup = (
            0.0
            if deficit == 0
            else (
                _AIR_HEAT_W_PER_LS_K * deficit * (to - ti) / 1000
                if to is not None and ti is not None
                else None
            )
        )
        if sensible is not None and makeup is not None:
            net = sensible + makeup
        else:
            flags.append("temperature_unavailable")
    expected = residual = None
    if model_verified is True and supply.power_w is not None and extract.power_w is not None:
        idle, _, _ = _manufacturer()
        expected = supply.power_w + extract.power_w + idle
    measured, auxiliary = _finite(measured_power_w), _finite(auxiliary_power_w)
    if measured is None or measured < 0:
        flags.append("electricity_unavailable")
    if auxiliary is None or auxiliary < 0:
        flags.append("auxiliary_power_unknown")
    if (
        expected is not None
        and measured is not None
        and measured >= 0
        and auxiliary is not None
        and auxiliary >= 0
    ):
        residual = measured - expected - auxiliary
        if abs(residual) > max(10.0, expected * 0.15):
            flags.append("electrical_mismatch")
    calibrated = (supply.calibrated or supply.reason == "off") and (
        extract.calibrated or extract.reason == "off"
    )
    calibrated = calibrated and model_verified is True and controller_verified is True
    reason = "calibrated" if calibrated else "airflow_uncalibrated"
    if calibrated and net is None:
        reason = "temperature_unavailable"
    if "electrical_mismatch" in flags:
        reason = "electrical_mismatch"
    return VentilationEstimate(
        supply, extract, sensible, makeup, net, expected, residual, calibrated, tuple(flags), reason
    )
