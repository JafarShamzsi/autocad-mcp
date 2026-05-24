"""Tests for the project model — loader, indexer, queries, MCP tool dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.arch import (
    create_room,
    create_wall,
    create_wall_with_openings,
    setup_aia_layers,
)
from autocad_mcp.project import (
    DXFIndexer,
    find_entities,
    init_db,
    list_drawings,
    list_layers,
    load_project,
    project_info,
)
from autocad_mcp.project.index import SchemaVersionMismatch


# ---------------------------------------------------------------------------
# Fixture project: three DXFs + an .acadproj.toml in a tmp dir
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_project(tmp_path):
    """Build a self-contained sample project in tmp_path and return its root.

    The project contains 3 architectural floor-plan DXFs with predictable
    entity / layer counts so tests can assert exact numbers.
    """
    # Drawing 1: 4 walls forming a room
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    msp = doc.modelspace()
    create_wall(msp, 0, 0, 240, 0, thickness=6)
    create_wall(msp, 240, 0, 240, 180, thickness=6)
    create_wall(msp, 240, 180, 0, 180, thickness=6)
    create_wall(msp, 0, 180, 0, 0, thickness=6)
    doc.saveas(str(tmp_path / "fp1.dxf"))

    # Drawing 2: wall with door + window + room boundary
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    msp = doc.modelspace()
    create_wall_with_openings(
        msp, 0, 0, 240, 0, thickness=6,
        openings=[
            {"type": "door", "position": 60, "width": 36},
            {"type": "window", "position": 144, "width": 30},
        ],
    )
    create_room(msp, [(0, 0), (240, 0), (240, 180), (0, 180)], name="LIVING")
    doc.saveas(str(tmp_path / "fp2.dxf"))

    # Drawing 3: an elevation-style drawing (different discipline tag in toml)
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    msp = doc.modelspace()
    create_wall(msp, 0, 0, 120, 0, thickness=6)
    doc.saveas(str(tmp_path / "elev.dxf"))

    toml = """
[project]
name = "Sample House"
units = "feet"
scale = 0.25

[standards]
layer_standard = "AIA"

[paths]
support = ["."]

[[drawings]]
name = "FloorPlan-1"
file = "fp1.dxf"
discipline = "A"

[[drawings]]
name = "FloorPlan-2"
file = "fp2.dxf"
discipline = "A"

