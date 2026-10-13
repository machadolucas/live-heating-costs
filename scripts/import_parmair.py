"""Extract manufacturer fan measurements without executing workbook formulas.

Usage: python scripts/import_parmair.py SOURCE.xlsx OUTPUT.json
Only numeric measurements and public source provenance enter the output; the
original workbook and its local path are deliberately not bundled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_RAW_SHEET = "Mittaustulokset raakadata"
_BLOCKS = ((100, 7, 18), (80, 21, 32), (70, 35, 45), (60, 48, 57), (50, 60, 68), (40, 71, 79))


def extract(source: Path) -> dict:
    """Read the raw worksheet by name; reject nonnumeric or malformed data."""
    with ZipFile(source) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            row.attrib["Id"]: row.attrib["Target"]
            for row in relationships.findall(f"{{{_REL}}}Relationship")
        }
        sheets = workbook.findall("m:sheets/m:sheet", _NS)

        def worksheet(name: str) -> dict:
            sheet = next((row for row in sheets if row.attrib["name"] == name), None)
            if sheet is None:
                raise ValueError(f"missing worksheet {name}")
            target = targets[sheet.attrib[_RID]]
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            root = ElementTree.fromstring(archive.read(member))
            return {row.attrib["r"]: row for row in root.findall("m:sheetData/m:row/m:c", _NS)}

        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_strings = [
                "".join(node.itertext())
                for node in ElementTree.fromstring(archive.read("xl/sharedStrings.xml")).findall(
                    "m:si", _NS
                )
            ]

        def text(cell: ElementTree.Element | None) -> str:
            if cell is None:
                return ""
            if cell.attrib.get("t") == "s":
                value = cell.find("m:v", _NS)
                return shared_strings[int(value.text)] if value is not None else ""
            inline = cell.find("m:is", _NS)
            return "".join(inline.itertext()) if inline is not None else ""

        identity = worksheet("Mitoitus laskenta")
        if "Parmair 120" not in text(identity.get("B11")) or "G3G146-ED19-11" not in text(
            identity.get("B16")
        ):
            raise ValueError("workbook does not identify the supported Parmair 120 fans")
        cells = worksheet(_RAW_SHEET)

        def number(address: str) -> float | None:
            cell = cells.get(address)
            if cell is None:
                return None
            value = cell.find("m:v", _NS)
            if value is None or value.text is None:
                return None
            if cell.find("m:f", _NS) is not None or cell.attrib.get("t", "n") != "n":
                raise ValueError(f"{_RAW_SHEET}!{address} is not a raw measurement")
            result = float(value.text)
            if not math.isfinite(result) or result < 0:
                raise ValueError(f"invalid measurement at {_RAW_SHEET}!{address}")
            return result

        curves: dict[str, list[dict]] = {"supply": [], "extract": []}
        for side, columns in (
            ("supply", ("C", "D", "I", "J")),
            ("extract", ("O", "P", "U", "V")),
        ):
            for control, first, last in _BLOCKS:
                label_column = "B" if side == "supply" else "N"
                if not text(cells.get(f"{label_column}{first - 1}")).startswith(f"{control}%"):
                    raise ValueError(f"missing {side} control label at {control}%")
                points = []
                for row in range(first, last + 1):
                    values = [number(f"{column}{row}") for column in columns]
                    if all(value is None for value in values):
                        continue
                    if any(value is None for value in values):
                        raise ValueError(f"incomplete {side} measurement at row {row}")
                    points.append(
                        {
                            "flow_ls": values[0],
                            "pressure_pa": values[1],
                            "power_w": values[2],
                            "rpm": values[3],
                        }
                    )
                points.sort(key=lambda point: point["flow_ls"])
                if len(points) < 2 or any(
                    b["flow_ls"] <= a["flow_ls"] or b["pressure_pa"] >= a["pressure_pa"]
                    for a, b in zip(points, points[1:], strict=False)
                ):
                    raise ValueError(f"nonmonotonic {side} pressure curve at {control}%")
                curves[side].append(
                    {
                        "control_percent": control,
                        "source_range": f"{_RAW_SHEET}!{columns[0]}{first}:{columns[-1]}{last}",
                        "points": points,
                    }
                )
        idle = number("C2")
        if idle is None:
            raise ValueError("missing idle power")

    return {
        "schema_version": 1,
        "model": "Parmair 120",
        "fans": ["EBM-PAPST G3G146-ED19-11", "EBM-PAPST G3G146-ED19-16"],
        "provenance": {
            "filename": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "measurement_identity_range": "Mitoitus laskenta!B11:B16",
            "idle_power_range": f"{_RAW_SHEET}!C2",
            "description": (
                "Manufacturer workbook laboratory measurements; "
                "SFS-EN 13141-7 and SFS-EN ISO 5801, installation D"
            ),
            "rights_note": (
                "Numeric measurement extraction; "
                "no workbook, images or workbook formulas included. "
                "Source licensing is not declared in the workbook."
            ),
        },
        "units": {"flow": "l/s", "pressure": "Pa", "power": "W", "control": "%"},
        "conditions": {
            "air_density_kg_m3": 1.2,
            "control_min_percent": 40,
            "control_max_percent": 100,
        },
        "idle_power_w": idle,
        "curves": {
            side: sorted(rows, key=lambda curve: curve["control_percent"])
            for side, rows in curves.items()
        },
    }


def main() -> None:
    """Write the compact, reproducible table to the explicitly selected path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = extract(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
