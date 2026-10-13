#!/usr/bin/env python3
"""Offline chronological RC grid fit. Emits a candidate; never writes HA configuration.

Run through the production host's heavy-job guard. Uses only stdlib and the pure
RC module; the integration's Home Assistant __init__ is deliberately not imported.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import types
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = "_lhc_offline"
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT / "custom_components" / "live_heating_costs")]
    sys.modules[_PACKAGE] = package


def _load(name: str):
    full = _PACKAGE + "." + name
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, Path(sys.modules[_PACKAGE].__path__[0]) / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


thermal = _load("thermal")
calibration = _load("calibration")
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_CANDIDATES = 225


def read_features(payload: dict) -> list:
    """Accept HA Store, runtime shadow object or raw calibration object."""
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    node = payload.get("data", payload)
    for key in ("shadow", "calibration"):
        if not isinstance(node, dict):
            raise ValueError("input wrapper must be an object")
        node = node.get(key, node)
    if not isinstance(node, dict):
        raise ValueError("calibration must be an object")
    raw = node.get("features")
    if not isinstance(raw, list) or len(raw) > calibration.MAX_FEATURE_ROWS:
        raise ValueError("input needs a bounded five-minute features list")
    rows = [calibration.FeatureRow.from_dict(item) for item in raw]
    for previous, current in zip(rows, rows[1:], strict=False):
        elapsed = (current.at - previous.at).total_seconds()
        if elapsed < 300 or elapsed % 300 != 0:
            raise ValueError("features must be chronological five-minute intervals")
    if any(row.at.second or row.at.microsecond or row.at.minute % 5 for row in rows):
        raise ValueError("feature timestamps must align to five-minute UTC boundaries")
    return rows


def _eligible(row, minimum_coverage: float) -> bool:
    return (
        row.valid
        and row.indoor_coverage >= minimum_coverage
        and all(
            value is not None
            for value in (
                row.indoor_c,
                row.outdoor_c,
                row.electric_kwh,
                row.measured_heat_kwh,
                row.modelled_heat_kwh,
                row.ventilation_kw,
                row.gains_kw,
            )
        )
    )


def _inputs(row, fabric_fraction: float):
    total = row.measured_heat_kwh + row.modelled_heat_kwh
    fabric = row.fabric_heat_kwh if row.fabric_heat_kwh is not None else total * fabric_fraction
    if fabric > total + 1e-9:
        raise ValueError("fabric heat exceeds total interval heat")
    return thermal.ThermalInputs(
        row.outdoor_c,
        (total - fabric) * 12,
        row.gains_kw,
        ventilation_kw=row.ventilation_kw,
        fabric_heat_kw=fabric * 12,
    )


@dataclass(frozen=True)
class Split:
    training_start: datetime
    training_end: datetime
    testing_start: datetime
    testing_end: datetime
    training_days: int
    testing_days: int


def chronological_split(
    rows: list, *, train_days: int = 14, test_days: int = 7, minimum_coverage: float = 0.9
) -> Split:
    if train_days < 14 or test_days < 7 or not 0.9 <= minimum_coverage <= 1:
        raise ValueError("require >=14 train days, >=7 test days and >=90% coverage")
    daily = defaultdict(set)
    for row in rows:
        if _eligible(row, minimum_coverage):
            midpoint = row.at - timedelta(seconds=150)
            daily[midpoint.date()].add(row.at)
    qualified = sorted(
        day for day, points in daily.items() if len(points) >= math.ceil(288 * minimum_coverage)
    )
    if len(qualified) < train_days + test_days:
        raise ValueError("insufficient complete days for chronological training/validation")
    test_start_day = qualified[-test_days]
    train = [day for day in qualified if day < test_start_day]
    start = rows[0].at.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = datetime.combine(test_start_day, datetime.min.time(), tzinfo=start.tzinfo)
    end = datetime.combine(
        qualified[-1] + timedelta(days=1), datetime.min.time(), tzinfo=start.tzinfo
    )
    return Split(start, boundary, boundary, end, len(train), test_days)


def evaluate(
    rows: list,
    parameters,
    baseline_parameters,
    split: Split,
    *,
    minimum_coverage: float = 0.9,
    fabric_fraction: float = 0,
    driver_mode: str = "persistence",
    target_c: float = 21,
    baseline_base_c: float = 17,
    training_only: bool = False,
) -> dict:
    """Replay fabric sequentially; air observations assimilate only past evidence.

    Persistence mode holds the latest observed driver over the future hour.
    Retrospective mode uses actual future drivers, a diagnostic upper bound which
    must never be represented as a forecast or production validation.
    """
    if driver_mode not in ("persistence", "retrospective"):
        raise ValueError("unknown driver mode")
    by_time = {row.at: row for row in rows}
    state = baseline_state = None
    previous = warm_start = None
    scores = {"training": [], "testing": []}
    for row in rows:
        if not _eligible(row, minimum_coverage):
            state = baseline_state = previous = warm_start = None
            continue
        inputs = _inputs(row, fabric_fraction)
        if previous is None or (row.at - previous.at).total_seconds() != 300:
            state = baseline_state = thermal.ThermalState(row.indoor_c, row.indoor_c)
            warm_start = row.at
        else:
            evolved = thermal.advance(state, parameters, inputs, previous.at, row.at)
            state = thermal.ThermalState(row.indoor_c, evolved.state.fabric_c)
            base_inputs = thermal.ThermalInputs(
                inputs.outdoor_c, inputs.heat_kw, fabric_heat_kw=inputs.fabric_heat_kw
            )
            evolved_base = thermal.advance(
                baseline_state, baseline_parameters, base_inputs, previous.at, row.at
            )
            baseline_state = thermal.ThermalState(row.indoor_c, evolved_base.state.fabric_c)
        previous = row
        if row.at.minute != 0 or row.at - warm_start < timedelta(hours=24):
            continue
        target = row.at + timedelta(hours=1)
        future = [by_time.get(row.at + timedelta(minutes=5 * i)) for i in range(1, 13)]
        if any(v is None or not _eligible(v, minimum_coverage) for v in future):
            continue
        # A training-origin forecast whose target reaches validation is excluded.
        if split.training_start <= row.at and target <= split.training_end:
            partition = "training"
        elif split.testing_start <= row.at and target <= split.testing_end:
            partition = "testing"
        else:
            continue
        forecast, base_forecast = state, baseline_state
        if driver_mode == "persistence":
            forecast = thermal.advance(state, parameters, inputs, row.at, target).state
            base_inputs = thermal.ThermalInputs(
                inputs.outdoor_c, inputs.heat_kw, fabric_heat_kw=inputs.fabric_heat_kw
            )
            base_forecast = thermal.advance(
                baseline_state, baseline_parameters, base_inputs, row.at, target
            ).state
        else:
            cursor = row.at
            for next_row in future:
                next_inputs = _inputs(next_row, fabric_fraction)
                forecast = thermal.advance(
                    forecast, parameters, next_inputs, cursor, next_row.at
                ).state
                base_inputs = thermal.ThermalInputs(
                    next_inputs.outdoor_c,
                    next_inputs.heat_kw,
                    fabric_heat_kw=next_inputs.fabric_heat_kw,
                )
                base_forecast = thermal.advance(
                    base_forecast, baseline_parameters, base_inputs, cursor, next_row.at
                ).state
                cursor = next_row.at
        actual = future[-1].indoor_c
        observed_heat = sum(v.measured_heat_kwh + v.modelled_heat_kwh for v in future)
        measured = sum(v.measured_heat_kwh for v in future)
        modelled = sum(v.modelled_heat_kwh for v in future)
        provenance = "mixed" if measured and modelled else "modelled" if modelled else "measured"
        demand = thermal.target_demand(state, parameters, inputs, row.at, target_c=target_c)
        scores[partition].append(
            {
                "thermal_error_c": abs(forecast.air_c - actual),
                "baseline_error_c": abs(base_forecast.air_c - actual),
                "persistence_error_c": abs(row.indoor_c - actual),
                "thermal_error_kwh": abs(demand.total_kw - observed_heat),
                "baseline_error_kwh": abs(
                    baseline_parameters.envelope_kw_per_c
                    * max(baseline_base_c - inputs.outdoor_c, 0)
                    - observed_heat
                ),
                "persistence_error_kwh": abs(
                    inputs.heat_kw + inputs.fabric_heat_kw - observed_heat
                ),
                "provenance": provenance,
            }
        )
    result = {}
    for partition, items in scores.items():
        if training_only and partition == "testing":
            continue
        if not items:
            raise ValueError("no contiguous, warmed one-hour predictions in " + partition)
        metrics = {"samples": len(items)}
        for field in ("thermal_error_c", "baseline_error_c", "persistence_error_c"):
            metrics[field] = sum(item[field] for item in items) / len(items)
        energy = {}
        for provenance in ("measured", "modelled", "mixed"):
            group = [item for item in items if item["provenance"] == provenance]
            if group:
                energy[provenance] = {
                    "samples": len(group),
                    **{
                        field: sum(item[field] for item in group) / len(group)
                        for field in (
                            "thermal_error_kwh",
                            "baseline_error_kwh",
                            "persistence_error_kwh",
                        )
                    },
                }
        metrics["energy_mae_by_provenance"] = energy
        result[partition] = metrics
    return result


def fit(
    rows: list,
    parameters,
    *,
    envelope_values=(0.06, 0.1, 0.14, 0.2, 0.3),
    fabric_values=(2, 5, 10, 20, 40),
    baseline_ua=0.14,
    minimum_coverage=0.9,
    fabric_fraction=0,
    driver_mode="persistence",
    target_c=21,
) -> dict:
    if (
        not envelope_values
        or not fabric_values
        or len(envelope_values) * len(fabric_values) > MAX_CANDIDATES
    ):
        raise ValueError("grid must contain 1..225 candidates")
    if any(not math.isfinite(v) or not 0.03 <= v <= 0.6 for v in envelope_values):
        raise ValueError("envelope candidates must be 0.03..0.6 kW/°C")
    if any(not math.isfinite(v) or not 1 <= v <= 100 for v in fabric_values):
        raise ValueError("fabric candidates must be 1..100 kWh/°C")
    if not math.isfinite(fabric_fraction) or not 0 <= fabric_fraction <= 1:
        raise ValueError("fabric fraction must be 0..1")
    if not math.isfinite(baseline_ua) or not 0.001 <= baseline_ua <= 1:
        raise ValueError("baseline UA must be .001..1 kW/°C")
    if not math.isfinite(target_c) or not 5 <= target_c <= 30:
        raise ValueError("target must be 5..30 °C")
    if (
        not 0.01 <= parameters.air_capacity_kwh_per_c <= 10
        or not 0.001 <= parameters.coupling_kw_per_c <= 20
    ):
        raise ValueError("fixed air capacity/coupling outside supported physical bounds")
    split = chronological_split(rows, minimum_coverage=minimum_coverage)
    baseline = replace(parameters, envelope_kw_per_c=baseline_ua)
    training_rows = [r for r in rows if r.at <= split.training_end]
    # Only training loss selects a candidate. Validation is evaluated afterwards.
    best, best_loss = None, math.inf
    for envelope in envelope_values:
        for fabric in fabric_values:
            candidate = replace(
                parameters, envelope_kw_per_c=envelope, fabric_capacity_kwh_per_c=fabric
            )
            metrics = evaluate(
                training_rows,
                candidate,
                baseline,
                split,
                minimum_coverage=minimum_coverage,
                fabric_fraction=fabric_fraction,
                driver_mode=driver_mode,
                target_c=target_c,
                training_only=True,
            )
            loss = metrics["training"]["thermal_error_c"]
            if loss < best_loss:
                best, best_loss = candidate, loss
    metrics = evaluate(
        rows,
        best,
        baseline,
        split,
        minimum_coverage=minimum_coverage,
        fabric_fraction=fabric_fraction,
        driver_mode=driver_mode,
        target_c=target_c,
    )
    return {
        "schema_version": 1,
        "candidate_parameters": asdict(best),
        "baseline_parameters": asdict(baseline),
        "fixed": {
            "air_capacity_kwh_per_c": parameters.air_capacity_kwh_per_c,
            "coupling_kw_per_c": parameters.coupling_kw_per_c,
            "fabric_fraction_fallback": fabric_fraction,
            "cop": "retained feature estimates unchanged",
            "ventilation_and_gains": "retained unchanged",
        },
        "split": {
            k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(split).items()
        },
        "metrics": metrics,
        "driver_assumption": "latest observed drivers held for one hour"
        if driver_mode == "persistence"
        else "actual future historical drivers; retrospective diagnostic, NOT a forecast",
        "ready": False,
        "applied": False,
        "required_review": [
            "indoor offsets",
            "airflow commissioning",
            "heat accounting and COP",
            "regime/event coverage",
            "28-day prospective comparison",
        ],
        "minimum_coverage": minimum_coverage,
        "events_observed": sorted({event for row in rows for event in row.events}),
        "regime_coverage_validated": False,
        "grid_candidates": len(envelope_values) * len(fabric_values),
    }


def _values(value: str) -> tuple[float, ...]:
    try:
        return tuple(float(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("use comma-separated numeric candidates") from error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--parameters", type=Path, help="JSON thermal parameters or shadow configuration"
    )
    parser.add_argument("--envelope-values", type=_values, default=(0.06, 0.1, 0.14, 0.2, 0.3))
    parser.add_argument("--fabric-values", type=_values, default=(2, 5, 10, 20, 40))
    parser.add_argument("--baseline-ua", type=float, default=0.14)
    parser.add_argument("--fabric-heat-fraction", type=float, default=0)
    parser.add_argument("--minimum-coverage", type=float, default=0.9)
    parser.add_argument(
        "--driver-mode", choices=("persistence", "retrospective"), default="persistence"
    )
    parser.add_argument("--target-c", type=float, default=21)
    args = parser.parse_args(argv)
    try:
        protected = {args.input.resolve()}
        if args.parameters:
            protected.add(args.parameters.resolve())
        if args.output and args.output.resolve() in protected:
            raise ValueError("output must not overwrite an input or parameters file")
        if args.input.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("input exceeds 64 MiB limit")
        payload = json.loads(args.input.read_text())
        rows = read_features(payload)
        raw = (
            json.loads(args.parameters.read_text())
            if args.parameters
            else {
                "envelope_kw_per_c": 0.14,
                "air_capacity_kwh_per_c": 0.5,
                "fabric_capacity_kwh_per_c": 10,
                "coupling_kw_per_c": 0.5,
            }
        )
        raw = raw.get("parameters", raw)
        report = fit(
            rows,
            thermal.ThermalParameters(**raw),
            envelope_values=args.envelope_values,
            fabric_values=args.fabric_values,
            baseline_ua=args.baseline_ua,
            minimum_coverage=args.minimum_coverage,
            fabric_fraction=args.fabric_heat_fraction,
            driver_mode=args.driver_mode,
            target_c=args.target_c,
        )
        report["features_sha256"] = hashlib.sha256(
            json.dumps([r.to_dict() for r in rows], sort_keys=True).encode()
        ).hexdigest()
        report["parameters_supplied"] = args.parameters is not None
        text = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(text)
        else:
            print(text, end="")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("calibration refused: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
