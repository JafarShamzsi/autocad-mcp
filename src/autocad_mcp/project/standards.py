"""Drawing-standard compliance check.

A `LayerStandard` declares the expected set of layers (and per-layer color /
linetype rules). Run `check_drawing()` / `check_project()` against the SQLite
index built by `DXFIndexer` to surface violations:

  - missing       — a required layer is absent from the drawing
  - wrong_color   — the layer exists but its ACI color doesn't match
  - wrong_linetype — the layer exists but its linetype doesn't match
  - extra         — the drawing has a non-standard layer (informational)

The standard also optionally checks dimstyle / textstyle compliance using
the rows the DXFIndexer already writes. Dimstyle rules cover the attribute
tuple (dimscale, dimtxt, dimasz, dimdec); textstyle rules cover the font
name. Missing styles raise "missing_dimstyle" / "missing_textstyle";
attribute disagreements raise "wrong_dimstyle" / "wrong_textstyle".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import ezdxf


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerRule:
    """Rules for one layer in a standard.

    Setting either field to None means "don't check that attribute" — useful
    when the standard cares about layer presence but not color, or vice versa.
    Both default to None so a caller that only cares about layer presence can
    omit both.
    """

    expected_color: int | None = None
    expected_linetype: str | None = None


@dataclass(frozen=True)
class DimstyleRule:
    """Expected attribute values for one dimstyle. Any field left at None is
    not enforced — useful when the standard only cares about, say, DIMSCALE."""

    dimscale: float | None = None
    dimtxt: float | None = None
    dimasz: float | None = None
    dimdec: int | None = None


@dataclass(frozen=True)
class TextstyleRule:
    """Expected attribute values for one textstyle. font is the TTF filename
    or .shx name; height = 0 means scalable. Either field can be None."""

    font: str | None = None
    height: float | None = None


@dataclass
class LayerStandard:
    """A named set of expected layers + (optional) dimstyle / textstyle rules.

    Kept named `LayerStandard` for backwards compatibility — it now also
    holds dim/textstyle rules. `check_drawing()` only enforces what's
    populated; an empty `dimstyle_rules` dict means "don't audit dimstyles."
    """

    name: str
    rules: dict[str, LayerRule] = field(default_factory=dict)
    dimstyle_rules: dict[str, DimstyleRule] = field(default_factory=dict)
    textstyle_rules: dict[str, TextstyleRule] = field(default_factory=dict)
    # Layer names that are always permitted even if not in `rules` — typically
    # AutoCAD-internal names like "0", "Defpoints".
    always_allowed: frozenset[str] = field(
        default_factory=lambda: frozenset({"0", "Defpoints", "DEFPOINTS"})
    )

    def is_required(self, layer_name: str) -> bool:
        return layer_name in self.rules

    def is_allowed(self, layer_name: str) -> bool:
        return layer_name in self.rules or layer_name in self.always_allowed


# Public alias — `Standard` reads better when the standard carries non-layer
# rules. Existing call sites can keep using `LayerStandard`.
Standard = LayerStandard


# ---------------------------------------------------------------------------
# Standard constructors
# ---------------------------------------------------------------------------


def aia_standard() -> LayerStandard:
    """Build a LayerStandard from the AIA preset baked into the arch module.

    Linetype names are resolved through the shared LINETYPE_ALIASES table the
    AIA layer setup uses (since ezdxf's catalog lacks HIDDEN). Without this,
    every compliant drawing would report a spurious 'wrong_linetype' on
    A-WALL-HEAD.
    """
    from autocad_mcp.arch.layers import AIA_LAYERS

    rules: dict[str, LayerRule] = {}
    for spec in AIA_LAYERS:
        rules[spec.name] = LayerRule(
            expected_color=spec.color,
            expected_linetype=_canonical_linetype(spec.linetype),
        )
    return LayerStandard(name="AIA", rules=rules)


def load_standard_from_dxf(
    path: str | Path,
    name: str | None = None,
    include_standard_styles: bool = False,
) -> LayerStandard:
    """Build a LayerStandard from a reference DXF (or .dws read as DXF).

    Every non-AutoCAD-internal layer in the file becomes a required entry.
    Dimstyles and textstyles named exactly "Standard" are excluded by
    default because AutoCAD ships those names in every new drawing — if
    your firm's reference uses them deliberately, pass
    `include_standard_styles=True` so compliance enforces them too. A
    warning is logged whenever non-Standard rules end up empty *and* a
    Standard rule was skipped, so users notice when the audit silently
    becomes a layers-only check.
    """
    import structlog
    log = structlog.get_logger()

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Standard DXF not found: {p}")

    doc = ezdxf.readfile(str(p))
    rules: dict[str, LayerRule] = {}
    for layer in doc.layers:
        layer_name = layer.dxf.name
        if layer_name in ("0", "Defpoints", "DEFPOINTS"):
            continue
        color = int(layer.dxf.get("color", 7))
        linetype = layer.dxf.get("linetype", "Continuous")
        rules[layer_name] = LayerRule(
            expected_color=color,
            expected_linetype=_canonical_linetype(linetype),
        )

    dimstyle_rules: dict[str, DimstyleRule] = {}
    skipped_dimstyle_standard = False
    for ds in doc.dimstyles:
        ds_name = ds.dxf.name
        if ds_name == "Standard" and not include_standard_styles:
            skipped_dimstyle_standard = True
            continue
        dimstyle_rules[ds_name] = DimstyleRule(
            dimscale=_safe_float(ds.dxf.get("dimscale")),
            dimtxt=_safe_float(ds.dxf.get("dimtxt")),
            dimasz=_safe_float(ds.dxf.get("dimasz")),
            dimdec=_safe_int(ds.dxf.get("dimdec")),
        )

    textstyle_rules: dict[str, TextstyleRule] = {}
    skipped_textstyle_standard = False
    for ts in doc.styles:
        ts_name = ts.dxf.name
        if ts_name == "Standard" and not include_standard_styles:
            skipped_textstyle_standard = True
            continue
        textstyle_rules[ts_name] = TextstyleRule(
            font=ts.dxf.get("font") or None,
            height=_safe_float(ts.dxf.get("height")),
        )

    if not dimstyle_rules and skipped_dimstyle_standard:
        log.warning(
            "standard_dimstyles_empty_due_to_skip",
            path=str(p),
            hint="Reference DXF only defined the 'Standard' dimstyle. Pass "
                 "include_standard_styles=True if 'Standard' is your firm's "
                 "primary dimstyle, otherwise dimstyle compliance is a no-op.",
        )
    if not textstyle_rules and skipped_textstyle_standard:
        log.warning(
            "standard_textstyles_empty_due_to_skip",
            path=str(p),
            hint="Reference DXF only defined the 'Standard' textstyle. Pass "
                 "include_standard_styles=True if that's intentional.",
        )

    return LayerStandard(
        name=name or f"DXF:{p.stem}",
        rules=rules,
        dimstyle_rules=dimstyle_rules,
        textstyle_rules=textstyle_rules,
    )


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _canonical_linetype(linetype: str) -> str:
    """Normalise linetype names through the shared alias table.

    AutoCAD writes 'Continuous', ezdxf 'CONTINUOUS'; HIDDEN/HIDDEN2/HIDDENX2
    are also aliased to their DASHED equivalents since that's what the AIA
    layer setup actually writes.
    """
    from autocad_mcp.arch.layers import LINETYPE_ALIASES

    if not linetype:
        return "CONTINUOUS"
    upper = linetype.strip().upper()
    return LINETYPE_ALIASES.get(upper, upper)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def check_drawing(
    conn: sqlite3.Connection,
    drawing_id: int,
    standard: LayerStandard,
    *,
    include_extra: bool = True,
) -> list[dict[str, Any]]:
    """Return a list of violations for a single drawing's layers.

    Args:
        conn: SQLite connection built by `project.index.init_db`.
        drawing_id: row id in the `drawings` table.
        standard: the LayerStandard to check against.
        include_extra: emit "extra" violations for non-standard layers.

    Output shape:
        [{"drawing": str, "layer": str, "kind": str,
          "expected": Any, "actual": Any}, ...]
    """
    drawing_row = conn.execute(
        "SELECT name FROM drawings WHERE id = ?", (drawing_id,)
    ).fetchone()
    if drawing_row is None:
        raise ValueError(f"No drawing with id={drawing_id}")
    drawing_name = drawing_row["name"]

    layer_rows = conn.execute(
        "SELECT name, color, linetype FROM layers WHERE drawing_id = ?",
        (drawing_id,),
    ).fetchall()
    actual_by_name = {
        r["name"]: {"color": r["color"], "linetype": _canonical_linetype(r["linetype"])}
        for r in layer_rows
    }

    violations: list[dict[str, Any]] = []

    # Missing + mismatched layers
    for layer_name, rule in standard.rules.items():
        actual = actual_by_name.get(layer_name)
        if actual is None:
            violations.append({
                "drawing": drawing_name,
                "layer": layer_name,
                "kind": "missing",
                "expected": _rule_summary(rule),
                "actual": None,
            })
            continue
        if (
            rule.expected_color is not None
            and actual["color"] != rule.expected_color
        ):
            violations.append({
                "drawing": drawing_name,
                "layer": layer_name,
                "kind": "wrong_color",
                "expected": rule.expected_color,
                "actual": actual["color"],
            })
        if (
            rule.expected_linetype is not None
            and actual["linetype"] != rule.expected_linetype
        ):
            violations.append({
                "drawing": drawing_name,
                "layer": layer_name,
                "kind": "wrong_linetype",
                "expected": rule.expected_linetype,
                "actual": actual["linetype"],
            })

    # Extra (non-standard) layers
    if include_extra:
        for layer_name in actual_by_name:
            if not standard.is_allowed(layer_name):
                violations.append({
                    "drawing": drawing_name,
                    "layer": layer_name,
                    "kind": "extra",
                    "expected": None,
                    "actual": _rule_summary_from_actual(actual_by_name[layer_name]),
                })

    # Dimstyle compliance — only run when the standard declares any.
    if standard.dimstyle_rules:
        violations.extend(_check_dimstyles(conn, drawing_id, drawing_name, standard))
    # Textstyle compliance — same.
    if standard.textstyle_rules:
        violations.extend(_check_textstyles(conn, drawing_id, drawing_name, standard))

    return violations


def _check_dimstyles(
    conn: sqlite3.Connection,
    drawing_id: int,
    drawing_name: str,
    standard: LayerStandard,
) -> list[dict[str, Any]]:
    """Compare a drawing's dimstyles against the standard's dimstyle_rules."""
    rows = conn.execute(
        "SELECT name, dimscale, dimtxt, dimasz, dimdec "
        "FROM dimstyles WHERE drawing_id = ?",
        (drawing_id,),
    ).fetchall()
    actual = {
        r["name"]: {
            "dimscale": r["dimscale"], "dimtxt": r["dimtxt"],
            "dimasz": r["dimasz"], "dimdec": r["dimdec"],
        }
        for r in rows
    }
    out: list[dict[str, Any]] = []
    for name, rule in standard.dimstyle_rules.items():
        got = actual.get(name)
        if got is None:
            out.append({
                "drawing": drawing_name, "dimstyle": name,
                "kind": "missing_dimstyle",
                "expected": _dimstyle_summary(rule),
                "actual": None,
            })
            continue
        mismatches = {}
        for attr in ("dimscale", "dimtxt", "dimasz", "dimdec"):
            expected_val = getattr(rule, attr)
            if expected_val is None:
                continue
            if got[attr] is None or not _close(got[attr], expected_val):
                mismatches[attr] = {"expected": expected_val, "actual": got[attr]}
        if mismatches:
            out.append({
                "drawing": drawing_name, "dimstyle": name,
                "kind": "wrong_dimstyle",
                "expected": _dimstyle_summary(rule),
                "actual": got,
                "mismatches": mismatches,
            })
    return out


def _check_textstyles(
    conn: sqlite3.Connection,
    drawing_id: int,
    drawing_name: str,
    standard: LayerStandard,
) -> list[dict[str, Any]]:
    """Compare a drawing's textstyles against the standard's textstyle_rules."""
    rows = conn.execute(
        "SELECT name, font, height FROM textstyles WHERE drawing_id = ?",
        (drawing_id,),
    ).fetchall()
    actual = {r["name"]: {"font": r["font"], "height": r["height"]} for r in rows}
    out: list[dict[str, Any]] = []
    for name, rule in standard.textstyle_rules.items():
        got = actual.get(name)
        if got is None:
            out.append({
                "drawing": drawing_name, "textstyle": name,
                "kind": "missing_textstyle",
                "expected": _textstyle_summary(rule),
                "actual": None,
            })
            continue
        mismatches = {}
        if rule.font is not None and (got["font"] or "") != rule.font:
            mismatches["font"] = {"expected": rule.font, "actual": got["font"]}
        if rule.height is not None and not _close(got["height"], rule.height):
            mismatches["height"] = {"expected": rule.height, "actual": got["height"]}
        if mismatches:
            out.append({
                "drawing": drawing_name, "textstyle": name,
                "kind": "wrong_textstyle",
                "expected": _textstyle_summary(rule),
                "actual": got,
                "mismatches": mismatches,
            })
    return out


def _close(a, b, tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a == b
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def _dimstyle_summary(rule: DimstyleRule) -> dict[str, Any]:
    return {
        "dimscale": rule.dimscale, "dimtxt": rule.dimtxt,
        "dimasz": rule.dimasz, "dimdec": rule.dimdec,
    }


def _textstyle_summary(rule: TextstyleRule) -> dict[str, Any]:
    return {"font": rule.font, "height": rule.height}


def check_project(
    conn: sqlite3.Connection,
    standard: LayerStandard,
    *,
    drawing_name: str | None = None,
    include_extra: bool = True,
    limit: int = 500,
) -> dict[str, Any]:
    """Check every (or one) indexed drawing in the project against a standard.

    Returns:
        {
            "standard_name": str,
            "drawings_checked": int,
            "total_violations": int,
            "violations": [...],  # bounded to `limit`
            "truncated": bool,
        }
    """
    if drawing_name is not None:
        rows = conn.execute(
            "SELECT id FROM drawings WHERE name = ?", (drawing_name,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM drawings WHERE error IS NULL").fetchall()

    all_violations: list[dict[str, Any]] = []
    for r in rows:
        all_violations.extend(
            check_drawing(conn, r["id"], standard, include_extra=include_extra)
        )

    truncated = len(all_violations) > limit
    return {
        "standard_name": standard.name,
        "drawings_checked": len(rows),
        "total_violations": len(all_violations),
        "violations": all_violations[:limit],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_summary(rule: LayerRule) -> dict[str, Any]:
    return {"color": rule.expected_color, "linetype": rule.expected_linetype}


def _rule_summary_from_actual(actual: dict[str, Any]) -> dict[str, Any]:
    return {"color": actual["color"], "linetype": actual["linetype"]}
