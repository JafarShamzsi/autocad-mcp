"""Tests for the new building primitives: stair, column, slab, beam,
roof, elevation, section, templates, plus the LISP-spec serializer.
"""

from __future__ import annotations

import math

import ezdxf
import pytest

from autocad_mcp.arch import (
    create_beam,
    create_column,
    create_elevation,
    create_roof_plan,
    create_section,
    create_slab,
    create_stair,
    setup_aia_layers,
)
from autocad_mcp.arch.geometry import (
    beam_endpoints,
    column_corners,
    elevation_view,
    roof_plan,
    section_view,
    slab_outline,
    stair_run,
    stair_u_shape,
)
from autocad_mcp.arch.lisp_render import (
    serialize_beam,
    serialize_column,
    serialize_elevation,
    serialize_roof,
    serialize_section,
    serialize_slab,
    serialize_stair,
    serialize_stair_u,
    serialize_template,
)
from autocad_mcp.arch.templates import (
    TEMPLATES,
    apply_template,
    template_names,
)
from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


@pytest.fixture
def doc():
    d = ezdxf.new("R2013", setup=True)
    setup_aia_layers(d)
    return d


@pytest.fixture
def msp(doc):
    return doc.modelspace()


# ---------------------------------------------------------------------------
# Stair geometry
# ---------------------------------------------------------------------------


class TestStairGeometry:
    def test_run_basic(self):
        s = stair_run(0, 0, 96, 0, width=36, num_treads=12)
        assert len(s["outline"]) == 4
        assert len(s["tread_lines"]) == 11
        assert s["length"] == 96.0
        assert s["tread_depth"] == 8.0
        assert s["width"] == 36

    def test_run_custom_tread_depth(self):
        s = stair_run(0, 0, 120, 0, width=42, num_treads=10, tread_depth=10.0)
        assert s["tread_depth"] == 10.0
        assert len(s["tread_lines"]) == 9

    def test_run_diagonal(self):
        s = stair_run(0, 0, 60, 80, width=36, num_treads=10)
        assert math.isclose(s["length"], 100.0)
        assert len(s["tread_lines"]) == 9

    def test_run_no_arrow(self):
        s = stair_run(0, 0, 96, 0, width=36, num_treads=12, direction_arrow=False)
        assert s["arrow"] is None
        assert s["arrow_head"] is None

    def test_run_zero_length_raises(self):
        with pytest.raises(ValueError):
            stair_run(0, 0, 0, 0, width=36, num_treads=12)

    def test_run_invalid_num_treads_raises(self):
        with pytest.raises(ValueError):
            stair_run(0, 0, 96, 0, width=36, num_treads=0)

    def test_run_invalid_width_raises(self):
        with pytest.raises(ValueError):
            stair_run(0, 0, 96, 0, width=0, num_treads=10)

    def test_u_shape_basic(self):
        u = stair_u_shape(0, 0, flight_length=120, width=36, num_treads_per_flight=10)
        assert u["total_treads"] == 20
        assert len(u["landing_corners"]) == 4
        assert len(u["flight1"]["tread_lines"]) == 9
        assert len(u["flight2"]["tread_lines"]) == 9

    def test_u_shape_rotated(self):
        u = stair_u_shape(0, 0, flight_length=120, width=36,
                          num_treads_per_flight=10, angle_deg=90.0)
        # Flight 1 going up the +Y axis: top of flight at (0, 120).
        flight1_outline = u["flight1"]["outline"]
        assert len(flight1_outline) == 4

    def test_u_shape_invalid_args(self):
        with pytest.raises(ValueError):
            stair_u_shape(0, 0, flight_length=0, width=36, num_treads_per_flight=10)
        with pytest.raises(ValueError):
            stair_u_shape(0, 0, flight_length=100, width=0, num_treads_per_flight=10)


# ---------------------------------------------------------------------------
# Column geometry
# ---------------------------------------------------------------------------


class TestColumnGeometry:
    def test_square(self):
        c = column_corners(0, 0, "square", 12.0)
        assert c["shape"] == "rect"
        assert c["size_x"] == 12.0
        assert c["size_y"] == 12.0
        assert len(c["corners"]) == 4

    def test_rect_requires_size_y(self):
        with pytest.raises(ValueError):
            column_corners(0, 0, "rect", 12.0)

    def test_rect_with_size_y(self):
        c = column_corners(0, 0, "rect", 10.0, size_y=20.0)
        assert c["size_x"] == 10.0
        assert c["size_y"] == 20.0

    def test_round(self):
        c = column_corners(0, 0, "round", 12.0)
        assert c["shape"] == "round"
        assert c["radius"] == 6.0

    def test_invalid_shape(self):
        with pytest.raises(ValueError):
            column_corners(0, 0, "triangle", 12.0)

    def test_rotation_changes_corners(self):
        c0 = column_corners(0, 0, "square", 10.0)
        c45 = column_corners(0, 0, "square", 10.0, angle_deg=45.0)
        assert c0["corners"] != c45["corners"]


