"""Tests for dimstyle / textstyle indexing + queries + MCP dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.project import (
    DXFIndexer,
    find_style_inconsistencies,
    list_dimstyles,
    list_textstyles,
    load_project,
)


# ---------------------------------------------------------------------------
# Fixture: two-drawing project with deliberate dim/text-style drift
# ---------------------------------------------------------------------------


def _build_drawing(path: Path, *, dim_scale: float, custom_font: str) -> None:
    doc = ezdxf.new("R2013", setup=True)
    # Override the STANDARD dimstyle so the two drawings disagree.
    std = doc.dimstyles.get("Standard")
    std.dxf.dimscale = dim_scale
    # Add a "FIRM-STANDARD" textstyle with a font we can vary across drawings.
    if "FIRM-STANDARD" not in doc.styles:
        doc.styles.add("FIRM-STANDARD", font=custom_font)
    doc.saveas(str(path))


@pytest.fixture
def style_project(tmp_path):
    """Two drawings where STANDARD dimstyle has different dimscale + the FIRM-STANDARD
    textstyle uses different fonts.  Both kinds of drift should surface in queries."""
    _build_drawing(
        tmp_path / "fp1.dxf",
        dim_scale=1.0, custom_font="LiberationSans-Regular.ttf",
    )
    _build_drawing(
        tmp_path / "fp2.dxf",
        dim_scale=48.0, custom_font="OpenSans-Regular.ttf",
    )
    (tmp_path / ".acadproj.toml").write_text(
        '[project]\nname = "Styles Test"\n\n'
        '[[drawings]]\nname = "FloorPlan-1"\nfile = "fp1.dxf"\n\n'
        '[[drawings]]\nname = "FloorPlan-2"\nfile = "fp2.dxf"\n'
    )
    cfg = load_project(tmp_path)
    idx = DXFIndexer(tmp_path / ".acadproj.db")
    idx.index_project(cfg)
    yield tmp_path, cfg, idx.conn
    idx.close()


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class TestStyleIndexing:
    def test_dimstyle_rows_populated(self, style_project):
        _, _, conn = style_project
        n = conn.execute("SELECT COUNT(*) FROM dimstyles").fetchone()[0]
        assert n > 0
        # Standard dimstyle exists in both drawings.
        std = conn.execute(
            "SELECT COUNT(*) FROM dimstyles WHERE name = 'Standard'"
        ).fetchone()[0]
        assert std == 2

    def test_textstyle_rows_populated(self, style_project):
        _, _, conn = style_project
        n = conn.execute("SELECT COUNT(*) FROM textstyles").fetchone()[0]
        assert n > 0
        # FIRM-STANDARD exists in both drawings.
        firm = conn.execute(
            "SELECT COUNT(*) FROM textstyles WHERE name = 'FIRM-STANDARD'"
        ).fetchone()[0]
        assert firm == 2

    def test_dimscale_drift_persists(self, style_project):
        _, _, conn = style_project
        rows = conn.execute(
            """SELECT d.name, ds.dimscale
               FROM dimstyles ds JOIN drawings d ON d.id = ds.drawing_id
               WHERE ds.name = 'Standard' ORDER BY d.name"""
        ).fetchall()
        scales = {r["name"]: r["dimscale"] for r in rows}
        assert scales["FloorPlan-1"] == pytest.approx(1.0)
        assert scales["FloorPlan-2"] == pytest.approx(48.0)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestStyleQueries:
    def test_list_dimstyles_detects_distinct_configs(self, style_project):
        _, _, conn = style_project
        rows = list_dimstyles(conn)
        std = next(r for r in rows if r["name"] == "Standard")
        assert std["drawings_using"] == 2
        # Two drawings, two different dimscales → distinct_configs = 2
        assert std["distinct_configs"] == 2

    def test_list_textstyles_detects_distinct_fonts(self, style_project):
        _, _, conn = style_project
        rows = list_textstyles(conn)
        firm = next(r for r in rows if r["name"] == "FIRM-STANDARD")
        assert firm["drawings_using"] == 2
        assert firm["distinct_fonts"] == 2

    def test_list_dimstyles_filtered_by_drawing(self, style_project):
        _, _, conn = style_project
        rows = list_dimstyles(conn, drawing="FloorPlan-1")
        # Just FP-1's dimstyles — each name should now show drawings_using=1.
        assert all(r["drawings_using"] == 1 for r in rows)

    def test_find_style_inconsistencies(self, style_project):
        _, _, conn = style_project
        result = find_style_inconsistencies(conn)

        dim_names = [c["name"] for c in result["dim_conflicts"]]
        assert "Standard" in dim_names
        std_conflict = next(c for c in result["dim_conflicts"] if c["name"] == "Standard")
        # Each conflict carries per-drawing detail rows.
        drawings_in_conflict = {d["drawing"] for d in std_conflict["drawings"]}
        assert drawings_in_conflict == {"FloorPlan-1", "FloorPlan-2"}

        text_names = [c["name"] for c in result["text_conflicts"]]
        assert "FIRM-STANDARD" in text_names

        assert result["total_conflicts"] >= 2


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestProjectToolStyles:
    async def test_list_dimstyles_via_tool(self, style_project, monkeypatch):
        tmp_path, _, _ = style_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")

        result = await fn(operation="list_dimstyles")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        names = [r["name"] for r in parsed["payload"]["dimstyles"]]
        assert "Standard" in names

    async def test_find_style_inconsistencies_via_tool(self, style_project, monkeypatch):
        tmp_path, _, _ = style_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="find_style_inconsistencies")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["total_conflicts"] >= 2

    async def test_list_textstyles_filtered_by_drawing(self, style_project, monkeypatch):
        tmp_path, _, _ = style_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="list_textstyles", data={"drawing": "FloorPlan-1"})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        # Filter should restrict each row's drawings_using count to 1.
        assert all(r["drawings_using"] == 1 for r in parsed["payload"]["textstyles"])
