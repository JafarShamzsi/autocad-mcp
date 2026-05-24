"""Tests for Cluster D — workflow polish.

Covers:
  * `system.tool_help` — index + single-tool detail
  * `entity.list(summary=true)` — token-efficient by_layer/by_type breakdown
  * `drawing.audit` / `drawing.recover` — graceful "not supported" on ezdxf
  * Drawing memory sidecar — `read_sidecar`, surfaced in `drawing.open`, +
    the `project.sidecar` op
  * Turn API — `system.turn_begin` / `turn_end`, entity-diff payload shape
  * Auto-screenshot cache — invalidation on entity ops, hit on read ops
  * Backend liveness probe — `_backend_is_alive` returns True on ezdxf

These are all pure-Python (no live AutoCAD); the COM-only branches are
guarded with a clear "not_supported" return.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autocad_mcp.backends.base import CommandResult


def _force_ezdxf(monkeypatch):
    """Pin the backend singleton to ezdxf so tests don't try to attach
    to AutoCAD COM."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)


# ---------------------------------------------------------------------------
# system.tool_help
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# system.preflight
# ---------------------------------------------------------------------------


async def test_preflight_returns_full_inventory(monkeypatch):
    """One-shot diagnostic must surface every external dependency
    (backend, DWG converter, DLLs, LISP files, catalog, env vars)."""
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import system as system_tool
    fn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    r = await fn(operation="preflight")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    p = parsed["payload"]
    # Section structure — locked so docs + Claude-side code keep working.
    for section in ("backend", "dwg", "dotnet", "lisp", "catalog",
                    "env_vars", "warnings", "ready", "version"):
        assert section in p, f"preflight payload missing {section!r}"
    # Backend section identifies what auto-mode would pick.
    assert "env" in p["backend"]
    assert "auto_selects" in p["backend"]
    # `ready` reflects warnings being empty.
    assert p["ready"] == (not p["warnings"])


