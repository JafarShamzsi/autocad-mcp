"""Asset catalog: TOML-on-disk metadata + SQLite cache for search.

Two layers:

* **Disk** — `library.toml` at the root, `meta.toml` per asset directory.
  Human-edited. Git-trackable. The source of truth.
* **Cache** — SQLite db (`.acad-library.db`) next to `library.toml`.
  Rebuilt by `reindex()` from the disk state. Powers `search()` /
  `list_categories()` without rescanning the filesystem on every call.

The cache stores the same fields as `meta.toml` plus a normalized
search-friendly `searchable_text` column (name + tags + notes excerpt
joined with spaces, lowercased). SQLite FTS5 would be a natural fit but
isn't enabled in every Python build; we use plain `LIKE` against the
normalized column, which covers keyword search well enough for the
expected catalog size (hundreds to low thousands of assets).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import structlog

log = structlog.get_logger()


_CATALOG_SCHEMA_VERSION = 1


_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id              TEXT PRIMARY KEY,        -- e.g. "furniture/seating/sofa-84-modern"
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,           -- e.g. "furniture/seating"
    discipline      TEXT,                    -- AIA code: A, S, M, E, P, etc.
    tags_joined     TEXT NOT NULL DEFAULT '',-- space-joined for LIKE; tag list in JSON below
    tags_json       TEXT NOT NULL DEFAULT '[]',
    width           REAL,                    -- nominal size (drawing units, typically inches)
    depth           REAL,
    height          REAL,
    units           TEXT,                    -- "inches" / "millimeters" / etc.
    default_layer   TEXT,
    license         TEXT,
    inserts_as      TEXT NOT NULL DEFAULT 'block',  -- "block" | "group" | "xref"
    block_file      TEXT NOT NULL,           -- absolute path to the .dxf
    block_name      TEXT,                    -- block name inside the .dxf to import
    thumbnail       TEXT,                    -- absolute path to thumb image, or NULL
    notes_excerpt   TEXT,                    -- first 200 chars of notes.md
    searchable_text TEXT NOT NULL DEFAULT '',-- lowercase concat of name+tags+notes
    indexed_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_category  ON assets(category);
CREATE INDEX IF NOT EXISTS idx_assets_discipline ON assets(discipline);
CREATE INDEX IF NOT EXISTS idx_assets_search    ON assets(searchable_text);
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryConfig:
    """Top-level `library.toml` contents.

    `root` is always the directory containing the file, regardless of what
    the toml says — relative paths in `meta.toml` resolve against the
    asset directory, not against this root.
    """

    root: Path
    name: str = "asset-library"
    version: str = "0.1.0"
    default_layer_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class Asset:
    """One catalog entry — the union of `meta.toml` and disk facts."""

    id: str                       # canonical id, derived from path
    name: str
    category: str                 # like "furniture/seating"
    discipline: str | None = None
    tags: list[str] = field(default_factory=list)
    width: float | None = None
    depth: float | None = None
    height: float | None = None
    units: str | None = None
    default_layer: str | None = None
    license: str | None = None
    inserts_as: str = "block"
    block_file: Path | None = None     # absolute path to block.dxf
    block_name: str | None = None      # block name inside the dxf (None ⇒ same as asset stem)
    thumbnail: Path | None = None
    notes_excerpt: str = ""


# ---------------------------------------------------------------------------
# Discovery & loading
# ---------------------------------------------------------------------------


def default_library_root() -> Path:
    """Repo-relative `asset-library/` directory.

    Resolves from `src/autocad_mcp/library/catalog.py` → repo root → `asset-library/`.
    Users can override per-project by passing an explicit `root` to
    `AssetCatalog`.
    """
    here = Path(__file__).resolve()
    # src/autocad_mcp/library/catalog.py → repo root is 4 up.
    return here.parent.parent.parent.parent / "asset-library"


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file via stdlib tomllib (3.11+) or tomli (3.10)."""
    if sys.version_info >= (3, 11):
        import tomllib  # type: ignore[import-not-found]
    else:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore[import-not-found]
        except ImportError as ex:
            raise RuntimeError(
                "TOML on Python 3.10 needs `pip install tomli`."
            ) from ex
    with path.open("rb") as fp:
        return tomllib.load(fp)


def load_library_config(root: Path) -> LibraryConfig:
    """Read `<root>/library.toml`. Tolerant of a missing file — returns
    defaults so a freshly-created `asset-library/` works without explicit
    config."""
    cfg_path = root / "library.toml"
    if not cfg_path.exists():
        return LibraryConfig(root=root)
    data = _load_toml(cfg_path)
    lib = data.get("library", {})
    layers = data.get("default_layers", {})
    return LibraryConfig(
        root=root,
        name=str(lib.get("name", "asset-library")),
        version=str(lib.get("version", "0.1.0")),
        default_layer_overrides=dict(layers),
    )


