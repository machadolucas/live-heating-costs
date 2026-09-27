"""Piecewise-linear lookup curves for heat-pump COP(T) and capacity(T).

Pure module (no Home Assistant import): a :class:`Curve` is a small, ordered set
of ``(temperature_c, value)`` points interpolated linearly between knots and
clamped flat beyond the endpoints — the modelling approach recommended in
``docs/heating-costs.md`` ("Recommended modeling approach"). Text/JSON parsing
here backs the config-flow text box that lets a user paste or hand-edit a
curve; :data:`PRESETS` are the two measured/published pump tables from that doc.

COP curves are **not required to be monotone** — the FD25 real-operation curve
dips above +10 °C (mild-weather cycling/defrost behaviour), so
:func:`validate_points` only rejects structurally broken input (too few points,
duplicate x, non-finite values, non-positive y), never non-monotonicity.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass


class CurveParseError(ValueError):
    """Raised when curve text/points can't be parsed into a valid Curve."""


def validate_points(points: list[tuple[float, float]]) -> list[str]:
    """Return a list of human-readable problems with ``points`` (empty = OK).

    Checks: at least 2 points, no duplicate x, all values finite, y > 0.
    Does **not** check monotonicity of y (COP curves are not monotone).
    """
    errors: list[str] = []
    if len(points) < 2:
        errors.append(f"a curve needs at least 2 points, got {len(points)}")

    seen_x: set[float] = set()
    for x, y in points:
        if not math.isfinite(x) or not math.isfinite(y):
            errors.append(f"non-finite point: ({x}, {y})")
            continue
        if x in seen_x:
            errors.append(f"duplicate x value: {x}")
        seen_x.add(x)
        if y <= 0:
            errors.append(f"y must be > 0, got {y} at x={x}")
    return errors


@dataclass(frozen=True)
class Curve:
    """A validated, x-sorted piecewise-linear curve."""

    points: tuple[tuple[float, float], ...]

    def value_at(self, x: float) -> float:
        """Interpolate at ``x``, clamped flat at both endpoints."""
        pts = self.points
        if x <= pts[0][0]:
            return pts[0][1]
        if x >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            if x0 <= x <= x1:
                if x1 == x0:
                    return y0
                return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
        return pts[-1][1]  # unreachable given the bounds checks above

    @classmethod
    def from_points(
        cls, points: list[tuple[float, float]] | tuple[tuple[float, float], ...]
    ) -> Curve:
        """Validate and sort ``points`` into a :class:`Curve`.

        Raises :class:`CurveParseError` if ``points`` fails :func:`validate_points`.
        """
        pts = [(float(x), float(y)) for x, y in points]
        errors = validate_points(pts)
        if errors:
            raise CurveParseError("; ".join(errors))
        return cls(points=tuple(sorted(pts, key=lambda p: p[0])))


def parse_curve_text(text: str) -> list[tuple[float, float]]:
    """Parse a curve from either a ``"t:v, t:v, ..."`` text box or a JSON list.

    Accepts comma- and/or newline-separated ``temp:value`` pairs (e.g.
    ``"-30:1.35, -25:1.6, 7:4.2"``) or JSON ``[[-30, 1.35], [-25, 1.6]]``.
    Raises :class:`CurveParseError` with a message pointing at the bad token.
    """
    text = text.strip()
    if not text:
        raise CurveParseError("curve text is empty")

    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise CurveParseError(f"invalid JSON curve: {err}") from err
        if not isinstance(data, list):
            raise CurveParseError("JSON curve must be a list of [temp, value] pairs")
        parsed: list[tuple[float, float]] = []
        for p in data:
            # Exactly [t, v]: a dict ({"t":..}) would KeyError on p[0], and a
            # 3-element row silently dropping its tail hides a typo.
            if not isinstance(p, list) or len(p) != 2:
                raise CurveParseError(f"expected a [temp, value] pair, got {p!r}")
            try:
                parsed.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError) as err:
                raise CurveParseError(f"invalid JSON curve point {p!r}: {err}") from err
        return parsed

    tokens = [t.strip() for t in re.split(r"[,\n]+", text) if t.strip()]
    if not tokens:
        raise CurveParseError("no points found in curve text")

    points: list[tuple[float, float]] = []
    for token in tokens:
        if ":" not in token:
            raise CurveParseError(f"expected 'temp:value' pair, got {token!r}")
        x_s, _, y_s = token.partition(":")
        try:
            points.append((float(x_s.strip()), float(y_s.strip())))
        except ValueError as err:
            raise CurveParseError(f"could not parse numbers from {token!r}: {err}") from err
    return points


def points_to_text(points: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> str:
    """Render points back to the ``"t:v, t:v, ..."`` text-box format (prefill)."""
    return ", ".join(f"{x:g}:{y:g}" for x, y in points)


@dataclass(frozen=True)
class PumpPreset:
    """A built-in heat-pump COP(T)/capacity(T) table with its operating bounds."""

    key: str
    label: str
    cop_points: tuple[tuple[float, float], ...]
    capacity_points: tuple[tuple[float, float], ...]
    operating_limit_c: float
    min_cop: float
    max_cop: float


PRESETS: dict[str, PumpPreset] = {
    "msz_fd25va": PumpPreset(
        key="msz_fd25va",
        label="Mitsubishi MSZ-FD25VA (measured, VTT)",
        # COP: VTT-measured real-operation curve, sliding 1 h mean incl. defrost
        # (report VTT-S-09606-08). Not monotone: drops above +10 °C as the unit
        # cycles at mild-weather part load.
        cop_points=(
            (-30.0, 1.35),
            (-25.0, 1.6),
            (-20.0, 1.85),
            (-15.0, 2.15),
            (-10.0, 2.5),
            (-7.0, 2.7),
            (-5.0, 2.9),
            (0.0, 3.3),
            (2.0, 3.6),
            (5.0, 4.0),
            (7.0, 4.2),
            (10.0, 4.2),
            (13.0, 3.8),
        ),
        # Capacity: 6.3 kW max (published) down to VTT's measured cold-weather
        # points.
        capacity_points=(
            (-30.0, 1.5),
            (-25.0, 2.0),
            (-15.0, 3.0),
            (7.0, 6.3),
        ),
        operating_limit_c=-28.0,
        min_cop=0.5,
        max_cop=6.5,
    ),
    "msz_rw35vg": PumpPreset(
        key="msz_rw35vg",
        label="Mitsubishi MSZ-RW35VG (EN 14825)",
        # COP: Eurovent EN 14825 load-following declared points; the (-25, 1.9)
        # point is an estimate (no published data that cold).
        cop_points=(
            (-25.0, 1.9),
            (-10.0, 2.9),
            (-7.0, 3.2),
            (2.0, 5.1),
            (7.0, 6.5),
            (12.0, 8.1),
        ),
        capacity_points=(
            (-25.0, 4.0),
            (-15.0, 5.3),
            (7.0, 6.6),
        ),
        operating_limit_c=-30.0,
        min_cop=0.5,
        max_cop=9.0,
    ),
}
