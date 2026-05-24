"""Tests for sheet-set (.dst) reading + indexing + MCP dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocad_mcp.project import (
    DXFIndexer,
    SheetSetBinaryUnsupported,
    find_sheets_for_drawing,
    list_sheets,
    load_project,
    read_sheetset,
)


_FIXTURES = Path(__file__).resolve().parent / "fixtures"


# A synthetic .dst that mimics what AutoCAD writes. Two sheets pointing at
# real DWGs, one sheet pointing at a missing DWG (to exercise is_dwg_present),
# plus container elements (Sheets, AcSmSheetSet) that the reader must NOT
# misclassify as sheets.
_SAMPLE_DST = """<?xml version="1.0" encoding="utf-8"?>
<AcSmSheetSet Class="AcSmSheetSet">
  <Name>Example House</Name>
  <DefDwgName>./fp1.dwg</DefDwgName>
  <Sheets>
    <AcSmSheet>
      <Number>A-101</Number>
      <Title>FLOOR PLAN 1</Title>
      <FileName>./fp1.dwg</FileName>
      <SheetLayoutName>Layout1</SheetLayoutName>
    </AcSmSheet>
    <AcSmSheet>
      <Number>A-201</Number>
      <Title>ELEVATIONS</Title>
      <Desc>North + South elevations</Desc>
      <FileName>./elev.dwg</FileName>
      <SheetLayoutName>Layout1</SheetLayoutName>
    </AcSmSheet>
    <AcSmSheet>
      <Number>A-999</Number>
      <Title>MISSING SHEET</Title>
      <FileName>./does-not-exist.dwg</FileName>
      <SheetLayoutName>Layout1</SheetLayoutName>
    </AcSmSheet>
  </Sheets>
</AcSmSheetSet>
"""


# ---------------------------------------------------------------------------
# Fixture: project with a .dst + matching DWG placeholders
# ---------------------------------------------------------------------------


@pytest.fixture
def sheetset_project(tmp_path):
    """A project whose .acadproj.toml references a .dst that points at two
    real (placeholder) DWGs and one missing DWG."""
    import ezdxf

    # Real placeholder DWGs (just empty DXFs saved with a .dwg extension —
    # the indexer is allowed to skip them. We don't list them in [[drawings]]
    # because the sheet set tracking is independent of the drawing index.).
    # For is_dwg_present, we only need the files to exist on disk.
    (tmp_path / "fp1.dwg").write_text("")
    (tmp_path / "elev.dwg").write_text("")
    # Note: does-not-exist.dwg is intentionally missing.

    (tmp_path / "sheets.dst").write_text(_SAMPLE_DST)

    # One real DXF so the indexer has at least one drawing to process; the
    # sheet set indexing is independent of [[drawings]] entries.
    doc = ezdxf.new("R2013", setup=True)
    doc.modelspace().add_line((0, 0), (10, 0))
    doc.saveas(str(tmp_path / "host.dxf"))

    (tmp_path / ".acadproj.toml").write_text(
        '[project]\nname = "Sheetset Test"\n\n'
        '[sheetset]\nfile = "sheets.dst"\n\n'
        '[[drawings]]\nname = "host"\nfile = "host.dxf"\n'
    )
    cfg = load_project(tmp_path)
    idx = DXFIndexer(tmp_path / ".acadproj.db")
    idx.index_project(cfg)
    yield tmp_path, cfg, idx.conn
    idx.close()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class TestSheetSetReader:
    def test_reads_sheet_count(self, tmp_path):
        p = tmp_path / "x.dst"
        p.write_text(_SAMPLE_DST)
        ss = read_sheetset(p)
        assert ss.name == "Example House"
        assert len(ss.sheets) == 3

    def test_extracts_number_title_dwg_layout(self, tmp_path):
        p = tmp_path / "x.dst"
        p.write_text(_SAMPLE_DST)
        ss = read_sheetset(p)
        first = ss.sheets[0]
        assert first.number == "A-101"
        assert first.title == "FLOOR PLAN 1"
        assert first.dwg_file == "./fp1.dwg"
        assert first.layout == "Layout1"

    def test_extracts_description_when_present(self, tmp_path):
        p = tmp_path / "x.dst"
        p.write_text(_SAMPLE_DST)
        ss = read_sheetset(p)
        elev = next(s for s in ss.sheets if s.number == "A-201")
        assert "elevations" in elev.description.lower()

    def test_container_elements_not_misclassified(self, tmp_path):
        """The <Sheets> and <AcSmSheetSet> containers must NOT appear as
        sheets even though their tags contain 'sheet'."""
        p = tmp_path / "x.dst"
        p.write_text(_SAMPLE_DST)
        ss = read_sheetset(p)
        # Exactly the three AcSmSheet elements, nothing else.
        assert len(ss.sheets) == 3
        assert all(s.dwg_file for s in ss.sheets)  # every emitted sheet has a DWG

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_sheetset(tmp_path / "no-such.dst")

    def test_nested_sheets_do_not_bleed_fields(self, tmp_path):
        """When an AcSmSheet contains a nested AcSmSheet, the outer's field
        extraction must not pull values from the inner one."""
        nested = """<?xml version="1.0"?>
