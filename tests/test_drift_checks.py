"""Tests for linetype + block drift detection in the project tool."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.project import (
    DXFIndexer,
    find_block_inconsistencies,
    find_linetype_inconsistencies,
    list_blocks,
    list_linetypes,
    load_project,
)


# ---------------------------------------------------------------------------
# Fixture: two drawings whose STANDARD-looking linetype + DOOR-36 block both
# drift between them — different pattern + different entity_count.
# ---------------------------------------------------------------------------


def _build_drawing(
    path: Path,
    *,
    hidden_pattern_length: float,
    door_block_entities: int,
) -> None:
    doc = ezdxf.new("R2013", setup=True)

    # Custom linetype "FIRM-HIDDEN" with the requested total pattern length.
    # ezdxf wants a list of (length, _) tuples that sum to the total; we use a
    # simple dash-gap-dash with that ratio.
    half = hidden_pattern_length / 2.0
    if "FIRM-HIDDEN" in doc.linetypes:
        doc.linetypes.remove("FIRM-HIDDEN")
    doc.linetypes.add(
        name="FIRM-HIDDEN",
        pattern=[hidden_pattern_length, half, -half],
        description=f"Firm hidden (pattern={hidden_pattern_length})",
    )

    # "DOOR-36" block with a variable number of line entities so block
    # drift surfaces in `distinct_entity_counts`.
    if "DOOR-36" in doc.blocks:
        doc.blocks.delete_block("DOOR-36")
    block = doc.blocks.new(name="DOOR-36")
    for i in range(door_block_entities):
        block.add_line((0, i), (10, i))

    # Insert the block once in modelspace so it isn't purged.
    doc.modelspace().add_blockref("DOOR-36", (0, 0))

    doc.saveas(str(path))


@pytest.fixture
def drift_project(tmp_path):
    """Two drawings with deliberate linetype + block drift on FIRM-HIDDEN / DOOR-36."""
    _build_drawing(
        tmp_path / "fp1.dxf",
        hidden_pattern_length=2.0,
        door_block_entities=3,
    )
    _build_drawing(
        tmp_path / "fp2.dxf",
        hidden_pattern_length=5.0,
        door_block_entities=5,
    )
    (tmp_path / ".acadproj.toml").write_text(
        '[project]\nname = "Drift Test"\n\n'
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


class TestLinetypeIndexing:
    def test_linetype_rows_populated(self, drift_project):
        _, _, conn = drift_project
        n = conn.execute("SELECT COUNT(*) FROM linetypes").fetchone()[0]
        # ezdxf setup=True ships 19 linetypes + ByBlock/ByLayer/Continuous;
        # plus FIRM-HIDDEN we added in each of 2 drawings.
        assert n > 0
        firm = conn.execute(
            "SELECT COUNT(*) FROM linetypes WHERE name = 'FIRM-HIDDEN'"
        ).fetchone()[0]
        assert firm == 2

    def test_pattern_length_drift_persists(self, drift_project):
        _, _, conn = drift_project
        rows = conn.execute(
            """SELECT d.name, lt.pattern_length
               FROM linetypes lt JOIN drawings d ON d.id = lt.drawing_id
               WHERE lt.name = 'FIRM-HIDDEN' ORDER BY d.name"""
        ).fetchall()
        lengths = {r["name"]: r["pattern_length"] for r in rows}
        assert lengths["FloorPlan-1"] == pytest.approx(2.0)
        assert lengths["FloorPlan-2"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestDriftQueries:
    def test_list_linetypes_aggregates(self, drift_project):
        _, _, conn = drift_project
        rows = list_linetypes(conn)
        firm = next(r for r in rows if r["name"] == "FIRM-HIDDEN")
        assert firm["drawings_using"] == 2
        # Different pattern_length in each drawing → 2 distinct configs.
        assert firm["distinct_configs"] == 2

    def test_find_linetype_inconsistencies(self, drift_project):
        _, _, conn = drift_project
        conflicts = find_linetype_inconsistencies(conn)
        names = [c["name"] for c in conflicts]
        assert "FIRM-HIDDEN" in names
        firm = next(c for c in conflicts if c["name"] == "FIRM-HIDDEN")
        drawings = {d["drawing"] for d in firm["drawings"]}
        assert drawings == {"FloorPlan-1", "FloorPlan-2"}

    def test_list_blocks_aggregates(self, drift_project):
        _, _, conn = drift_project
        rows = list_blocks(conn)
        door = next(r for r in rows if r["name"] == "DOOR-36")
        assert door["drawings_using"] == 2
        # 3 entities vs 5 entities → 2 distinct counts.
        assert door["distinct_entity_counts"] == 2

    def test_find_block_inconsistencies(self, drift_project):
        _, _, conn = drift_project
        conflicts = find_block_inconsistencies(conn)
        names = [c["name"] for c in conflicts]
        assert "DOOR-36" in names

    def test_list_linetypes_filtered_by_drawing(self, drift_project):
        _, _, conn = drift_project
        rows = list_linetypes(conn, drawing="FloorPlan-1")
        # Scoped to one drawing — drawings_using = 1 everywhere.
        assert all(r["drawings_using"] == 1 for r in rows)


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestProjectToolDrift:
    async def test_list_linetypes_via_tool(self, drift_project, monkeypatch):
        tmp_path, _, _ = drift_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")

        result = await fn(operation="list_linetypes")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        names = [r["name"] for r in parsed["payload"]["linetypes"]]
        assert "FIRM-HIDDEN" in names

    async def test_find_linetype_inconsistencies_via_tool(self, drift_project, monkeypatch):
        tmp_path, _, _ = drift_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="find_linetype_inconsistencies")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["count"] >= 1

    async def test_find_block_inconsistencies_via_tool(self, drift_project, monkeypatch):
        tmp_path, _, _ = drift_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="find_block_inconsistencies")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        names = [c["name"] for c in parsed["payload"]["conflicts"]]
        assert "DOOR-36" in names

    async def test_list_blocks_via_tool(self, drift_project, monkeypatch):
        tmp_path, _, _ = drift_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="list_blocks")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        names = [b["name"] for b in parsed["payload"]["blocks"]]
        assert "DOOR-36" in names
