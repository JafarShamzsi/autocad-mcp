"""Tests for the DWG reader (accoreconsole / ODA File Converter wrapper).

Three layers of testing:
  1. Path-detection unit tests (mocked filesystem).
  2. .dxf passthrough — runs everywhere.
  3. Real .dwg integration test — requires accoreconsole or ODA installed; skipped otherwise.
     When AutoCAD is running locally, also builds the test .dwg via the COM backend.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.project import (
    DWGConverterMissing,
    available_dwg_converters,
    read_drawing,
)
from autocad_mcp.project.dwg_reader import (
    find_accoreconsole,
    find_oda_converter,
)


# ---------------------------------------------------------------------------
# Path detection (real probe — observational, not assertion-heavy)
# ---------------------------------------------------------------------------


class TestConverterDiscovery:
    def test_returns_path_or_none(self):
        # Both functions return Path-or-None; either is acceptable depending
        # on machine state. We just confirm the type contract.
        for result in (find_accoreconsole(), find_oda_converter()):
            assert result is None or result.exists()

    def test_available_dict_has_both_keys(self):
        info = available_dwg_converters()
        assert "accoreconsole" in info
        assert "oda_file_converter" in info


# ---------------------------------------------------------------------------
# .dxf passthrough
# ---------------------------------------------------------------------------


class TestDXFPassthrough:
    def test_reads_dxf_directly(self, tmp_path):
        out = tmp_path / "simple.dxf"
        doc = ezdxf.new("R2013", setup=True)
        doc.modelspace().add_line((0, 0), (10, 0))
        doc.saveas(str(out))

        loaded = read_drawing(out)
        assert loaded.dxfversion is not None
        entities = list(loaded.modelspace())
        assert len(entities) == 1
        assert entities[0].dxftype() == "LINE"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_drawing(tmp_path / "no-such-file.dxf")

    def test_unsupported_extension_raises(self, tmp_path):
        weird = tmp_path / "drawing.iges"
        weird.write_text("")
        with pytest.raises(ValueError, match="Unsupported"):
            read_drawing(weird)


# ---------------------------------------------------------------------------
# DWG conversion — graceful failure without a converter
# ---------------------------------------------------------------------------


class TestDWGConversionMissing:
    def test_dwg_without_converter_raises(self, tmp_path, monkeypatch):
        from autocad_mcp.project import dwg_reader

        # Pretend neither converter exists.
        monkeypatch.setattr(dwg_reader, "find_accoreconsole", lambda: None)
        monkeypatch.setattr(dwg_reader, "find_oda_converter", lambda: None)

        fake_dwg = tmp_path / "phantom.dwg"
        fake_dwg.write_text("")  # not actually a DWG, but read_drawing checks
                                  # extension *before* trying to convert.

        with pytest.raises(DWGConverterMissing, match="accoreconsole"):
            read_drawing(fake_dwg)


# ---------------------------------------------------------------------------
# DWG conversion — real integration (skips if no converter)
# ---------------------------------------------------------------------------


class TestDWGConversionReal:
    """Exercise the actual DWG → DXF conversion path on this machine."""

    # Module-scope so the COM mint (initialize → create_doc → save) runs once
    # per file, not per test. Each downstream test only *reads* the resulting
    # DWG, so sharing is safe.
    @pytest.fixture(scope="class")
    def sample_dwg(self, tmp_path_factory):
        """Mint a tiny DWG via the COM backend (if AutoCAD is running),
        otherwise skip the integration tests."""
        tmp_path = tmp_path_factory.mktemp("dwg_sample")
        if find_accoreconsole() is None and find_oda_converter() is None:
            pytest.skip("No DWG converter available")

        # The simplest portable source of a DWG: build a DXF with ezdxf,
        # then ask the converter to round-trip it through a save_as_dwg.
        # accoreconsole can also do DXF → DWG via DXFIN + SAVEAS DWG.
        # Easier path: drive the COM backend if it's running.
        import asyncio
        import sys

        if sys.platform != "win32":
            pytest.skip("Windows-only")

        try:
            from autocad_mcp.backends.com_backend import COMBackend
        except Exception:
            pytest.skip("pywin32 not available")

        async def _build():
            b = COMBackend()
            r = await b.initialize()
            if not r.ok:
                return None
            await b.drawing_create()
            await b.arch_setup_layers()
            await b.arch_wall(0, 0, 100, 0, thickness=6)
            await b.arch_wall(100, 0, 100, 50, thickness=6)
            out = tmp_path / "smoke.dwg"

            def _save():
                doc = b._app.ActiveDocument
                doc.SaveAs(str(out), 60)  # ac2018_dwg

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(b._executor, b._wrap_for_com, _save)

            def _close():
                try:
                    b._app.ActiveDocument.Close(False)
                except Exception:
                    pass

            await loop.run_in_executor(b._executor, b._wrap_for_com, _close)
            return out

        dwg = asyncio.run(_build())
        if dwg is None or not dwg.exists():
            pytest.skip("Couldn't mint a test DWG (AutoCAD not available)")
        return dwg

    @pytest.mark.integration
    def test_real_dwg_round_trips(self, sample_dwg):
        """A DWG saved by AutoCAD must come back through read_drawing with
        the same layers and entities we put in it."""
        doc = read_drawing(sample_dwg)
        layers = sorted(l.dxf.name for l in doc.layers)
        # AIA layers should survive the DWG round-trip.
        assert "A-WALL" in layers
        # Two walls we drew should be present.
        wall_entities = [
            e for e in doc.modelspace()
            if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == "A-WALL"
        ]
        assert len(wall_entities) == 2

    @pytest.mark.integration
    def test_indexer_processes_mixed_dwg_dxf_project(self, sample_dwg, tmp_path):
        """End-to-end: an .acadproj.toml with both a .dwg and a .dxf must
        index correctly, with entities + layers from both ending up in
        SQLite."""
        import shutil

        from autocad_mcp.arch import create_wall, setup_aia_layers
        from autocad_mcp.project import DXFIndexer, load_project

        # Stage the DWG into the project directory.
        proj_dir = tmp_path / "project"
        proj_dir.mkdir()
        shutil.copy(sample_dwg, proj_dir / "plan-dwg.dwg")

        # Build a sibling .dxf entirely in ezdxf.
        doc = ezdxf.new("R2013", setup=True)
        setup_aia_layers(doc)
        create_wall(doc.modelspace(), 0, 0, 50, 0, thickness=6)
        doc.saveas(str(proj_dir / "plan-dxf.dxf"))

        (proj_dir / ".acadproj.toml").write_text(
            '[project]\nname = "MixedFormat"\n\n'
            '[[drawings]]\nname = "DWG"\nfile = "plan-dwg.dwg"\n\n'
            '[[drawings]]\nname = "DXF"\nfile = "plan-dxf.dxf"\n'
        )

        cfg = load_project(proj_dir)
        idx = DXFIndexer(proj_dir / ".acadproj.db")
        try:
            result = idx.index_project(cfg)
            assert result.indexed == 2
            assert result.skipped == 0

            # Both drawings should now have entities on A-WALL.
            rows = idx.conn.execute(
                """SELECT d.name AS drawing, COUNT(*) AS n
                   FROM entities e JOIN drawings d ON d.id = e.drawing_id
                   WHERE e.layer = 'A-WALL'
                   GROUP BY d.name"""
            ).fetchall()
            wall_counts = {r["drawing"]: r["n"] for r in rows}
            assert wall_counts.get("DWG", 0) > 0
            assert wall_counts.get("DXF", 0) > 0
        finally:
            idx.close()
