"""Offline fitting respects chronology, fixed physics and unknown drivers."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "calibrate_shadow.py"
_spec = importlib.util.spec_from_file_location("offline_calibration_test", _PATH)
offline = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = offline
_spec.loader.exec_module(offline)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PARAMS = offline.thermal.ThermalParameters(0.14, 0.5, 10, 0.5)


def sample(at, **updates):
    data = dict(
        at=at,
        indoor_c=21,
        outdoor_c=1,
        indoor_coverage=1,
        electric_kwh=7 / 30,
        measured_heat_kwh=7 / 30,
        modelled_heat_kwh=0,
        ventilation_kw=0,
        gains_kw=0,
        valid=True,
        fabric_heat_kwh=0,
    )
    data.update(updates)
    return offline.calibration.FeatureRow(**data)


@pytest.fixture(scope="module")
def rows():
    # Exact steady heat balance makes the true candidate identifiable without
    # a dependency on random noise or fitted test observations.
    return [sample(T0 + timedelta(minutes=5 * (i + 1))) for i in range(21 * 288)]


def test_store_and_calibration_json_shapes(rows):
    raw = {"features": [r.to_dict() for r in rows[:3]]}
    assert offline.read_features(raw) == rows[:3]
    assert offline.read_features({"calibration": raw}) == rows[:3]
    assert offline.read_features({"data": {"shadow": {"calibration": raw}}}) == rows[:3]
    with pytest.raises(ValueError):
        offline.read_features({"features": list(reversed(raw["features"]))})
    with pytest.raises(ValueError):
        offline.read_features({"features": [sample(T0 + timedelta(seconds=1)).to_dict()]})


def test_chronological_split_requires_distributed_complete_days(rows):
    split = offline.chronological_split(rows)
    assert split.training_days == 14
    assert split.testing_days == 7
    assert split.training_end == split.testing_start
    assert split.testing_start == T0 + timedelta(days=14)
    with pytest.raises(ValueError):
        offline.chronological_split(rows[: 20 * 288])
    with pytest.raises(ValueError):
        offline.chronological_split([sample(T0 + timedelta(days=i)) for i in range(30)])
    with pytest.raises(ValueError):
        offline.chronological_split(rows, minimum_coverage=0.5)


def test_missing_gain_or_heat_never_fabricated_zero_and_partition_checked():
    assert not offline._eligible(sample(T0, gains_kw=None), 0.9)
    assert not offline._eligible(sample(T0, modelled_heat_kwh=None), 0.9)
    with pytest.raises(ValueError):
        offline._inputs(sample(T0, fabric_heat_kwh=1), 0)


def test_grid_selects_training_candidate_never_marks_ready(rows):
    report = offline.fit(rows, PARAMS, envelope_values=(0.1, 0.14), fabric_values=(10,))
    assert report["candidate_parameters"]["envelope_kw_per_c"] == 0.14
    assert report["candidate_parameters"]["air_capacity_kwh_per_c"] == 0.5
    assert report["candidate_parameters"]["coupling_kw_per_c"] == 0.5
    assert report["metrics"]["testing"]["thermal_error_c"] == pytest.approx(0, abs=1e-8)
    assert report["metrics"]["testing"]["samples"] > 100
    assert not report["ready"]
    assert not report["applied"]
    assert not report["regime_coverage_validated"]
    assert "latest observed drivers" in report["driver_assumption"]


def test_validation_observations_cannot_change_training_selection(rows):
    changed = [r if r.at <= T0 + timedelta(days=14) else sample(r.at, indoor_c=25) for r in rows]
    report = offline.fit(changed, PARAMS, envelope_values=(0.1, 0.14), fabric_values=(10,))
    assert report["candidate_parameters"]["envelope_kw_per_c"] == 0.14
    assert report["metrics"]["training"]["thermal_error_c"] == pytest.approx(0, abs=1e-8)


def test_retrospective_drivers_are_explicitly_not_a_forecast(rows):
    report = offline.fit(
        rows, PARAMS, envelope_values=(0.14,), fabric_values=(10,), driver_mode="retrospective"
    )
    assert "NOT a forecast" in report["driver_assumption"]
    assert not report["ready"]


@pytest.mark.parametrize(
    ("envelope", "fabric"),
    [((0.001,), (10,)), ((0.7,), (10,)), ((0.14,), (0.5,)), ((0.14,) * 226, (10,))],
)
def test_grid_and_parameter_bounds_before_heavy_work(rows, envelope, fabric):
    with pytest.raises(ValueError):
        offline.fit(rows, PARAMS, envelope_values=envelope, fabric_values=fabric)


def test_cli_refuses_insufficient_input_and_preserves_input(tmp_path):
    source = tmp_path / "features.json"
    source.write_text(json.dumps({"features": [sample(T0).to_dict()]}))
    before = source.read_bytes()
    assert offline.main([str(source), "--output", str(source)]) == 2
    assert source.read_bytes() == before
