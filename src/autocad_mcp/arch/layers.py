"""AIA / NCS architectural layer standard.

Layer naming follows the AIA CAD Layer Guidelines / NCS (National CAD Standard)
discipline-major-minor convention: `<discipline>-<major>-<minor>[-<status>]`.

Discipline codes:
  A — Architecture
  S — Structural
  M — Mechanical
  E — Electrical
  P — Plumbing
  C — Civil
  L — Landscape

For this initial slice we ship the architectural (A-) layers most floor-plan
work touches. Add others on demand.

ezdxf color indices (ACI):
  1 red, 2 yellow, 3 green, 4 cyan, 5 blue, 6 magenta, 7 white/black,
  8 dark grey, 9 light grey, 30 orange, 250-255 dark greys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import ezdxf
import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class AIALayer:
    """A single AIA/NCS layer specification."""

    name: str
    color: int
    linetype: str = "CONTINUOUS"
    lineweight: int = -3  # -3 = ByLayer default
    description: str = ""


# Standard architectural floor-plan layers.
# Colors chosen for legibility against a black model-space background.
AIA_LAYERS: tuple[AIALayer, ...] = (
    AIALayer("A-WALL", color=4, description="Wall — full height"),
    AIALayer("A-WALL-EXTR", color=4, description="Wall — exterior"),
    AIALayer("A-WALL-INTR", color=4, description="Wall — interior"),
    AIALayer("A-WALL-PATT", color=251, description="Wall hatch / fill"),
    AIALayer("A-WALL-HEAD", color=4, linetype="HIDDEN", description="Wall above (header)"),
    AIALayer("A-DOOR", color=2, description="Door — leaf, frame, swing"),
    AIALayer("A-GLAZ", color=5, description="Glazing / window"),
    AIALayer("A-COLS", color=6, description="Columns — structural"),
    AIALayer("A-FLOR", color=3, description="Floor — fixtures, casework"),
    AIALayer("A-FLOR-FIXT", color=3, description="Floor — plumbing fixtures"),
    AIALayer("A-FLOR-CASE", color=30, description="Floor — casework / millwork"),
    AIALayer("A-FLOR-STRS", color=6, description="Floor — stairs"),
    AIALayer("A-FLOR-RAIL", color=7, description="Floor — railings / handrails"),
    AIALayer("A-FLOR-ELEV", color=7, description="Floor — elevator / lift"),
    AIALayer("A-FURN", color=30, description="Furniture"),
    AIALayer("A-EQPM", color=30, description="Equipment — appliances, fixed equipment"),
    AIALayer("A-LITE", color=6, description="Lighting — ceiling, wall, task"),
    AIALayer("A-AREA", color=1, description="Area — room boundary"),
    AIALayer("A-AREA-IDEN", color=1, description="Area — room identification / tag"),
    AIALayer("A-ANNO-DIMS", color=2, description="Annotation — dimensions"),
    AIALayer("A-ANNO-TEXT", color=7, description="Annotation — general text / notes"),
    AIALayer("A-ANNO-NOTE", color=7, description="Annotation — keynotes"),
    AIALayer("A-ANNO-SYMB", color=4, description="Annotation — symbols (north arrow, scale)"),
    AIALayer("A-ANNO-TTLB", color=7, description="Annotation — title block"),
    AIALayer("A-GRID", color=8, linetype="CENTER", description="Column / planning grid"),
    AIALayer("A-CLNG", color=6, description="Ceiling"),
    AIALayer("A-ROOF", color=6, description="Roof"),
)


def setup_aia_layers(
    doc: ezdxf.document.Drawing,
    layers: Iterable[AIALayer] = AIA_LAYERS,
) -> list[str]:
    """Create AIA standard layers on a drawing if not already present.

    Returns the list of layer names that were created (skipping pre-existing).
    Idempotent: safe to call repeatedly.

    Loads CENTER and HIDDEN linetypes from ezdxf's built-in linetype catalog
    on demand; falls back to CONTINUOUS if a linetype isn't available.
    """
    created: list[str] = []
    for spec in layers:
        if spec.name in doc.layers:
            continue

        linetype = _ensure_linetype(doc, spec.linetype)

        attribs: dict = {
            "color": spec.color,
            "linetype": linetype,
        }
        if spec.lineweight != -3:
            attribs["lineweight"] = spec.lineweight

        doc.layers.add(spec.name, **attribs)
        if spec.description:
            try:
                doc.layers.get(spec.name).description = spec.description
            except AttributeError:
                pass  # older ezdxf versions don't expose .description
        created.append(spec.name)
    return created


# AutoCAD's HIDDEN/HIDDEN2/HIDDENX2 linetypes aren't in ezdxf's standard
# catalog; DASHED/DASHED2/DASHEDX2 are near-equivalents. Exposed at module
# scope so the standards-check module sees the same canonicalisation rules
# (otherwise it would false-positive on layers using these linetypes).
LINETYPE_ALIASES: dict[str, str] = {
    "HIDDEN": "DASHED",
    "HIDDEN2": "DASHED2",
    "HIDDENX2": "DASHEDX2",
}


def _ensure_linetype(doc: ezdxf.document.Drawing, linetype: str) -> str:
    """Ensure the requested linetype exists on the doc; load it if not.

    Falls back to CONTINUOUS if the linetype isn't found in ezdxf's catalog.
    Aliases (e.g. HIDDEN→DASHED) follow `LINETYPE_ALIASES`.
    """
    if linetype.upper() == "CONTINUOUS":
        return "CONTINUOUS"

    canonical = LINETYPE_ALIASES.get(linetype.upper(), linetype)

    if canonical in doc.linetypes:
        return canonical

    try:
        from ezdxf.tools.standards import linetypes as std_linetypes

        for name, desc, pattern in std_linetypes():
            if name.upper() == canonical.upper():
                doc.linetypes.add(name=name, pattern=pattern, description=desc)
                return name
    except Exception as ex:
        # ezdxf catalog miss or DXF write error — fall through to CONTINUOUS
        # rather than failing the surrounding layer setup.
        log.debug("ensure_linetype_lookup_failed", linetype=canonical, error=str(ex))
    return "CONTINUOUS"
