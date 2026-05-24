"""Project-level query helpers backed by the SQLite index.

All functions take a `sqlite3.Connection` and return plain Python data
(dicts / lists). They use parameterised queries — no string concatenation
against user input.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def project_info(conn: sqlite3.Connection) -> dict[str, Any]:
    """High-level project summary: counts + last-indexed timestamp."""
    cur = conn.cursor()
    drawings = cur.execute("SELECT COUNT(*) FROM drawings").fetchone()[0]
    indexed = cur.execute(
        "SELECT COUNT(*) FROM drawings WHERE error IS NULL"
    ).fetchone()[0]
    failed = drawings - indexed
    total_entities = cur.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    total_layers = cur.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
    distinct_layers = cur.execute(
        "SELECT COUNT(DISTINCT name) FROM layers"
    ).fetchone()[0]
    last_indexed = cur.execute(
        "SELECT MAX(indexed_at) FROM drawings"
    ).fetchone()[0]
    return {
        "drawings": drawings,
        "indexed": indexed,
        "failed": failed,
        "total_entities": total_entities,
        "total_layers": total_layers,
        "distinct_layers": distinct_layers,
        "last_indexed": last_indexed,
    }


def list_drawings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per drawing, ordered by name."""
    rows = conn.execute(
        """SELECT name, file_path, discipline, dxf_version,
                  entity_count, layer_count, block_count, indexed_at, error
           FROM drawings ORDER BY name"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_layers(
    conn: sqlite3.Connection,
    drawing: str | None = None,
    discipline: str | None = None,
) -> list[dict[str, Any]]:
    """Layers aggregated across drawings, with usage counts.

    Filters:
      drawing    — restrict to a single drawing's layers
      discipline — restrict to drawings of a given discipline code (e.g. "A")
    """
    sql = """
        SELECT l.name AS name,
               COUNT(DISTINCT l.drawing_id) AS drawings_using,
               COUNT(DISTINCT l.color)      AS distinct_colors
        FROM layers l
        JOIN drawings d ON d.id = l.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if discipline is not None:
        where.append("d.discipline = ?")
        params.append(discipline.upper())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY l.name ORDER BY l.name"

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_dimstyles(
    conn: sqlite3.Connection,
    drawing: str | None = None,
) -> list[dict[str, Any]]:
    """List dimension styles, aggregated by name across the project.

    Each row reports how many drawings define a dimstyle of that name and
    how many distinct (dimscale, dimtxt, dimasz, dimdec) attribute tuples
    exist — `distinct_configs > 1` means firms are using "the same" style
    name with diverging settings, which is the kind of drift you want to
    catch in an audit.
    """
    sql = """
        SELECT ds.name AS name,
               COUNT(DISTINCT ds.drawing_id) AS drawings_using,
               COUNT(DISTINCT
                   ds.dimscale || '|' || COALESCE(ds.dimtxt, '') || '|' ||
                   COALESCE(ds.dimasz, '') || '|' || COALESCE(ds.dimdec, '')
               ) AS distinct_configs
        FROM dimstyles ds
        JOIN drawings d ON d.id = ds.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY ds.name ORDER BY ds.name"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_textstyles(
    conn: sqlite3.Connection,
    drawing: str | None = None,
) -> list[dict[str, Any]]:
    """List text styles, aggregated by name across the project.

    `distinct_fonts > 1` means the same style name maps to different fonts
    in different drawings — usually a sign that someone authored a drawing
    with the firm's font missing on their machine.
    """
    sql = """
        SELECT ts.name AS name,
               COUNT(DISTINCT ts.drawing_id) AS drawings_using,
               COUNT(DISTINCT ts.font) AS distinct_fonts
        FROM textstyles ts
        JOIN drawings d ON d.id = ts.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY ts.name ORDER BY ts.name"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_linetypes(
    conn: sqlite3.Connection,
    drawing: str | None = None,
) -> list[dict[str, Any]]:
    """List linetypes aggregated by name across the project.

    `distinct_configs > 1` surfaces same-name pattern drift (a HIDDEN with
    different total length / dash count in different drawings is a sign
    someone copied the linetype from a non-standard source).
    """
    sql = """
        SELECT lt.name AS name,
               COUNT(DISTINCT lt.drawing_id) AS drawings_using,
               COUNT(DISTINCT
                   COALESCE(lt.pattern_length, '') || '|' ||
                   COALESCE(lt.dash_count, '') || '|' ||
                   COALESCE(lt.description, '')
               ) AS distinct_configs
        FROM linetypes lt
        JOIN drawings d ON d.id = lt.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY lt.name ORDER BY lt.name"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_blocks(
    conn: sqlite3.Connection,
    drawing: str | None = None,
) -> list[dict[str, Any]]:
    """List block definitions aggregated by name across the project.

    `distinct_entity_counts > 1` is a weak-but-useful drift indicator:
    if the firm's TITLE-BLOCK block has 12 entities in one drawing and
    14 in another, someone modified the definition locally.
    """
    sql = """
        SELECT b.name AS name,
               COUNT(DISTINCT b.drawing_id)  AS drawings_using,
               COUNT(DISTINCT b.entity_count) AS distinct_entity_counts
        FROM blocks b
        JOIN drawings d ON d.id = b.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY b.name ORDER BY b.name"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def find_linetype_inconsistencies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Linetype names whose pattern attributes disagree across drawings."""
    # Two-query-per-conflict shape matches find_style_inconsistencies;
    # collapse with a window function if perf becomes an issue at scale.
    rows = conn.execute(
        """SELECT name, COUNT(DISTINCT
               COALESCE(pattern_length, '') || '|' ||
               COALESCE(dash_count, '')     || '|' ||
               COALESCE(description, '')
           ) AS variants
           FROM linetypes
           GROUP BY name
           HAVING variants > 1"""
    ).fetchall()
    conflicts: list[dict[str, Any]] = []
    for r in rows:
        detail = conn.execute(
            """SELECT d.name AS drawing, lt.pattern_length, lt.dash_count, lt.description
               FROM linetypes lt JOIN drawings d ON d.id = lt.drawing_id
               WHERE lt.name = ? ORDER BY d.name""",
            (r["name"],),
        ).fetchall()
        conflicts.append({
            "name": r["name"],
            "kind": "linetype",
            "drawings": [dict(d) for d in detail],
        })
    return conflicts


