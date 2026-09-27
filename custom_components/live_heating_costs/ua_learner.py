"""Learn the house heat-loss coefficient (UA, kW/°C) from self-logged history.

Mirrors the sibling `load-need-predictor`'s philosophy: a seeded prior, an
online EWMA drift correction fed by daily outcomes, a seed/learned blend that
only trusts the learned value once enough samples have accumulated, and a
shadow accuracy metric (rolling MAE) so the model's quality is observable
before it is trusted.

Heat demand for a day is modelled as ``E ≈ UA × degree_hours`` (integrated
°C·h against a configurable base temperature — see :mod:`price_source`'s
sibling docs and ``docs/heating-costs.md``). Each day's *actual* delivered
heat (Σ pump ``energy × COP`` + resistive kWh + logged/estimated wood) is
logged as a :class:`DailyRow`; :func:`ingest` folds a quality-gated row into
the learned ratio ``delivered_kwh / degree_hours``.

Pure module — no Home Assistant imports, no ``datetime.now()``/``date.today()``.
All state is immutable (:func:`ingest` / :func:`note_prediction` return a new
``UaState``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .const import (
    UA_DELIVERED_RANGE_KWH,
    UA_EWMA_ALPHA_BACKFILL,
    UA_EWMA_ALPHA_LIVE,
    UA_LEARNED_CLAMP,
    UA_MIN_COVERAGE,
    UA_MIN_DEGREE_HOURS,
    UA_ROWS_CAP,
    UA_SEED_BLEND_SAMPLES,
)

# EWMA weight on the newest day's |predicted - delivered| error (not in
# const.py — it tunes the shadow accuracy metric only, not the UA model).
_MAE_ALPHA = 0.2


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` to the inclusive ``[lo, hi]`` range."""
    return max(lo, min(value, hi))


@dataclass(frozen=True)
class DailyRow:
    """One logged day's heat-demand outcome.

    ``degree_hours`` is the day's integrated °C·h against the configured base
    temperature; ``delivered_kwh`` is the day's total delivered heat (Σ pump
    ``energy × COP`` + resistive kWh + wood, logged or estimated). ``quality``
    gates whether the row is trusted to teach the UA model (see
    :func:`make_row`); it is still stored and shown even when ``False``.
    """

    date: str  # local ISO date, e.g. "2026-07-08"
    degree_hours: float
    delivered_kwh: float
    electricity_kwh: float
    wood_kwh: float
    quality: bool
    backfilled: bool = False
    wood_estimated: bool = False

    def to_dict(self) -> dict:
        """JSON-safe representation."""
        return {
            "date": self.date,
            "degree_hours": self.degree_hours,
            "delivered_kwh": self.delivered_kwh,
            "electricity_kwh": self.electricity_kwh,
            "wood_kwh": self.wood_kwh,
            "quality": self.quality,
            "backfilled": self.backfilled,
            "wood_estimated": self.wood_estimated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DailyRow:
        return cls(
            date=d["date"],
            degree_hours=d["degree_hours"],
            delivered_kwh=d["delivered_kwh"],
            electricity_kwh=d["electricity_kwh"],
            wood_kwh=d["wood_kwh"],
            quality=d["quality"],
            backfilled=d.get("backfilled", False),
            wood_estimated=d.get("wood_estimated", False),
        )


@dataclass(frozen=True)
class UaState:
    """The learnable + seeded UA model. Immutable; updates return a new state.

    ``seed`` is the configured prior (kW/°C, derived from
    ``CONF_UA_SEED_W_PER_C``). ``learned`` is the EWMA-tracked ratio once at
    least one quality row has been ingested; :func:`effective_ua` blends the
    two by ``sample_count``. ``rolling_mae_kwh`` and ``last_predicted_kwh``
    are the shadow-accuracy bookkeeping (see :func:`note_prediction` /
    :func:`ingest`).
    """

    seed: float
    learned: float | None = None
    sample_count: int = 0
    rolling_mae_kwh: float | None = None
    last_predicted_kwh: float | None = None
    rows: tuple[DailyRow, ...] = ()

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "learned": self.learned,
            "sample_count": self.sample_count,
            "rolling_mae_kwh": self.rolling_mae_kwh,
            "last_predicted_kwh": self.last_predicted_kwh,
            "rows": [r.to_dict() for r in self.rows],
        }

    @classmethod
    def from_dict(cls, d: dict) -> UaState:
        return cls(
            seed=d["seed"],
            learned=d.get("learned"),
            sample_count=d.get("sample_count", 0),
            rolling_mae_kwh=d.get("rolling_mae_kwh"),
            last_predicted_kwh=d.get("last_predicted_kwh"),
            rows=tuple(DailyRow.from_dict(r) for r in d.get("rows", ())),
        )