# ---------------------------------------------------------------------------
# Slab / beam / roof geometry
# ---------------------------------------------------------------------------


class TestSlabGeometry:
    def test_basic(self):
        s = slab_outline([(0, 0), (100, 0), (100, 80), (0, 80)])
        assert s["area"] == 8000.0
        assert s["centroid"] == (50.0, 40.0)
        assert s["inner"] is None

    def test_edge_thickness_produces_inner(self):
        s = slab_outline([(0, 0), (100, 0), (100, 80), (0, 80)], edge_thickness=4)
        assert s["inner"] is not None
        # Inner polygon must be smaller than the outer
        from autocad_mcp.arch.geometry import polygon_area
        assert polygon_area(s["inner"]) < s["area"]

    def test_too_few_points_raises(self):
        with pytest.raises(ValueError):
            slab_outline([(0, 0), (1, 1)])


class TestBeamGeometry:
    def test_centerline_only(self):
        b = beam_endpoints(0, 0, 100, 0, width=0)
        assert b["edges"] is None
        assert b["length"] == 100.0

    def test_two_edges(self):
        b = beam_endpoints(0, 0, 100, 0, width=4)
        assert b["edges"] is not None
        assert len(b["edges"]) == 2

    def test_zero_length_raises(self):
        with pytest.raises(ValueError):
            beam_endpoints(0, 0, 0, 0, width=4)


class TestRoofGeometry:
    def test_gable_x(self):
        r = roof_plan([(0, 0), (120, 0), (120, 80), (0, 80)],
                      style="gable", ridge_axis="x", overhang=12)
        assert len(r["ridges"]) == 1
        assert len(r["hips"]) == 0

    def test_gable_y(self):
        r = roof_plan([(0, 0), (120, 0), (120, 80), (0, 80)],
                      style="gable", ridge_axis="y", overhang=12)
        assert len(r["ridges"]) == 1

    def test_hip(self):
        r = roof_plan([(0, 0), (120, 0), (120, 80), (0, 80)],
                      style="hip", overhang=12)
        assert len(r["ridges"]) == 1
        assert len(r["hips"]) == 4

    def test_flat(self):
        r = roof_plan([(0, 0), (120, 0), (120, 80), (0, 80)],
                      style="flat", overhang=12)
        assert len(r["ridges"]) == 0

    def test_invalid_style_raises(self):
        with pytest.raises(ValueError):
            roof_plan([(0, 0), (120, 0), (120, 80), (0, 80)],
                      style="hexagonal", overhang=12)

    def test_eaves_larger_than_footprint(self):
        from autocad_mcp.arch.geometry import polygon_area
        r = roof_plan([(0, 0), (100, 0), (100, 80), (0, 80)],
                      style="flat", overhang=12)
        assert polygon_area(r["eaves"]) > polygon_area(r["footprint"])


# ---------------------------------------------------------------------------
# Elevation + section geometry
# ---------------------------------------------------------------------------


class TestElevationGeometry:
    def test_basic_outline(self):
        e = elevation_view(120, 96)
        assert len(e["outline"]) == 4
        assert e["floor_line"] == ((0.0, 0.0), (120.0, 0.0))
        assert e["openings"] == []

    def test_door_opening(self):
        e = elevation_view(120, 96, openings=[
            {"position": 30, "width": 32, "height": 84, "type": "door"},
        ])
        assert len(e["openings"]) == 1
        assert e["openings"][0]["type"] == "door"

    def test_window_with_panes(self):
        e = elevation_view(120, 96, openings=[
            {"position": 60, "width": 36, "height": 48, "sill": 30, "type": "window"},
        ])
        assert len(e["openings"]) == 1
        assert "panes" in e["openings"][0]
        assert len(e["openings"][0]["panes"]) == 2

    def test_opening_outside_wall_raises(self):
        with pytest.raises(ValueError):
            elevation_view(120, 96, openings=[
                {"position": 110, "width": 32, "height": 84, "type": "door"},
            ])

    def test_zero_dimensions_raise(self):
        with pytest.raises(ValueError):
            elevation_view(0, 96)
        with pytest.raises(ValueError):
            elevation_view(120, 0)


