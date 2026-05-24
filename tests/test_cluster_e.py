"""Tests for Cluster E — 3D primitives + Express Tools wrappers.

Headless ezdxf tests cover:
  * `entity.extrude` produces a polyface MESH from a closed LWPOLYLINE
  * `entity.revolve` / `entity.region` / `entity.boolean` → not_supported
  * Express Tools: `layiso` works headless (`.off()` emulation); the rest
    return `not_supported` since they need real AutoCAD.

COM-backed paths share the shape and are tested via the integration suite
(needs a running AutoCAD).
"""

from __future__ import annotations

import json

import pytest


def _force_ezdxf(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)


# ---------------------------------------------------------------------------
# 3D — extrude (ezdxf polyface mesh)
# ---------------------------------------------------------------------------


async def test_entity_extrude_closed_polyline_builds_mesh(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    # 10x10 closed square.
    r = await efn(operation="create_polyline", points=[
        [0, 0], [10, 0], [10, 10], [0, 10],
    ], data={"closed": True})
    handle = json.loads(r)["payload"]["handle"]

    r = await efn(operation="extrude", entity_id=handle, data={"height": 9.0})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    p = parsed["payload"]
    assert p["entity_type"] == "POLYFACE"
    assert p["height"] == pytest.approx(9.0)
    # 4-vertex profile → 4 side quads + top + bottom = 6 faces total.
    assert p["vertex_count"] == 4


async def test_entity_extrude_unsupported_curve_returns_not_supported(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    # Circle — ezdxf extrude needs LWPOLYLINE/POLYLINE.
    r = await efn(operation="create_circle", data={"cx": 0, "cy": 0, "radius": 5})
    handle = json.loads(r)["payload"]["handle"]
    r = await efn(operation="extrude", entity_id=handle, data={"height": 3.0})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_supported"


async def test_entity_extrude_missing_entity_returns_not_found(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    r = await efn(operation="extrude", entity_id="NOTAHANDLE",
                  data={"height": 1.0})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_found"


# ---------------------------------------------------------------------------
# 3D — revolve / region / boolean (not_supported on ezdxf)
# ---------------------------------------------------------------------------


async def test_revolve_not_supported_on_ezdxf(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    r = await efn(operation="create_polyline", points=[
        [0, 0], [10, 0], [10, 10], [0, 10],
    ], data={"closed": True})
    handle = json.loads(r)["payload"]["handle"]
    r = await efn(operation="revolve", entity_id=handle, data={
        "axis_x": 0, "axis_y": 0, "axis_dx": 0, "axis_dy": 1, "angle": 360,
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_supported"


async def test_region_not_supported_on_ezdxf(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    r = await efn(operation="region", data={"entity_ids": ["AA", "BB"]})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_supported"


async def test_boolean_not_supported_on_ezdxf(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    r = await efn(operation="boolean", data={
        "operation": "union", "target_id": "AA", "tool_ids": ["BB"],
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_supported"


async def test_region_requires_entity_ids(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, entity as entity_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool

    await dfn(operation="create")
    r = await efn(operation="region", data={})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert "entity_ids" in parsed["error"]


# ---------------------------------------------------------------------------
# Express Tools
# ---------------------------------------------------------------------------


async def test_express_layiso_emulated_on_ezdxf(monkeypatch):
    """ezdxf path: isolate by calling Layer.off() on every layer not in
    the keep set. Verify the listed layers are still on, others are off."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, layer as layer_tool, express as express_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    lfn = layer_tool.fn if hasattr(layer_tool, "fn") else layer_tool
    xfn = express_tool.fn if hasattr(express_tool, "fn") else express_tool

    await dfn(operation="create")
    # Three layers.
    for name in ("A-WALL", "A-DOOR", "A-GLAZ"):
        await lfn(operation="create", data={"name": name})

    r = await xfn(operation="layiso", data={"layers": ["A-WALL"]})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["isolated_layers"] == ["A-WALL"]

    # Verify layer states.
    from autocad_mcp.client import get_backend
    backend = await get_backend()
    doc = backend._doc
    assert doc.layers.get("A-WALL").is_off() is False
    assert doc.layers.get("A-DOOR").is_off() is True
    assert doc.layers.get("A-GLAZ").is_off() is True


async def test_express_layiso_empty_list_returns_invalid_arg(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, express as express_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    xfn = express_tool.fn if hasattr(express_tool, "fn") else express_tool

    await dfn(operation="create")
    r = await xfn(operation="layiso", data={"layers": []})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "invalid_arg"


@pytest.mark.parametrize("op,data", [
    ("burst", {"entity_ids": ["1A"]}),
    ("flatten", {}),
    ("overkill", {}),
    ("txt2mtxt", {"entity_ids": ["1A"]}),
])
async def test_express_op_not_supported_on_ezdxf(monkeypatch, op, data):
    """All express tools except `layiso` are full-AutoCAD-only."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, express as express_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    xfn = express_tool.fn if hasattr(express_tool, "fn") else express_tool

    await dfn(operation="create")
    r = await xfn(operation=op, data=data)
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed.get("ok") is False
    assert parsed.get("category") == "not_supported"


async def test_express_unknown_op_returns_error(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, express as express_tool,
    )
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    xfn = express_tool.fn if hasattr(express_tool, "fn") else express_tool

    await dfn(operation="create")
    r = await xfn(operation="bogus")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert "bogus" in parsed["error"]