<AcSmSheetSet>
  <Name>Nested</Name>
  <Sheets>
    <AcSmSheet>
      <Number>OUTER-A</Number>
      <Title>OUTER TITLE</Title>
      <FileName>./outer.dwg</FileName>
      <Sheets>
        <AcSmSheet>
          <Number>INNER-X</Number>
          <Title>INNER TITLE</Title>
          <FileName>./inner.dwg</FileName>
        </AcSmSheet>
      </Sheets>
    </AcSmSheet>
  </Sheets>
</AcSmSheetSet>
"""
        p = tmp_path / "nested.dst"
        p.write_text(nested)
        ss = read_sheetset(p)
        # Both sheets discovered independently...
        assert len(ss.sheets) == 2
        outer = next(s for s in ss.sheets if s.dwg_file == "./outer.dwg")
        inner = next(s for s in ss.sheets if s.dwg_file == "./inner.dwg")
        # ...and each carries its own fields (no bleed across the boundary).
        assert outer.number == "OUTER-A"
        assert outer.title == "OUTER TITLE"
        assert inner.number == "INNER-X"
        assert inner.title == "INNER TITLE"

    def test_real_autocad_dst_raises_binary_unsupported(self):
        """Regression for G6: the .dst files AutoCAD 2024+ ships are
        proprietary binary, NOT the XML format we assumed. Without this
        check the user got a confusing
        `not well-formed (invalid token): line 1, column 0` parse error.
        Now we detect the binary header and raise something actionable."""
        fixture = _FIXTURES / "IRD_Addition.dst"
        if not fixture.exists():
            pytest.skip(
                f"IRD_Addition.dst fixture missing — copy from AutoCAD 2027 "
                "Sample/Sheet Sets/Architectural/ to tests/fixtures/."
            )
        # Sanity-check the binary header is intact (so our test isn't a
        # false-pass against a tampered fixture).
        with fixture.open("rb") as fp:
            head = fp.read(4)
        assert head == bytes.fromhex("d0 cd 14 e3"), (
            "Fixture doesn't have the expected binary .dst header"
        )
        with pytest.raises(SheetSetBinaryUnsupported) as exc:
            read_sheetset(fixture)
        # Message must point users at the workarounds.
        msg = str(exc.value)
        assert "binary .dst format" in msg
        assert "AcSmSheetSetMgr" in msg

    def test_handles_namespaced_xml(self, tmp_path):
        """Some AutoCAD versions write namespaced XML. The reader should
        strip the namespace prefix when comparing local names."""
        ns_dst = """<?xml version="1.0" encoding="utf-8"?>
<acsm:AcSmSheetSet xmlns:acsm="http://example.com/ns">
  <acsm:Name>NS Project</acsm:Name>
  <acsm:Sheets>
    <acsm:AcSmSheet>
      <acsm:Number>1</acsm:Number>
      <acsm:Title>NS Sheet</acsm:Title>
      <acsm:FileName>./ns.dwg</acsm:FileName>
    </acsm:AcSmSheet>
  </acsm:Sheets>
