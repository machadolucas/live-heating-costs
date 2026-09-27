"""Unit tests for curves.py (pure, no Home Assistant)."""

from __future__ import annotations

import pytest

from custom_components.live_heating_costs.curves import (
    PRESETS,
    Curve,
    CurveParseError,
    parse_curve_text,
    points_to_text,
    validate_points,
)

# --------------------------------------------------------------------------- #
# Curve.value_at — interpolation, exact knots, clamping
# --------------------------------------------------------------------------- #


def test_value_at_midpoint_interpolates_linearly():
    curve = Curve.from_points([(0.0, 1.0), (10.0, 2.0)])
    assert curve.value_at(5.0) == pytest.approx(1.5)


def test_value_at_exact_knot():
    curve = Curve.from_points([(0.0, 1.0), (10.0, 2.0), (20.0, 4.0)])
    assert curve.value_at(0.0) == pytest.approx(1.0)
    assert curve.value_at(10.0) == pytest.approx(2.0)
    assert curve.value_at(20.0) == pytest.approx(4.0)


def test_value_at_clamps_below_first_point():
    curve = Curve.from_points([(0.0, 1.0), (10.0, 2.0)])
    assert curve.value_at(-100.0) == pytest.approx(1.0)


def test_value_at_clamps_above_last_point():
    curve = Curve.from_points([(0.0, 1.0), (10.0, 2.0)])
    assert curve.value_at(100.0) == pytest.approx(2.0)


def test_from_points_sorts_out_of_order_input():
    curve = Curve.from_points([(10.0, 2.0), (0.0, 1.0)])
    assert curve.points == ((0.0, 1.0), (10.0, 2.0))


# --------------------------------------------------------------------------- #
# Preset spot checks (docs/heating-costs.md "Recommended modeling approach")
# --------------------------------------------------------------------------- #


def test_fd25_cop_exact_knot_at_minus_15():
    cop_curve = Curve.from_points(PRESETS["msz_fd25va"].cop_points)
    assert cop_curve.value_at(-15.0) == pytest.approx(2.15)


def test_fd25_cop_clamps_above_top_knot_13c():
    # Table tops out at (13, 3.8); +20 must clamp flat, not extrapolate the dip.
    cop_curve = Curve.from_points(PRESETS["msz_fd25va"].cop_points)
    assert cop_curve.value_at(20.0) == pytest.approx(3.8)


def test_rw35_capacity_exact_knot_at_minus_25():
    cap_curve = Curve.from_points(PRESETS["msz_rw35vg"].capacity_points)
    assert cap_curve.value_at(-25.0) == pytest.approx(4.0)


def test_rw35_capacity_clamps_above_top_knot_7c():
    cap_curve = Curve.from_points(PRESETS["msz_rw35vg"].capacity_points)
    assert cap_curve.value_at(10.0) == pytest.approx(6.6)


def test_presets_have_expected_keys_and_labels():
    assert set(PRESETS) == {"msz_fd25va", "msz_rw35vg"}
    assert PRESETS["msz_fd25va"].label == "Mitsubishi MSZ-FD25VA (measured, VTT)"
    assert PRESETS["msz_rw35vg"].label == "Mitsubishi MSZ-RW35VG (EN 14825)"


def test_preset_cop_curves_are_internally_valid():
    for preset in PRESETS.values():
        assert not validate_points(list(preset.cop_points))
        assert not validate_points(list(preset.capacity_points))


# --------------------------------------------------------------------------- #
# parse_curve_text — "t:v" text format
# --------------------------------------------------------------------------- #


def test_parse_text_comma_separated():
    points = parse_curve_text("-30:1.35, -25:1.6, 7:4.2")
    assert points == [(-30.0, 1.35), (-25.0, 1.6), (7.0, 4.2)]


def test_parse_text_newline_separated():
    points = parse_curve_text("-30:1.35\n-25:1.6\n7:4.2")
    assert points == [(-30.0, 1.35), (-25.0, 1.6), (7.0, 4.2)]