def make_row(
    date: str,
    degree_hours: float,
    delivered_kwh: float,
    electricity_kwh: float,
    wood_kwh: float,
    backfilled: bool = False,
    wood_estimated: bool = False,
    coverage: float = 1.0,
) -> DailyRow:
    """Build a :class:`DailyRow`, applying the quality gate from ``const``.

    A day only teaches the model when it had a meaningful heating load
    (``degree_hours >= UA_MIN_DEGREE_HOURS``), the logged delivered heat is
    plausible (inside ``UA_DELIVERED_RANGE_KWH`` — guards against meter
    resets / missing wood logging producing a wild ratio), and the outdoor
    temperature was observed for most of it (``coverage >= UA_MIN_COVERAGE``,
    the fraction of the day with a known reading). Heat keeps accumulating
    through a temperature outage while degree-hours stall, so a half-observed
    day would otherwise teach roughly twice the true UA.
    """
    lo, hi = UA_DELIVERED_RANGE_KWH
    quality = (
        degree_hours >= UA_MIN_DEGREE_HOURS
        and lo <= delivered_kwh <= hi
        and coverage >= UA_MIN_COVERAGE
    )
    return DailyRow(
        date=date,
        degree_hours=degree_hours,
        delivered_kwh=delivered_kwh,
        electricity_kwh=electricity_kwh,
        wood_kwh=wood_kwh,
        quality=quality,
        backfilled=backfilled,
        wood_estimated=wood_estimated,
    )


def ingest(state: UaState, row: DailyRow) -> UaState:
    """Fold one day's outcome into the state. Pure — returns a new ``UaState``.

    Rows are kept in a ring capped at ``UA_ROWS_CAP`` (oldest dropped first,
    ordered by date). A date already present is never learned twice: a live
    row replaces the stored one for display but leaves ``learned`` and
    ``sample_count`` alone, and a backfilled row never overwrites an existing
    row at all (the live capture saw wood and the full day; backfill cannot).

    Quality rows update ``learned`` via an EWMA of the day's
    ``delivered_kwh / degree_hours`` ratio (backfilled rows use a slower
    alpha so bulk-imported history can't dominate live learning) and are
    clamped to ``UA_LEARNED_CLAMP × seed`` so a bad day can't run away with
    the model. Non-backfilled rows also update the shadow-accuracy
    ``rolling_mae_kwh`` against whatever prediction :func:`note_prediction`
    last stashed (regardless of that row's own quality gate — the prediction
    was for the demand that *did* happen).
    """
    existing = next((r for r in state.rows if r.date == row.date), None)
    if existing is not None and row.backfilled:
        return state
    rows = [r for r in state.rows if r.date != row.date]
    rows.append(row)
    rows.sort(key=lambda r: r.date)
    if len(rows) > UA_ROWS_CAP:
        rows = rows[-UA_ROWS_CAP:]

    learned = state.learned
    sample_count = state.sample_count
    if row.quality and existing is None:
        ratio = row.delivered_kwh / row.degree_hours
        alpha = UA_EWMA_ALPHA_BACKFILL if row.backfilled else UA_EWMA_ALPHA_LIVE
        learned = ratio if learned is None else (1 - alpha) * learned + alpha * ratio
        lo, hi = UA_LEARNED_CLAMP
        learned = _clamp(learned, lo * state.seed, hi * state.seed)
        sample_count += 1

    rolling_mae = state.rolling_mae_kwh
    last_predicted = state.last_predicted_kwh
    if last_predicted is not None and not row.backfilled:
        error = abs(last_predicted - row.delivered_kwh)
        rolling_mae = (
            error if rolling_mae is None else (1 - _MAE_ALPHA) * rolling_mae + _MAE_ALPHA * error
        )
        last_predicted = None  # consumed by this outcome

    return replace(
        state,
        rows=tuple(rows),
        learned=learned,
        sample_count=sample_count,
        rolling_mae_kwh=rolling_mae,
        last_predicted_kwh=last_predicted,
    )


def effective_ua(state: UaState) -> float:
    """The UA value to use right now: seed until learned, then blended in.

    Weight ``w = min(sample_count, UA_SEED_BLEND_SAMPLES) / UA_SEED_BLEND_SAMPLES``
    ramps the learned value in linearly over the first
    ``UA_SEED_BLEND_SAMPLES`` quality samples.
    """
    if state.learned is None:
        return state.seed
    w = min(state.sample_count, UA_SEED_BLEND_SAMPLES) / UA_SEED_BLEND_SAMPLES
    return state.seed * (1 - w) + state.learned * w


def predict_daily_kwh(state: UaState, degree_hours: float) -> float:
    """Predicted daily delivered heat (kWh) = effective UA × degree_hours."""
    return effective_ua(state) * degree_hours


def note_prediction(state: UaState, predicted_kwh: float) -> UaState:
    """Stash a prediction so the next :func:`ingest` can score it for MAE."""
    return replace(state, last_predicted_kwh=predicted_kwh)
