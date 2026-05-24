"""Integration tests against a running full AutoCAD.

These tests require AutoCAD (full, not LT) to be running and willing to
create a new document. They are skipped by default to keep the everyday
test suite fast and side-effect-free.

Enable with:
    pytest -m integration tests/test_com_integration.py

WARNING: these tests open a new drawing in your running AutoCAD session
and save it to a tmp_path. They do not modify any pre-existing drawing.
"""

from __future__ import annotations

import sys

import ezdxf
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="COM backend is Windows-only"),
]


@pytest.fixture
async def com_backend(monkeypatch):
    """Live-attached COM backend; skip if AutoCAD isn't actually running.

    After the test, close any document the test created (without saving)
    so we don't leave AutoCAD piled up with untitled drawings — that
    progressively slows AutoCAD's message pump and triggers
    RPC_E_CALL_REJECTED across runs.
    """
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "com")
    from autocad_mcp.backends.com_backend import COMBackend

    b = COMBackend()
    result = await b.initialize()
    if not result.ok:
        pytest.skip(f"AutoCAD not available for integration test: {result.error}")

    yield b

    # Teardown: close the cached document if one was created during the test.
    try:
        if b._doc is not None:
            def _close():
                try:
                    b._doc.Close(False)  # SaveChanges=False
                except Exception:
                    pass
            import asyncio as _asyncio

            loop = _asyncio.get_running_loop()
            await loop.run_in_executor(b._executor, b._wrap_for_com, _close)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


class TestCOMSmoke:
    async def test_status(self, com_backend):
        r = await com_backend.status()
        assert r.ok
        assert r.payload["backend"] == "com"

    async def test_create_new_drawing(self, com_backend):
        r = await com_backend.drawing_create()
        assert r.ok
        assert r.payload["name"]


class TestCOMArchPrimitives:
    async def test_setup_layers_then_wall(self, com_backend):
        await com_backend.drawing_create()

        r = await com_backend.arch_setup_layers()
        assert r.ok
        # Some AIA layers may already exist on a stock template; created should
        # be ≥ 0 and ≤ total AIA layer count.
        assert r.payload["count"] >= 0

        r = await com_backend.arch_wall(0, 0, 120, 0, thickness=6)
        assert r.ok
        assert r.payload["length"] == 120.0
        assert r.payload["layer"] == "A-WALL"

    async def test_full_floor_plan_round_trip(self, com_backend, tmp_path):
        """End-to-end: build a tiny floor plan, save as DXF, reload, assert."""
        await com_backend.drawing_create()
        await com_backend.arch_setup_layers()

        # 4 outer walls
        await com_backend.arch_wall(0, 0, 240, 0, thickness=6)
        await com_backend.arch_wall(240, 0, 240, 180, thickness=6)
        await com_backend.arch_wall(240, 180, 0, 180, thickness=6)
        await com_backend.arch_wall(0, 180, 0, 0, thickness=6)

        await com_backend.arch_door(60, 0, width=36, angle=90, swing="left")
        await com_backend.arch_window(120, 0, 168, 0, sill_thickness=6)
        await com_backend.arch_room(
            [[3, 3], [237, 3], [237, 177], [3, 177]],
            name="LIVING ROOM",
        )

        out_path = tmp_path / "com_floor_plan.dxf"
        save_result = await com_backend.drawing_save_as_dxf(str(out_path))
        assert save_result.ok
        assert out_path.exists()

        # Reload via ezdxf and check entity layer assignments
        reloaded = ezdxf.readfile(str(out_path))
        rmsp = reloaded.modelspace()
        wall_ents = [e for e in rmsp if e.dxf.layer == "A-WALL"]
        door_ents = [e for e in rmsp if e.dxf.layer == "A-DOOR"]
        glaz_ents = [e for e in rmsp if e.dxf.layer == "A-GLAZ"]

        assert len(wall_ents) == 4
        assert len(door_ents) == 2  # leaf + arc
        assert len(glaz_ents) == 2  # sill + glass