</acsm:AcSmSheetSet>
"""
        p = tmp_path / "ns.dst"
        p.write_text(ns_dst)
        ss = read_sheetset(p)
        assert ss.name == "NS Project"
        assert len(ss.sheets) == 1
        assert ss.sheets[0].number == "1"


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class TestSheetSetIndexing:
    def test_sheets_table_populated(self, sheetset_project):
        _, _, conn = sheetset_project
        n = conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]
        assert n == 3

    def test_resolved_paths_are_absolute(self, sheetset_project):
        _, _, conn = sheetset_project
        rows = conn.execute(
            "SELECT resolved_dwg_path FROM sheets WHERE number = 'A-101'"
        ).fetchall()
        assert len(rows) == 1
        assert Path(rows[0]["resolved_dwg_path"]).is_absolute()

    def test_is_dwg_present_flag(self, sheetset_project):
        _, _, conn = sheetset_project
        rows = conn.execute(
            "SELECT number, is_dwg_present FROM sheets ORDER BY number"
        ).fetchall()
        flags = {r["number"]: bool(r["is_dwg_present"]) for r in rows}
        assert flags["A-101"] is True
        assert flags["A-201"] is True
        assert flags["A-999"] is False  # the missing one

    def test_reindex_is_idempotent(self, sheetset_project):
        tmp_path, cfg, conn = sheetset_project
        # Run another index pass; row count should stay the same.
        idx = DXFIndexer(tmp_path / ".acadproj.db")
        idx.index_project(cfg)
        n = idx.conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]
        assert n == 3
        idx.close()

    def test_missing_sheetset_logged_not_fatal(self, tmp_path):
        """A configured-but-missing .dst should not fail index_project."""
        import ezdxf

        doc = ezdxf.new("R2013", setup=True)
        doc.modelspace().add_line((0, 0), (10, 0))
        doc.saveas(str(tmp_path / "host.dxf"))

        (tmp_path / ".acadproj.toml").write_text(
            '[project]\nname = "X"\n\n'
            '[sheetset]\nfile = "nope.dst"\n\n'
            '[[drawings]]\nname = "host"\nfile = "host.dxf"\n'
        )
        cfg = load_project(tmp_path)
        idx = DXFIndexer(tmp_path / ".acadproj.db")
        try:
            result = idx.index_project(cfg)
            assert result.indexed == 1  # host.dxf indexed fine
            # Sheets table should be empty.
            assert idx.conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        finally:
            idx.close()

    def test_binary_dst_falls_back_to_com_reader(self, tmp_path, monkeypatch):
        """A binary .dst should hit the COM-backed reader if it can; on
        success, sheets land in the table just like the XML path."""
        import ezdxf
        from autocad_mcp.project import sheetset_com_reader
        from autocad_mcp.project.sheetset import SheetSet, Sheet

        # Minimal hostfile so the indexer has something else to do.
        doc = ezdxf.new("R2013", setup=True)
        doc.modelspace().add_line((0, 0), (10, 0))
        doc.saveas(str(tmp_path / "host.dxf"))

        # Create a binary .dst by writing the 4-byte header — the XML
        # reader's early-detection path triggers immediately, so the file
        # doesn't need to be a complete binary .dst.
        binary_dst = tmp_path / "sheets.dst"
        binary_dst.write_bytes(bytes.fromhex("d0 cd 14 e3") + b"\x00" * 16)

        (tmp_path / ".acadproj.toml").write_text(
            '[project]\nname = "BX"\n\n'
            '[sheetset]\nfile = "sheets.dst"\n\n'
            '[[drawings]]\nname = "host"\nfile = "host.dxf"\n'
        )
        cfg = load_project(tmp_path)

        # Inject a fake COM reader that returns a known sheet set.
        def fake_com_reader(p, **kwargs):
            return SheetSet(
                name="ViaCOM",
                path=p,
                sheets=[Sheet(number="X-1", title="From COM", dwg_file="x1.dwg")],
            )

        monkeypatch.setattr(
            sheetset_com_reader, "read_sheetset_via_com", fake_com_reader,
        )

        idx = DXFIndexer(tmp_path / ".acadproj.db")
        try:
            result = idx.index_project(cfg)
            # No errors — the binary file was handled by the COM fallback.
            sheetset_errors = [
                e for e in result.errors if "sheetset" in e
            ]
            assert sheetset_errors == [], (
                f"Expected COM fallback to handle binary .dst, got errors: {sheetset_errors}"
            )
            row = idx.conn.execute(
                "SELECT number, title, sheetset_name FROM sheets"
            ).fetchone()
            assert row["number"] == "X-1"
            assert row["title"] == "From COM"
            assert row["sheetset_name"] == "ViaCOM"
        finally:
            idx.close()

    def test_binary_dst_resurfaces_original_error_when_com_fails(
        self, tmp_path, monkeypatch,
    ):
        """If the COM fallback can't handle the binary .dst either,
        the user gets the original SheetSetBinaryUnsupported message —
        with its workarounds — rather than a confusing COM stack trace.
        """
        import ezdxf
        from autocad_mcp.project import sheetset_com_reader

        doc = ezdxf.new("R2013", setup=True)
        doc.modelspace().add_line((0, 0), (10, 0))
        doc.saveas(str(tmp_path / "host.dxf"))

        binary_dst = tmp_path / "sheets.dst"
        binary_dst.write_bytes(bytes.fromhex("d0 cd 14 e3") + b"\x00" * 16)

        (tmp_path / ".acadproj.toml").write_text(
            '[project]\nname = "BX"\n\n'
            '[sheetset]\nfile = "sheets.dst"\n\n'
            '[[drawings]]\nname = "host"\nfile = "host.dxf"\n'
        )
        cfg = load_project(tmp_path)

        def broken_com_reader(p, **kwargs):
            raise RuntimeError("AutoCAD not running")

        monkeypatch.setattr(
            sheetset_com_reader, "read_sheetset_via_com", broken_com_reader,
        )

        idx = DXFIndexer(tmp_path / ".acadproj.db")
        try:
            result = idx.index_project(cfg)
            # The original binary-unsupported error must surface; the
            # error string must mention the workaround language so users
            # know what to do.
            sheetset_errors = [e for e in result.errors if "sheetset" in e]
            assert len(sheetset_errors) == 1
            err = sheetset_errors[0]["error"]
            assert "binary .dst" in err, f"Unexpected error message: {err}"
        finally:
            idx.close()

    def test_reindex_parse_failure_preserves_prior_rows(self, sheetset_project):
        """Regression for N11: a malformed .dst on a re-index used to DELETE
        the sheets table BEFORE parsing, which left zero rows when parse
        raised. Fix: parse first, swap on success. Prior rows survive a
        failed re-index attempt."""
        tmp_path, cfg, conn = sheetset_project

        # Confirm we start with the 3-row index from the fixture.
        assert conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 3

        # Corrupt the .dst so read_sheetset raises a parse error.
        (tmp_path / "sheets.dst").write_text("<not-valid-xml>")

        # Re-index. The exception in _index_sheetset should be caught by
        # index_project and recorded as an error, but the sheets table must
        # still hold the previous 3 rows.
        idx = DXFIndexer(tmp_path / ".acadproj.db")
        result = idx.index_project(cfg)
        assert any("sheetset" in e for e in result.errors), result.errors
        n = idx.conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0]
        assert n == 3, (
            f"Sheets table was wiped on parse failure: expected 3 prior rows, got {n}"
        )
        idx.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestSheetSetQueries:
    def test_list_sheets(self, sheetset_project):
        _, _, conn = sheetset_project
        sheets = list_sheets(conn)
        assert len(sheets) == 3
        numbers = sorted(s["number"] for s in sheets)
        assert numbers == ["A-101", "A-201", "A-999"]

    def test_find_sheets_for_drawing_by_basename(self, sheetset_project):
        _, _, conn = sheetset_project
        rows = find_sheets_for_drawing(conn, "elev.dwg")
        assert len(rows) == 1
        assert rows[0]["number"] == "A-201"

    def test_find_sheets_for_drawing_by_full_path(self, sheetset_project):
        tmp_path, _, conn = sheetset_project
        full = str((tmp_path / "fp1.dwg").resolve())
        rows = find_sheets_for_drawing(conn, full)
        assert len(rows) == 1
        assert rows[0]["number"] == "A-101"

    def test_find_sheets_for_drawing_escapes_like_wildcards(self, tmp_path):
        """A drawing argument containing LIKE wildcards (_ or %) must match
        literally — not broadly. Regression for H3 in the 2026-05-11 audit."""
        import sqlite3

        from autocad_mcp.project import init_db

        db_path = tmp_path / "wildcard.db"
        conn = init_db(db_path)
        # Two rows: one literal match for "_DETAIL.dwg", one decoy that would
        # match the LIKE wildcard interpretation of `_`.
        conn.executemany(
            """INSERT INTO sheets
                 (sheetset_name, sheetset_path, number, title, description,
                  dwg_file, layout_name, resolved_dwg_path, is_dwg_present)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("X", "x.dst", "1", "literal", "", "./_DETAIL.dwg", "L1", "/a/_DETAIL.dwg", 1),
                ("X", "x.dst", "2", "decoy",   "", "./ABDETAIL.dwg", "L2", "/a/ABDETAIL.dwg", 1),
            ],
        )
        conn.commit()
        rows = find_sheets_for_drawing(conn, "_DETAIL.dwg")
        # Should match only the literal _DETAIL.dwg, not the decoy.
        assert len(rows) == 1
        assert rows[0]["number"] == "1"


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestProjectToolSheetSet:
    async def test_list_sheets_via_tool(self, sheetset_project, monkeypatch):
        tmp_path, _, _ = sheetset_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")

        result = await fn(operation="list_sheets")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["count"] == 3
        assert parsed["payload"]["missing_dwg_count"] == 1

    async def test_find_sheets_for_drawing_via_tool(self, sheetset_project, monkeypatch):
        tmp_path, _, _ = sheetset_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")

        result = await fn(operation="find_sheets_for_drawing", data={"drawing": "fp1.dwg"})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["payload"]["count"] == 1
        assert parsed["payload"]["sheets"][0]["number"] == "A-101"

    async def test_find_sheets_for_drawing_missing_arg(self, sheetset_project, monkeypatch):
        tmp_path, _, _ = sheetset_project
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(
            server_mod, "_project_state",
            {"config": None, "conn": None, "db_path": None},
        )
        fn = server_mod.project.fn if hasattr(server_mod.project, "fn") else server_mod.project

        await fn(operation="load", data={"path": str(tmp_path)})
        await fn(operation="index")
        result = await fn(operation="find_sheets_for_drawing")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed
        assert "drawing" in parsed["error"]
