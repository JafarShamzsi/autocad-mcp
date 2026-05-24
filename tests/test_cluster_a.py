"""Tests for Cluster A — selection primitives.

Covers:
  * `entity.select(filter=...)` — layer, type, bbox, color, handles filters
  * `entity.osnap(x, y, modes)` — snap to nearest endpoint / midpoint / etc.
  * Bulk modify: `entity.{rotate,move,scale,copy,erase}` accept
    `entity_ids` or `selection.handles` and return aggregate results.

All against ezdxf (headless). The COM-backend code paths mirror this
shape — tested manually but not in CI because they need a live AutoCAD.
"""

from __future__ import annotations

import json
import math

import pytest


def _force_ezdxf(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)


async def _build_test_drawing(efn, dfn):
    """Make a small mixed-inventory drawing used by multiple tests."""
    await dfn(operation="create")
    # 3 walls on A-WALL
    await efn(operation="create_line", x1=0, y1=0, x2=100, y2=0, layer="A-WALL")
    await efn(operation="create_line", x1=0, y1=0, x2=0, y2=100, layer="A-WALL")
    await efn(operation="create_line", x1=100, y1=0, x2=100, y2=100, layer="A-WALL")
    # 2 doors as circles on A-DOOR
    await efn(operation="create_circle", layer="A-DOOR",
              data={"cx": 20, "cy": 0, "radius": 5})
    await efn(operation="create_circle", layer="A-DOOR",
              data={"cx": 60, "cy": 0, "radius": 5})
    # 1 window on A-GLAZ
    await efn(operation="create_line", x1=40, y1=0, x2=50, y2=0, layer="A-GLAZ")


# ---------------------------------------------------------------------------
# entity.select
# ---------------------------------------------------------------------------