class TestSectionGeometry:
    def test_basic(self):
        s = section_view(0, 96, 240)
        assert s["floor_line"] == ((0.0, 0.0), (240.0, 0.0))
        assert s["ceiling_line"] == ((0.0, 96.0), (240.0, 96.0))
        assert s["walls"] == []

    def test_walls_and_openings(self):
        s = section_view(0, 96, 240, elements=[
            {"type": "wall", "x": 0, "thickness": 6},
            {"type": "wall", "x": 240, "thickness": 6},
            {"type": "opening", "x": 120, "width": 36, "sill": 30, "head": 84, "kind": "window"},
        ])
        assert len(s["walls"]) == 2
        assert len(s["openings"]) == 1
        assert s["openings"][0]["kind"] == "window"

    def test_floor_slab(self):
        s = section_view(0, 96, 240, elements=[
            {"type": "floor_slab", "thickness": 6},
        ])
        assert len(s["slabs"]) == 1

    def test_ceiling_above_floor_required(self):
        with pytest.raises(ValueError):
            section_view(100, 50, 240)


# ---------------------------------------------------------------------------
# ezdxf primitive composites
# ---------------------------------------------------------------------------


class TestEzdxfPrimitives:
    def test_create_stair_straight(self, msp):
        info = create_stair(msp, 0, 0, 96, 0, width=36, num_treads=12)
        assert info["shape"] == "straight"
        assert info["num_treads"] == 12

    def test_create_stair_u(self, msp):
        info = create_stair(msp, 0, 0, 120, 0, width=36, num_treads=10,
                            shape="u", landing_depth=40)
        assert info["shape"] == "u"
        assert info["total_treads"] == 20

    def test_create_column(self, msp):
        info = create_column(msp, 0, 0, shape="round", size=12.0)
        assert info["shape"] == "round"
        assert info["radius"] == 6.0

    def test_create_slab(self, msp):
        info = create_slab(msp, [(0, 0), (100, 0), (100, 80), (0, 80)],
                            edge_thickness=4.0)
        assert info["area"] == 8000.0
        assert "inner" in info["handles"]

    def test_create_beam(self, msp):
        info = create_beam(msp, 0, 0, 100, 0, width=4)
        assert info["length"] == 100.0
        assert "edges" in info["handles"]

    def test_create_roof_plan(self, msp):
        info = create_roof_plan(
            msp, [(0, 0), (120, 0), (120, 80), (0, 80)],
            style="hip", overhang=12,
        )
        assert info["style"] == "hip"
        assert len(info["handles"]["hips"]) == 4

    def test_create_elevation(self, msp):
        info = create_elevation(
            msp, 0, 0, 120, 96,
            openings=[{"position": 30, "width": 32, "height": 84, "type": "door"}],
        )
        assert info["opening_count"] == 1

    def test_create_section(self, msp):
        info = create_section(
            msp, 0, 0, 240, 0, 96,
            elements=[{"type": "wall", "x": 120, "thickness": 6}],
        )
        assert info["section_width"] == 240


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_names_present(self):
        names = template_names()
        assert "residential_1br" in names
        assert "residential_2br" in names
        assert "residential_3br_house" in names
        assert "commercial_open_office" in names

    def test_unknown_template_raises(self, msp):
        with pytest.raises(ValueError):
            apply_template(msp, "skyscraper", 0, 0)

    def test_1br(self, msp):
        info = apply_template(msp, "residential_1br", 0, 0)
        names = {r["name"] for r in info["rooms"]}
        assert "LIVING/DINING" in names
        assert "BEDROOM" in names
        assert all(r["area"] > 0 for r in info["rooms"])

    def test_2br(self, msp):
        info = apply_template(msp, "residential_2br", 0, 0)
        names = {r["name"] for r in info["rooms"]}
        assert "BEDROOM 1" in names
        assert "BEDROOM 2" in names

    def test_3br_house(self, msp):
        info = apply_template(msp, "residential_3br_house", 0, 0)
        names = {r["name"] for r in info["rooms"]}
        assert "MASTER BEDROOM" in names
        assert len(info["rooms"]) >= 5

    def test_open_office(self, msp):
        info = apply_template(msp, "commercial_open_office", 0, 0)
        names = {r["name"] for r in info["rooms"]}
        assert "OPEN OFFICE" in names
        assert "CONFERENCE" in names

    def test_template_writes_to_doc(self, msp):
        before = len(msp)
        apply_template(msp, "residential_1br", 0, 0)
        after = len(msp)
        assert after > before


# ---------------------------------------------------------------------------
# LISP spec serializers
# ---------------------------------------------------------------------------