[[drawings]]
name = "Elevation"
file = "elev.dxf"
discipline = "A"
"""
    (tmp_path / ".acadproj.toml").write_text(toml)
    return tmp_path


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestProjectConfig:
    def test_load_from_directory(self, sample_project):
        cfg = load_project(sample_project)
        assert cfg.name == "Sample House"
        assert cfg.units == "feet"
        assert cfg.scale == pytest.approx(0.25)
        assert cfg.layer_standard == "AIA"
        assert len(cfg.drawings) == 3

    def test_load_from_explicit_toml_path(self, sample_project):
        cfg = load_project(sample_project / ".acadproj.toml")
        assert cfg.name == "Sample House"

    def test_paths_resolved_absolute(self, sample_project):
        cfg = load_project(sample_project)
        for d in cfg.drawings:
            assert d.file.is_absolute()
            assert d.file.exists()

    def test_missing_toml_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_project(tmp_path)

    def test_missing_name_raises(self, tmp_path):
        (tmp_path / ".acadproj.toml").write_text("[project]\nunits = \"feet\"\n")
        with pytest.raises(ValueError, match="name"):
            load_project(tmp_path)

    def test_missing_drawing_file_raises(self, tmp_path):
        (tmp_path / ".acadproj.toml").write_text(
            "[project]\nname = \"X\"\n\n[[drawings]]\nname = \"a\"\n"
        )
        with pytest.raises(ValueError, match="file"):
            load_project(tmp_path)

    def test_discipline_normalized_uppercase(self, tmp_path):
        (tmp_path / "x.dxf").write_text("")  # not opened by loader
        (tmp_path / ".acadproj.toml").write_text(
            "[project]\nname = \"X\"\n\n[[drawings]]\nname = \"a\"\nfile = \"x.dxf\"\ndiscipline = \"a\"\n"
        )
        cfg = load_project(tmp_path)
        assert cfg.drawings[0].discipline == "A"


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class TestIndexer:
    def test_index_runs_clean(self, sample_project):
        cfg = load_project(sample_project)
        idx = DXFIndexer(sample_project / ".acadproj.db")
        result = idx.index_project(cfg)
        assert result.indexed == 3
        assert result.skipped == 0
        assert result.errors == []

        info = project_info(idx.conn)
        assert info["drawings"] == 3
        assert info["failed"] == 0
        # 3 drawings × 19 AIA layers + default "0" layer per file ≈ 60.
        # Exact count is N*(len(AIA_LAYERS)+1).
        assert info["total_entities"] > 0

        idx.close()

    def test_index_is_idempotent(self, sample_project):
        cfg = load_project(sample_project)
        idx = DXFIndexer(sample_project / ".acadproj.db")
        r1 = idx.index_project(cfg)
        info1 = project_info(idx.conn)

        # Second run should produce identical counts (rows replaced, not duplicated).
        r2 = idx.index_project(cfg)
        info2 = project_info(idx.conn)

        assert r1.indexed == r2.indexed == 3
        assert info1["total_entities"] == info2["total_entities"]
        assert info1["total_layers"] == info2["total_layers"]
        idx.close()

    def test_schema_version_mismatch_raises(self, tmp_path):
        """A db file written under a newer schema_version must refuse to open."""
        import sqlite3

        db_path = tmp_path / "future.db"
        # Create a minimal db with a meta table claiming schema_version=999.
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()

        with pytest.raises(SchemaVersionMismatch, match="999"):
            init_db(db_path)

    def test_outdated_schema_version_auto_rebuilds(self, tmp_path):
        """An older db file is wiped and re-created at the current schema.

        The db is a derived cache: re-indexing repopulates it from the source
        drawings, so we don't need ALTER-TABLE migrations. The caller still
        has to re-run `index_project()` to refill rows — `init_db` only
        rebuilds the schema, not the data.
        """
        import sqlite3

        from autocad_mcp.project.index import SCHEMA_VERSION

        db_path = tmp_path / "old.db"
        # Pre-v2 db: just the meta table claiming schema_version=1.
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.commit()
        conn.close()

        # Should NOT raise; instead, wipe + recreate.
        new_conn = init_db(db_path)
        try:
            row = new_conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(row["value"]) == SCHEMA_VERSION
            # The new schema includes the xrefs table; presence proves the wipe
            # happened rather than silently keeping the v1 shape.
            tables = {
                r["name"] for r in new_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "xrefs" in tables
        finally:
            new_conn.close()

    async def test_force_reindex_recovers_from_outdated_db(self, sample_project, monkeypatch):
        """`project.index(force=True)` should let users recover from an
        older db by wiping it and rebuilding from scratch."""
        import sqlite3

        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        # Drop a pre-v2 db on disk where the project tool expects to find one.
        old_db = sample_project / ".acadproj.db"
        conn = sqlite3.connect(str(old_db))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.commit()
        conn.close()

        await fn(operation="load", data={"path": str(sample_project)})
        # Without force, index() should attempt to open the existing db and fail.
        # With force, it wipes and rebuilds cleanly.
        result = await fn(operation="index", data={"force": True})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["indexed"] == 3

    def test_missing_drawing_recorded_as_error(self, tmp_path):
        (tmp_path / ".acadproj.toml").write_text(
            "[project]\nname = \"X\"\n\n"
            "[[drawings]]\nname = \"phantom\"\nfile = \"nope.dxf\"\n"
        )
        cfg = load_project(tmp_path)
        idx = DXFIndexer(tmp_path / ".acadproj.db")
        result = idx.index_project(cfg)
        assert result.indexed == 0
        assert result.skipped == 1
        assert result.errors[0]["drawing"] == "phantom"
        idx.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@pytest.fixture
def indexed(sample_project):
    """A built-and-indexed sample project; yields (config, sqlite3.Connection)."""
    cfg = load_project(sample_project)
    idx = DXFIndexer(sample_project / ".acadproj.db")
    idx.index_project(cfg)
    yield cfg, idx.conn
    idx.close()


class TestQueries:
    def test_list_drawings(self, indexed):
        _, conn = indexed
        rows = list_drawings(conn)
        names = [r["name"] for r in rows]
        assert sorted(names) == ["Elevation", "FloorPlan-1", "FloorPlan-2"]

    def test_list_layers_aggregates(self, indexed):
        _, conn = indexed
        rows = list_layers(conn)
        # A-WALL appears in all three drawings.
        a_wall = next(r for r in rows if r["name"] == "A-WALL")
        assert a_wall["drawings_using"] == 3

    def test_list_layers_filtered_by_drawing(self, indexed):
        _, conn = indexed
        rows = list_layers(conn, drawing="FloorPlan-2")
        names = {r["name"] for r in rows}
        # FP-2 has door + window opening symbols, so its layer set should
        # at least include the standard set we wrote into it.
        assert "A-WALL" in names
        assert "A-DOOR" in names
        assert "A-GLAZ" in names

    def test_find_entities_by_layer(self, indexed):
        _, conn = indexed
        walls = find_entities(conn, layer="A-WALL")
        # FP-1: 4 walls; FP-2: ~2-3 segments (with openings); elev: 1 wall.
        assert len(walls) >= 6
        assert all(e["layer"] == "A-WALL" for e in walls)

    def test_find_entities_by_drawing(self, indexed):
        _, conn = indexed
        rows = find_entities(conn, drawing="Elevation")
        assert len(rows) >= 1
        assert all(r["drawing"] == "Elevation" for r in rows)

    def test_find_entities_limit_clamped(self, indexed):
        _, conn = indexed
        rows = find_entities(conn, limit=2)
        assert len(rows) <= 2


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestProjectToolDispatch:
    async def test_full_session(self, sample_project, monkeypatch):
        """Drive the MCP `project` tool through a load → index → info → find flow."""
        # The tool keeps state in module-level singletons; reset between runs.
        from autocad_mcp import server as server_mod
        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )

        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        # load
        result = await fn(operation="load", data={"path": str(sample_project)})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["name"] == "Sample House"
        assert parsed["payload"]["drawing_count"] == 3

        # index
        result = await fn(operation="index")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["indexed"] == 3

        # info
        result = await fn(operation="info")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["drawings"] == 3

        # list_drawings
        result = await fn(operation="list_drawings")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        names = [d["name"] for d in parsed["payload"]["drawings"]]
        assert "FloorPlan-1" in names

        # find_entities filtered
        result = await fn(operation="find_entities", data={"layer": "A-WALL"})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["count"] >= 6

        # unload
        result = await fn(operation="unload")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["unloaded"] is True

    async def test_unknown_operation(self, monkeypatch):
        from autocad_mcp import server as server_mod
        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project
        result = await fn(operation="bogus")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed

    async def test_concurrent_calls_serialise_safely(self, sample_project, monkeypatch):
        """Two concurrent project() calls should not corrupt _project_state."""
        import asyncio

        from autocad_mcp import server as server_mod
        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )

        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project
        await fn(operation="load", data={"path": str(sample_project)})

        # Fire two indexes back-to-back; the lock should serialise them and
        # both should report a clean indexed=3.
        r1, r2 = await asyncio.gather(
            fn(operation="index"),
            fn(operation="index"),
        )
        p1 = json.loads(r1) if isinstance(r1, str) else json.loads(r1[0].text)
        p2 = json.loads(r2) if isinstance(r2, str) else json.loads(r2[0].text)
        assert p1["payload"]["indexed"] == 3
        assert p2["payload"]["indexed"] == 3

    async def test_load_required_before_query(self, monkeypatch):
        from autocad_mcp import server as server_mod
        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project
        result = await fn(operation="info")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed
        assert "No project loaded" in parsed["error"]
