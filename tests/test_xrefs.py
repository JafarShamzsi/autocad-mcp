"""Tests for XREF discovery + queries + MCP tool dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.arch import create_wall, setup_aia_layers
from autocad_mcp.project import (
    DXFIndexer,
    find_broken_xrefs,
    list_xrefs,
    load_project,
)


# ---------------------------------------------------------------------------
# Helpers / fixture
# ---------------------------------------------------------------------------


def _build_host_with_xrefs(path: Path, refs: list[tuple[str, str]]) -> None:
    """Build a host DXF that declares each (block_name, raw_xref_path) pair.

    Each ref becomes an external-reference block + a single insert in modelspace.
    """
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    msp = doc.modelspace()
    for block_name, xref_path in refs:
        doc.blocks.new(
            name=block_name,
            dxfattribs={"flags": 4, "xref_path": xref_path},  # bit 4 = EXTERNAL_REFERENCE
        )
        msp.add_blockref(block_name, (0, 0))
    doc.saveas(str(path))


def _build_target(path: Path) -> None:
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    create_wall(doc.modelspace(), 0, 0, 100, 0, thickness=6)
    doc.saveas(str(path))


@pytest.fixture
def xref_project(tmp_path):
    """Project with one host drawing referencing a real DXF + one missing target."""
    # Real XREF target lives in tmp_path/shared/
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    _build_target(shared_dir / "title.dxf")

    # Host drawing in tmp_path: refs ./shared/title.dxf (real) and ./missing.dxf (broken)
    _build_host_with_xrefs(
        tmp_path / "fp1.dxf",
        [
            ("TITLE-BLOCK", "./shared/title.dxf"),
            ("STRUCT-OVERLAY", "./missing.dxf"),
        ],
    )

    # Second drawing in tmp_path: no XREFs
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    doc.saveas(str(tmp_path / "fp2.dxf"))

    (tmp_path / ".acadproj.toml").write_text(
        '[project]\nname = "XREF Test"\n\n'
        '[[drawings]]\nname = "FloorPlan-1"\nfile = "fp1.dxf"\n\n'
        '[[drawings]]\nname = "FloorPlan-2"\nfile = "fp2.dxf"\n'
    )
    cfg = load_project(tmp_path)
    idx = DXFIndexer(tmp_path / ".acadproj.db")
    idx.index_project(cfg)
    yield tmp_path, cfg, idx.conn
    idx.close()


# ---------------------------------------------------------------------------
# Indexer extension
# ---------------------------------------------------------------------------


class TestXRefIndexing:
    def test_xrefs_table_populated(self, xref_project):
        _, _, conn = xref_project
        rows = conn.execute("SELECT name, path FROM xrefs ORDER BY name").fetchall()
        names = [r["name"] for r in rows]
        assert "STRUCT-OVERLAY" in names
        assert "TITLE-BLOCK" in names

    def test_resolved_path_is_absolute(self, xref_project):
        _, _, conn = xref_project
        row = conn.execute(
            "SELECT resolved_path FROM xrefs WHERE name = 'TITLE-BLOCK'"
        ).fetchone()
        assert Path(row["resolved_path"]).is_absolute()
        assert Path(row["resolved_path"]).exists()

    def test_is_broken_flag_set_for_missing_targets(self, xref_project):
        _, _, conn = xref_project
        rows = conn.execute(
            "SELECT name, is_broken FROM xrefs ORDER BY name"
        ).fetchall()
        by_name = {r["name"]: bool(r["is_broken"]) for r in rows}
        assert by_name["TITLE-BLOCK"] is False
        assert by_name["STRUCT-OVERLAY"] is True

    def test_drawings_without_xrefs_have_no_rows(self, xref_project):
        _, _, conn = xref_project
        fp2_id = conn.execute(
            "SELECT id FROM drawings WHERE name = 'FloorPlan-2'"
        ).fetchone()["id"]
        n = conn.execute(
            "SELECT COUNT(*) FROM xrefs WHERE drawing_id = ?", (fp2_id,)
        ).fetchone()[0]
        assert n == 0


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


class TestXRefQueries:
    def test_list_xrefs_all(self, xref_project):
        _, _, conn = xref_project
        rows = list_xrefs(conn)
        assert len(rows) == 2
        broken_flags = {r["xref_name"]: r["is_broken"] for r in rows}
        assert broken_flags["TITLE-BLOCK"] is False
        assert broken_flags["STRUCT-OVERLAY"] is True

    def test_list_xrefs_filtered_by_drawing(self, xref_project):
        _, _, conn = xref_project
        rows = list_xrefs(conn, drawing="FloorPlan-2")
        assert rows == []  # FP-2 has no XREFs

    def test_find_broken_xrefs(self, xref_project):
        _, _, conn = xref_project
        broken = find_broken_xrefs(conn)
        assert len(broken) == 1
        assert broken[0]["xref_name"] == "STRUCT-OVERLAY"


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestProjectToolXRefs:
    async def test_list_xrefs_and_find_broken(self, xref_project, monkeypatch):
        tmp_path, _, _ = xref_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")

        result = await fn(operation="list_xrefs")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["count"] == 2
        assert parsed["payload"]["broken_count"] == 1

        result = await fn(operation="find_broken_xrefs")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["count"] == 1
        assert parsed["payload"]["broken_xrefs"][0]["xref_name"] == "STRUCT-OVERLAY"