def _walk_assets(root: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    """Yield (asset_dir, parsed_meta) for every `meta.toml` under `root`.

    Skips `library.toml` itself plus any directory starting with `.` (dot
    dirs like `.acad-library.db` or `.git/`).
    """
    if not root.is_dir():
        return
    for meta_path in root.rglob("meta.toml"):
        # Skip if the asset dir is hidden / inside one.
        if any(part.startswith(".") for part in meta_path.relative_to(root).parts):
            continue
        try:
            data = _load_toml(meta_path)
        except Exception as ex:
            log.warning(
                "asset_meta_unreadable",
                path=str(meta_path), error=str(ex),
            )
            continue
        yield meta_path.parent, data


def _asset_from_meta(asset_dir: Path, root: Path, meta: dict[str, Any]) -> Asset | None:
    """Build an `Asset` from a parsed `meta.toml`. Returns None when the
    record is unusable (missing required fields or missing block.dxf)."""
    rel_id = asset_dir.relative_to(root).as_posix()
    # `id` is the relative directory by default; meta can override but we
    # normalize trailing slashes either way.
    asset_id = str(meta.get("id", rel_id)).strip("/")
    name = str(meta.get("name", asset_dir.name)).strip()
    category = str(meta.get("category", asset_dir.parent.relative_to(root).as_posix())).strip("/")

    files = meta.get("files") or {}
    block_rel = files.get("block", "block.dxf")
    block_path = (asset_dir / block_rel).resolve()
    if not block_path.exists():
        log.warning(
            "asset_block_missing",
            asset_id=asset_id, expected=str(block_path),
        )
        return None

    thumb_rel = files.get("thumbnail")
    thumb_path: Path | None = None
    if thumb_rel:
        candidate = (asset_dir / thumb_rel).resolve()
        if candidate.exists():
            thumb_path = candidate

    notes_path = asset_dir / "notes.md"
    notes_excerpt = ""
    if notes_path.exists():
        try:
            notes_excerpt = notes_path.read_text(encoding="utf-8")[:200]
        except OSError:
            pass

    size = meta.get("size") or {}
    return Asset(
        id=asset_id,
        name=name,
        category=category,
        discipline=meta.get("discipline"),
        tags=[str(t) for t in (meta.get("tags") or [])],
        width=_opt_float(size.get("width")),
        depth=_opt_float(size.get("depth")),
        height=_opt_float(size.get("height")),
        units=size.get("units"),
        default_layer=meta.get("default_layer"),
        license=meta.get("license"),
        inserts_as=str(meta.get("inserts_as", "block")),
        block_file=block_path,
        block_name=meta.get("block_name"),
        thumbnail=thumb_path,
        notes_excerpt=notes_excerpt,
    )


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class AssetCatalog:
    """Asset catalog with a SQLite cache.

    Typical lifecycle:
        cat = AssetCatalog(root)        # opens / creates the cache db
        cat.reindex()                   # rescan disk, replace cache rows
        cat.search("sofa")              # query the cache
        cat.get("furniture/seating/sofa-84-modern")
    """

    def __init__(self, root: Path | str | None = None, *, db_path: Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else default_library_root()
        self.db_path = (db_path or (self.root / ".acad-library.db")).resolve()
        self.config = load_library_config(self.root)
        self.conn = _open_db(self.db_path)

    # --- Inspection -------------------------------------------------------

    def list_categories(self) -> list[dict[str, Any]]:
        """Return (category, count) rows sorted by category."""
        rows = self.conn.execute(
            "SELECT category, COUNT(*) AS n FROM assets GROUP BY category ORDER BY category"
        ).fetchall()
        return [{"category": r["category"], "count": r["n"]} for r in rows]

    def stats(self) -> dict[str, Any]:
        # Single aggregated query — saves two round-trips for a hot path
        # the catalog UI calls on every page load.
        row = self.conn.execute(
            """SELECT COUNT(*)                    AS total,
                      COUNT(DISTINCT category)    AS categories,
                      MAX(indexed_at)             AS last_indexed
               FROM assets"""
        ).fetchone()
        return {
            "root": str(self.root),
            "total_assets": row["total"],
            "category_count": row["categories"],
            "last_indexed": row["last_indexed"],
            "library_name": self.config.name,
            "library_version": self.config.version,
        }

    def get(self, asset_id: str) -> Asset | None:
        row = self.conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_asset(row)

    def search(
        self,
        query: str | None = None,
        *,
        category: str | None = None,
        discipline: str | None = None,
        tag: str | None = None,
        max_width: float | None = None,
        limit: int = 50,
    ) -> list[Asset]:
        """Keyword + filter search across the catalog.

        `query` matches against the normalized `searchable_text` column
        (name + tags + notes-excerpt). All filters AND together.
        """
        sql = "SELECT * FROM assets"
        where: list[str] = []
        params: list[Any] = []
        if query:
            # Case-insensitive substring across the pre-joined searchable col.
            where.append("searchable_text LIKE ?")
            params.append(f"%{query.lower()}%")
        if category:
            # Match category or any child category.
            where.append("(category = ? OR category LIKE ?)")
            params.extend([category, f"{category}/%"])
        if discipline:
            where.append("discipline = ?")
            params.append(discipline.upper())
        if tag:
            # Word-boundary match against the space-joined tag list.
            # Without the spaces around the value, searching `tag="mode"`
            # would match assets tagged "modern" (and vice versa). The
            # `' ' || ... || ' '` wraps the column so leading + trailing
            # boundaries also match.
            where.append("(' ' || tags_joined || ' ') LIKE ?")
            params.append(f"% {tag.lower()} %")
        if max_width is not None:
            where.append("(width IS NULL OR width <= ?)")
            params.append(float(max_width))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY name LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_asset(r) for r in rows]

    # --- Reindex ----------------------------------------------------------

    def reindex(self) -> dict[str, Any]:
        """Rebuild the cache from disk. Atomic-replace: walk the tree into
        a list first, then truncate + insert in a single transaction.

        Returns counts of indexed / skipped assets plus any per-asset errors.
        """
        assets: list[Asset] = []
        errors: list[dict] = []
        for asset_dir, meta in _walk_assets(self.root):
            try:
                a = _asset_from_meta(asset_dir, self.root, meta)
                if a is None:
                    errors.append({
                        "asset_dir": str(asset_dir.relative_to(self.root)),
                        "error": "no block.dxf or missing required fields",
                    })
                    continue
                assets.append(a)
            except Exception as ex:
                errors.append({
                    "asset_dir": str(asset_dir.relative_to(self.root)),
                    "error": str(ex)[:300],
                })
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM assets")
            for a in assets:
                self.conn.execute(_INSERT_SQL, _insert_params(a, now))
        log.info("asset_catalog_reindexed",
                 root=str(self.root),
                 indexed=len(assets),
                 errors=len(errors))
        return {
            "root": str(self.root),
            "indexed": len(assets),
            "errors": errors,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_CATALOG_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(_CATALOG_SCHEMA_VERSION)),
    )
    conn.commit()
    return conn


