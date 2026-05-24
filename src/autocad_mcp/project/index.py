"""SQLite-backed indexer for a multi-DXF project.

Walks each drawing in a `ProjectConfig`, extracts entities / layers / blocks,
and writes them to a SQLite database that callers can query without
re-opening the DXFs.

DWG support is deferred — `ezdxf.readfile` is DXF-only. To index DWGs you'd
either preconvert via ODA File Converter or wire the `com_backend` to read
them through AutoCAD. Both are non-goals of this slice.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import ezdxf
import structlog

from autocad_mcp.project.config import ProjectConfig
from autocad_mcp.project.dwg_reader import read_drawing

log = structlog.get_logger()


# SCHEMA_VERSION bump checklist (when adding a new column / table):
#   1. Bump this constant by 1.
#   2. Add the new column/table to `_SCHEMA` below.
#   3. The current contract is "the db is a derived cache" — `init_db` wipes
#      the file and re-creates the schema when versions disagree (callers
#      can detect this via the `rebuilt` flag returned). Re-indexing then
#      repopulates everything; no ALTER-TABLE migration is needed unless
#      that contract changes.
#   4. Add a regression test using a fixture db at version (N-1).
SCHEMA_VERSION = 5


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS drawings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    file_path    TEXT NOT NULL UNIQUE,
    discipline   TEXT,
    indexed_at   TEXT,
    dxf_version  TEXT,
    entity_count INTEGER DEFAULT 0,
    layer_count  INTEGER DEFAULT 0,
    block_count  INTEGER DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    drawing_id INTEGER NOT NULL,
    type       TEXT NOT NULL,
    layer      TEXT NOT NULL,
    handle     TEXT NOT NULL,
    space      TEXT NOT NULL,
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entities_layer    ON entities(layer);
CREATE INDEX IF NOT EXISTS idx_entities_type     ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_drawing  ON entities(drawing_id);

CREATE TABLE IF NOT EXISTS layers (
    drawing_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    color      INTEGER,
    linetype   TEXT,
    is_frozen  INTEGER DEFAULT 0,
    is_locked  INTEGER DEFAULT 0,
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_layers_name ON layers(name);

CREATE TABLE IF NOT EXISTS blocks (
    drawing_id   INTEGER NOT NULL,
    name         TEXT NOT NULL,
    entity_count INTEGER DEFAULT 0,
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS xrefs (
    drawing_id    INTEGER NOT NULL,
    name          TEXT NOT NULL,            -- block name in the host drawing
    path          TEXT NOT NULL,            -- raw xref_path as written in the DXF
    resolved_path TEXT,                     -- absolute path resolved vs. host dir
    is_broken     INTEGER NOT NULL DEFAULT 0,  -- 1 when resolved_path doesn't exist
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_xrefs_drawing ON xrefs(drawing_id);
CREATE INDEX IF NOT EXISTS idx_xrefs_broken  ON xrefs(is_broken);

CREATE TABLE IF NOT EXISTS dimstyles (
    drawing_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    dimscale   REAL,                        -- overall scale factor (DIMSCALE)
    dimtxt     REAL,                        -- text height (DIMTXT)
    dimasz     REAL,                        -- arrow size (DIMASZ)
    dimdec     INTEGER,                     -- decimal places (DIMDEC)
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dimstyles_name ON dimstyles(name);

CREATE TABLE IF NOT EXISTS textstyles (
    drawing_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    font       TEXT,                        -- TTF filename or .shx name
    height     REAL,                        -- fixed text height (0 = scalable)
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_textstyles_name ON textstyles(name);

CREATE TABLE IF NOT EXISTS linetypes (
    drawing_id     INTEGER NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT,
    pattern_length REAL,                    -- DXF tag 40: total length of one pattern repeat
    dash_count     INTEGER,                 -- DXF tag 73: number of dash elements
    FOREIGN KEY (drawing_id) REFERENCES drawings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_linetypes_name ON linetypes(name);

-- Sheet set (.dst) contents. There's no FK to `drawings` because sheets can
-- reference DWGs that aren't in the project's [[drawings]] list.
CREATE TABLE IF NOT EXISTS sheets (
    sheetset_name      TEXT,                -- name from the .dst root
    sheetset_path      TEXT NOT NULL,
    number             TEXT,
    title              TEXT,
    description        TEXT,
    dwg_file           TEXT,                -- raw value from the .dst
    layout_name        TEXT,
    resolved_dwg_path  TEXT,                -- absolute, resolved vs. .dst dir
    is_dwg_present     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sheets_dwg ON sheets(resolved_dwg_path);
"""


