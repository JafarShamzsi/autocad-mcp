"""Tests for the standards-compliance check."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.arch import create_wall, setup_aia_layers
from autocad_mcp.arch.layers import AIA_LAYERS
from autocad_mcp.project import (
    DXFIndexer,
    aia_standard,
    check_drawing,
    check_project,
    load_project,
    load_standard_from_dxf,
)


# ---------------------------------------------------------------------------
# Fixtures: compliant + noncompliant drawings + an indexed project
# ---------------------------------------------------------------------------


def _build_compliant(path: Path) -> None:
    doc = ezdxf.new("R2013", setup=True)
    setup_aia_layers(doc)
    create_wall(doc.modelspace(), 0, 0, 100, 0, thickness=6)
    doc.saveas(str(path))


def _build_noncompliant(path: Path) -> None:
    """Drawing that breaks AIA in several ways:
       - A-WALL with wrong color (red=1 instead of cyan=4)
       - JUNK-LAYER that isn't in AIA at all
       - Missing every other AIA layer (A-DOOR, A-GLAZ, etc.)
    """
    doc = ezdxf.new("R2013", setup=True)
    doc.layers.add("A-WALL", color=1)
    doc.layers.add("JUNK-LAYER", color=3)
    create_wall(doc.modelspace(), 0, 0, 100, 0, thickness=6)
    doc.saveas(str(path))


@pytest.fixture
def std_project(tmp_path):
    """Two-drawing project: one compliant, one noncompliant."""
    _build_compliant(tmp_path / "compliant.dxf")
    _build_noncompliant(tmp_path / "noncompliant.dxf")
    (tmp_path / ".acadproj.toml").write_text(
        '[project]\nname = "Std Test"\n\n'
        '[[drawings]]\nname = "compliant"\nfile = "compliant.dxf"\n\n'
        '[[drawings]]\nname = "noncompliant"\nfile = "noncompliant.dxf"\n'
    )
    cfg = load_project(tmp_path)
    idx = DXFIndexer(tmp_path / ".acadproj.db")
    idx.index_project(cfg)
    yield tmp_path, cfg, idx.conn
    idx.close()


# ---------------------------------------------------------------------------
# Standard constructors
# ---------------------------------------------------------------------------


class TestAIAStandard:
    def test_returns_all_aia_layers(self):
        std = aia_standard()
        assert std.name == "AIA"
        for spec in AIA_LAYERS:
            assert spec.name in std.rules

    def test_layer_0_is_always_allowed(self):
        std = aia_standard()
        assert std.is_allowed("0")
        assert std.is_allowed("Defpoints")

    def test_hidden_linetype_resolved_to_dashed(self):
        """A-WALL-HEAD spec says HIDDEN; the standard should expect DASHED
        (matching what the AIA layer setup actually writes)."""
        std = aia_standard()
        rule = std.rules["A-WALL-HEAD"]
        assert rule.expected_linetype == "DASHED"


class TestLoadFromDXF:
    def test_extracts_non_internal_layers(self, tmp_path):
        ref = tmp_path / "ref.dxf"
        doc = ezdxf.new("R2013", setup=True)
        doc.layers.add("CUSTOM-A", color=5)
        doc.layers.add("CUSTOM-B", color=6)
        doc.saveas(str(ref))

        std = load_standard_from_dxf(ref, name="firm")
        assert std.name == "firm"
        assert "CUSTOM-A" in std.rules
        assert "CUSTOM-B" in std.rules
        # Layer "0" should be excluded from the rule set.
        assert "0" not in std.rules

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_standard_from_dxf(tmp_path / "nope.dxf")


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


class TestCheckDrawing:
    def test_compliant_zero_violations(self, std_project):
        _, _, conn = std_project
        row = conn.execute(
            "SELECT id FROM drawings WHERE name = 'compliant'"
        ).fetchone()
        violations = check_drawing(conn, row["id"], aia_standard())
        assert violations == []

    def test_noncompliant_reports_wrong_color(self, std_project):
        _, _, conn = std_project
        row = conn.execute(
            "SELECT id FROM drawings WHERE name = 'noncompliant'"
        ).fetchone()
        violations = check_drawing(conn, row["id"], aia_standard())
        wrong_colors = [v for v in violations if v["kind"] == "wrong_color"]
        # A-WALL was set to red=1 instead of cyan=4
        a_wall = next(v for v in wrong_colors if v["layer"] == "A-WALL")
        assert a_wall["expected"] == 4
        assert a_wall["actual"] == 1

    def test_noncompliant_reports_missing(self, std_project):
        _, _, conn = std_project
        row = conn.execute(
            "SELECT id FROM drawings WHERE name = 'noncompliant'"
        ).fetchone()
        violations = check_drawing(conn, row["id"], aia_standard())
        missing = [v["layer"] for v in violations if v["kind"] == "missing"]
        # The noncompliant drawing only declared A-WALL, so almost all other
        # AIA layers should appear as missing.
        assert "A-DOOR" in missing
        assert "A-GLAZ" in missing

    def test_extra_layer_reported(self, std_project):
        _, _, conn = std_project
        row = conn.execute(
            "SELECT id FROM drawings WHERE name = 'noncompliant'"
        ).fetchone()
        violations = check_drawing(conn, row["id"], aia_standard())
        extras = [v["layer"] for v in violations if v["kind"] == "extra"]
        assert "JUNK-LAYER" in extras

    def test_include_extra_false_suppresses_extras(self, std_project):
        _, _, conn = std_project
        row = conn.execute(
            "SELECT id FROM drawings WHERE name = 'noncompliant'"
        ).fetchone()
        violations = check_drawing(conn, row["id"], aia_standard(), include_extra=False)
        assert all(v["kind"] != "extra" for v in violations)

    def test_unknown_drawing_id_raises(self, std_project):
        _, _, conn = std_project
        with pytest.raises(ValueError):
            check_drawing(conn, 99999, aia_standard())


class TestCheckProject:
    def test_scope_to_single_drawing(self, std_project):
        _, _, conn = std_project
        result = check_project(conn, aia_standard(), drawing_name="compliant")
        assert result["drawings_checked"] == 1
        assert result["total_violations"] == 0

    def test_full_project_aggregates(self, std_project):
        _, _, conn = std_project
        result = check_project(conn, aia_standard())
        assert result["drawings_checked"] == 2
        assert result["total_violations"] > 0
        assert result["standard_name"] == "AIA"

    def test_limit_truncates(self, std_project):
        _, _, conn = std_project
        result = check_project(conn, aia_standard(), limit=3)
        assert len(result["violations"]) == 3
        assert result["truncated"] is True


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestProjectToolCheckStandard:
    async def test_check_standard_default_aia(self, std_project, monkeypatch):
        tmp_path, _, _ = std_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")

        result = await fn(operation="check_standard")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["standard_name"] == "AIA"
        assert parsed["payload"]["drawings_checked"] == 2
        assert parsed["payload"]["total_violations"] > 0

    async def test_check_standard_scoped_to_drawing(self, std_project, monkeypatch):
        tmp_path, _, _ = std_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="check_standard", data={"drawing": "compliant"})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["drawings_checked"] == 1
        assert parsed["payload"]["total_violations"] == 0

    async def test_check_standard_from_dxf(self, std_project, monkeypatch, tmp_path):
        proj_path, _, _ = std_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        # Build a tiny "standard" that requires only A-WALL with color=1.
        # The compliant drawing's A-WALL has color=4 — that's a wrong_color.
        ref = tmp_path / "firm-std.dxf"
        doc = ezdxf.new("R2013", setup=True)
        doc.layers.add("A-WALL", color=1)
        doc.saveas(str(ref))

        await fn(operation="load", data={"path": str(proj_path)})
        await fn(operation="index")
        result = await fn(
            operation="check_standard",
            data={"standard": str(ref), "include_extra": False},
        )
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        # The compliant drawing has A-WALL color=4; the firm standard wants 1.
        wrong_colors = [
            v for v in parsed["payload"]["violations"] if v["kind"] == "wrong_color"
        ]
        assert any(v["layer"] == "A-WALL" for v in wrong_colors)
