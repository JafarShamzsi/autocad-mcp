"""CTO P&ID Symbol Library — DWG block catalog with DXF cache.

The CAD Tools Online (CTO) library lives at C:/PIDv4-CTO/ and contains
600+ ISA 5.1-2009 standard symbols as .dwg files organized by category.

For the ezdxf headless backend, symbols need DWG→DXF conversion via
ezdxf.addons.odafc (ODA File Converter) or manual batch conversion.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

CTO_ROOT = Path(os.environ.get("CTO_LIBRARY_PATH", "C:/PIDv4-CTO"))


def _default_cache_dir() -> Path:
    """Default DXF cache lives in tempdir, not the CTO install directory.

    The CTO library is typically installed under C:/PIDv4-CTO/ which is a
    third-party install path the user may not have write permission to.
    Caching there silently fails with PermissionError on locked-down
    machines.
    """
    return Path(tempfile.gettempdir()) / "autocad-mcp" / "cto_cache"

# Category → list of (display_name, symbol_filename) pairs
CTO_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "ACTUATORS": [
        ("Bellows Spring", "ACT-BELLOWS_SPRING"),
        ("Motor", "ACT-MOTOR"),
        ("Solenoid", "ACT-SOLENOID"),
        ("Spring Diaphragm", "ACT-SPRING_DIAPHRAGM"),
    ],
    "ANNOTATION": [
        ("Equipment Tag", "ANNOT-EQUIP_TAG"),
        ("Equipment Description", "ANNOT-EQUIP_DESCR"),
        ("Flow Arrow", "ANNOT-FLOWARROW"),
        ("Line Number", "ANNOT-LINE_NUMBER"),
    ],
    "EQUIPMENT": [
        ("Clarifier", "EQUIP-CLARIFIER"),
        ("Filter", "EQUIP-FILTER"),
        ("Filter Press", "EQUIP-FILTER_PRESS"),
        ("Heat Exchanger", "EQUIP-HEAT_EXCH-GENERIC"),
        ("Motor", "EQUIP-MOTOR"),
        ("Screen Bar", "EQUIP-SCREENBAR"),
    ],
    "PUMPS-BLOWERS": [
        ("Centrifugal Pump 1", "PUMP-CENTRIF1"),
        ("Centrifugal Pump 2", "PUMP-CENTRIF2"),
        ("Diaphragm Pump", "PUMP-DIAPHRAGM"),
        ("Metering Pump", "PUMP-METERING"),
        ("Progressive Cavity", "PUMP-PROGRESSIVE_CAVITY"),
        ("Submersible Pump", "PUMP-SUBMERSIBLE"),
    ],
    "TANKS": [
        ("Vertical Open", "TANK-VERTICAL_OPEN"),
        ("Vertical Dome", "TANK-VERTICAL_DOME"),
        ("Horizontal", "TANK-HORIZONTAL"),
        ("Cone Bottom", "TANK-CONE_BOTTOM_DOME"),
    ],
    "VALVES": [
        ("Gate Valve", "VA-GATE"),
        ("Globe Valve", "VA-GLOBE"),
        ("Check Valve", "VA-CHECK"),
        ("Ball Valve", "VA-BALL"),
        ("Butterfly Valve", "VA-BUTTERFLY"),
        ("Knife Gate", "VA-KNIFEGATE"),
    ],
}


def list_categories() -> list[str]:
    """Return available CTO categories."""
    if CTO_ROOT.exists():
        return sorted(d.name for d in CTO_ROOT.iterdir() if d.is_dir())
    return sorted(CTO_CATEGORIES.keys())


def list_symbols(category: str) -> list[str]:
    """Return symbol names for a category (from disk if available)."""
    cat_dir = CTO_ROOT / category
    if cat_dir.exists():
        return sorted(f.stem for f in cat_dir.glob("*.dwg"))
    return [s[1] for s in CTO_CATEGORIES.get(category, [])]


def symbol_path(category: str, symbol: str) -> Path:
    """Return full path to a CTO symbol .dwg file."""
    return CTO_ROOT / category / f"{symbol}.dwg"


def symbol_dxf_path(category: str, symbol: str, cache_dir: Path | None = None) -> Path:
    """Return path to cached DXF version of a CTO symbol.

    Default cache is in tempdir to avoid PermissionError on read-only CTO
    install paths. Override via the `cache_dir` argument if you want the
    cache to persist across reboots or live next to project files.
    """
    if cache_dir is None:
        cache_dir = _default_cache_dir()
    return cache_dir / category / f"{symbol}.dxf"