# Block flag bits (DXF BLOCK group code 70). Bit 4 = external reference.
_BLOCK_FLAG_XREF = 4


def _opt_float(value) -> float | None:
    """Coerce a value to float, returning None for unset / unparseable."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value) -> int | None:
    """Coerce a value to int, returning None for unset / unparseable."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _linetype_pattern_stats(lt) -> tuple[float | None, int | None]:
    """Extract (pattern_length, dash_count) from an ezdxf linetype.

    The pattern lives in `lt.pattern_tags.tags` as DXF group-code tuples:
      tag code 40 — total pattern length (single float).
      tag code 73 — number of dash items in the pattern (single int).
    Returns (None, None) for solid/Continuous types that have no pattern.
    """
    try:
        tags = lt.pattern_tags.tags
    except Exception:
        return None, None
    pattern_length: float | None = None
    dash_count: int | None = None
    for tag in tags:
        code = getattr(tag, "code", None)
        value = getattr(tag, "value", None)
        if code == 40 and pattern_length is None:
            pattern_length = _opt_float(value)
        elif code == 73 and dash_count is None:
            dash_count = _opt_int(value)
    return pattern_length, dash_count


class SchemaVersionMismatch(RuntimeError):
    """Raised when an on-disk db has a schema_version newer than this code.

    Older databases are rebuilt in place (the db is a derived cache —
    re-indexing produces the same answers from the source drawings). A db
    written by a *newer* schema_version is still surfaced as an error
    because we'd silently lose access to tables / columns the newer code
    added; the user should upgrade the package instead.
    """


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a SQLite database with the project schema.

    Foreign keys are enabled (needed for ON DELETE CASCADE when re-indexing).

    Schema-version contract:
      - existing == SCHEMA_VERSION: open as-is.
      - existing < SCHEMA_VERSION: wipe + re-create. The db is a derived
        cache; re-indexing is the migration. `init_db` doesn't trigger the
        re-index itself — callers should observe an empty db and run
        `DXFIndexer.index_project()` to refill it.
      - existing > SCHEMA_VERSION: raise SchemaVersionMismatch. Loading
        a newer db with older code would silently miss tables/columns.
    """
    db_path = Path(db_path)
    rebuild_required = False

    if db_path.exists():
        # Cheap probe to read schema_version without running the CREATE-IF-NOT-
        # EXISTS script (which is idempotent but writes a meta row regardless).
        probe = sqlite3.connect(str(db_path))
        probe.row_factory = sqlite3.Row
        try:
            has_meta = probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if has_meta is not None:
                row = probe.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None:
                    existing = int(row["value"])
                    if existing > SCHEMA_VERSION:
                        raise SchemaVersionMismatch(
                            f"Database {db_path} was written by "
                            f"schema_version={existing}; this code understands "
                            f"{SCHEMA_VERSION}. Upgrade the autocad_mcp package."
                        )
                    if existing < SCHEMA_VERSION:
                        rebuild_required = True
                else:
                    # meta table exists but no version row — treat as old.
                    rebuild_required = True
            else:
                # A SQLite file that isn't ours — leave it alone, but the
                # script below will add our tables. If those names clash
                # with the existing file's tables, that's the caller's
                # problem and we surface it as the raw sqlite error.
                pass
        finally:
            probe.close()

    if rebuild_required:
        # Drop the old file rather than running per-version ALTER scripts.
        # The db is a derived cache: re-indexing repopulates it from source.
        db_path.unlink()
        log.info("project_db_rebuilt_for_schema_bump", path=str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()
    return conn


@dataclass
class IndexResult:
    """Summary of a single index_project() run."""

    indexed: int = 0
    skipped: int = 0
    errors: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class DXFIndexer:
    """Index a `ProjectConfig` into a SQLite database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn = init_db(self.db_path)

    def close(self) -> None:
        self.conn.close()

    def index_project(self, config: ProjectConfig) -> IndexResult:
        """Walk every drawing and (re-)populate its rows in the database.

        Also re-indexes the project's sheet set (if `config.sheetset_path` is
        set) into the `sheets` table.
        """
        result = IndexResult()

        for ref in config.drawings:
            try:
                if not ref.file.exists():
                    result.errors.append({"drawing": ref.name, "error": "file not found"})
                    result.skipped += 1
                    log.warning("project_index_drawing_missing", drawing=ref.name, file=str(ref.file))
                    continue
                self._index_one(ref.name, ref.file, ref.discipline)
                result.indexed += 1
                log.info(
                    "project_index_drawing_indexed",
                    drawing=ref.name,
                    file=str(ref.file),
                    discipline=ref.discipline,
                )
            except Exception as ex:
                # Record the failure but keep going so one bad DXF doesn't stop
                # the rest of the project from indexing.
                self._record_failed_drawing(ref.name, ref.file, ref.discipline, str(ex))
                result.errors.append({"drawing": ref.name, "error": str(ex)})
                result.skipped += 1
                log.warning("project_index_drawing_failed", drawing=ref.name, error=str(ex))

        # Sheet set (.dst) — independent of drawings, never fatal.
        if config.sheetset_path is not None:
            try:
                self._index_sheetset(config.sheetset_path)
            except Exception as ex:
                log.warning(
                    "project_index_sheetset_failed",
                    path=str(config.sheetset_path),
                    error=str(ex),
                )
                result.errors.append({
                    "sheetset": str(config.sheetset_path),
                    "error": str(ex),
                })

        self.conn.commit()
        return result

    def _index_sheetset(self, dst_path: Path) -> None:
        """Parse a .dst and replace the project's sheets table contents.

        The DELETE-then-INSERT is wrapped so the previous sheets data isn't
        wiped on a parse failure — a malformed .dst used to leave the table
        empty and silently broke `list_sheets` until the next successful
        re-index.

        Binary `.dst` fallback: AutoCAD 2024+ writes sheet sets in a
        proprietary binary format the XML reader can't decode. When that
        happens we try the COM-backed reader (NETLOADs SheetSetReader.dll
        into a running AutoCAD). If AutoCAD isn't running, the original
        binary-unsupported error surfaces unchanged.
        """
        from autocad_mcp.project.sheetset import (
            SheetSetBinaryUnsupported, read_sheetset,
        )

        if not dst_path.exists():
            log.warning("project_sheetset_missing", path=str(dst_path))
            # Treat a missing file the same as "no sheet set" — clear any
            # stale rows so subsequent queries don't return rows for a file
            # the user has removed.
            self.conn.execute("DELETE FROM sheets")
            return

        # Parse first; if read_sheetset raises, we leave the previous rows
        # in place and surface the error upstream.
        try:
            ss = read_sheetset(dst_path)
        except SheetSetBinaryUnsupported as binary_err:
            ss = self._read_binary_sheetset_via_com(dst_path, binary_err)
        dst_dir = dst_path.parent
        rows: list[tuple] = []
        for sheet in ss.sheets:
            resolved = (dst_dir / sheet.dwg_file).resolve() if sheet.dwg_file else None
            present = bool(resolved and resolved.exists())
            rows.append((
                ss.name,
                str(dst_path),
                sheet.number,
                sheet.title,
                sheet.description,
                sheet.dwg_file,
                sheet.layout,
                str(resolved) if resolved else None,
                int(present),
            ))

        # Now that parsing succeeded, atomically swap: clear + insert in
        # the same implicit SQLite transaction. `index_project` commits at
        # the end of its pass; on error before that commit, SQLite rolls
        # back and the previous rows survive.
        self.conn.execute("DELETE FROM sheets")
        if rows:
            self.conn.executemany(
                """INSERT INTO sheets
                   (sheetset_name, sheetset_path, number, title, description,
                    dwg_file, layout_name, resolved_dwg_path, is_dwg_present)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def _read_binary_sheetset_via_com(self, dst_path: Path, original_err):
        """Fallback when the XML reader rejects a binary `.dst`.

        Tries `sheetset_com_reader.read_sheetset_via_com` (NETLOAD'd .NET
        helper). On any failure — DLL missing, AutoCAD not running, COM
        error — re-raises the original `SheetSetBinaryUnsupported` so the
        caller sees the actionable workaround message rather than a
        confusing COM stack trace.
        """
        try:
            from autocad_mcp.project.sheetset_com_reader import (
                read_sheetset_via_com,
            )
        except ImportError as imp_err:
            log.info(
                "project_sheetset_binary_com_unavailable",
                path=str(dst_path),
                reason=f"import error: {imp_err}",
            )
            raise original_err from None

        try:
            ss = read_sheetset_via_com(dst_path)
            log.info(
                "project_sheetset_binary_via_com",
                path=str(dst_path),
                sheets=len(ss.sheets),
            )
            return ss
        except Exception as com_err:
            log.warning(
                "project_sheetset_binary_com_failed",
                path=str(dst_path),
                error=str(com_err),
            )
            raise original_err from com_err

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _delete_existing(self, file_path: str) -> None:
        """Remove any pre-existing rows for this drawing (idempotent re-index)."""
        row = self.conn.execute(
            "SELECT id FROM drawings WHERE file_path = ?", (file_path,)
        ).fetchone()
        if row is not None:
            self.conn.execute("DELETE FROM drawings WHERE id = ?", (row["id"],))

    def _record_failed_drawing(self, name: str, file: Path, discipline: str, error: str) -> None:
        file_path = str(file)
        self._delete_existing(file_path)
        self.conn.execute(
            """INSERT INTO drawings (name, file_path, discipline, indexed_at, error)
               VALUES (?, ?, ?, ?, ?)""",
            (name, file_path, discipline, _dt.datetime.now(_dt.timezone.utc).isoformat(), error),
        )

    def _index_one(self, name: str, file: Path, discipline: str) -> None:
        file_path = str(file)
        self._delete_existing(file_path)

        # `read_drawing` handles both .dxf and .dwg — the latter is routed
        # through accoreconsole / ODA File Converter (see dwg_reader.py).
        doc = read_drawing(file_path)
        indexed_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        dxf_version = doc.dxfversion

        # Entities across all layouts (model + paper).
        entity_rows: list[tuple] = []
        for layout in doc.layouts:
            space = "modelspace" if layout.is_modelspace else f"paperspace:{layout.name}"
            for e in layout:
                try:
                    handle = e.dxf.handle
                    layer = e.dxf.layer if hasattr(e.dxf, "layer") else "0"
                    entity_rows.append((e.dxftype(), layer, handle, space))
                except Exception as ex:
                    # Skip any malformed entity rather than aborting the index.
                    # Log so a re-index that misses entities silently is at
                    # least diagnosable via the structlog output.
                    log.debug(
                        "project_index_entity_skipped",
                        drawing=name,
                        space=space,
                        error=str(ex),
                    )
                    continue

        # Layers.
        layer_rows: list[tuple] = []
        for l in doc.layers:
            layer_rows.append((
                l.dxf.name,
                int(l.dxf.get("color", 7)),
                l.dxf.get("linetype", "Continuous"),
                int(l.is_frozen()),
                int(l.is_locked()),
            ))

        # Blocks (skip AutoCAD-internal ones like *Model_Space, *Paper_Space).
        block_rows: list[tuple] = []
        xref_rows: list[tuple] = []
        host_dir = file.parent
        for b in doc.blocks:
            if b.name.startswith("*"):
                continue
            block_rows.append((b.name, sum(1 for _ in b)))

            # XREF detection: block flag bit 4 means EXTERNAL_REFERENCE.
            block_record = getattr(b, "block", None)
            if block_record is None:
                continue
            flags = int(getattr(block_record.dxf, "flags", 0) or 0)
            if not (flags & _BLOCK_FLAG_XREF):
                continue
            xref_path = str(getattr(block_record.dxf, "xref_path", "") or "")
            if not xref_path:
                continue
            # Resolve relative paths against the host drawing's directory.
            resolved = (host_dir / xref_path).resolve()
            is_broken = 0 if resolved.exists() else 1
            xref_rows.append((b.name, xref_path, str(resolved), is_broken))

        cursor = self.conn.execute(
            """INSERT INTO drawings
               (name, file_path, discipline, indexed_at, dxf_version,
                entity_count, layer_count, block_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, file_path, discipline, indexed_at, dxf_version,
                len(entity_rows), len(layer_rows), len(block_rows),
            ),
        )
        drawing_id = cursor.lastrowid

        if entity_rows:
            self.conn.executemany(
                """INSERT INTO entities (drawing_id, type, layer, handle, space)
                   VALUES (?, ?, ?, ?, ?)""",
                [(drawing_id, *row) for row in entity_rows],
            )
        if layer_rows:
            self.conn.executemany(
                """INSERT INTO layers (drawing_id, name, color, linetype, is_frozen, is_locked)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(drawing_id, *row) for row in layer_rows],
            )
        if block_rows:
            self.conn.executemany(
                """INSERT INTO blocks (drawing_id, name, entity_count)
                   VALUES (?, ?, ?)""",
                [(drawing_id, *row) for row in block_rows],
            )
        if xref_rows:
            self.conn.executemany(
                """INSERT INTO xrefs
                   (drawing_id, name, path, resolved_path, is_broken)
                   VALUES (?, ?, ?, ?, ?)""",
                [(drawing_id, *row) for row in xref_rows],
            )

        # Dimension styles.
        dimstyle_rows: list[tuple] = []
        for ds in doc.dimstyles:
            dimstyle_rows.append((
                ds.dxf.name,
                _opt_float(ds.dxf.get("dimscale")),
                _opt_float(ds.dxf.get("dimtxt")),
                _opt_float(ds.dxf.get("dimasz")),
                _opt_int(ds.dxf.get("dimdec")),
            ))
        if dimstyle_rows:
            self.conn.executemany(
                """INSERT INTO dimstyles
                   (drawing_id, name, dimscale, dimtxt, dimasz, dimdec)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(drawing_id, *row) for row in dimstyle_rows],
            )

        # Text styles (`doc.styles` is the TEXTSTYLE table in DXF).
        textstyle_rows: list[tuple] = []
        for s in doc.styles:
            textstyle_rows.append((
                s.dxf.name,
                s.dxf.get("font") or None,
                _opt_float(s.dxf.get("height")),
            ))
        if textstyle_rows:
            self.conn.executemany(
                """INSERT INTO textstyles (drawing_id, name, font, height)
                   VALUES (?, ?, ?, ?)""",
                [(drawing_id, *row) for row in textstyle_rows],
            )

        # Linetypes (`doc.linetypes`). Pattern stats come from DXF group codes:
        #   tag 40 = total pattern length, tag 73 = dash count.
        linetype_rows: list[tuple] = []
        for lt in doc.linetypes:
            description = ""
            try:
                description = lt.dxf.description or ""
            except Exception as ex:
                # ezdxf raises DXFAttributeError on linetypes that lack the
                # description tag — that's fine, we just store an empty one.
                log.debug(
                    "linetype_description_unavailable",
                    name=getattr(lt.dxf, "name", "?"),
                    error=str(ex),
                )
            pattern_length, dash_count = _linetype_pattern_stats(lt)
            linetype_rows.append((
                lt.dxf.name, description, pattern_length, dash_count,
            ))
        if linetype_rows:
            self.conn.executemany(
                """INSERT INTO linetypes
                   (drawing_id, name, description, pattern_length, dash_count)
                   VALUES (?, ?, ?, ?, ?)""",
                [(drawing_id, *row) for row in linetype_rows],
            )
