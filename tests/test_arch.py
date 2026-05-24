"""Tests for the arch module — AIA layer preset + composite primitives."""

from __future__ import annotations

import math

import ezdxf
import pytest

from autocad_mcp.arch import (
    AIA_LAYERS,
    create_dimension_chain,
    create_door,
    create_room,
    create_wall,
    create_wall_with_openings,
    create_window,
    setup_aia_layers,
)
from autocad_mcp.arch.geometry import (
    polygon_area,
    polygon_centroid,
    wall_corners,
    wall_segments_with_openings,
    window_corners,
    door_geometry,
)
from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def doc():
    """A fresh ezdxf document with default setup.

    ezdxf's `R2013` template sets `$INSUNITS=6` (meters), but historically
    this test suite assumed imperial units throughout (areas displayed as
    "SF"). Pin the doc to inches so the area-label assertions remain
    backwards-compatible — metric behavior has its own dedicated tests in
    `test_n_fixes.py`.
    """
    d = ezdxf.new("R2013", setup=True)
    # Historical default for this suite was imperial-feet (area in ft²,
    # display "<n> SF" with no conversion). ezdxf 1.x's `R2013` template
    # sets $INSUNITS=6 (meters); pin to feet so existing assertions hold.
    d.header["$INSUNITS"] = 2
    return d


@pytest.fixture
def msp(doc):
    return doc.modelspace()


@pytest.fixture
async def backend():
    b = EzdxfBackend()
    result = await b.initialize()
    assert result.ok
    return b


# ---------------------------------------------------------------------------
# AIA layers
# ---------------------------------------------------------------------------


class TestAIALayers:
    def test_setup_creates_all_layers(self, doc):
        created = setup_aia_layers(doc)
        # Excluding the default "0" layer, every AIA spec layer must be created.
        for spec in AIA_LAYERS:
            assert spec.name in doc.layers, f"Layer {spec.name} missing"
        assert len(created) == len(AIA_LAYERS)

    def test_setup_is_idempotent(self, doc):
        first = setup_aia_layers(doc)
        second = setup_aia_layers(doc)
        assert len(first) == len(AIA_LAYERS)
        assert second == [], "Second call should create nothing"

    def test_layer_colors_match_spec(self, doc):
        setup_aia_layers(doc)
        for spec in AIA_LAYERS:
            layer = doc.layers.get(spec.name)
            assert layer.color == spec.color, (
                f"{spec.name} color mismatch: got {layer.color}, expected {spec.color}"
            )

    def test_hidden_linetype_aliased(self, doc):
        """A-WALL-HEAD requests HIDDEN; should resolve via DASHED alias or CONTINUOUS."""
        setup_aia_layers(doc)
        layer = doc.layers.get("A-WALL-HEAD")
        # Either DASHED was loaded (alias) or fell back to CONTINUOUS — both acceptable.
        assert layer.dxf.linetype in ("DASHED", "CONTINUOUS", "HIDDEN")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


