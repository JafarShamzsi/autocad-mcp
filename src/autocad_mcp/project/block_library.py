"""External block library indexer.

The `arch.library` catalogue ships ~22 procedural CC0 symbols defined in
code. Real architectural projects need a much wider catalogue: doors,
windows, furniture, plumbing, electrical — typically dropped in as
external DWG files. This module walks a directory of those DWGs, extracts
every non-anonymous block definition, and writes a searchable index to
SQLite so Claude can answer "what chair blocks do we have" without
opening every file.

The index is keyed by (source_path, block_name). A single block name can
appear in multiple source files — the index keeps all instances and
clients pick the one they want by source.

Pure-Python — no live AutoCAD needed for indexing. Reading `.dwg` files
goes through the existing `dwg_reader` (accoreconsole / ODA File
Converter); reading `.dxf` is direct.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import structlog

log = structlog.get_logger()


_LIBRARY_SCHEMA_VERSION = 1


_LIBRARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- One row per (file, block) discovered while scanning. A block name that
-- exists in multiple files gets multiple rows; the catalogue is the union.
CREATE TABLE IF NOT EXISTS library_blocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL,
    file_basename TEXT NOT NULL,
    block_name    TEXT NOT NULL,
    prefix        TEXT,                -- everything before the first dash
    entity_count  INTEGER NOT NULL,
    has_attribs   INTEGER NOT NULL DEFAULT 0,
    indexed_at    TEXT NOT NULL,
    UNIQUE (file_path, block_name)
);

CREATE INDEX IF NOT EXISTS idx_libblocks_name   ON library_blocks(block_name);
CREATE INDEX IF NOT EXISTS idx_libblocks_prefix ON library_blocks(prefix);
CREATE INDEX IF NOT EXISTS idx_libblocks_file   ON library_blocks(file_basename);
"""


@dataclass
class LibraryScanResult:
    """Summary of a `scan_library` run."""

    files_scanned: int = 0
    blocks_indexed: int = 0
    files_failed: list[dict] | None = None

    def __post_init__(self) -> None:
        if self.files_failed is None:
            self.files_failed = []


def init_library_db(db_path: str | Path) -> sqlite3.Connection:
    """Open / create the block-library db at `db_path` with schema applied.

    Unlike the project index, the library db is purely additive — re-running
    `scan_library` replaces rows for files it visits and leaves the rest.
    Schema version is tracked for forward-compat but no migration is
    needed yet (only one version exists).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(_LIBRARY_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(_LIBRARY_SCHEMA_VERSION)),
    )
    conn.commit()
    return conn


def _iter_drawings(root: Path, recursive: bool = True) -> Iterable[Path]:
    """Yield every `.dwg` / `.dxf` under `root`."""
    if not root.is_dir():
        return
    if recursive:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".dwg", ".dxf"):
                yield p
    else:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in (".dwg", ".dxf"):
                yield p


def _block_rows_from(path: Path) -> list[tuple]:
    """Extract (block_name, prefix, entity_count, has_attribs) tuples from
    every non-anonymous, non-xref block in `path`."""
    from autocad_mcp.project.dwg_reader import read_drawing

    doc = read_drawing(path)
    rows: list[tuple] = []
    for blk in doc.blocks:
        if blk.name.startswith("*"):
            continue
        try:
            flags = int(getattr(blk.block.dxf, "flags", 0) or 0)
        except Exception:
            flags = 0
        # Skip XREFs (flag 4); they aren't standalone re-usable blocks.
        if flags & 4:
            continue
        entity_count = 0
        has_attribs = False
        for e in blk:
            entity_count += 1
            if e.dxftype() == "ATTDEF":
                has_attribs = True
        prefix = blk.name.split("-", 1)[0] if "-" in blk.name else ""
        rows.append((blk.name, prefix, entity_count, int(has_attribs)))
    return rows


def scan_library(
    conn: sqlite3.Connection,
    roots: Iterable[str | Path],
    *,
    recursive: bool = True,
) -> LibraryScanResult:
    """Walk every `.dwg` / `.dxf` under `roots` and (re-)populate the index.

    `roots` can be one directory or several. Each file is indexed atomically
    — rows for a file are wiped and re-inserted in one transaction, so a
    partial scan never leaves stale rows behind for a file that was visited.
    """
    result = LibraryScanResult()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    seen_paths: set[Path] = set()

    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        for path in _iter_drawings(root, recursive=recursive):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            file_path = str(path)
            try:
                rows = _block_rows_from(path)
            except Exception as ex:
                # Some DWGs are damaged / unreadable / vendor-locked.
                # Skip and keep going.
                result.files_failed.append(
                    {"file": file_path, "error": str(ex)[:300]}
                )
                log.warning(
                    "block_library_scan_failed",
                    file=file_path, error=str(ex),
                )
                continue
            # Atomically replace this file's rows.
            conn.execute(
                "DELETE FROM library_blocks WHERE file_path = ?",
                (file_path,),
            )
            if rows:
                conn.executemany(
                    """INSERT INTO library_blocks
                       (file_path, file_basename, block_name, prefix,
                        entity_count, has_attribs, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (file_path, path.name, name, prefix, ec, ha, now)
                        for (name, prefix, ec, ha) in rows
                    ],
                )
            result.files_scanned += 1
            result.blocks_indexed += len(rows)
            log.info(
                "block_library_file_indexed",
                file=file_path, blocks=len(rows),
            )
    conn.commit()
    return result


def search_library(
    conn: sqlite3.Connection,
    *,
    name: str | None = None,
    prefix: str | None = None,
    file: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Search the library index. All filters are optional and combine.

    `name` matches block_name as a LIKE pattern (case-insensitive). Pass
    the bare term ("chair") and we wrap it as `%chair%`. `prefix` is exact-
    match on the discipline prefix (`FURN`, `PLMB`, `ELEC`). `file`
    matches the source filename (basename, case-insensitive substring).
    """
    sql = """
        SELECT block_name, prefix, file_basename, file_path,
               entity_count, has_attribs, indexed_at
        FROM library_blocks
    """
    where: list[str] = []
    params: list = []
    if name:
        where.append("block_name LIKE ? COLLATE NOCASE")
        params.append(f"%{name}%")
    if prefix:
        where.append("prefix = ? COLLATE NOCASE")
        params.append(prefix)
    if file:
        where.append("file_basename LIKE ? COLLATE NOCASE")
        params.append(f"%{file}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY block_name, file_basename LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "block_name": r["block_name"],
            "prefix": r["prefix"],
            "file_basename": r["file_basename"],
            "file_path": r["file_path"],
            "entity_count": r["entity_count"],
            "has_attribs": bool(r["has_attribs"]),
            "indexed_at": r["indexed_at"],
        }
        for r in rows
    ]


def library_stats(conn: sqlite3.Connection) -> dict:
    """High-level summary of the index — counts + last-scan timestamp."""
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM library_blocks").fetchone()[0]
    files = cur.execute(
        "SELECT COUNT(DISTINCT file_path) FROM library_blocks"
    ).fetchone()[0]
    prefixes = cur.execute(
        """SELECT prefix, COUNT(*) AS n
           FROM library_blocks
           WHERE prefix != ''
           GROUP BY prefix ORDER BY n DESC LIMIT 20"""
    ).fetchall()
    last_indexed = cur.execute(
        "SELECT MAX(indexed_at) FROM library_blocks"
    ).fetchone()[0]
    return {
        "total_blocks": total,
        "files_indexed": files,
        "top_prefixes": [{"prefix": r["prefix"], "count": r["n"]} for r in prefixes],
        "last_indexed": last_indexed,
    }
