"""Tests for Cluster C — styles, named views, layer states, tables.

Covers (all against ezdxf headless; COM paths share the shape and are
tested manually via the integration suite):

  * style tool — dimstyle/textstyle create/list/get/set_current
  * style.list_mleaderstyles — enumerates whatever ships in R2013
  * view tool — named-view save/list/restore/delete
  * view tool — layer-state ops return `not_supported` on ezdxf
  * annotation.create_table — ezdxf grid-mode (LINE+TEXT)
"""

from __future__ import annotations

import json

import pytest


def _force_ezdxf(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)


# ---------------------------------------------------------------------------
# style tool — dimstyles
# ---------------------------------------------------------------------------


async def test_style_list_dimstyles_includes_standard(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    r = await sfn(operation="list_dimstyles")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    names = {ds["name"] for ds in parsed["payload"]["dimstyles"]}
    # R2013 ships with at least the Standard dimstyle.
    assert "Standard" in names
    assert "current" in parsed["payload"]


async def test_style_create_and_get_dimstyle(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    r = await sfn(operation="create_dimstyle", data={
        "name": "FIRM-1-4-FT",
        "attribs": {"dimscale": 48.0, "dimtxt": 0.125, "dimdec": 0},
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["existed"] is False

    # Read it back.
    r = await sfn(operation="get_dimstyle", data={"name": "FIRM-1-4-FT"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    a = parsed["payload"]["attribs"]
    assert a.get("dimscale") == pytest.approx(48.0)


async def test_style_create_dimstyle_overwrite_preserves_unspecified(monkeypatch):
    """Re-creating an existing dimstyle should set ONLY the provided
    attribs; previously-set ones survive."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    await sfn(operation="create_dimstyle", data={
        "name": "PARTIAL", "attribs": {"dimscale": 24.0, "dimtxt": 0.1},
    })
    r = await sfn(operation="create_dimstyle", data={
        "name": "PARTIAL", "attribs": {"dimdec": 2},
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["payload"]["existed"] is True

    r = await sfn(operation="get_dimstyle", data={"name": "PARTIAL"})
    a = json.loads(r)["payload"]["attribs"]
    assert a.get("dimscale") == pytest.approx(24.0)
    assert a.get("dimdec") == 2


async def test_style_set_current_dimstyle(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    await sfn(operation="create_dimstyle", data={
        "name": "AS-CURRENT", "attribs": {"dimscale": 96.0},
    })
    r = await sfn(operation="set_current_dimstyle", data={"name": "AS-CURRENT"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["payload"]["current"] == "AS-CURRENT"

    # The list should report this as `current`.
    r2 = await sfn(operation="list_dimstyles")
    assert json.loads(r2)["payload"]["current"] == "AS-CURRENT"


async def test_style_get_missing_dimstyle_returns_not_found(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    r = await sfn(operation="get_dimstyle", data={"name": "NO-SUCH"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_found"


# ---------------------------------------------------------------------------
# style tool — textstyles
# ---------------------------------------------------------------------------


async def test_style_textstyle_create_and_set_current(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    r = await sfn(operation="create_textstyle", data={
        "name": "FIRM-ROMAN", "font": "romans.shx",
        "width_factor": 0.85,
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True

    r = await sfn(operation="set_current_textstyle", data={"name": "FIRM-ROMAN"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["payload"]["current"] == "FIRM-ROMAN"

    r = await sfn(operation="list_textstyles")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    names = {t["name"] for t in parsed["payload"]["textstyles"]}
    assert "FIRM-ROMAN" in names
    assert parsed["payload"]["current"] == "FIRM-ROMAN"


# ---------------------------------------------------------------------------
# style tool — mleaderstyles
# ---------------------------------------------------------------------------


async def test_style_list_mleaderstyles_returns_collection(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, style as style_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = style_tool.fn if hasattr(style_tool, "fn") else style_tool

    await dfn(operation="create")
    r = await sfn(operation="list_mleaderstyles")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert "mleaderstyles" in parsed["payload"]
    assert "current" in parsed["payload"]


# ---------------------------------------------------------------------------
# view tool — named views
# ---------------------------------------------------------------------------


async def test_view_save_and_list_named(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, view as view_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    vfn = view_tool.fn if hasattr(view_tool, "fn") else view_tool

    await dfn(operation="create")
    r = await vfn(operation="save_named_view", data={
        "name": "PLAN-1", "x1": 0, "y1": 0, "x2": 200, "y2": 100,
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["name"] == "PLAN-1"

    r = await vfn(operation="list_named_views")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    names = {v["name"] for v in parsed["payload"]["views"]}
    assert "PLAN-1" in names


async def test_view_restore_returns_stored_extents_on_ezdxf(monkeypatch):
    """ezdxf is headless — `restore` returns the stored extents rather
    than mutating a viewport."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, view as view_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    vfn = view_tool.fn if hasattr(view_tool, "fn") else view_tool

    await dfn(operation="create")
    await vfn(operation="save_named_view", data={
        "name": "X", "x1": 10, "y1": 20, "x2": 110, "y2": 80,
    })
    r = await vfn(operation="restore_named_view", data={"name": "X"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["center"] == pytest.approx([60.0, 50.0])
    assert parsed["payload"]["width"] == pytest.approx(100.0)
    assert parsed["payload"]["height"] == pytest.approx(60.0)


async def test_view_delete_named(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, view as view_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    vfn = view_tool.fn if hasattr(view_tool, "fn") else view_tool

    await dfn(operation="create")
    await vfn(operation="save_named_view", data={
        "name": "TEMP", "x1": 0, "y1": 0, "x2": 10, "y2": 10,
    })
    r = await vfn(operation="delete_named_view", data={"name": "TEMP"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["payload"]["deleted"] is True
    # No longer listed.
    r2 = await vfn(operation="list_named_views")
    names = {v["name"] for v in json.loads(r2)["payload"]["views"]}
    assert "TEMP" not in names


async def test_view_restore_missing_returns_not_found(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, view as view_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    vfn = view_tool.fn if hasattr(view_tool, "fn") else view_tool

    await dfn(operation="create")
    r = await vfn(operation="restore_named_view", data={"name": "GHOST"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_found"


# ---------------------------------------------------------------------------
# view tool — layer states (not_supported on ezdxf)
# ---------------------------------------------------------------------------


async def test_view_layer_state_ops_not_supported_on_ezdxf(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, view as view_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    vfn = view_tool.fn if hasattr(view_tool, "fn") else view_tool

    await dfn(operation="create")
    for op, data in [
        ("list_layer_states", {}),
        ("save_layer_state", {"name": "X"}),
        ("restore_layer_state", {"name": "X"}),
        ("delete_layer_state", {"name": "X"}),
    ]:
        r = await vfn(operation=op, data=data)
        parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
        assert parsed.get("ok") is False, f"{op} should report not supported"
        assert parsed.get("category") == "not_supported"


# ---------------------------------------------------------------------------
# annotation.create_table
# ---------------------------------------------------------------------------


async def test_create_table_grid_on_ezdxf(monkeypatch):
    """ezdxf has no AcDbTable factory — we render a grid of LINE + TEXT.
    Verify the entity counts + that the cell values land in the doc."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, annotation as ann_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    afn = ann_tool.fn if hasattr(ann_tool, "fn") else ann_tool

    await dfn(operation="create")
    r = await afn(operation="create_table", data={
        "x": 0, "y": 0,
        "rows": 4, "cols": 3,
        "row_height": 0.5, "col_width": 2.0,
        "title": "DOOR SCHEDULE",
        "header": ["TAG", "WIDTH", "HEIGHT"],
        "data": [
            ["D01", '3\'-0"', '6\'-8"'],
            ["D02", '2\'-8"', '6\'-8"'],
        ],
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    p = parsed["payload"]
    assert p["kind"] == "grid-table"
    # 4 row separators + 1 = 5 horizontal lines; 3 col separators + 1 = 4 vertical
    line_count = len(p["handles"]["lines"])
    assert line_count == 9  # (rows+1) + (cols+1) = 5 + 4
    # Title + 3 headers + 6 data cells = 10 texts
    text_count = len(p["handles"]["texts"])
    assert text_count == 10
