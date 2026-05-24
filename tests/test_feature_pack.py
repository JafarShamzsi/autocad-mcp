"""Tests for the post-fix-pass feature pack.

Covers:
  * F6 — drawing-units propagation (area suffix swaps SF ↔ m²)
  * Asset reuse — `block.list_from_file` + `block.import_from_file`
  * Hatch library — `arch.list_hatches` + `arch.hatch_region`
  * XREF mutation — `xref_attach` + `xref_detach` (ezdxf path)
  * Layout / paperspace — `layout_list`, `layout_create`, `layout_create_viewport`
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def backend():
    b = EzdxfBackend()
    await b.initialize()
    return b


# ---------------------------------------------------------------------------
# F6: Units propagation
# ---------------------------------------------------------------------------


class TestUnitsPropagation:
    def test_imperial_feet_uses_SF_suffix(self):
        from autocad_mcp.arch.units import UnitSystem
        u = UnitSystem.imperial_feet()
        assert u.format_area(120.0) == "120 SF"
        assert u.format_area(85.5) == "85.5 SF"

    def test_metric_meters_uses_m2_suffix(self):
        from autocad_mcp.arch.units import UnitSystem
        u = UnitSystem.metric_meters()
        assert u.format_area(120.0) == "120 m²"

    def test_metric_mm_converts_to_m2(self):
        from autocad_mcp.arch.units import UnitSystem
        u = UnitSystem.metric_mm()
        # 10 000 000 mm² == 10 m² — formatter uses 1 decimal under 100.
        assert u.format_area(10_000_000.0) == "10.0 m²"

    def test_imperial_inches_converts_to_SF(self):
        from autocad_mcp.arch.units import UnitSystem
        u = UnitSystem.imperial_inches()
        # 1440 in² == 10 ft² — formatter uses 1 decimal under 100.
        assert u.format_area(1440.0) == "10.0 SF"

    def test_unitless_preserves_SF_for_backwards_compat(self):
        from autocad_mcp.arch.units import UnitSystem
        u = UnitSystem.unitless()
        # The default before the units module existed was a raw "<n> SF"
        # suffix regardless of $INSUNITS; preserved for tolerant unitless.
        assert u.format_area(16.0) == "16.0 SF"

    def test_from_doc_picks_metric_for_meter_doc(self):
        from autocad_mcp.arch.units import UnitSystem
        doc = ezdxf.new("R2013")
        doc.header["$INSUNITS"] = 6  # meters
        u = UnitSystem.from_doc(doc)
        assert u.area_suffix == "m²"

    async def test_arch_room_label_switches_with_insunits(self, backend):
        """A metric drawing should show m² instead of SF in room labels."""
        # Force metric on the backend's doc.
        backend._doc.header["$INSUNITS"] = 6
        boundary = [[0, 0], [10, 0], [10, 10], [0, 10]]
        r = await backend.arch_room(boundary, name="LIVING")
        assert r.ok
        handles = r.payload["handles"]
        label = backend._doc.entitydb.get(handles["label"])
        assert "m²" in label.text
        assert "SF" not in label.text


# ---------------------------------------------------------------------------
# Asset reuse: list / import blocks from external DXF
# ---------------------------------------------------------------------------


@pytest.fixture
def src_dxf(tmp_path):
    """Build a tiny DXF containing two named blocks + skip a third anon block."""
    p = tmp_path / "src.dxf"
    src = ezdxf.new("R2013")
    blk1 = src.blocks.new("TEST-WIDGET")
    blk1.add_line((0, 0), (10, 0))
    blk1.add_circle((5, 0), 1.0)
    blk2 = src.blocks.new("FURN-CHAIR-X")
    blk2.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    src.saveas(p)
    return p


class TestBlockListFromFile:
    async def test_list_finds_named_blocks(self, backend, src_dxf):
        r = await backend.block_list_from_file(str(src_dxf))
        assert r.ok, r.error
        names = [b["name"] for b in r.payload["blocks"]]
        assert "TEST-WIDGET" in names
        assert "FURN-CHAIR-X" in names
        # `*Model_Space` / `*Paper_Space` are filtered out.
        assert not any(n.startswith("*") for n in names)

    async def test_list_reports_entity_count(self, backend, src_dxf):
        r = await backend.block_list_from_file(str(src_dxf))
        assert r.ok
        blocks = {b["name"]: b for b in r.payload["blocks"]}
        assert blocks["TEST-WIDGET"]["entity_count"] == 2  # line + circle
        assert blocks["FURN-CHAIR-X"]["entity_count"] == 1  # lwpolyline

    async def test_list_extracts_discipline_prefix(self, backend, src_dxf):
        r = await backend.block_list_from_file(str(src_dxf))
        assert r.ok
        blocks = {b["name"]: b for b in r.payload["blocks"]}
        assert blocks["FURN-CHAIR-X"]["prefix"] == "FURN"
        # Names without `-` get an empty prefix.
        assert blocks["TEST-WIDGET"]["prefix"] == "TEST"  # 'TEST-WIDGET' splits on first '-'

    async def test_list_missing_file_returns_not_found(self, backend, tmp_path):
        r = await backend.block_list_from_file(str(tmp_path / "missing.dxf"))
        assert not r.ok
        assert r.category == "not_found"


class TestBlockImportFromFile:
    async def test_import_all_copies_named_blocks(self, backend, src_dxf):
        r = await backend.block_import_from_file(str(src_dxf))
        assert r.ok, r.error
        assert "TEST-WIDGET" in r.payload["imported"]
        assert "FURN-CHAIR-X" in r.payload["imported"]
        # The destination doc should now contain the definitions.
        assert "TEST-WIDGET" in backend._doc.blocks
        assert "FURN-CHAIR-X" in backend._doc.blocks

    async def test_import_specific_names_only(self, backend, src_dxf):
        r = await backend.block_import_from_file(
            str(src_dxf), names=["TEST-WIDGET"],
        )
        assert r.ok
        assert r.payload["imported"] == ["TEST-WIDGET"]
        assert "FURN-CHAIR-X" not in backend._doc.blocks

    async def test_import_skips_existing_names(self, backend, src_dxf):
        # First import lands the block.
        await backend.block_import_from_file(str(src_dxf), names=["TEST-WIDGET"])
        # Second import should report skipped_exists rather than overwriting.
        r = await backend.block_import_from_file(
            str(src_dxf), names=["TEST-WIDGET"],
        )
        assert r.ok
        assert r.payload["imported"] == []
        assert r.payload["skipped_exists"] == ["TEST-WIDGET"]

    async def test_import_reports_missing_names(self, backend, src_dxf):
        r = await backend.block_import_from_file(
            str(src_dxf), names=["NOPE", "TEST-WIDGET"],
        )
        assert r.ok
        assert "TEST-WIDGET" in r.payload["imported"]
        assert r.payload["not_found_in_source"] == ["NOPE"]


# ---------------------------------------------------------------------------
# Hatch library / arch.hatch_region
# ---------------------------------------------------------------------------


class TestHatchLibrary:
    async def test_list_hatches_returns_catalogue(self, backend):
        r = await backend.arch_list_hatches()
        assert r.ok
        names = [p["name"] for p in r.payload["patterns"]]
        for required in ("BRICK", "CONCRETE", "INSULATION", "WOOD", "SOLID"):
            assert required in names, f"missing pattern: {required}"

    async def test_hatch_region_creates_hatch_entity(self, backend):
        boundary = [[0, 0], [10, 0], [10, 10], [0, 10]]
        r = await backend.arch_hatch_region(boundary, pattern="CONCRETE")
        assert r.ok, r.error
        assert r.payload["friendly_name"] == "CONCRETE"
        assert r.payload["pattern"] == "AR-CONC"

    async def test_hatch_region_unknown_pattern_errors(self, backend):
        r = await backend.arch_hatch_region(
            [[0, 0], [10, 0], [10, 10]], pattern="NOSUCH",
        )
        assert not r.ok
        assert r.category == "not_found"

    async def test_hatch_region_solid_uses_solid_fill(self, backend):
        r = await backend.arch_hatch_region(
            [[0, 0], [10, 0], [10, 10], [0, 10]], pattern="SOLID",
        )
        assert r.ok
        # Verify the entity is a HATCH with SOLID pattern.
        hatch = backend._doc.entitydb.get(r.payload["handle"])
        assert hatch.dxftype() == "HATCH"


# ---------------------------------------------------------------------------
# Layout / paperspace ops (ezdxf)
# ---------------------------------------------------------------------------


class TestLayouts:
    async def test_list_layouts_includes_modelspace(self, backend):
        r = await backend.layout_list()
        assert r.ok
        names = [l["name"] for l in r.payload["layouts"]]
        assert "Model" in names

    async def test_create_layout_then_list_shows_it(self, backend):
        r = await backend.layout_create("Sheet-1")
        assert r.ok, r.error
        r2 = await backend.layout_list()
        names = [l["name"] for l in r2.payload["layouts"]]
        assert "Sheet-1" in names

    async def test_create_duplicate_layout_fails(self, backend):
        await backend.layout_create("Sheet-X")
        r = await backend.layout_create("Sheet-X")
        assert not r.ok
        assert r.category == "invalid_arg"

    async def test_create_viewport_in_paperspace(self, backend):
        await backend.layout_create("Sheet-V")
        r = await backend.layout_create_viewport(
            "Sheet-V", 0, 0, 200, 150,
            view_extents=(0, 0, 50, 50),
        )
        assert r.ok, r.error
        assert r.payload["layout"] == "Sheet-V"

    async def test_viewport_in_modelspace_fails(self, backend):
        r = await backend.layout_create_viewport("Model", 0, 0, 100, 100)
        assert not r.ok
        assert r.category == "invalid_arg"


# ---------------------------------------------------------------------------
# XREF mutation (ezdxf path)
# ---------------------------------------------------------------------------


class TestXrefMutation:
    async def test_detach_unknown_xref_returns_not_found(self, backend):
        r = await backend.xref_detach("NO-SUCH-XREF")
        assert not r.ok
        assert r.category == "not_found"

    async def test_reload_on_ezdxf_is_not_supported(self, backend):
        r = await backend.xref_reload()
        assert not r.ok
        assert r.category == "not_supported"

    async def test_bind_on_ezdxf_is_not_supported(self, backend):
        r = await backend.xref_bind("any")
        assert not r.ok
        assert r.category == "not_supported"

    async def test_detach_refuses_regular_block(self, backend):
        """Bug fix: xref_detach used to delete any block whose name matched,
        including non-XREFs. Must guard via the DXF EXTERNAL_REFERENCE flag
        (bit 4) and refuse with invalid_arg when the target is a real block."""
        # Define a regular block, not an XREF.
        backend._doc.blocks.new("REGULAR-BLOCK")
        r = await backend.xref_detach("REGULAR-BLOCK")
        assert not r.ok
        assert r.category == "invalid_arg"
        # Still in the block table.
        assert "REGULAR-BLOCK" in backend._doc.blocks


# ---------------------------------------------------------------------------
# External block library indexer
# ---------------------------------------------------------------------------


@pytest.fixture
def library_dir(tmp_path):
    """Stage a few DXFs with named blocks so the indexer has something
    deterministic to scan. Multiple files lets us verify the same block
    name across files produces multiple rows."""
    # File A: chair blocks
    a = ezdxf.new("R2013")
    blk = a.blocks.new("FURN-CHAIR-A1")
    blk.add_line((0, 0), (5, 0))
    blk2 = a.blocks.new("FURN-CHAIR-A2")
    blk2.add_circle((0, 0), 1.0)
    blk2.add_line((0, 0), (2, 0))
    (tmp_path / "chairs.dxf").write_text("")  # placeholder
    a.saveas(tmp_path / "chairs.dxf")

    # File B: plumbing blocks (with an attribute)
    b = ezdxf.new("R2013")
    blk = b.blocks.new("PLMB-TOILET-X")
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("MFG", (0, 0))
    b.saveas(tmp_path / "plumbing.dxf")

    # File C: same block name as A — verifies dedup-by-(file,name).
    c = ezdxf.new("R2013")
    c.blocks.new("FURN-CHAIR-A1").add_line((0, 0), (1, 1))
    c.saveas(tmp_path / "chairs_v2.dxf")

    return tmp_path


class TestBlockLibraryIndexer:
    def test_scan_then_search_by_name(self, tmp_path, library_dir):
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library, search_library,
        )
        conn = init_library_db(tmp_path / "lib.db")
        result = scan_library(conn, [library_dir])
        assert result.files_scanned == 3
        assert result.blocks_indexed == 4
        assert result.files_failed == []

        hits = search_library(conn, name="chair")
        names = {h["block_name"] for h in hits}
        assert {"FURN-CHAIR-A1", "FURN-CHAIR-A2"} <= names
        # FURN-CHAIR-A1 appears in two files — both rows returned.
        a1_hits = [h for h in hits if h["block_name"] == "FURN-CHAIR-A1"]
        assert len(a1_hits) == 2

    def test_search_by_prefix(self, tmp_path, library_dir):
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library, search_library,
        )
        conn = init_library_db(tmp_path / "lib.db")
        scan_library(conn, [library_dir])
        plmb = search_library(conn, prefix="PLMB")
        assert len(plmb) == 1
        assert plmb[0]["block_name"] == "PLMB-TOILET-X"
        assert plmb[0]["has_attribs"] is True

    def test_search_by_file(self, tmp_path, library_dir):
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library, search_library,
        )
        conn = init_library_db(tmp_path / "lib.db")
        scan_library(conn, [library_dir])
        v2_hits = search_library(conn, file="chairs_v2")
        assert len(v2_hits) == 1
        assert v2_hits[0]["file_basename"] == "chairs_v2.dxf"

    def test_stats_summary(self, tmp_path, library_dir):
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library, library_stats,
        )
        conn = init_library_db(tmp_path / "lib.db")
        scan_library(conn, [library_dir])
        s = library_stats(conn)
        assert s["total_blocks"] == 4
        assert s["files_indexed"] == 3
        prefixes = {p["prefix"] for p in s["top_prefixes"]}
        assert {"FURN", "PLMB"} <= prefixes

    def test_rescan_replaces_file_rows_atomically(self, tmp_path, library_dir):
        """Re-scanning an existing file should replace its rows, not duplicate."""
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library, library_stats,
        )
        conn = init_library_db(tmp_path / "lib.db")
        scan_library(conn, [library_dir])
        before = library_stats(conn)["total_blocks"]
        scan_library(conn, [library_dir])
        after = library_stats(conn)["total_blocks"]
        assert before == after  # no duplication

    def test_scan_skips_unreadable_files_gracefully(self, tmp_path):
        """A junk file should land in `files_failed`, not crash the scan."""
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library,
        )
        # Bogus .dxf — text but not valid DXF.
        bad = tmp_path / "bogus.dxf"
        bad.write_text("This is not a DXF file")
        # And one real DXF alongside.
        good = ezdxf.new("R2013")
        good.blocks.new("OK-BLOCK").add_circle((0, 0), 1)
        good.saveas(tmp_path / "good.dxf")
        conn = init_library_db(tmp_path / "lib.db")
        r = scan_library(conn, [tmp_path])
        assert r.files_scanned == 1  # good.dxf
        assert len(r.files_failed) == 1
        assert "bogus.dxf" in r.files_failed[0]["file"]
