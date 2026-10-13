"""Manufacturer fan-domain and whole-house boundary-heat behavior, without HA."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from dataclasses import asdict

import pytest

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "live_heating_costs"
    / "ventilation.py"
)
_spec = importlib.util.spec_from_file_location("ls_ventilation", _PATH)
vent = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vent
_spec.loader.exec_module(vent)

_VERIFIED = {"model_verified": True, "controller_verified": True}


def _raw_point(side: str, control: int, index: int = 4) -> dict:
    data = json.loads((_PATH.parent / "data" / "parmair120.json").read_text())
    return next(curve for curve in data["curves"][side] if curve["control_percent"] == control)[
        "points"
    ][index]


def _resistance(point: dict) -> float:
    return point["pressure_pa"] / point["flow_ls"] ** 2


def test_manufacturer_table_preserves_rpm_and_public_source_provenance() -> None:
    data = json.loads((_PATH.parent / "data" / "parmair120.json").read_text())
    assert len(data["provenance"]["sha256"]) == 64
    assert "/" not in data["provenance"]["filename"]
    for side, last_column in (("supply", "J"), ("extract", "V")):
        for curve in data["curves"][side]:
            assert f":{last_column}" in curve["source_range"]
            assert all(0 < point["rpm"] < 4000 for point in curve["points"])
    curve = next(c for c in data["curves"]["supply"] if c["control_percent"] == 60)
    point = next(p for p in curve["points"] if p["flow_ls"] == pytest.approx(48.82366458342551))
    assert point["rpm"] == pytest.approx(1946.6666666666667)


@pytest.mark.parametrize("side", ["supply", "extract"])
def test_recovers_actual_manufacturer_point(side: str) -> None:
    """A measured pressure/flow pair selects its same flow and power."""
    point = _raw_point(side, 60)
    got = vent.estimate_fan(side, 60, _resistance(point), **_VERIFIED)
    assert got.flow_ls == pytest.approx(point["flow_ls"])
    assert got.power_w == pytest.approx(point["power_w"])
    assert got.reason == "calibrated"
    assert got.calibrated


def test_interpolates_control_on_common_measured_domain() -> None:
    """Halfway control uses both curves at one common physical flow."""
    _, supply, _ = vent._manufacturer()
    low = next(curve for curve in supply if curve.control_percent == 50)
    high = next(curve for curve in supply if curve.control_percent == 60)
    flow = 40.0
    pressure = (vent._value(low, flow, 1) + vent._value(high, flow, 1)) / 2
    expected_power = (vent._value(low, flow, 2) + vent._value(high, flow, 2)) / 2
    got = vent.estimate_fan("supply", 55, pressure / flow**2, **_VERIFIED)
    assert got.flow_ls == pytest.approx(flow)
    assert got.power_w == pytest.approx(expected_power)
    assert got.flow_low_ls == max(low.points[0][0], high.points[0][0])
    assert got.flow_high_ls == min(low.points[-1][0], high.points[-1][0])


@pytest.mark.parametrize("control", [40, 100])
def test_accepts_measured_control_endpoints(control: int) -> None:
    point = _raw_point("supply", control)
    got = vent.estimate_fan("supply", control, _resistance(point), **_VERIFIED)
    assert got.flow_ls == pytest.approx(point["flow_ls"])


@pytest.mark.parametrize("control", [28.8, 39.999, 100.001, -1])
def test_nonzero_command_outside_measured_domain_stays_unknown(control: float) -> None:
    got = vent.estimate_fan("supply", control, 0.03, **_VERIFIED)
    assert got.flow_ls is None
    assert got.power_w is None
    assert got.reason == "control_outside_measured_domain"


@pytest.mark.parametrize("control", [None, True, "60", float("nan"), float("inf"), 10**400])
def test_bad_or_missing_command_is_not_zero(control: object) -> None:
    got = vent.estimate_fan("supply", control, 0.03, **_VERIFIED)
    assert got.flow_ls is None
    assert got.reason == "control_unavailable"


def test_explicit_off_does_not_require_a_curve() -> None:
    got = vent.estimate_fan("extract", 0)
    assert got.flow_ls == got.power_w == 0
    assert got.reason == "off"


@pytest.mark.parametrize("flag", ["model_verified", "controller_verified"])
def test_truthy_text_does_not_verify_hardware(flag: str) -> None:
    options = {**_VERIFIED, flag: "yes"}
    got = vent.estimate_fan("supply", 60, 0.03, **options)
    assert got.flow_ls is None
    assert got.reason == flag.replace("verified", "unverified")


@pytest.mark.parametrize(
    ("options", "resistance", "reason"),
    [
        ({}, 0.03, "model_unverified"),
        ({"model_verified": True}, 0.03, "controller_unverified"),
        (_VERIFIED, None, "resistance_missing"),
        (_VERIFIED, -0.03, "resistance_invalid"),
        (_VERIFIED, float("nan"), "resistance_invalid"),
        (_VERIFIED, float("inf"), "resistance_invalid"),
        (_VERIFIED, True, "resistance_invalid"),
    ],
)
def test_calibration_requires_verified_identity_mapping_and_resistance(
    options: dict, resistance: object, reason: str
) -> None:
    got = vent.estimate_fan("supply", 60, resistance, **options)
    assert got.flow_ls is None
    assert got.flow_low_ls > 0
    assert got.flow_high_ls > got.flow_low_ls
    assert got.power_low_w > 0
    assert not got.calibrated
    assert got.reason == reason


@pytest.mark.parametrize("resistance", [0, 1e6, 1e308])
def test_no_operating_point_extrapolation(resistance: float) -> None:
    got = vent.estimate_fan("supply", 60, resistance, **_VERIFIED)
    assert got.flow_ls is None
    assert got.reason == "operating_point_outside_measured_domain"


def _whole(**overrides):
    supply = _raw_point("supply", 60)
    extract = _raw_point("extract", 60)
    return vent.estimate_ventilation(
        **{
            "supply_control_percent": 60,
            "extract_control_percent": 60,
            "supply_resistance_pa_per_ls2": _resistance(supply),
            "extract_resistance_pa_per_ls2": _resistance(extract),
            "supply_temp_c": 17,
            "indoor_temp_c": 21,
            "outdoor_temp_c": -5,
            **_VERIFIED,
            **overrides,
        }
    )


def test_extract_imbalance_adds_cold_makeup_once() -> None:
    got = _whole()
    qs, qe = got.supply.flow_ls, got.extract.flow_ls
    assert qe > qs
    assert got.sensible_kw == pytest.approx(1.2072 * qs * (17 - 21) / 1000)
    assert got.makeup_loss_kw == pytest.approx(1.2072 * (qe - qs) * (-5 - 21) / 1000)
    assert got.net_sensible_kw == pytest.approx(got.sensible_kw + got.makeup_loss_kw)
    assert got.net_sensible_kw < 0


def test_balanced_flow_needs_no_outdoor_temperature() -> None:
    _, _, extract = vent._manufacturer()
    curve = next(c for c in extract if c.control_percent == 60)
    flow = _whole().supply.flow_ls
    got = _whole(
        extract_resistance_pa_per_ls2=vent._value(curve, flow, 1) / flow**2, outdoor_temp_c=None
    )
    assert got.extract.flow_ls == pytest.approx(got.supply.flow_ls)
    assert got.makeup_loss_kw == 0
    assert got.net_sensible_kw == got.sensible_kw


def test_supply_excess_does_not_invent_negative_makeup() -> None:
    got = _whole(extract_resistance_pa_per_ls2=0.5, outdoor_temp_c=None)
    assert got.supply.flow_ls > got.extract.flow_ls
    assert got.makeup_loss_kw == 0
    assert got.net_sensible_kw == got.sensible_kw


def test_summer_and_warm_supply_preserve_heat_gain_sign() -> None:
    got = _whole(supply_temp_c=25, outdoor_temp_c=30)
    assert got.sensible_kw > 0
    assert got.makeup_loss_kw > 0
    assert got.net_sensible_kw > 0


@pytest.mark.parametrize("temperature", [None, float("nan"), float("inf"), True])
def test_missing_temperature_does_not_erase_a_loss(temperature: object) -> None:
    got = _whole(outdoor_temp_c=temperature)
    assert got.net_sensible_kw is None
    assert got.makeup_loss_kw is None
    assert "temperature_unavailable" in got.flags


def test_both_off_has_zero_heat_without_temperatures_and_one_idle_load() -> None:
    got = vent.estimate_ventilation(
        supply_control_percent=0, extract_control_percent=0, **_VERIFIED
    )
    assert got.net_sensible_kw == 0
    assert got.expected_fan_power_w == pytest.approx(5.169444444444445)
    assert got.electricity_residual_w is None


def test_unverified_airflow_is_not_recovered_from_total_electricity() -> None:
    got = _whole(model_verified=False, measured_power_w=186, auxiliary_power_w=0)
    assert got.supply.flow_ls is got.extract.flow_ls is None
    assert got.net_sensible_kw is None
    assert got.electricity_residual_w is None
    assert not got.calibrated
    assert "model_unverified" in got.flags


def test_unknown_auxiliary_load_leaves_residual_unknown() -> None:
    got = _whole(measured_power_w=186)
    assert got.expected_fan_power_w is not None
    assert got.electricity_residual_w is None
    assert "auxiliary_power_unknown" in got.flags


def test_electrical_residual_subtracts_known_auxiliary_and_idle_once() -> None:
    initial = _whole()
    got = _whole(measured_power_w=initial.expected_fan_power_w + 1000, auxiliary_power_w=1000)
    assert got.electricity_residual_w == pytest.approx(0)
    assert "electrical_mismatch" not in got.flags
    # Heater watts already appear in the supply-air thermometer; the residual
    # accounting must not add them a second time to the house's heat exchange.
    assert got.net_sensible_kw == initial.net_sensible_kw


def test_electrical_mismatch_is_flagged_without_forcing_airflow() -> None:
    initial = _whole()
    got = _whole(measured_power_w=initial.expected_fan_power_w + 50, auxiliary_power_w=0)
    assert got.electricity_residual_w == pytest.approx(50)
    assert "electrical_mismatch" in got.flags
    assert got.reason == "electrical_mismatch"
    assert got.supply.flow_ls == initial.supply.flow_ls
    assert got.extract.flow_ls == initial.extract.flow_ls


def test_diagnostics_are_json_serializable() -> None:
    assert "net_sensible_kw" in json.loads(json.dumps(asdict(_whole())))


def test_invalid_side_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="side"):
        vent.estimate_fan("both", 60)