async def test_tool_help_index_lists_every_tool(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import system as system_tool
    fn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    r = await fn(operation="tool_help")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    names = {t["name"] for t in parsed["payload"]["tools"]}
    # Every tool we ship should appear.
    expected = {
        "drawing", "entity", "layer", "block", "annotation",
        "pid", "arch", "project", "library", "events", "view", "system",
    }
    assert expected <= names, f"missing tools in index: {expected - names}"
    # Each entry has the index shape.
    for t in parsed["payload"]["tools"]:
        assert "summary" in t and t["summary"]
        # Index entries DON'T carry the full description + schema.
        assert "description" not in t


async def test_tool_help_single_returns_full_detail(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import system as system_tool
    fn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    r = await fn(operation="tool_help", data={"tool": "entity"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    p = parsed["payload"]
    assert p["name"] == "entity"
    # Full mode includes the full docstring + JSON schema. The `entity`
    # tool's docstring enumerates ops under "Create operations:" / "Read
    # operations:" / "Modify operations:" — assert one of those is present.
    assert "description" in p
    assert any(s in p["description"] for s in (
        "Create operations:", "Read operations:", "Modify operations:",
    ))
    assert "parameters" in p
    assert "operation" in p["parameters"]["properties"]


async def test_tool_help_unknown_tool_returns_not_found(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import system as system_tool
    fn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    r = await fn(operation="tool_help", data={"tool": "nope"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert parsed.get("category") == "not_found"
    # Should surface the available list so callers can recover.
    assert "available_tools" in parsed


# ---------------------------------------------------------------------------
# entity.list summary
# ---------------------------------------------------------------------------


async def test_entity_summary_returns_counts_and_samples(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    # Make a small mixed inventory.
    await efn(operation="create_line", x1=0, y1=0, x2=10, y2=0, layer="A-WALL")
    await efn(operation="create_line", x1=0, y1=10, x2=10, y2=10, layer="A-WALL")
    await efn(operation="create_circle", layer="A-DOOR",
              data={"cx": 5, "cy": 5, "radius": 1})
    await efn(operation="create_circle", layer="A-DOOR",
              data={"cx": 8, "cy": 8, "radius": 1})
    await efn(operation="create_circle", layer="A-DOOR",
              data={"cx": 2, "cy": 2, "radius": 1})

    r = await efn(operation="list", data={"summary": True})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    p = parsed["payload"]
    assert p["total"] == 5
    assert p["by_layer"]["A-WALL"] == 2
    assert p["by_layer"]["A-DOOR"] == 3
    assert p["by_type"]["LINE"] == 2
    assert p["by_type"]["CIRCLE"] == 3
    assert len(p["samples"]) == 5  # all 5 fit under the 10-sample cap


async def test_entity_summary_filters_by_layer(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    await efn(operation="create_line", x1=0, y1=0, x2=1, y2=0, layer="A-WALL")
    await efn(operation="create_line", x1=0, y1=1, x2=1, y2=1, layer="A-DOOR")
    r = await efn(operation="list", layer="A-WALL", data={"summary": True})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    p = parsed["payload"]
    assert p["total"] == 1
    assert list(p["by_layer"].keys()) == ["A-WALL"]
    assert p["layer_filter"] == "A-WALL"


# ---------------------------------------------------------------------------
# drawing.audit / recover
# ---------------------------------------------------------------------------


async def test_drawing_audit_not_supported_on_ezdxf(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="audit")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed or parsed.get("ok") is False


async def test_drawing_recover_requires_path(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="recover")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert "path" in parsed["error"]


# ---------------------------------------------------------------------------
# Drawing memory sidecar
# ---------------------------------------------------------------------------


def test_sidecar_path_pairs_with_drawing(tmp_path):
    from autocad_mcp.project.sidecar import sidecar_path

    dwg = tmp_path / "floor-plan-1.dwg"
    dwg.touch()
    side = sidecar_path(dwg)
    assert side.name == "floor-plan-1.claude.md"
    assert side.parent == dwg.parent.resolve()


def test_read_sidecar_returns_content_when_present(tmp_path):
    from autocad_mcp.project.sidecar import read_sidecar

    dwg = tmp_path / "x.dxf"
    dwg.touch()
    side = tmp_path / "x.claude.md"
    side.write_text("Type A walls = 2x6 GWB both sides", encoding="utf-8")
    text = read_sidecar(dwg)
    assert text is not None
    assert "Type A walls" in text


def test_read_sidecar_returns_none_when_absent(tmp_path):
    from autocad_mcp.project.sidecar import read_sidecar

    dwg = tmp_path / "missing.dxf"
    dwg.touch()
    assert read_sidecar(dwg) is None


def test_sidecar_summary_truncates_large_files(tmp_path):
    from autocad_mcp.project.sidecar import sidecar_summary

    dwg = tmp_path / "x.dxf"
    dwg.touch()
    side = tmp_path / "x.claude.md"
    side.write_text("a" * 5000, encoding="utf-8")
    summary = sidecar_summary(dwg)
    assert summary is not None
    assert summary["size_bytes"] == 5000
    assert len(summary["excerpt"]) == 1000
    assert summary["truncated"] is True


async def test_drawing_open_surfaces_sidecar(monkeypatch, tmp_path):
    """When ezdxf opens a drawing with a sidecar nearby, the response
    payload includes the sidecar excerpt."""
    import ezdxf

    _force_ezdxf(monkeypatch)
    dxf = tmp_path / "with-sidecar.dxf"
    ezdxf.new("R2013").saveas(dxf)
    (tmp_path / "with-sidecar.claude.md").write_text(
        "Ceiling height = 9'-0\"", encoding="utf-8",
    )

    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="open", data={"path": str(dxf)})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert "sidecar" in parsed["payload"]
    assert "Ceiling height" in parsed["payload"]["sidecar"]["excerpt"]


async def test_drawing_open_no_sidecar_omits_field(monkeypatch, tmp_path):
    import ezdxf

    _force_ezdxf(monkeypatch)
    dxf = tmp_path / "no-sidecar.dxf"
    ezdxf.new("R2013").saveas(dxf)

    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="open", data={"path": str(dxf)})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert "sidecar" not in parsed["payload"]


async def test_project_sidecar_op(monkeypatch, tmp_path):
    import ezdxf

    _force_ezdxf(monkeypatch)
    dxf = tmp_path / "p.dxf"
    ezdxf.new("R2013").saveas(dxf)
    (tmp_path / "p.claude.md").write_text("test notes", encoding="utf-8")

    from autocad_mcp.server import project as project_tool
    fn = project_tool.fn if hasattr(project_tool, "fn") else project_tool

    # `path` form (no project loaded).
    r = await fn(operation="sidecar", data={"path": str(dxf)})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["exists"] is True
    assert parsed["payload"]["content"] == "test notes"

    # `drawing` form without a loaded project errors cleanly.
    r = await fn(operation="sidecar", data={"drawing": "anything"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed


# ---------------------------------------------------------------------------
# Turn API
# ---------------------------------------------------------------------------


async def test_turn_begin_end_returns_diff(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool,
        entity as entity_tool,
        system as system_tool,
    )
    # Reset turn state in case prior tests ran in the same process.
    from autocad_mcp import server as server_mod
    server_mod._turn_state.update({
        "active": False, "label": None, "started_at": None,
        "snapshot": None, "undo_started": False,
    })
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    sfn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    await dfn(operation="create")

    r = await sfn(operation="turn_begin", data={"label": "test-turn"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["label"] == "test-turn"

    # Add 3 entities mid-turn.
    await efn(operation="create_line", x1=0, y1=0, x2=10, y2=0, layer="A-WALL")
    await efn(operation="create_line", x1=0, y1=0, x2=0, y2=10, layer="A-WALL")
    await efn(operation="create_circle", layer="A-DOOR",
              data={"cx": 5, "cy": 5, "radius": 1})

    r = await sfn(operation="turn_end")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    diff = parsed["payload"]["diff"]
    assert diff["total_after"] - diff["total_before"] == 3
    assert diff["by_layer"]["A-WALL"] == 2
    assert diff["by_layer"]["A-DOOR"] == 1


async def test_turn_begin_when_already_in_turn_is_noop(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import (
        drawing as drawing_tool, system as system_tool,
    )
    from autocad_mcp import server as server_mod
    server_mod._turn_state.update({
        "active": False, "label": None, "started_at": None,
        "snapshot": None, "undo_started": False,
    })
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool
    sfn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    await dfn(operation="create")
    await sfn(operation="turn_begin", data={"label": "first"})
    r = await sfn(operation="turn_begin", data={"label": "second"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["already_in_turn"] is True
    assert parsed["payload"]["label"] == "first"
    # Clean up.
    await sfn(operation="turn_end")


async def test_turn_end_without_begin_is_clean(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.server import system as system_tool
    from autocad_mcp import server as server_mod
    server_mod._turn_state.update({
        "active": False, "label": None, "started_at": None,
        "snapshot": None, "undo_started": False,
    })
    sfn = system_tool.fn if hasattr(system_tool, "fn") else system_tool

    r = await sfn(operation="turn_end")
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    assert parsed["payload"]["in_turn"] is False


# ---------------------------------------------------------------------------
# Auto-screenshot cache
# ---------------------------------------------------------------------------


async def test_screenshot_cache_invalidated_by_entity_write(monkeypatch):
    _force_ezdxf(monkeypatch)
    from autocad_mcp.client import (
        screenshot_cache_stats, _screenshot_cache,
    )
    # Reset cache state.
    _screenshot_cache["data"] = "fake-png-data"
    _screenshot_cache["dirty"] = False
    _screenshot_cache["captured_at"] = 1.0

    assert screenshot_cache_stats()["dirty"] is False

    from autocad_mcp.server import entity as entity_tool, drawing as drawing_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    dfn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    await dfn(operation="create")
    # Read-only op must NOT dirty the cache.
    # (drawing.create just dirtied it; reset manually for the read assertion.)
    _screenshot_cache["dirty"] = False
    await efn(operation="list")
    assert screenshot_cache_stats()["dirty"] is False, \
        "entity.list is read-only and must not invalidate the cache"

    # A write op must dirty it.
    await efn(operation="create_line", x1=0, y1=0, x2=10, y2=0)
    assert screenshot_cache_stats()["dirty"] is True


# ---------------------------------------------------------------------------
# Backend liveness probe
# ---------------------------------------------------------------------------


async def test_backend_is_alive_true_for_ezdxf():
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
    from autocad_mcp.client import _backend_is_alive

    b = EzdxfBackend()
    await b.initialize()
    assert await _backend_is_alive(b) is True
