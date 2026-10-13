"""Shadow scoring cannot learn from future outcomes or pass sparse evidence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.live_heating_costs.calibration import (
    MAX_FEATURE_ROWS,
    CalibrationFlags,
    CalibrationState,
    FeatureRow,
    ShadowPrediction,
    ShadowScore,
    prune,
    readiness,
    record_feature,
    record_prediction,
    score_observation,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def feature(at, valid=True):
    return FeatureRow(at, 21, 0, 1, 0.1, 0.1, 0.3, -0.3, gains_kw=0, valid=valid)


def prediction(at=T0):
    return ShadowPrediction(at, at + timedelta(hours=1), 21, 20, 19, 3, 4, 5)


def test_predictions_precede_future_outcomes_and_score_only_once():
    state = record_feature(CalibrationState(), feature(T0))
    state = record_prediction(state, prediction())
    state = score_observation(
        state, T0 + timedelta(hours=1), 21.2, heat_kwh=3.2, provenance="modelled"
    )
    assert len(state.scores) == 1
    assert state.scores[0].thermal_error_c == pytest.approx(0.2)
    assert state.scores[0].thermal_error_kwh == pytest.approx(0.2)
    assert state.scores[0].provenance == "modelled"
    assert not state.pending
    with pytest.raises(ValueError):
        score_observation(state, T0 + timedelta(hours=1), 21.2, heat_kwh=3.2, provenance="modelled")
    with pytest.raises(ValueError):
        record_prediction(
            record_feature(CalibrationState(), feature(T0 + timedelta(hours=1))), prediction()
        )


def test_chronological_feature_gate_and_missing_energy_not_zero_score():
    state = record_feature(CalibrationState(), feature(T0))
    with pytest.raises(ValueError):
        record_feature(state, feature(T0 + timedelta(seconds=1)))
    state = record_prediction(state, prediction())
    scored = score_observation(
        state, T0 + timedelta(hours=1), 21, heat_kwh=None, provenance="mixed"
    )
    assert scored.scores[0].thermal_error_kwh is None
    assert not replace(feature(T0), ventilation_kw=None).complete
    assert not replace(feature(T0), indoor_coverage=0.5).complete
    assert not replace(feature(T0), gains_kw=None).complete
    assert replace(feature(T0), fabric_heat_kwh=0.1).to_dict()["fabric_heat_kwh"] == 0.1


def test_serialization_and_retention_bounds():
    state = record_prediction(record_feature(CalibrationState(), feature(T0)), prediction())
    assert CalibrationState.from_dict(state.to_dict()) == state
    assert not prune(state, T0 + timedelta(days=91)).features
    assert not prune(state, T0 + timedelta(hours=4)).pending
    huge = {"features": [feature(T0).to_dict()] * (MAX_FEATURE_ROWS + 1)}
    assert len(CalibrationState.from_dict(huge).features) == MAX_FEATURE_ROWS


def evidence(
    days=28,
    thermal_error=0.5,
    baseline_error=1,
    persistence_error=1.2,
    thermal_energy=0.2,
    sparse=False,
):
    features, scores = [], []
    for day in range(days):
        start = T0 + timedelta(days=day)
        if sparse:
            features.append(feature(start + timedelta(minutes=5)))
            hours = [12]
        else:
            features.extend(feature(start + timedelta(minutes=5 * (i + 1))) for i in range(288))
            hours = range(24)
        for hour in hours:
            scores.append(
                ShadowScore(
                    start + timedelta(hours=hour),
                    thermal_error,
                    baseline_error,
                    persistence_error,
                    thermal_energy,
                    0.3,
                    0.4,
                    "mixed",
                    "initial",
                    True,
                )
            )
    return CalibrationState(tuple(features), scores=tuple(scores))


def flags():
    return CalibrationFlags(True, True, True, True, True, True)


def test_readiness_requires_28_distributed_days_and_explicit_flags():
    state = evidence()
    result = readiness(state, flags())
    assert result.ready
    assert result.valid_days == 28
    assert result.scores == 28 * 24
    assert result.energy_mae_by_provenance["mixed"]["samples"] == 28 * 24
    assert "insufficient_distributed_days" in readiness(evidence(27), flags()).reasons
    assert not readiness(evidence(sparse=True), flags()).ready
    assert "uncalibrated_event_coverage" in readiness(state, CalibrationFlags()).reasons
    assert not readiness(state, replace(flags(), indoor_offsets=False)).ready


def test_temperature_improvement_required_against_both_and_energy_degradation_blocks():
    assert not readiness(evidence(thermal_error=0.86), flags()).ready
    assert not readiness(evidence(persistence_error=0.4), flags()).ready
    assert not readiness(evidence(baseline_error=0), flags()).ready
    result = readiness(evidence(thermal_energy=1), flags())
    assert "energy_degradation_mixed" in result.reasons
    assert not readiness(evidence(), flags(), model_revision="new-parameters").ready


def test_invalid_or_unscored_data_cannot_make_ready():
    state = evidence()
    assert not readiness(replace(state, scores=()), flags()).ready
    bad = tuple(replace(row, valid=False) for row in state.features)
    assert not readiness(replace(state, features=bad), flags()).ready
    no_energy = tuple(replace(row, thermal_error_kwh=None) for row in state.scores)
    assert (
        "insufficient_energy_scores" in readiness(replace(state, scores=no_energy), flags()).reasons
    )
    sparse_energy = tuple(replace(row, thermal_error_kwh=None) for row in state.scores[:-1])
    result = readiness(replace(state, scores=(*sparse_energy, state.scores[-1])), flags())
    assert "insufficient_energy_coverage" in result.reasons


def test_only_one_hour_aware_predictions_and_valid_features():
    with pytest.raises(ValueError):
        ShadowPrediction(T0, T0 + timedelta(minutes=5), 21, 20, 20)
    with pytest.raises(ValueError):
        replace(feature(T0), at=T0.replace(tzinfo=None))
    with pytest.raises(ValueError):
        replace(feature(T0), electric_kwh=-1)