class TestCOMWallWithOpenings:
    async def test_wall_with_one_door_and_one_window(self, com_backend):
        await com_backend.drawing_create()
        await com_backend.arch_setup_layers()

        r = await com_backend.arch_wall_with_openings(
            0, 0, 240, 0, thickness=6,
            openings=[
                {"type": "door", "position": 60, "width": 36, "swing": "left"},
                {"type": "window", "position": 144, "width": 30},
            ],
        )
        assert r.ok
        assert r.payload["segment_count"] == 3
        assert r.payload["opening_count"] == 2
        assert len(r.payload["handles"]["doors"]) == 1
        assert len(r.payload["handles"]["windows"]) == 1


class TestCOMSymbolLibrary:
    async def test_define_all_then_insert(self, com_backend):
        """Define every library symbol, then insert one to confirm it materializes."""
        from autocad_mcp.arch.library import SYMBOL_LIBRARY

        await com_backend.drawing_create()

        r = await com_backend.library_define_all()
        assert r.ok, r.error
        assert r.payload["total"] >= 5
        assert r.payload["errors"] == []

        # Insert a toilet at a known point.
        r = await com_backend.library_insert("PLMB-TOILET", 0, 0, scale=1.0, rotation=0.0)
        assert r.ok, r.error
        # An INSERT entity should now exist.
        r = await com_backend.entity_count()
        assert r.ok
        assert r.payload["count"] >= 1

    async def test_entity_count_in_define_payload_matches_spec(self, com_backend):
        """`block.Count` in COM must return the same number of entities the
        spec defines. Without this assertion the count column in the payload
        could silently report stale or zero values."""
        from autocad_mcp.arch.library import SYMBOL_LIBRARY

        await com_backend.drawing_create()

        # Pick a symbol with a stable count > 1 (the toilet has 2 entities).
        name = "PLMB-TOILET"
        expected = len(SYMBOL_LIBRARY[name])
        r = await com_backend.library_define(name)
        assert r.ok, r.error
        assert r.payload["entity_count"] == expected, (
            f"block.Count reported {r.payload['entity_count']}, "
            f"expected {expected} (len of spec)"
        )


class TestCOMFilletChamfer:
    async def test_fillet_two_lines(self, com_backend):
        await com_backend.drawing_create()
        r1 = await com_backend.create_line(0, 0, 100, 0)
        r2 = await com_backend.create_line(100, 0, 100, 100)
        h1, h2 = r1.payload["handle"], r2.payload["handle"]

        result = await com_backend.entity_fillet(h1, h2, radius=10)
        assert result.ok, result.error
        assert "fillet_handle" in result.payload
        # Trim points match the analytical answer.
        assert result.payload["trim1"] == pytest.approx([90.0, 0.0], abs=1e-3)
        assert result.payload["trim2"] == pytest.approx([100.0, 10.0], abs=1e-3)

    async def test_chamfer_two_lines(self, com_backend):
        await com_backend.drawing_create()
        r1 = await com_backend.create_line(0, 0, 100, 0)
        r2 = await com_backend.create_line(100, 0, 100, 100)
        h1, h2 = r1.payload["handle"], r2.payload["handle"]

        result = await com_backend.entity_chamfer(h1, h2, dist1=5, dist2=8)
        assert result.ok, result.error
        assert result.payload["trim1"] == pytest.approx([95.0, 0.0], abs=1e-3)
        assert result.payload["trim2"] == pytest.approx([100.0, 8.0], abs=1e-3)


class TestCOMLayerOps:
    async def test_layer_create_list(self, com_backend):
        await com_backend.drawing_create()

        r = await com_backend.layer_create("TEST-LAYER", color="red")
        assert r.ok

        r = await com_backend.layer_list()
        assert r.ok
        names = [l["name"] for l in r.payload["layers"]]
        assert "TEST-LAYER" in names

    async def test_layer_freeze_thaw(self, com_backend):
        await com_backend.drawing_create()
        await com_backend.layer_create("FREEZE-ME", color="blue")

        r = await com_backend.layer_freeze("FREEZE-ME")
        assert r.ok

        r = await com_backend.layer_thaw("FREEZE-ME")
        assert r.ok
