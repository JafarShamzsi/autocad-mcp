"""Tests for the COM-backed binary `.dst` reader.

The reader requires AutoCAD running, so the live path is integration-only.
The unit tests here cover the pure-Python logic: JSON-to-SheetSet
mapping, path normalisation, error handling on missing prerequisites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocad_mcp.project.sheetset import SheetSet, SheetSetBinaryUnsupported
from autocad_mcp.project.sheetset_com_reader import (
    _build_sheetset,
    _lispify,
    default_dll_path,
    read_sheetset_via_com,
)


_FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestPureFunctions:
    def test_lispify_replaces_backslashes(self):
        p = Path(r"C:\foo\bar\baz.dst")
        assert "\\" not in _lispify(p)
        assert _lispify(p).startswith("C:/foo/bar")

    def test_lispify_handles_forward_slashes(self):
        p = Path("C:/foo/bar.dst")
        assert _lispify(p) == "C:/foo/bar.dst"

    def test_default_dll_path_points_at_bin(self):
        p = default_dll_path()
        assert p.name == "SheetSetReader.dll"
        assert p.parent.name == "dotnet"
        assert p.parent.parent.name == "bin"

    def test_build_sheetset_minimal(self):
        data = {
            "ok": True, "name": "ExampleSet",
            "sheets": [
                {"number": "A-101", "title": "FLOOR PLAN",
                 "description": "First floor", "dwg_file": "./fp1.dwg",
                 "layout": "Layout1"},
            ],
        }
        ss = _build_sheetset(Path("C:/x/example.dst"), data)
        assert isinstance(ss, SheetSet)
        assert ss.name == "ExampleSet"
        assert ss.path == Path("C:/x/example.dst")
        assert len(ss.sheets) == 1
        s = ss.sheets[0]
        assert s.number == "A-101"
        assert s.title == "FLOOR PLAN"
        assert s.description == "First floor"
        assert s.dwg_file == "./fp1.dwg"
        assert s.layout == "Layout1"

    def test_build_sheetset_falls_back_to_filename_when_name_blank(self):
        # The .NET reader can return empty `name` if the .dst has no
        # explicit set name. Fall back to the path stem so callers don't
        # see "unnamed sheet set" rows.
        data = {"ok": True, "name": "", "sheets": []}
        ss = _build_sheetset(Path("C:/x/IRD_Addition.dst"), data)
        assert ss.name == "IRD_Addition"

    def test_build_sheetset_strips_blank_fields(self):
        # Fields can be missing entirely. They default to empty strings.
        data = {
            "ok": True, "name": "x",
            "sheets": [{"number": "A-1"}],  # no title, dwg, etc.
        }
        ss = _build_sheetset(Path("C:/x/y.dst"), data)
        assert ss.sheets[0].number == "A-1"
        assert ss.sheets[0].title == ""
        assert ss.sheets[0].dwg_file == ""


class TestPrerequisiteErrors:
    def test_raises_when_dst_missing(self, tmp_path):
        missing = tmp_path / "nope.dst"
        with pytest.raises(FileNotFoundError, match="not found"):
            read_sheetset_via_com(missing)

    def test_raises_when_dll_missing(self, tmp_path):
        # Fixture .dst exists; specify a non-existent DLL.
        dst = _FIXTURES / "IRD_Addition.dst"
        if not dst.exists():
            pytest.skip("IRD_Addition.dst fixture not in tree")
        with pytest.raises(FileNotFoundError, match="SheetSetReader.dll"):
            read_sheetset_via_com(dst, dll_path=tmp_path / "missing.dll")


# ---------------------------------------------------------------------------
# Live AutoCAD integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLive:
    """Requires `pytest -m integration` and a running AutoCAD with the
    SheetSetReader DLL built AND at least one open document. Run via:

        dotnet build -c Release dotnet/SheetSetReader/SheetSetReader.csproj
        # In AutoCAD: open or create a drawing (Start tab is not enough).
        pytest -m integration tests/test_sheetset_com_reader.py

    The "open document" prereq is a hard requirement of AutoCAD's
    `SendCommand` API — see the skip-with-explanation in the test if
    AutoCAD is on the Start tab.
    """

    def test_reads_real_binary_dst(self):
        dst = _FIXTURES / "IRD_Addition.dst"
        if not dst.exists():
            pytest.skip("IRD_Addition.dst fixture not in tree")
        if not default_dll_path().exists():
            pytest.skip(
                "SheetSetReader.dll not built. Run: "
                "dotnet build -c Release "
                "dotnet/SheetSetReader/SheetSetReader.csproj"
            )

        # Guard against AutoCAD-on-Start-tab: read_sheetset_via_com will
        # raise "AutoCAD has no open document" which is correct behavior
        # but unhelpful as a test failure. Skip explicitly so the user
        # knows what to do.
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            app = win32com.client.GetActiveObject("AutoCAD.Application")
            if app.Documents.Count == 0:
                pytest.skip(
                    "AutoCAD has no open document. Open or create a "
                    "drawing (the Start tab doesn't host SendCommand)."
                )
        except Exception as ex:
            pytest.skip(f"AutoCAD COM activation failed: {ex}")

        ss = read_sheetset_via_com(dst)
        # The IRD_Addition.dst sample ships 11 sheets — fixed by AutoCAD's
        # template, not something we can alter from outside.
        assert len(ss.sheets) >= 1
        # First sheet's title comes from the .dst metadata.
        s = ss.sheets[0]
        assert s.number  # not empty
        assert s.title   # not empty
        # ResolveFileName in the .NET reader returns absolute paths.
        # Empty `dwg_file` is acceptable for sheets whose layout
        # reference can't be resolved (no underlying .dwg on disk).