def find_block_inconsistencies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Block names whose entity_count differs across drawings."""
    rows = conn.execute(
        """SELECT name, COUNT(DISTINCT entity_count) AS variants
           FROM blocks
           GROUP BY name
           HAVING variants > 1"""
    ).fetchall()
    conflicts: list[dict[str, Any]] = []
    for r in rows:
        detail = conn.execute(
            """SELECT d.name AS drawing, b.entity_count
               FROM blocks b JOIN drawings d ON d.id = b.drawing_id
               WHERE b.name = ? ORDER BY d.name""",
            (r["name"],),
        ).fetchall()
        conflicts.append({
            "name": r["name"],
            "kind": "block",
            "drawings": [dict(d) for d in detail],
        })
    return conflicts


def find_style_inconsistencies(conn: sqlite3.Connection) -> dict[str, Any]:
    """Surface style names that disagree across drawings.

    Two kinds of disagreement:
      - "dimstyle": same name, different attribute tuple
      - "textstyle": same name, different font

    Each conflict row carries the style name, the kind, and a list of
    (drawing_name, attributes) pairs showing the divergent values.
    """
    # Two-query-per-conflict shape kept for clarity; collapse with a window
    # function if perf becomes an issue at >200 drawings.
    dim_conflicts: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT name, COUNT(DISTINCT
               COALESCE(dimscale, '') || '|' || COALESCE(dimtxt, '') || '|' ||
               COALESCE(dimasz, '') || '|' || COALESCE(dimdec, '')
           ) AS variants
           FROM dimstyles
           GROUP BY name
           HAVING variants > 1"""
    ).fetchall()
    for r in rows:
        detail_rows = conn.execute(
            """SELECT d.name AS drawing, ds.dimscale, ds.dimtxt, ds.dimasz, ds.dimdec
               FROM dimstyles ds JOIN drawings d ON d.id = ds.drawing_id
               WHERE ds.name = ? ORDER BY d.name""",
            (r["name"],),
        ).fetchall()
        dim_conflicts.append({
            "name": r["name"],
            "kind": "dimstyle",
            "drawings": [dict(d) for d in detail_rows],
        })

    text_conflicts: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT name, COUNT(DISTINCT font) AS variants
           FROM textstyles
           GROUP BY name
           HAVING variants > 1"""
    ).fetchall()
    for r in rows:
        detail_rows = conn.execute(
            """SELECT d.name AS drawing, ts.font, ts.height
               FROM textstyles ts JOIN drawings d ON d.id = ts.drawing_id
               WHERE ts.name = ? ORDER BY d.name""",
            (r["name"],),
        ).fetchall()
        text_conflicts.append({
            "name": r["name"],
            "kind": "textstyle",
            "drawings": [dict(d) for d in detail_rows],
        })

    return {
        "dim_conflicts": dim_conflicts,
        "text_conflicts": text_conflicts,
        "total_conflicts": len(dim_conflicts) + len(text_conflicts),
    }


def list_sheets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every sheet in the indexed sheet set.

    Empty list when no sheet set was configured / indexed.
    """
    rows = conn.execute(
        """SELECT sheetset_name, number, title, description,
                  dwg_file, layout_name, resolved_dwg_path, is_dwg_present
           FROM sheets
           ORDER BY number, title"""
    ).fetchall()
    return [
        {
            "sheetset_name": r["sheetset_name"],
            "number": r["number"],
            "title": r["title"],
            "description": r["description"],
            "dwg_file": r["dwg_file"],
            "layout_name": r["layout_name"],
            "resolved_dwg_path": r["resolved_dwg_path"],
            "is_dwg_present": bool(r["is_dwg_present"]),
        }
        for r in rows
    ]