_INSERT_SQL = """
INSERT INTO assets
  (id, name, category, discipline, tags_joined, tags_json,
   width, depth, height, units, default_layer, license, inserts_as,
   block_file, block_name, thumbnail, notes_excerpt,
   searchable_text, indexed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_params(a: Asset, now: str) -> tuple:
    import json as _json
    tags_joined = " ".join(t.lower() for t in a.tags)
    searchable = " ".join(filter(None, [
        a.name.lower(),
        a.id.lower(),
        tags_joined,
        a.notes_excerpt.lower(),
    ]))
    return (
        a.id,
        a.name,
        a.category,
        a.discipline,
        tags_joined,
        _json.dumps(a.tags),
        a.width, a.depth, a.height, a.units,
        a.default_layer, a.license, a.inserts_as,
        str(a.block_file) if a.block_file else "",
        a.block_name,
        str(a.thumbnail) if a.thumbnail else None,
        a.notes_excerpt,
        searchable,
        now,
    )


def _row_to_asset(r: sqlite3.Row) -> Asset:
    import json as _json
    return Asset(
        id=r["id"],
        name=r["name"],
        category=r["category"],
        discipline=r["discipline"],
        tags=_json.loads(r["tags_json"]) if r["tags_json"] else [],
        width=r["width"],
        depth=r["depth"],
        height=r["height"],
        units=r["units"],
        default_layer=r["default_layer"],
        license=r["license"],
        inserts_as=r["inserts_as"],
        block_file=Path(r["block_file"]) if r["block_file"] else None,
        block_name=r["block_name"],
        thumbnail=Path(r["thumbnail"]) if r["thumbnail"] else None,
        notes_excerpt=r["notes_excerpt"] or "",
    )