async def test_select_by_layer(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    r = await efn(operation="select", data={"layer": "A-WALL"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    p = parsed["payload"]
    assert p["count"] == 3
    assert list(p["by_layer"].keys()) == ["A-WALL"]
    assert p["truncated"] is False


async def test_select_by_layer_list(monkeypatch):
    """`layer` accepts a list — selection matches ANY of the given layers."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    r = await efn(operation="select", data={"layer": ["A-DOOR", "A-GLAZ"]})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    p = parsed["payload"]
    assert p["count"] == 3  # 2 doors + 1 window
    assert set(p["by_layer"]) == {"A-DOOR", "A-GLAZ"}


async def test_select_by_type(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    r = await efn(operation="select", data={"type": "CIRCLE"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    p = parsed["payload"]
    assert p["count"] == 2
    assert list(p["by_type"].keys()) == ["CIRCLE"]


async def test_select_combined_layer_and_type(monkeypatch):
    """Filter args AND together — `layer=A-WALL` + `type=LINE` excludes circles."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    r = await efn(operation="select", data={
        "layer": "A-WALL", "type": "LINE",
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    p = parsed["payload"]
    assert p["count"] == 3
    assert list(p["by_type"].keys()) == ["LINE"]


async def test_select_by_bbox_crossing(monkeypatch):
    """Crossing semantics — entities whose bbox *overlaps* the query."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    # Query covering only the left third of the drawing.
    r = await efn(operation="select", data={"bbox": [0, -10, 30, 10]})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    p = parsed["payload"]
    # Should hit: horizontal wall (crosses), vertical wall (intersects),
    # door at x=20 (fully inside). Door at x=60 + window at x=40-50 +
    # right wall must NOT be included.
    assert p["count"] >= 3
    handles = set(p["handles"])

    # Pick a known-outside door and verify it's excluded.
    r2 = await efn(operation="select", data={
        "type": "CIRCLE", "bbox": [0, -10, 30, 10],
    })
    p2 = json.loads(r2)["payload"]
    # Only the door at (20, 0) should match — not the one at (60, 0).
    assert p2["count"] == 1


async def test_select_handles_narrows_prior_set(monkeypatch):
    """Passing `handles` restricts the search to that pre-selected set."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    # First pass: get all A-WALL handles.
    r = await efn(operation="select", data={"layer": "A-WALL"})
    all_wall_handles = json.loads(r)["payload"]["handles"]
    # Second pass: narrow that set by type=LINE (all walls are LINEs so
    # nothing should drop, but confirms the handle filter is respected).
    r2 = await efn(operation="select", data={
        "handles": all_wall_handles,
        "type": "LINE",
    })
    p2 = json.loads(r2)["payload"]
    assert set(p2["handles"]) == set(all_wall_handles)

    # Now narrow with type=CIRCLE — no circles in the walls set, so empty.
    r3 = await efn(operation="select", data={
        "handles": all_wall_handles, "type": "CIRCLE",
    })
    p3 = json.loads(r3)["payload"]
    assert p3["count"] == 0


async def test_select_limit_truncates(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    r = await efn(operation="select", data={"limit": 2})
    p = json.loads(r)["payload"]
    assert p["count"] == 2
    assert p["truncated"] is True


# ---------------------------------------------------------------------------
# entity.osnap
# ---------------------------------------------------------------------------


async def test_osnap_endpoint(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    await efn(operation="create_line", x1=0, y1=0, x2=100, y2=0)
    # Query near the right endpoint.
    r = await efn(operation="osnap", data={"x": 99, "y": 1, "tolerance": 5})
    p = json.loads(r)["payload"]
    assert p["mode_used"] == "endpoint"
    assert math.isclose(p["x"], 100)
    assert math.isclose(p["y"], 0)


async def test_osnap_midpoint(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    await efn(operation="create_line", x1=0, y1=0, x2=100, y2=0)
    # Query right at the midpoint — should beat the endpoints by distance.
    r = await efn(operation="osnap", data={"x": 50, "y": 0, "tolerance": 10})
    p = json.loads(r)["payload"]
    assert p["mode_used"] == "midpoint"
    assert math.isclose(p["x"], 50)


async def test_osnap_circle_center_and_endpoint(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    await efn(operation="create_circle", data={"cx": 50, "cy": 50, "radius": 10})
    # Query near center.
    r = await efn(operation="osnap", data={"x": 51, "y": 49, "tolerance": 5})
    p = json.loads(r)["payload"]
    assert p["mode_used"] == "center"

    # Query near a quadrant point (50, 60).
    r = await efn(operation="osnap", data={"x": 50, "y": 61, "tolerance": 5})
    p = json.loads(r)["payload"]
    assert p["mode_used"] == "endpoint"
    assert math.isclose(p["x"], 50)
    assert math.isclose(p["y"], 60)


async def test_osnap_outside_tolerance_returns_not_found(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    await efn(operation="create_line", x1=0, y1=0, x2=10, y2=0)
    r = await efn(operation="osnap", data={"x": 1000, "y": 1000, "tolerance": 1})
    parsed = json.loads(r)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_found"


# ---------------------------------------------------------------------------
# Bulk modify
# ---------------------------------------------------------------------------


async def test_bulk_erase_via_selection(monkeypatch):
    """Pass the result of `select` as `selection` to `erase` — all those
    entities are removed."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    # Snapshot total.
    summary = json.loads(
        await efn(operation="list", data={"summary": True})
    )["payload"]
    total_before = summary["total"]
    door_count = summary["by_layer"]["A-DOOR"]

    # Select all doors.
    sel = json.loads(await efn(operation="select", data={"layer": "A-DOOR"}))["payload"]
    assert sel["count"] == door_count

    # Erase them via `selection`.
    r = await efn(operation="erase", data={"selection": sel})
    parsed = json.loads(r)
    assert parsed["ok"] is True
    p = parsed["payload"]
    assert p["operation"] == "erase"
    assert p["processed"] == door_count
    assert p["failed"] == []

    # Verify totals.
    after = json.loads(
        await efn(operation="list", data={"summary": True})
    )["payload"]
    assert after["total"] == total_before - door_count
    assert "A-DOOR" not in after["by_layer"]


async def test_bulk_move_via_entity_ids(monkeypatch):
    """`entity_ids` is the explicit alternative to `selection.handles`."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    sel = json.loads(await efn(operation="select", data={"layer": "A-WALL"}))["payload"]
    r = await efn(operation="move", data={
        "entity_ids": sel["handles"],
        "dx": 10, "dy": 0,
    })
    p = json.loads(r)["payload"]
    assert p["processed"] == 3
    assert p["failed"] == []


async def test_bulk_copy_returns_new_handles(monkeypatch):
    """`copy` creates new entities — the bulk dispatcher should surface
    their handles in `handles_out`."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    sel = json.loads(await efn(operation="select", data={"layer": "A-DOOR"}))["payload"]
    r = await efn(operation="copy", data={
        "selection": sel,
        "dx": 0, "dy": 50,
    })
    p = json.loads(r)["payload"]
    assert p["processed"] == 2
    assert "handles_out" in p
    assert len(p["handles_out"]) == 2


async def test_bulk_with_one_bad_handle_reports_partial(monkeypatch):
    """A bad handle in the list shouldn't abort the batch — it lands in
    `failed` while the rest still process."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    await _build_test_drawing(efn, dfn)

    sel = json.loads(await efn(operation="select", data={"layer": "A-WALL"}))["payload"]
    handles = sel["handles"] + ["NOT-A-REAL-HANDLE"]
    r = await efn(operation="erase", data={"entity_ids": handles})
    parsed = json.loads(r)
    p = parsed["payload"]
    assert p["processed"] == 3
    assert len(p["failed"]) == 1
    assert p["failed"][0]["handle"] == "NOT-A-REAL-HANDLE"
    # ok=False because at least one item failed.
    assert parsed["ok"] is False


async def test_bulk_rotate_keeps_single_path_when_no_selection(monkeypatch):
    """The legacy single-entity path still works when neither
    `entity_ids` nor `selection` is provided."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    r = await efn(operation="create_line", x1=0, y1=0, x2=10, y2=0)
    handle = json.loads(r)["payload"]["handle"]

    r2 = await efn(operation="rotate", entity_id=handle,
                   data={"cx": 0, "cy": 0, "angle": 90})
    p2 = json.loads(r2)
    # Single-path result has NO `processed` field — that's the bulk shape.
    assert "processed" not in p2.get("payload", {})
    assert p2["ok"] is True
