"""Chronological shadow evaluation; readiness is evidence, never activation.

Feature intervals are five minutes. One-hour forecasts must be saved before the
future observation exists. Counters and scores are bounded, and heat errors stay
separated by measured/modelled provenance rather than claiming a heat-meter truth.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

from .thermal import require_utc

RETENTION_DAYS = 90
MAX_FEATURE_ROWS = RETENTION_DAYS * 24 * 12
MAX_PREDICTIONS = MAX_FEATURE_ROWS


@dataclass(frozen=True)
class FeatureRow:
    at: datetime
    indoor_c: float | None
    outdoor_c: float | None
    indoor_coverage: float
    electric_kwh: float | None
    measured_heat_kwh: float | None
    modelled_heat_kwh: float | None
    ventilation_kw: float | None
    gains_kw: float | None = None
    valid: bool = True
    events: tuple[str, ...] = ()
    fabric_heat_kwh: float | None = None

    def __post_init__(self) -> None:
        require_utc(self.at)
        if not math.isfinite(self.indoor_coverage) or not 0 <= self.indoor_coverage <= 1:
            raise ValueError("invalid feature coverage")
        values = (
            self.indoor_c,
            self.outdoor_c,
            self.electric_kwh,
            self.measured_heat_kwh,
            self.modelled_heat_kwh,
            self.ventilation_kw,
            self.gains_kw,
            self.fabric_heat_kwh,
        )
        if any(v is not None and not math.isfinite(v) for v in values):
            raise ValueError("feature values must be finite or unknown")
        for v in (
            self.electric_kwh,
            self.measured_heat_kwh,
            self.modelled_heat_kwh,
            self.fabric_heat_kwh,
        ):
            if v is not None and v < 0:
                raise ValueError("energy must be nonnegative")

    @property
    def complete(self) -> bool:
        return (
            self.valid
            and self.indoor_coverage >= 0.9
            and all(
                v is not None
                for v in (
                    self.indoor_c,
                    self.outdoor_c,
                    self.electric_kwh,
                    self.measured_heat_kwh,
                    self.modelled_heat_kwh,
                    self.ventilation_kw,
                    self.gains_kw,
                )
            )
        )

    def to_dict(self) -> dict:
        return {**asdict(self), "at": self.at.isoformat(), "events": list(self.events)}

    @classmethod
    def from_dict(cls, data: dict) -> FeatureRow:
        return cls(
            **{
                **data,
                "at": datetime.fromisoformat(data["at"]),
                "events": tuple(data.get("events", ())),
            }
        )


@dataclass(frozen=True)
class ShadowPrediction:
    issued_at: datetime
    target_at: datetime
    thermal_c: float
    baseline_c: float
    persistence_c: float
    thermal_kwh: float | None = None
    baseline_kwh: float | None = None
    persistence_kwh: float | None = None
    model_revision: str = "initial"

    def __post_init__(self) -> None:
        require_utc(self.issued_at)
        require_utc(self.target_at)
        if (self.target_at - self.issued_at).total_seconds() != 3600:
            raise ValueError("shadow prediction horizon must be one hour")
        values = (
            self.thermal_c,
            self.baseline_c,
            self.persistence_c,
            self.thermal_kwh,
            self.baseline_kwh,
            self.persistence_kwh,
        )
        if any(v is not None and not math.isfinite(v) for v in values):
            raise ValueError("predictions must be finite")
        if any(
            v is not None and v < 0
            for v in (self.thermal_kwh, self.baseline_kwh, self.persistence_kwh)
        ):
            raise ValueError("predicted energy must be nonnegative")

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "issued_at": self.issued_at.isoformat(),
            "target_at": self.target_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ShadowPrediction:
        return cls(
            **{
                **data,
                "issued_at": datetime.fromisoformat(data["issued_at"]),
                "target_at": datetime.fromisoformat(data["target_at"]),
            }
        )


@dataclass(frozen=True)
class ShadowScore:
    target_at: datetime
    thermal_error_c: float
    baseline_error_c: float
    persistence_error_c: float
    thermal_error_kwh: float | None
    baseline_error_kwh: float | None
    persistence_error_kwh: float | None
    provenance: str
    model_revision: str
    valid: bool

    def __post_init__(self) -> None:
        require_utc(self.target_at)
        if self.provenance not in {"measured", "modelled", "mixed"}:
            raise ValueError("unknown score provenance")
        errors = (
            self.thermal_error_c,
            self.baseline_error_c,
            self.persistence_error_c,
            self.thermal_error_kwh,
            self.baseline_error_kwh,
            self.persistence_error_kwh,
        )
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in errors):
            raise ValueError("score errors must be finite and nonnegative")

    def to_dict(self) -> dict:
        return {**asdict(self), "target_at": self.target_at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> ShadowScore:
        return cls(**{**data, "target_at": datetime.fromisoformat(data["target_at"])})


@dataclass(frozen=True)
class CalibrationState:
    features: tuple[FeatureRow, ...] = ()
    pending: tuple[ShadowPrediction, ...] = ()
    scores: tuple[ShadowScore, ...] = ()

    def to_dict(self) -> dict:
        return {
            "features": [v.to_dict() for v in self.features],
            "pending": [v.to_dict() for v in self.pending],
            "scores": [v.to_dict() for v in self.scores],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CalibrationState:
        return cls(
            tuple(FeatureRow.from_dict(v) for v in data.get("features", [])[-MAX_FEATURE_ROWS:]),
            tuple(
                ShadowPrediction.from_dict(v) for v in data.get("pending", [])[-MAX_PREDICTIONS:]
            ),
            tuple(ShadowScore.from_dict(v) for v in data.get("scores", [])[-MAX_PREDICTIONS:]),
        )


def prune(state: CalibrationState, now: datetime) -> CalibrationState:
    require_utc(now)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    return CalibrationState(
        tuple(r for r in state.features if cutoff <= r.at <= now)[-MAX_FEATURE_ROWS:],
        tuple(
            p
            for p in state.pending
            if now - timedelta(hours=2) <= p.target_at and p.issued_at <= now
        )[-MAX_PREDICTIONS:],
        tuple(s for s in state.scores if cutoff <= s.target_at <= now)[-MAX_PREDICTIONS:],
    )


def record_feature(state: CalibrationState, row: FeatureRow) -> CalibrationState:
    """Append once per five-minute interval; reject replay/backwards observations."""
    if state.features and (row.at - state.features[-1].at).total_seconds() < 300:
        raise ValueError("feature observations must be chronological five-minute intervals")
    return prune(replace(state, features=(*state.features, row)), row.at)


def record_prediction(state: CalibrationState, prediction: ShadowPrediction) -> CalibrationState:
    """Future outcomes cannot be used to construct or retroactively record forecasts."""
    if state.features and state.features[-1].at > prediction.issued_at:
        raise ValueError("prediction would use future observations")
    if state.scores and state.scores[-1].target_at > prediction.issued_at:
        raise ValueError("prediction predates scored observations")
    if state.pending and prediction.issued_at <= state.pending[-1].issued_at:
        raise ValueError("predictions must be strictly chronological")
    if any(s.target_at == prediction.target_at for s in state.scores):
        raise ValueError("prediction target already scored")
    return prune(replace(state, pending=(*state.pending, prediction)), prediction.issued_at)


def score_observation(
    state: CalibrationState,
    observed_at: datetime,
    indoor_c: float,
    *,
    heat_kwh: float | None,
    provenance: str,
    valid: bool = True,
) -> CalibrationState:
    """Score a pre-existing forecast; no matching prediction means no manufactured score."""
    require_utc(observed_at)
    if provenance not in {"measured", "modelled", "mixed"}:
        raise ValueError("unknown observed heat provenance")
    if not math.isfinite(indoor_c) or (
        heat_kwh is not None and (not math.isfinite(heat_kwh) or heat_kwh < 0)
    ):
        raise ValueError("invalid observation")
    if state.scores and observed_at <= state.scores[-1].target_at:
        raise ValueError("scores must be strictly chronological")
    prediction = next((p for p in state.pending if p.target_at == observed_at), None)
    if prediction is None:
        return prune(state, observed_at)

    def energy_error(value: float | None) -> float | None:
        return abs(value - heat_kwh) if value is not None and heat_kwh is not None else None

    score = ShadowScore(
        observed_at,
        abs(prediction.thermal_c - indoor_c),
        abs(prediction.baseline_c - indoor_c),
        abs(prediction.persistence_c - indoor_c),
        energy_error(prediction.thermal_kwh),
        energy_error(prediction.baseline_kwh),
        energy_error(prediction.persistence_kwh),
        provenance,
        prediction.model_revision,
        valid,
    )
    return prune(
        replace(
            state,
            pending=tuple(p for p in state.pending if p != prediction),
            scores=(*state.scores, score),
        ),
        observed_at,
    )


@dataclass(frozen=True)
class CalibrationFlags:
    indoor_offsets: bool = False
    envelope: bool = False
    thermal_capacities: bool = False
    ventilation_airflow: bool = False
    heat_accounting: bool = False
    event_coverage: bool = False


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]
    valid_days: int
    scores: int
    thermal_mae_c: float | None
    baseline_mae_c: float | None
    persistence_mae_c: float | None
    energy_mae_by_provenance: dict[str, dict[str, float | int]]


def readiness(
    state: CalibrationState, flags: CalibrationFlags, *, model_revision: str = "initial"
) -> Readiness:
    """Require distributed coverage, independent calibration and both comparisons.

    A valid day has at least 90% of five-minute features and predictions spanning
    at least 22 different hours. A single sparse sample on each date cannot pass.
    """
    reasons = []
    for key, enabled in asdict(flags).items():
        if not enabled:
            reasons.append("uncalibrated_" + key)
    rows_by_day: dict[str, set[int]] = defaultdict(set)
    for row in state.features:
        if row.complete:
            # An interval ending at midnight belongs to the preceding day.
            midpoint = row.at - timedelta(seconds=150)
            rows_by_day[midpoint.date().isoformat()].add(int(row.at.timestamp() // 300))
    scores = [s for s in state.scores if s.valid and s.model_revision == model_revision]
    hours_by_day: dict[str, set[int]] = defaultdict(set)
    for score in scores:
        hours_by_day[score.target_at.date().isoformat()].add(score.target_at.hour)
    days = {
        d
        for d, intervals in rows_by_day.items()
        if len(intervals) >= math.ceil(288 * 0.9) and len(hours_by_day[d]) >= 22
    }
    if len(days) < 28:
        reasons.append("insufficient_distributed_days")
    qualifying = [s for s in scores if s.target_at.date().isoformat() in days]
    means = [
        sum(getattr(s, field) for s in qualifying) / len(qualifying) if qualifying else None
        for field in ("thermal_error_c", "baseline_error_c", "persistence_error_c")
    ]
    thermal, baseline, persistence = means
    if thermal is None or baseline is None or thermal > baseline * 0.85 or baseline <= 0:
        reasons.append("temperature_baseline_not_improved")
    if thermal is None or persistence is None or thermal > persistence * 0.85 or persistence <= 0:
        reasons.append("temperature_persistence_not_improved")
    energy_complete = sum(
        all(
            value is not None
            for value in (
                score.thermal_error_kwh,
                score.baseline_error_kwh,
                score.persistence_error_kwh,
            )
        )
        for score in qualifying
    )
    if not qualifying or energy_complete / len(qualifying) < 0.9:
        reasons.append("insufficient_energy_coverage")
    energy: dict[str, dict[str, float | int]] = {}
    for provenance in ("measured", "modelled", "mixed"):
        group = [
            s
            for s in qualifying
            if s.provenance == provenance
            and all(
                v is not None
                for v in (s.thermal_error_kwh, s.baseline_error_kwh, s.persistence_error_kwh)
            )
        ]
        if not group:
            continue
        metrics = {
            field: sum(getattr(s, field) for s in group) / len(group)
            for field in ("thermal_error_kwh", "baseline_error_kwh", "persistence_error_kwh")
        }
        energy[provenance] = {**metrics, "samples": len(group)}
        if (
            metrics["thermal_error_kwh"]
            > min(metrics["baseline_error_kwh"], metrics["persistence_error_kwh"]) * 1.05 + 0.05
        ):
            reasons.append("energy_degradation_" + provenance)
    if not energy:
        reasons.append("insufficient_energy_scores")
    return Readiness(
        not reasons,
        tuple(reasons),
        len(days),
        len(qualifying),
        thermal,
        baseline,
        persistence,
        energy,
    )