def test_parse_text_tolerates_extra_whitespace():
    points = parse_curve_text("  -30 : 1.35 ,\n   -25:1.6  ")
    assert points == [(-30.0, 1.35), (-25.0, 1.6)]


# --------------------------------------------------------------------------- #
# parse_curve_text — JSON format
# --------------------------------------------------------------------------- #


def test_parse_json_list_of_pairs():
    points = parse_curve_text("[[-30, 1.35], [-25, 1.6]]")
    assert points == [(-30.0, 1.35), (-25.0, 1.6)]


def test_parse_json_whitespace_tolerant():
    points = parse_curve_text("  [ [-30, 1.35] , [7, 4.2] ]  ")
    assert points == [(-30.0, 1.35), (7.0, 4.2)]


# --------------------------------------------------------------------------- #
# parse errors
# --------------------------------------------------------------------------- #


def test_parse_empty_text_raises():
    with pytest.raises(CurveParseError):
        parse_curve_text("   ")


def test_parse_garbage_text_raises():
    with pytest.raises(CurveParseError):
        parse_curve_text("this is not a curve")


def test_parse_missing_colon_raises():
    with pytest.raises(CurveParseError):
        parse_curve_text("-30, -25:1.6")


def test_parse_non_numeric_value_raises():
    with pytest.raises(CurveParseError):
        parse_curve_text("cold:1.6")


def test_parse_bad_json_raises():
    with pytest.raises(CurveParseError):
        parse_curve_text("[[-30, 1.35], [-25")


def test_from_points_single_point_raises():
    with pytest.raises(CurveParseError):
        Curve.from_points([(0.0, 1.0)])


def test_from_points_duplicate_x_raises():
    with pytest.raises(CurveParseError):
        Curve.from_points([(0.0, 1.0), (0.0, 2.0), (5.0, 3.0)])


def test_from_points_non_positive_y_raises():
    with pytest.raises(CurveParseError):
        Curve.from_points([(0.0, 1.0), (5.0, 0.0)])


def test_from_points_negative_y_raises():
    with pytest.raises(CurveParseError):
        Curve.from_points([(0.0, 1.0), (5.0, -2.0)])


def test_from_points_non_finite_raises():
    with pytest.raises(CurveParseError):
        Curve.from_points([(0.0, 1.0), (float("nan"), 2.0)])


def test_validate_points_does_not_require_monotonicity():
    # FD25-shaped: COP rises then dips again above +10 — must be accepted.
    errors = validate_points([(-10.0, 2.5), (0.0, 3.3), (7.0, 4.2), (13.0, 3.8)])
    assert errors == []


def test_validate_points_reports_multiple_problems():
    errors = validate_points([(0.0, 1.0)])
    assert len(errors) == 1
    assert "at least 2 points" in errors[0]


# --------------------------------------------------------------------------- #
# points_to_text round-trip
# --------------------------------------------------------------------------- #


def test_points_to_text_round_trips_through_parse():
    original = [(-30.0, 1.35), (-25.0, 1.6), (7.0, 4.2), (13.0, 3.8)]
    text = points_to_text(original)
    round_tripped = parse_curve_text(text)
    assert len(round_tripped) == len(original)
    for (x0, y0), (x1, y1) in zip(original, round_tripped, strict=True):
        assert x1 == pytest.approx(x0)
        assert y1 == pytest.approx(y0)


def test_points_to_text_format():
    assert points_to_text([(-30.0, 1.35), (7.0, 4.2)]) == "-30:1.35, 7:4.2"


@pytest.mark.parametrize(
    "text",
    [
        '[{"t": 1, "v": 2}, {"t": 3, "v": 4}]',  # dicts: used to escape as KeyError
        "[[1, 2, 3], [4, 5, 6]]",  # 3-element rows
        "[[1], [2]]",
        "[1, 2]",
        '[["a", 2], [3, 4]]',
        "[[1, null], [3, 4]]",
    ],
)
def test_parse_json_malformed_points_raise_parse_error(text):
    with pytest.raises(CurveParseError):
        parse_curve_text(text)
