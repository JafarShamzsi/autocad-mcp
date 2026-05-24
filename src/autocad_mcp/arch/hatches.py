"""Hatch pattern catalogue for AEC drafting.

AutoCAD ships dozens of hatch patterns; the `arch.*` toolbox standardizes
on a small curated set so plans drawn with this server look consistent.
Each pattern maps to AutoCAD's built-in name plus a sensible default
scale + angle that matches typical architectural sheet output.

This is the data layer. The `entity.create_hatch` op and the new
`arch.hatch_region` composite consume it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HatchPattern:
    """One AEC-friendly hatch preset."""

    name: str              # AutoCAD pattern name (e.g. "AR-BRSTD")
    description: str       # human-readable
    discipline: str        # AIA discipline code: A, S, M, E, P, C, L
    layer_hint: str        # recommended layer (informational)
    scale: float           # default `HatchScale` for typical 1/4"=1'-0" output
    angle: float           # default `PatternAngle` (degrees)


# Curated catalogue keyed by friendly name. Pattern names follow AutoCAD's
# built-in pattern set (no external `.pat` file needed) plus the AR-*
# "Architectural Pattern Library" that ships with full AutoCAD.
HATCH_LIBRARY: dict[str, HatchPattern] = {
    # --- Materials in plan / section ---
    "BRICK":         HatchPattern("AR-B816",  "Brick — running bond",                "A", "A-WALL-PATT", 1.0, 0.0),
    "BRICK-COURSE":  HatchPattern("AR-BRSTD", "Brick — coursed (vertical section)",  "A", "A-WALL-PATT", 1.0, 0.0),
    "CONCRETE":      HatchPattern("AR-CONC",  "Concrete — speckled (random dots)",   "A", "A-FLOR-PATT", 1.0, 0.0),
    "CMU":           HatchPattern("AR-HBONE", "CMU block — herringbone",             "A", "A-WALL-PATT", 1.0, 0.0),
    "EARTH":         HatchPattern("EARTH",    "Earth / soil",                        "C", "C-TOPO-EXST", 1.0, 0.0),
    "GRASS":         HatchPattern("GRASS",    "Grass / lawn",                        "L", "L-PLNT",      1.0, 0.0),
    "INSULATION":    HatchPattern("INSUL",    "Batt insulation",                     "A", "A-WALL-INSU", 1.0, 0.0),
    "MUDST":         HatchPattern("MUDST",    "Mud / stone",                         "C", "C-TOPO-EXST", 1.0, 0.0),
    "SAND":          HatchPattern("SAND",     "Sand",                                "C", "C-TOPO-EXST", 1.0, 0.0),
    "STEEL":         HatchPattern("STEEL",    "Steel — diagonal section",            "S", "S-COLS",      1.0, 45.0),
    "WOOD":          HatchPattern("AR-RROOF", "Wood — diagonal grain (roof)",        "A", "A-WALL-PATT", 1.0, 0.0),
    "WOOD-PARQUET":  HatchPattern("AR-PARQ1", "Wood — parquet flooring",             "A", "A-FLOR-PATT", 1.0, 0.0),
    "TILE-12":       HatchPattern("AR-RSHKE", "Tile — 12\" square grid",             "A", "A-FLOR-PATT", 1.0, 0.0),
    "ROOF-SHINGLE":  HatchPattern("AR-RSHKE", "Roof — wood shingle",                 "A", "A-ROOF-PATT", 1.0, 0.0),
    "ROOF-TILE":     HatchPattern("AR-RROOF", "Roof — tile",                         "A", "A-ROOF-PATT", 1.0, 0.0),
    # --- ANSI standard patterns ---
    "ANSI-IRON":     HatchPattern("ANSI31", "ANSI31 — iron/brick/stone (diag 45°)",  "S", "S-COLS",      1.0, 0.0),
    "ANSI-BRONZE":   HatchPattern("ANSI32", "ANSI32 — bronze/brass",                 "S", "S-COLS",      1.0, 0.0),
    "ANSI-RUBBER":   HatchPattern("ANSI37", "ANSI37 — lead/rubber (cross-hatched)",  "P", "P-FIXT",      1.0, 0.0),
    # --- Solid fills ---
    "SOLID":         HatchPattern("SOLID",  "Solid fill",                            "A", "A-WALL",      1.0, 0.0),
}


def hatch_names() -> list[str]:
    """Friendly names available in the catalogue."""
    return sorted(HATCH_LIBRARY.keys())


def hatch_pattern(name: str) -> HatchPattern | None:
    """Look up a pattern by friendly name (case-insensitive)."""
    return HATCH_LIBRARY.get(name.upper())
