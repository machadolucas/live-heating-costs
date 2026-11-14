"""Load pure integration modules without executing the HA-only package init."""

from __future__ import annotations

import sys
import types
from pathlib import Path

_PACKAGE = "_live_heating_costs_unit"
_PACKAGE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "live_heating_costs"


def install_pure_package() -> None:
    """Register an isolated namespace for pure modules and their relative imports."""
    if _PACKAGE in sys.modules:
        return
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_PACKAGE_PATH)]
    package.__package__ = _PACKAGE
    sys.modules[_PACKAGE] = package


install_pure_package()