class TestGeometryHelpers:
    def test_square_area(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert polygon_area(sq) == pytest.approx(100.0)

    def test_square_centroid(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
        cx, cy = polygon_centroid(sq)
        assert (cx, cy) == pytest.approx((5.0, 5.0))

    def test_triangle_area(self):
        tri = [(0, 0), (12, 0), (0, 9)]
        assert polygon_area(tri) == pytest.approx(54.0)

    def test_orientation_independence(self):
        cw = [(0, 0), (0, 10), (10, 10), (10, 0)]
        ccw = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert polygon_area(cw) == polygon_area(ccw)


# ---------------------------------------------------------------------------
# Wall
# ---------------------------------------------------------------------------


class TestWall:
    def test_basic_wall(self, msp):
        info = create_wall(msp, 0, 0, 100, 0, thickness=6)
        assert info["length"] == pytest.approx(100.0)
        assert info["thickness"] == 6
        assert info["layer"] == "A-WALL"
        ent = msp.doc.entitydb.get(info["handle"])
        assert ent.dxftype() == "LWPOLYLINE"
        assert ent.dxf.layer == "A-WALL"
        assert ent.is_closed

    def test_wall_polygon_has_four_corners(self, msp):
        info = create_wall(msp, 0, 0, 100, 0, thickness=6)
        ent = msp.doc.entitydb.get(info["handle"])
        pts = list(ent.get_points("xy"))
        assert len(pts) == 4

    def test_wall_zero_length_raises(self, msp):
        with pytest.raises(ValueError, match="zero length"):
            create_wall(msp, 5, 5, 5, 5, thickness=6)

    def test_wall_invalid_justify_raises(self, msp):
        with pytest.raises(ValueError, match="justify"):
            create_wall(msp, 0, 0, 100, 0, thickness=6, justify="bogus")

    def test_wall_creates_layer_if_missing(self, msp):
        # Use a non-AIA name to force the auto-create path
        info = create_wall(msp, 0, 0, 100, 0, layer="CUSTOM-WALL")
        assert "CUSTOM-WALL" in msp.doc.layers

    def test_wall_diagonal_geometry(self, msp):
        """A 45° wall of length √2 should produce a 4-corner polygon
        whose perpendicular dimension equals thickness."""
        info = create_wall(msp, 0, 0, 10, 10, thickness=2.0, justify="center")
        ent = msp.doc.entitydb.get(info["handle"])
        pts = list(ent.get_points("xy"))
        assert len(pts) == 4
        # Distance between opposite corners across width should be ≈ thickness
        d_across = math.hypot(pts[0][0] - pts[3][0], pts[0][1] - pts[3][1])
        assert d_across == pytest.approx(2.0, rel=1e-6)


# ---------------------------------------------------------------------------
# Door
# ---------------------------------------------------------------------------


class TestDoor:
    def test_door_creates_leaf_and_arc(self, msp):
        info = create_door(msp, 0, 0, width=36, angle=0, swing="left")
        leaf = msp.doc.entitydb.get(info["handles"]["leaf"])
        arc = msp.doc.entitydb.get(info["handles"]["arc"])
        assert leaf.dxftype() == "LINE"
        assert arc.dxftype() == "ARC"
        assert leaf.dxf.layer == "A-DOOR"
        assert arc.dxf.layer == "A-DOOR"

    def test_door_arc_radius_equals_width(self, msp):
        info = create_door(msp, 5, 5, width=36)
        arc = msp.doc.entitydb.get(info["handles"]["arc"])
        assert arc.dxf.radius == pytest.approx(36.0)

    def test_door_arc_centered_at_hinge(self, msp):
        info = create_door(msp, 5, 7, width=36)
        arc = msp.doc.entitydb.get(info["handles"]["arc"])
        cx, cy, _ = arc.dxf.center
        assert (cx, cy) == pytest.approx((5.0, 7.0))

    def test_door_swing_directions(self, msp):
        info_l = create_door(msp, 0, 0, width=36, angle=0, swing="left")
        info_r = create_door(msp, 0, 0, width=36, angle=0, swing="right")
        arc_l = msp.doc.entitydb.get(info_l["handles"]["arc"])
        arc_r = msp.doc.entitydb.get(info_r["handles"]["arc"])
        # Left swing: 0° → 90°.  Right swing: -90° (=270°) → 0°.
        assert arc_l.dxf.start_angle == pytest.approx(0.0)
        assert arc_l.dxf.end_angle == pytest.approx(90.0)
        assert arc_r.dxf.end_angle == pytest.approx(0.0)

    def test_door_invalid_swing_raises(self, msp):
        with pytest.raises(ValueError, match="swing"):
            create_door(msp, 0, 0, swing="middle")

    def test_door_room_side_above_picks_left_swing(self, msp):
        """A horizontal closed door (angle=0) with room_side above it (+y)
        should resolve to swing='left' regardless of the explicit swing arg."""
        info = create_door(
            msp, 0, 0, width=36, angle=0,
            swing="right",            # explicit, but room_side should override
            room_side=(0, 100),       # point is above the wall (+y side)
        )
        arc = msp.doc.entitydb.get(info["handles"]["arc"])
        # Left swing: arc sweeps 0° → 90° CCW.
        assert arc.dxf.start_angle == pytest.approx(0.0)
        assert arc.dxf.end_angle == pytest.approx(90.0)
        assert info["swing"] == "left"

    def test_door_room_side_below_picks_right_swing(self, msp):
        info = create_door(
            msp, 0, 0, width=36, angle=0,
            swing="left",
            room_side=(0, -100),
        )
        arc = msp.doc.entitydb.get(info["handles"]["arc"])
        # Right swing: arc goes from -90° (normalized to 270°) CCW to 0°.
        assert arc.dxf.start_angle == pytest.approx(270.0)
        assert arc.dxf.end_angle == pytest.approx(0.0) or arc.dxf.end_angle == pytest.approx(360.0)
        assert info["swing"] == "right"


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


class TestWindow:
    def test_window_creates_sill_and_glass(self, msp):
        info = create_window(msp, 0, 0, 30, 0, sill_thickness=6)
        sill = msp.doc.entitydb.get(info["handles"]["sill"])
        glass = msp.doc.entitydb.get(info["handles"]["glass"])
        assert sill.dxftype() == "LWPOLYLINE"
        assert sill.is_closed
        assert glass.dxftype() == "LINE"
        assert sill.dxf.layer == "A-GLAZ"
        assert info["width"] == pytest.approx(30.0)

    def test_window_zero_length_raises(self, msp):
        with pytest.raises(ValueError, match="zero length"):
            create_window(msp, 5, 5, 5, 5)


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------


class TestRoom:
    def test_room_simple_rectangle(self, msp):
        info = create_room(msp, [(0, 0), (12, 0), (12, 9), (0, 9)], name="OFFICE")
        boundary = msp.doc.entitydb.get(info["handles"]["boundary"])
        label = msp.doc.entitydb.get(info["handles"]["label"])
        assert boundary.dxftype() == "LWPOLYLINE"
        assert boundary.is_closed
        assert label.dxftype() == "MTEXT"
        assert info["area"] == pytest.approx(108.0)
        assert info["centroid"] == pytest.approx((6.0, 4.5))
        assert "OFFICE" in label.text

    def test_room_l_shape_area(self, msp):
        # L-shape: 10x10 with a 4x4 bite out of the top-right corner = 100-16 = 84
        l_shape = [(0, 0), (10, 0), (10, 6), (6, 6), (6, 10), (0, 10)]
        info = create_room(msp, l_shape, name="ROOM 102")
        assert info["area"] == pytest.approx(84.0)

    def test_room_uses_provided_layers(self, msp):
        info = create_room(
            msp, [(0, 0), (10, 0), (10, 10), (0, 10)],
            name="X", area_layer="A-AREA", text_layer="A-AREA-IDEN",
        )
        boundary = msp.doc.entitydb.get(info["handles"]["boundary"])
        label = msp.doc.entitydb.get(info["handles"]["label"])
        assert boundary.dxf.layer == "A-AREA"
        assert label.dxf.layer == "A-AREA-IDEN"

    def test_room_too_few_points_raises(self, msp):
        with pytest.raises(ValueError, match="at least 3"):
            create_room(msp, [(0, 0), (10, 10)])

    def test_room_no_name_label_is_area_only(self, msp):
        info = create_room(msp, [(0, 0), (4, 0), (4, 4), (0, 4)])
        label = msp.doc.entitydb.get(info["handles"]["label"])
        assert "16.0 SF" in label.text


# ---------------------------------------------------------------------------
# Dimension chain
# ---------------------------------------------------------------------------


class TestDimensionChain:
    def test_chain_creates_n_minus_one_dims(self, msp):
        info = create_dimension_chain(msp, [(0, 0), (10, 0), (20, 0), (30, 0)], offset=5)
        assert info["count"] == 3
        assert len(info["handles"]) == 3
        for h in info["handles"]:
            ent = msp.doc.entitydb.get(h)
            assert ent.dxftype() == "DIMENSION"
            assert ent.dxf.layer == "A-ANNO-DIMS"

    def test_chain_too_few_points_raises(self, msp):
        with pytest.raises(ValueError, match="≥2 points"):
            create_dimension_chain(msp, [(0, 0)], offset=5)

    def test_chain_invalid_direction_raises(self, msp):
        with pytest.raises(ValueError, match="direction"):
            create_dimension_chain(msp, [(0, 0), (10, 0)], direction="diagonal")


# ---------------------------------------------------------------------------
# Wall with openings (geometry helper)
# ---------------------------------------------------------------------------


class TestWallSegmentsWithOpenings:
    def test_no_openings_returns_single_segment(self):
        segments, openings, length = wall_segments_with_openings(
            0, 0, 100, 0, thickness=6, openings=[],
        )
        assert len(segments) == 1
        assert openings == []
        assert length == 100.0
        # 4-corner closed polygon
        assert len(segments[0]) == 4

    def test_one_middle_opening_yields_two_segments(self):
        segments, openings, _ = wall_segments_with_openings(
            0, 0, 100, 0, thickness=6,
            openings=[{"type": "door", "position": 50, "width": 36}],
        )
        assert len(segments) == 2
        assert len(openings) == 1
        # Door midpoint should be on the centerline at x=50.
        assert openings[0]["midpoint"] == pytest.approx((50.0, 0.0))
        # Centerline endpoints span position-w/2 .. position+w/2 = 32 .. 68
        assert openings[0]["centerline_start"] == pytest.approx((32.0, 0.0))
        assert openings[0]["centerline_end"] == pytest.approx((68.0, 0.0))

    def test_multiple_openings_unsorted_input(self):
        # Window after door, but input is intentionally out of order.
        segments, openings, _ = wall_segments_with_openings(
            0, 0, 240, 0, thickness=6,
            openings=[
                {"type": "window", "position": 144, "width": 30},
                {"type": "door", "position": 60, "width": 36, "swing": "left"},
            ],
        )
        # 2 openings → 3 wall segments
        assert len(segments) == 3
        # Openings are returned sorted by position.
        assert openings[0]["type"] == "door"
        assert openings[1]["type"] == "window"

    def test_opening_at_start_yields_one_tail_segment(self):
        segments, _, _ = wall_segments_with_openings(
            0, 0, 100, 0, thickness=6,
            openings=[{"type": "door", "position": 18, "width": 36}],
        )
        # Opening starts at 0 → no leading segment, only the tail.
        assert len(segments) == 1

    def test_opening_at_end_yields_one_lead_segment(self):
        segments, _, _ = wall_segments_with_openings(
            0, 0, 100, 0, thickness=6,
            openings=[{"type": "door", "position": 82, "width": 36}],
        )
        assert len(segments) == 1

    def test_overlap_raises(self):
        with pytest.raises(ValueError, match="overlap"):
            wall_segments_with_openings(
                0, 0, 100, 0, thickness=6,
                openings=[
                    {"type": "door", "position": 50, "width": 40},
                    {"type": "door", "position": 60, "width": 30},
                ],
            )

    def test_opening_outside_wall_raises(self):
        with pytest.raises(ValueError, match="outside"):
            wall_segments_with_openings(
                0, 0, 100, 0, thickness=6,
                openings=[{"type": "door", "position": 95, "width": 36}],
            )

    def test_zero_width_opening_raises(self):
        with pytest.raises(ValueError, match="width"):
            wall_segments_with_openings(
                0, 0, 100, 0, thickness=6,
                openings=[{"type": "door", "position": 50, "width": 0}],
            )

    def test_diagonal_wall_angle_reported(self):
        # 45° wall: angle should be 45.
        _, openings, _ = wall_segments_with_openings(
            0, 0, 100, 100, thickness=6,
            openings=[{"type": "door", "position": math.hypot(50, 50), "width": 10}],
        )
        assert openings[0]["angle_deg"] == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Wall with openings (ezdxf primitive)
# ---------------------------------------------------------------------------


class TestCreateWallWithOpenings:
    def test_two_openings_three_segments_and_symbols(self, msp):
        info = create_wall_with_openings(
            msp, 0, 0, 240, 0, thickness=6,
            openings=[
                {"type": "door", "position": 60, "width": 36, "swing": "left"},
                {"type": "window", "position": 144, "width": 30},
            ],
        )
        assert info["segment_count"] == 3
        assert info["opening_count"] == 2

        # Wall segments on A-WALL
        wall_ents = [e for e in msp if e.dxf.layer == "A-WALL"]
        assert len(wall_ents) == 3
        for e in wall_ents:
            assert e.dxftype() == "LWPOLYLINE"
            assert e.is_closed

        # Door symbol = leaf line + arc on A-DOOR
        door_ents = [e for e in msp if e.dxf.layer == "A-DOOR"]
        assert {e.dxftype() for e in door_ents} == {"LINE", "ARC"}

        # Window symbol = sill polyline + glass line on A-GLAZ
        glaz_ents = [e for e in msp if e.dxf.layer == "A-GLAZ"]
        assert {e.dxftype() for e in glaz_ents} == {"LWPOLYLINE", "LINE"}

    def test_no_openings_falls_back_to_single_segment(self, msp):
        info = create_wall_with_openings(msp, 0, 0, 100, 0, thickness=6, openings=[])
        assert info["segment_count"] == 1
        assert info["opening_count"] == 0

    def test_via_backend(self, msp):
        # End-to-end through the ezdxf backend interface.
        pass  # covered in TestBackendIntegration below


# ---------------------------------------------------------------------------
# Backend integration
# ---------------------------------------------------------------------------


class TestBackendIntegration:
    async def test_arch_setup_layers(self, backend):
        r = await backend.arch_setup_layers()
        assert r.ok
        assert r.payload["count"] == len(AIA_LAYERS)

    async def test_arch_wall_via_backend(self, backend):
        r = await backend.arch_wall(0, 0, 100, 0, thickness=6)
        assert r.ok
        assert r.payload["length"] == 100.0
        assert r.payload["layer"] == "A-WALL"

    async def test_arch_room_via_backend(self, backend):
        r = await backend.arch_room(
            [[0, 0], [10, 0], [10, 10], [0, 10]],
            name="LIVING ROOM",
        )
        assert r.ok
        assert r.payload["area"] == pytest.approx(100.0)
        assert r.payload["name"] == "LIVING ROOM"

    async def test_arch_dimension_chain_via_backend(self, backend):
        r = await backend.arch_dimension_chain(
            [[0, 0], [50, 0], [100, 0]], offset=10,
        )
        assert r.ok
        assert r.payload["count"] == 2

    async def test_entity_fillet_two_lines(self, backend):
        """Two perpendicular lines meeting at (100,0): a fillet of radius 10
        should trim line1 to end at (90,0), trim line2 to start at (100,10),
        and add an arc on the same layer."""
        r1 = await backend.create_line(0, 0, 100, 0, layer="A-WALL")
        r2 = await backend.create_line(100, 0, 100, 100, layer="A-WALL")
        h1, h2 = r1.payload["handle"], r2.payload["handle"]

        result = await backend.entity_fillet(h1, h2, radius=10)
        assert result.ok, result.error
        assert "fillet_handle" in result.payload

        # Verify line endpoints were trimmed.
        line1 = backend._doc.entitydb.get(h1)
        line2 = backend._doc.entitydb.get(h2)
        assert tuple(line1.dxf.end)[:2] == pytest.approx((90.0, 0.0))
        assert tuple(line2.dxf.start)[:2] == pytest.approx((100.0, 10.0))

        # Arc was added on the same layer as line1.
        arc = backend._doc.entitydb.get(result.payload["fillet_handle"])
        assert arc.dxftype() == "ARC"
        assert arc.dxf.layer == "A-WALL"
        assert arc.dxf.radius == pytest.approx(10.0)

    async def test_entity_chamfer_two_lines(self, backend):
        """Asymmetric chamfer: dist1=5 on line1, dist2=8 on line2."""
        r1 = await backend.create_line(0, 0, 100, 0, layer="A-WALL")
        r2 = await backend.create_line(100, 0, 100, 100, layer="A-WALL")
        h1, h2 = r1.payload["handle"], r2.payload["handle"]

        result = await backend.entity_chamfer(h1, h2, dist1=5, dist2=8)
        assert result.ok, result.error
        # Lines trimmed asymmetrically.
        line1 = backend._doc.entitydb.get(h1)
        line2 = backend._doc.entitydb.get(h2)
        assert tuple(line1.dxf.end)[:2] == pytest.approx((95.0, 0.0))
        assert tuple(line2.dxf.start)[:2] == pytest.approx((100.0, 8.0))
        # New chamfer line must inherit line1's layer.
        cham = backend._doc.entitydb.get(result.payload["chamfer_handle"])
        assert cham.dxftype() == "LINE"
        assert cham.dxf.layer == "A-WALL"

    async def test_fillet_rejects_non_line(self, backend):
        r1 = await backend.create_line(0, 0, 10, 0)
        r2 = await backend.create_circle(5, 5, 3)
        result = await backend.entity_fillet(
            r1.payload["handle"], r2.payload["handle"], radius=1,
        )
        assert result.ok is False
        assert "LINE+LINE" in result.error

    async def test_fillet_rejects_parallel_lines(self, backend):
        r1 = await backend.create_line(0, 0, 10, 0)
        r2 = await backend.create_line(0, 5, 10, 5)
        result = await backend.entity_fillet(
            r1.payload["handle"], r2.payload["handle"], radius=1,
        )
        # The two lines don't share an endpoint at all — that's the failure
        # mode the geometry helper trips on first.
        assert result.ok is False

    async def test_arch_wall_with_openings_via_backend(self, backend):
        await backend.arch_setup_layers()
        r = await backend.arch_wall_with_openings(
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

    async def test_arch_tool_dispatch_wall_with_openings(self, backend):
        """Drive the MCP tool function directly to catch operation-string typos."""
        from autocad_mcp.server import arch

        fn = arch.fn if hasattr(arch, "fn") else arch
        await fn(operation="setup_layers")
        result = await fn(operation="wall_with_openings", data={
            "x1": 0, "y1": 0, "x2": 120, "y2": 0,
            "openings": [{"type": "door", "position": 60, "width": 36}],
        })
        # The tool returns a JSON string for the ok path.
        import json
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["segment_count"] == 2

    async def test_arch_tool_unknown_operation_returns_error(self, backend):
        from autocad_mcp.server import arch
        import json

        fn = arch.fn if hasattr(arch, "fn") else arch
        result = await fn(operation="not_a_real_op")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed
        assert "Unknown arch operation" in parsed["error"]

    async def test_full_floor_plan_smoke(self, backend, tmp_path):
        """Build a tiny floor plan end-to-end + save as DXF + reload, asserting
        entity counts and layer assignments survive a round-trip."""
        await backend.arch_setup_layers()

        # Outer walls of a 20×15 room
        await backend.arch_wall(0, 0, 240, 0, thickness=6)      # bottom (20')
        await backend.arch_wall(240, 0, 240, 180, thickness=6)  # right (15')
        await backend.arch_wall(240, 180, 0, 180, thickness=6)  # top
        await backend.arch_wall(0, 180, 0, 0, thickness=6)      # left

        # One door + one window
        await backend.arch_door(60, 0, width=36, angle=90, swing="left")
        await backend.arch_window(120, 0, 168, 0, sill_thickness=6)

        # Room boundary + dim chain along the bottom
        await backend.arch_room(
            [[3, 3], [237, 3], [237, 177], [3, 177]],
            name="LIVING ROOM",
        )
        await backend.arch_dimension_chain(
            [[0, 0], [60, 0], [120, 0], [168, 0], [240, 0]],
            offset=24,
        )

        # Save and reload to confirm DXF is valid
        out_path = tmp_path / "floor_plan.dxf"
        save_result = await backend.drawing_save_as_dxf(str(out_path))
        assert save_result.ok
        assert out_path.exists()

        # Reload and check layer set + entity layer assignments
        reloaded = ezdxf.readfile(str(out_path))
        assert "A-WALL" in reloaded.layers
        assert "A-DOOR" in reloaded.layers
        assert "A-GLAZ" in reloaded.layers
        assert "A-AREA" in reloaded.layers
        assert "A-ANNO-DIMS" in reloaded.layers

        rmsp = reloaded.modelspace()
        wall_ents = [e for e in rmsp if e.dxf.layer == "A-WALL"]
        door_ents = [e for e in rmsp if e.dxf.layer == "A-DOOR"]
        glaz_ents = [e for e in rmsp if e.dxf.layer == "A-GLAZ"]

        assert len(wall_ents) == 4
        # Door = leaf line + swing arc
        assert len(door_ents) == 2
        # Window = sill polygon + glass line
        assert len(glaz_ents) == 2