def _escape_like(value: str) -> str:
    r"""Escape SQLite LIKE wildcards (`%`, `_`) plus the escape char itself.

    Without escaping, a filename like `_DETAIL.dwg` matches `AB-DETAIL.dwg`
    because `_` is a LIKE wildcard. The companion ESCAPE clause must use the
    same `\` character used here.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_sheets_for_drawing(
    conn: sqlite3.Connection,
    drawing: str,
) -> list[dict[str, Any]]:
    """Find every sheet whose underlying DWG matches the given drawing name
    or path. Match is on file basename or on the full resolved path.
    """
    rows = conn.execute(
        r"""SELECT sheetset_name, number, title, description,
                   dwg_file, layout_name, resolved_dwg_path, is_dwg_present
            FROM sheets
            WHERE resolved_dwg_path = ?
               OR dwg_file = ?
               OR dwg_file LIKE ? ESCAPE '\'
            ORDER BY number, title""",
        (drawing, drawing, f"%/{_escape_like(drawing)}"),
    ).fetchall()
    return [
        {
            "sheetset_name": r["sheetset_name"],
            "number": r["number"],
            "title": r["title"],
            "description": r["description"],
            "dwg_file": r["dwg_file"],
            "layout_name": r["layout_name"],
            "resolved_dwg_path": r["resolved_dwg_path"],
            "is_dwg_present": bool(r["is_dwg_present"]),
        }
        for r in rows
    ]


def list_xrefs(
    conn: sqlite3.Connection,
    drawing: str | None = None,
) -> list[dict[str, Any]]:
    """List external references, optionally scoped to one drawing.

    Each row carries the host drawing name, the XREF block name, the raw path
    written in the DXF, its resolved absolute path, and whether the target
    file currently exists on disk.
    """
    sql = """
        SELECT d.name AS drawing,
               x.name AS xref_name,
               x.path AS path,
               x.resolved_path AS resolved_path,
               x.is_broken AS is_broken
        FROM xrefs x
        JOIN drawings d ON d.id = x.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name, x.name"

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "drawing": r["drawing"],
            "xref_name": r["xref_name"],
            "path": r["path"],
            "resolved_path": r["resolved_path"],
            "is_broken": bool(r["is_broken"]),
        }
        for r in rows
    ]


def find_broken_xrefs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """XREFs whose resolved target file doesn't exist on disk."""
    rows = conn.execute(
        """SELECT d.name AS drawing,
                  x.name AS xref_name,
                  x.path AS path,
                  x.resolved_path AS resolved_path
           FROM xrefs x
           JOIN drawings d ON d.id = x.drawing_id
           WHERE x.is_broken = 1
           ORDER BY d.name, x.name"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_entities(
    conn: sqlite3.Connection,
    layer: str | None = None,
    type: str | None = None,
    drawing: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Find entities matching filters across the project.

    Filter args are all optional; passing none returns the first `limit`
    entities project-wide.
    """
    limit = max(1, min(int(limit), 5000))  # guardrail

    sql = """
        SELECT d.name AS drawing, e.type AS type, e.layer AS layer,
               e.handle AS handle, e.space AS space
        FROM entities e
        JOIN drawings d ON d.id = e.drawing_id
    """
    where: list[str] = []
    params: list[Any] = []
    if layer is not None:
        where.append("e.layer = ?")
        params.append(layer)
    if type is not None:
        where.append("e.type = ?")
        params.append(type)
    if drawing is not None:
        where.append("d.name = ?")
        params.append(drawing)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name, e.type LIMIT ?"
    params.append(limit)

    return [dict(r) for r in conn.execute(sql, params).fetchall()]