class TestLispRender:
    def test_serialize_stair_starts_with_polyline(self):
        s = stair_run(0, 0, 96, 0, width=36, num_treads=12)
        spec = serialize_stair(s)
        assert spec.startswith("@A-FLOR-STRS@P,1,4,")
        # One outline P, plus tread LINEs, plus arrow line, plus arrow_head P
        assert spec.count(";") == 1 + 11 + 1  # 11 treads + arrow line + arrow_head

    def test_serialize_stair_u_no_panes_or_text(self):
        u = stair_u_shape(0, 0, flight_length=96, width=36, num_treads_per_flight=10)
        spec = serialize_stair_u(u)
        assert "@A-FLOR-STRS@" in spec
        # No text-shaped tokens
        assert "@T," not in spec.replace(",T,", "")  # naive check

    def test_serialize_column_rect(self):
        spec = serialize_column(column_corners(0, 0, "square", 12.0))
        assert spec.startswith("@A-COLS@P,1,4,")

    def test_serialize_column_round(self):
        spec = serialize_column(column_corners(0, 0, "round", 12.0))
        assert spec.startswith("@A-COLS@C,")
        assert spec.endswith(",6.000000")

    def test_serialize_slab_with_inner(self):
        spec = serialize_slab(slab_outline(
            [(0, 0), (100, 0), (100, 80), (0, 80)], edge_thickness=4,
        ))
        assert spec.count(";") == 1  # outer + inner

    def test_serialize_beam_two_edges(self):
        spec = serialize_beam(beam_endpoints(0, 0, 100, 0, width=4))
        assert spec.count(";") == 1  # two LINEs

    def test_serialize_roof_layer_split(self):
        r = roof_plan(
            [(0, 0), (120, 0), (120, 80), (0, 80)], style="hip", overhang=12,
        )
        spec = serialize_roof(r, eaves_layer="A-ROOF-EAVES",
                               ridge_layer="A-ROOF-RIDG")
        assert "@A-ROOF-EAVES@P," in spec
        assert "@A-ROOF-RIDG@L," in spec

    def test_serialize_elevation_with_window(self):
        e = elevation_view(120, 96, openings=[
            {"position": 60, "width": 36, "height": 48, "sill": 30, "type": "window"},
        ])
        spec = serialize_elevation(e, origin_x=1000, origin_y=2000)
        # outline + floor_line + window rect + 2 panes = 5 tokens
        assert spec.count(";") == 4
        assert "@A-GLAZ@" in spec

    def test_serialize_section_includes_slab_layer(self):
        s = section_view(0, 96, 240, elements=[
            {"type": "floor_slab", "thickness": 6},
            {"type": "wall", "x": 120, "thickness": 6},
        ])
        spec = serialize_section(s, origin_x=0, origin_y=0)
        assert "@A-FLOR@" in spec
        assert "@A-WALL@" in spec

    def test_serialize_template_emits_multilayer_string(self):
        spec, info = serialize_template(None, "residential_1br", 0, 0)
        assert info["template"] == "residential_1br"
        assert "@A-WALL@" in spec
        assert ";" in spec


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEzdxfBackendArchOps:
    async def test_arch_stair_straight(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_stair(0, 0, 96, 0, width=36, num_treads=12)
        assert r.ok
        assert r.payload["shape"] == "straight"

    async def test_arch_stair_u(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_stair(0, 0, 120, 0, width=36, num_treads=10,
                                shape="u", landing_depth=40)
        assert r.ok
        assert r.payload["shape"] == "u"

    async def test_arch_stair_zero_length_invalid_arg(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_stair(0, 0, 0, 0, width=36, num_treads=12)
        assert not r.ok
        assert r.category == "invalid_arg"

    async def test_arch_column_round(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_column(0, 0, shape="round", size=12.0)
        assert r.ok
        assert r.payload["radius"] == 6.0

    async def test_arch_slab_area(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_slab(
            [[0, 0], [100, 0], [100, 80], [0, 80]], edge_thickness=4,
        )
        assert r.ok
        assert r.payload["area"] == 8000.0

    async def test_arch_beam_with_width(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_beam(0, 0, 100, 0, width=4.0)
        assert r.ok
        assert r.payload["length"] == 100.0

    async def test_arch_roof_hip(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_roof(
            [[0, 0], [120, 0], [120, 80], [0, 80]],
            style="hip", overhang=12,
        )
        assert r.ok
        assert r.payload["style"] == "hip"

    async def test_arch_elevation_with_window(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_elevation(
            0, 0, 120, 96,
            openings=[{"position": 60, "width": 36, "height": 48, "sill": 30, "type": "window"}],
        )
        assert r.ok
        assert r.payload["opening_count"] == 1

    async def test_arch_section_walls(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_section(
            0, 0, 240, 0, 96,
            elements=[
                {"type": "wall", "x": 0, "thickness": 6},
                {"type": "wall", "x": 240, "thickness": 6},
            ],
        )
        assert r.ok
        assert r.payload["section_width"] == 240

    async def test_arch_template_unknown_returns_not_found(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_template("skyscraper_500floors", 0, 0)
        assert not r.ok
        assert r.category == "not_found"

    async def test_arch_template_residential_2br(self):
        b = EzdxfBackend()
        await b.initialize()
        r = await b.arch_template("residential_2br", 0, 0)
        assert r.ok
        room_names = {rm["name"] for rm in r.payload["rooms"]}
        assert "BEDROOM 1" in room_names
        assert "BEDROOM 2" in room_names


# ---------------------------------------------------------------------------
# DWG write
# ---------------------------------------------------------------------------


class TestDWGWrite:
    def test_write_dwg_raises_without_converter(self, tmp_path, monkeypatch):
        from autocad_mcp.project import dwg_reader
        monkeypatch.setattr(dwg_reader, "find_accoreconsole", lambda: None)
        monkeypatch.setattr(dwg_reader, "find_oda_converter", lambda: None)

        doc = ezdxf.new("R2013")
        out = tmp_path / "test.dwg"
        with pytest.raises(dwg_reader.DWGConverterMissing):
            dwg_reader.write_dwg(doc, out)

    @pytest.mark.asyncio
    async def test_drawing_save_dwg_returns_backend_unavailable_without_converter(
        self, tmp_path, monkeypatch,
    ):
        from autocad_mcp.project import dwg_reader
        monkeypatch.setattr(dwg_reader, "find_accoreconsole", lambda: None)
        monkeypatch.setattr(dwg_reader, "find_oda_converter", lambda: None)

        b = EzdxfBackend()
        await b.initialize()
        out = tmp_path / "test.dwg"
        r = await b.drawing_save(str(out))
        assert not r.ok
        assert r.category == "backend_unavailable"


# ---------------------------------------------------------------------------
# MCP tool dispatch — server.py arch() function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestArchToolDispatch:
    """Drive the MCP arch tool directly to catch operation-string typos and
    parameter-mapping mistakes — same shape as the existing arch dispatch tests."""

    async def _fn(self):
        from autocad_mcp.server import arch
        return arch.fn if hasattr(arch, "fn") else arch

    async def _init_backend(self, monkeypatch):
        from autocad_mcp import client
        client._backend = None  # force re-init
        backend = EzdxfBackend()
        await backend.initialize()
        monkeypatch.setattr(client, "_backend", backend)
        monkeypatch.setattr(client, "get_backend", lambda: backend_awaitable(backend))
        return backend

    async def test_stair_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="stair", data={
            "x1": 0, "y1": 0, "x2": 96, "y2": 0,
            "width": 36, "num_treads": 12,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["shape"] == "straight"

    async def test_column_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="column", data={"x": 0, "y": 0,
                                                     "shape": "round", "size": 12})
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["radius"] == 6.0

    async def test_slab_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="slab", data={
            "boundary": [[0, 0], [100, 0], [100, 80], [0, 80]],
            "edge_thickness": 4.0,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["area"] == 8000.0

    async def test_beam_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="beam", data={
            "x1": 0, "y1": 0, "x2": 100, "y2": 0, "width": 4.0,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True

    async def test_roof_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="roof", data={
            "boundary": [[0, 0], [120, 0], [120, 80], [0, 80]],
            "style": "hip", "overhang": 12,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["style"] == "hip"

    async def test_elevation_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="elevation", data={
            "origin_x": 0, "origin_y": 0,
            "wall_length": 120, "height": 96,
            "openings": [{"position": 60, "width": 36, "height": 48,
                          "sill": 30, "type": "window"}],
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["opening_count"] == 1

    async def test_section_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="section", data={
            "origin_x": 0, "origin_y": 0,
            "section_width": 240, "floor_y": 0, "ceiling_y": 96,
            "elements": [{"type": "wall", "x": 120, "thickness": 6}],
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True

    async def test_template_dispatch(self, monkeypatch):
        await self._init_backend(monkeypatch)
        import json
        fn = await self._fn()
        result = await fn(operation="template", data={
            "name": "residential_1br", "x": 0, "y": 0,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["template"] == "residential_1br"


async def backend_awaitable(b):
    """Match client.get_backend's async signature for monkeypatching."""
    return b
