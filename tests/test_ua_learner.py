"""Unit tests for the UA (heat-loss coefficient) learner (pure, no Home Assistant)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.live_heating_costs.const import (
    UA_DELIVERED_RANGE_KWH,
    UA_EWMA_ALPHA_BACKFILL,
    UA_EWMA_ALPHA_LIVE,
    UA_LEARNED_CLAMP,
    UA_MIN_DEGREE_HOURS,
    UA_ROWS_CAP,
    UA_SEED_BLEND_SAMPLES,
)
from custom_components.live_heating_costs.ua_learner import (
    DailyRow,
    UaState,
    effective_ua,
    ingest,
    make_row,
    note_prediction,
    predict_daily_kwh,
)

SEED = 0.10
TRUE_UA = 0.14


def _iso_date(day: int) -> str:
    return f"2026-01-{day:02d}"


def _noise_free_row(day: int, degree_hours: float, backfilled: bool = False) -> DailyRow:
    return make_row(
        date=_iso_date(day),
        degree_hours=degree_hours,
        delivered_kwh=TRUE_UA * degree_hours,
        electricity_kwh=TRUE_UA * degree_hours,
        wood_kwh=0.0,
        backfilled=backfilled,
    )


# --------------------------------------------------------------------------- #
# convergence + seed blend
# --------------------------------------------------------------------------- #


def test_synthetic_winter_convergence():
    state = UaState(seed=SEED)
    for day in range(1, 31):
        degree_hours = 60.0 + day  # varies but ratio stays exactly TRUE_UA
        row = _noise_free_row(day, degree_hours)
        state = ingest(state, row)
    assert state.sample_count == 30
    assert effective_ua(state) > 0.135
    assert effective_ua(state) == pytest.approx(TRUE_UA, abs=1e-9)


def test_seed_blend_monotone_weight():
    state = UaState(seed=SEED)
    state_after_1 = ingest(state, _noise_free_row(1, 100.0))
    w1 = 1 / UA_SEED_BLEND_SAMPLES
    expected_after_1 = SEED * (1 - w1) + TRUE_UA * w1
    assert effective_ua(state_after_1) == pytest.approx(expected_after_1)

    state_n = state
    for day in range(1, UA_SEED_BLEND_SAMPLES + 1):
        state_n = ingest(state_n, _noise_free_row(day, 100.0))
    assert state_n.sample_count == UA_SEED_BLEND_SAMPLES
    assert effective_ua(state_n) == pytest.approx(TRUE_UA)

    # after 1 sample the blend weight is far smaller than after UA_SEED_BLEND_SAMPLES.
    assert abs(effective_ua(state_after_1) - SEED) < abs(effective_ua(state_n) - SEED)
    assert effective_ua(state_after_1) < effective_ua(state_n)


# --------------------------------------------------------------------------- #
# quality gate
# --------------------------------------------------------------------------- #


def test_quality_gate_rejects_low_degree_hours():
    state = ingest(UaState(seed=SEED), _noise_free_row(1, 100.0))
    baseline_learned = state.learned
    baseline_count = state.sample_count

    low_dh = UA_MIN_DEGREE_HOURS - 1.0
    row = make_row(
        date=_iso_date(2),
        degree_hours=low_dh,
        delivered_kwh=TRUE_UA * low_dh if low_dh > 0 else 1.0,
        electricity_kwh=1.0,
        wood_kwh=0.0,
    )
    assert row.quality is False
    state2 = ingest(state, row)
    assert state2.sample_count == baseline_count
    assert state2.learned == baseline_learned
    # the row is still stored (kept for the history/audit trail).
    assert any(r.date == _iso_date(2) for r in state2.rows)


def test_quality_gate_rejects_delivered_out_of_range():
    state = ingest(UaState(seed=SEED), _noise_free_row(1, 100.0))
    baseline_learned = state.learned
    baseline_count = state.sample_count

    lo, hi = UA_DELIVERED_RANGE_KWH
    row = make_row(
        date=_iso_date(2),
        degree_hours=100.0,
        delivered_kwh=hi + 50.0,  # absurdly high, e.g. a meter glitch
        electricity_kwh=hi + 50.0,
        wood_kwh=0.0,
    )
    assert row.quality is False
    state2 = ingest(state, row)
    assert state2.sample_count == baseline_count
    assert state2.learned == baseline_learned
    assert lo <= hi  # sanity on the const shape used above


# --------------------------------------------------------------------------- #
# clamp
# --------------------------------------------------------------------------- #


def test_clamp_prevents_runaway_learned():
    state = UaState(seed=SEED)
    degree_hours = 100.0
    # keep delivered inside the quality gate's plausible range but the *ratio*
    # (delivered/degree_hours) is set far above 3x seed.
    absurd_delivered = min(degree_hours * TRUE_UA * 20, UA_DELIVERED_RANGE_KWH[1] - 1)
    for day in range(1, 51):
        row = make_row(
            date=_iso_date(day),
            degree_hours=degree_hours,
            delivered_kwh=absurd_delivered,
            electricity_kwh=absurd_delivered,
            wood_kwh=0.0,
        )
        state = ingest(state, row)
    hi = UA_LEARNED_CLAMP[1] * SEED
    assert state.learned is not None
    assert state.learned <= hi + 1e-9


# --------------------------------------------------------------------------- #
# backfilled alpha
# --------------------------------------------------------------------------- #


def test_backfilled_rows_move_learned_less_than_live():
    base_state = ingest(UaState(seed=SEED), _noise_free_row(1, 100.0))
    baseline_learned = base_state.learned
    assert baseline_learned is not None

    new_ratio_dh = 100.0
    new_ratio_delivered = 0.20 * new_ratio_dh  # a different ratio than TRUE_UA

    live_row = make_row(
        date=_iso_date(2),
        degree_hours=new_ratio_dh,
        delivered_kwh=new_ratio_delivered,
        electricity_kwh=new_ratio_delivered,
        wood_kwh=0.0,
        backfilled=False,
    )
    backfilled_row = make_row(
        date=_iso_date(2),
        degree_hours=new_ratio_dh,
        delivered_kwh=new_ratio_delivered,
        electricity_kwh=new_ratio_delivered,
        wood_kwh=0.0,
        backfilled=True,
    )

    live_state = ingest(base_state, live_row)
    backfill_state = ingest(base_state, backfilled_row)

    live_move = abs(live_state.learned - baseline_learned)
    backfill_move = abs(backfill_state.learned - baseline_learned)
    assert backfill_move < live_move
    assert UA_EWMA_ALPHA_BACKFILL < UA_EWMA_ALPHA_LIVE


# --------------------------------------------------------------------------- #
# ring: same-date replace + cap
# --------------------------------------------------------------------------- #


def test_same_date_row_replaces():
    state = ingest(UaState(seed=SEED), _noise_free_row(1, 100.0))
    assert len(state.rows) == 1
    assert state.rows[0].delivered_kwh == pytest.approx(TRUE_UA * 100.0)

    new_delivered = UA_DELIVERED_RANGE_KWH[1] - 1.0  # distinct from the original row, still valid
    replacement = make_row(
        date=_iso_date(1),
        degree_hours=100.0,
        delivered_kwh=new_delivered,
        electricity_kwh=1.0,
        wood_kwh=0.0,
    )
    state2 = ingest(state, replacement)
    assert len(state2.rows) == 1
    assert state2.rows[0].delivered_kwh == pytest.approx(replacement.delivered_kwh)


def test_ring_cap_drops_oldest():
    state = UaState(seed=SEED)
    n = UA_ROWS_CAP + 5
    base = date(2026, 1, 1)
    all_dates = [(base + timedelta(days=i)).isoformat() for i in range(n)]
    for d in all_dates:
        row = DailyRow(
            date=d,
            degree_hours=50.0,
            delivered_kwh=5.0,
            electricity_kwh=5.0,
            wood_kwh=0.0,
            quality=False,  # keep this test focused on ring behaviour, not learning
        )
        state = ingest(state, row)
    assert len(state.rows) == UA_ROWS_CAP
    dates = [r.date for r in state.rows]
    assert dates == sorted(dates)
    # the oldest 5 dates were evicted; the newest UA_ROWS_CAP dates remain.
    assert all_dates[0] not in dates
    assert all_dates[-1] in dates
    assert dates == all_dates[-UA_ROWS_CAP:]


# --------------------------------------------------------------------------- #
# MAE + round-trip
# --------------------------------------------------------------------------- #


def test_mae_updates_after_note_prediction_and_ingest():
    state = UaState(seed=SEED)
    state = note_prediction(state, predicted_kwh=10.0)
    assert state.last_predicted_kwh == 10.0

    row = make_row(
        date=_iso_date(1),
        degree_hours=100.0,
        delivered_kwh=12.0,
        electricity_kwh=12.0,
        wood_kwh=0.0,
    )
    state2 = ingest(state, row)
    assert state2.rolling_mae_kwh == pytest.approx(2.0)
    assert state2.last_predicted_kwh is None  # consumed


def test_mae_not_updated_for_backfilled_rows():
    state = UaState(seed=SEED)
    state = note_prediction(state, predicted_kwh=10.0)
    row = make_row(
        date=_iso_date(1),
        degree_hours=100.0,
        delivered_kwh=12.0,
        electricity_kwh=12.0,
        wood_kwh=0.0,
        backfilled=True,
    )
    state2 = ingest(state, row)
    assert state2.rolling_mae_kwh is None
    assert state2.last_predicted_kwh == 10.0  # not consumed by a backfilled row


def test_predict_daily_kwh_matches_effective_ua():
    state = ingest(UaState(seed=SEED), _noise_free_row(1, 100.0))
    dh = 42.0
    assert predict_daily_kwh(state, dh) == pytest.approx(effective_ua(state) * dh)


def test_state_to_dict_from_dict_round_trip():
    state = UaState(seed=SEED)
    for day in range(1, 5):
        state = ingest(state, _noise_free_row(day, 80.0 + day, backfilled=(day % 2 == 0)))
    state = note_prediction(state, predicted_kwh=11.0)
    state = ingest(state, _noise_free_row(5, 90.0))

    d = state.to_dict()
    restored = UaState.from_dict(d)
    assert restored == state


def test_daily_row_to_dict_from_dict_round_trip():
    row = make_row(
        date="2026-02-01",
        degree_hours=88.0,
        delivered_kwh=12.3,
        electricity_kwh=10.0,
        wood_kwh=2.3,
        backfilled=True,
        wood_estimated=True,
    )
    restored = DailyRow.from_dict(row.to_dict())
    assert restored == row
