"""Composite architectural primitives — wall, door, window, room, dim chain.

Backed by ezdxf. Geometry math lives in `arch.geometry` so the same logic
can be reused by the COM backend without duplication.

Each function operates on an ezdxf modelspace (or any layout) and returns a
dict with the handles of created entities so the caller can address them
later (modify / erase / annotate).
"""

from __future__ import annotations

import math
from typing import Any

import ezdxf
import structlog
from ezdxf.layouts import BaseLayout

from autocad_mcp.arch.geometry import (
    beam_endpoints,
    column_corners,
    door_geometry,
    elevation_view,
    polygon_area,
    polygon_centroid,
    roof_plan,
    section_view,
    slab_outline,
    stair_run,
    stair_u_shape,
    wall_corners,
    wall_segments_with_openings,
    window_corners,
)
from autocad_mcp.arch.layers import setup_aia_layers

log = structlog.get_logger()


def _ensure_layer(doc: ezdxf.document.Drawing, name: str) -> None:
    """Best-effort layer creation if a layer is referenced before AIA setup."""
    if name not in doc.layers:
        try:
            setup_aia_layers(doc)
        except Exception as ex:
            # AIA setup is a best-effort optimization; on failure we still
            # fall through to a single-layer add below.
            log.debug("aia_layers_setup_failed", error=str(ex))
        if name not in doc.layers:
            doc.layers.add(name)


def create_wall(
    msp: BaseLayout,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float = 6.0,
    layer: str = "A-WALL",
    justify: str = "center",
) -> dict[str, Any]:
    """Draw a double-line wall as a closed rectangle along the centerline."""
    _ensure_layer(msp.doc, layer)
    corners, length = wall_corners(x1, y1, x2, y2, thickness, justify)
    pline = msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": layer})
    return {
        "handle": pline.dxf.handle,
        "layer": layer,
        "length": length,
        "thickness": thickness,
        "justify": justify,
        "centerline": [(x1, y1), (x2, y2)],
    }


def create_wall_with_openings(
    msp: BaseLayout,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float = 6.0,
    openings: list[dict] | None = None,
    layer: str = "A-WALL",
    door_layer: str = "A-DOOR",
    window_layer: str = "A-GLAZ",
    justify: str = "center",
) -> dict[str, Any]:
    """Draw a wall with door / window openings cut into it.

    The wall centerline runs from (x1,y1) to (x2,y2). Each entry in
    `openings` is a dict with keys:
        position (float, required): centerline distance from (x1,y1)
        width    (float, required): opening width
        type     (str, optional):   "door" (default) or "window"
        # For doors:
        swing       (str, optional):   "left" (default) or "right"
        open_angle  (float, optional): degrees the door is shown open (default 90)
        # For windows:
        sill_thickness (float, optional): defaults to the wall thickness

    Returns a dict with the segment polyline handles, plus per-opening
    handle dicts (leaf/arc for doors; sill/glass for windows).
    """
    _ensure_layer(msp.doc, layer)
    _ensure_layer(msp.doc, door_layer)
    _ensure_layer(msp.doc, window_layer)

    openings = openings or []
    segments, resolved, total_length = wall_segments_with_openings(
        x1, y1, x2, y2, thickness, openings, justify,
    )

    segment_handles: list[str] = []
    for corners in segments:
        pline = msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": layer})
        segment_handles.append(pline.dxf.handle)

    door_handles: list[dict[str, str]] = []
    window_handles: list[dict[str, str]] = []

    for o in resolved:
        kind = o.get("type", "door")
        mx, my = o["midpoint"]
        cs_x, cs_y = o["centerline_start"]
        ce_x, ce_y = o["centerline_end"]
        angle_deg = o["angle_deg"]

        if kind == "door":
            # Hinge sits at the opening's centerline-start. From the hinge,
            # the door leaf swings out an arc of radius = opening width.
            door_info = create_door(
                msp,
                cs_x, cs_y,
                width=float(o["width"]),
                angle=angle_deg,
                swing=o.get("swing", "left"),
                open_angle=float(o.get("open_angle", 90.0)),
                layer=door_layer,
            )
            door_handles.append(door_info["handles"])
        elif kind == "window":
            sill_t = float(o.get("sill_thickness", thickness))
            win_info = create_window(
                msp,
                cs_x, cs_y, ce_x, ce_y,
                sill_thickness=sill_t,
                layer=window_layer,
            )
            window_handles.append(win_info["handles"])
        else:
            # Unknown opening type — leave gap; do not draw a symbol.
            pass

    return {
        "handles": {
            "segments": segment_handles,
            "doors": door_handles,
            "windows": window_handles,
        },
        "layer": layer,
        "length": total_length,
        "thickness": thickness,
        "segment_count": len(segment_handles),
        "opening_count": len(resolved),
        "centerline": [(x1, y1), (x2, y2)],
    }


def create_door(
    msp: BaseLayout,
    x: float,
    y: float,
    width: float = 36.0,
    angle: float = 0.0,
    swing: str = "left",
    open_angle: float = 90.0,
    layer: str = "A-DOOR",
    room_side: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Draw a door symbol: leaf line + swing arc at hinge.

    When `room_side` is given (a point inside the room), the `swing` arg is
    overridden so the door opens toward that point.
    """
    _ensure_layer(msp.doc, layer)
    hinge, leaf_end, arc_start, arc_end, resolved_swing = door_geometry(
        x, y, width, angle, swing, open_angle, room_side=room_side
    )

    leaf = msp.add_line(hinge, leaf_end, dxfattribs={"layer": layer})
    arc = msp.add_arc(
        center=hinge,
        radius=width,
        start_angle=arc_start,
        end_angle=arc_end,
        dxfattribs={"layer": layer},
    )

    return {
        "handles": {"leaf": leaf.dxf.handle, "arc": arc.dxf.handle},
        "layer": layer,
        "width": width,
        "hinge": hinge,
        "angle": angle,
        "swing": resolved_swing,
    }


def create_window(
    msp: BaseLayout,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    sill_thickness: float = 6.0,
    layer: str = "A-GLAZ",
) -> dict[str, Any]:
    """Draw a window: outer sill rectangle + center glass line."""
    _ensure_layer(msp.doc, layer)
    corners, length = window_corners(x1, y1, x2, y2, sill_thickness)

    sill = msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": layer})
    glass = msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})

    return {
        "handles": {"sill": sill.dxf.handle, "glass": glass.dxf.handle},
        "layer": layer,
        "width": length,
        "endpoints": [(x1, y1), (x2, y2)],
    }


def create_room(
    msp: BaseLayout,
    boundary: list[tuple[float, float]],
    name: str = "",
    area_layer: str = "A-AREA",
    text_layer: str = "A-AREA-IDEN",
    text_height: float = 6.0,
) -> dict[str, Any]:
    """Draw a room boundary polyline + name/area label at the centroid."""
    if len(boundary) < 3:
        raise ValueError(f"Room boundary needs at least 3 points; got {len(boundary)}")

    _ensure_layer(msp.doc, area_layer)
    _ensure_layer(msp.doc, text_layer)

    pts = [(p[0], p[1]) for p in boundary]
    pline = msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": area_layer})

    area = polygon_area(pts)
    cx, cy = polygon_centroid(pts)

    # Area suffix + conversion follow the drawing's $INSUNITS. Imperial
    # drawings still get "SF"; metric drawings get "m²" with the right
    # divisor. Pure unitless drawings keep the historic raw-number form.
    from autocad_mcp.arch.units import UnitSystem
    units = UnitSystem.from_doc(msp.doc)
    area_str = units.format_area(area)
    label_text = f"{name}\\P{area_str}" if name else area_str
    label = msp.add_mtext(
        label_text,
        dxfattribs={
            "insert": (cx, cy),
            "char_height": text_height,
            "attachment_point": 5,  # MIDDLE_CENTER
            "layer": text_layer,
        },
    )

    return {
        "handles": {"boundary": pline.dxf.handle, "label": label.dxf.handle},
        "layer": area_layer,
        "area": area,
        "centroid": (cx, cy),
        "name": name,
    }


def create_dimension_chain(
    msp: BaseLayout,
    points: list[tuple[float, float]],
    offset: float = 24.0,
    direction: str = "horizontal",
    layer: str = "A-ANNO-DIMS",
) -> dict[str, Any]:
    """Draw a chain of consecutive linear dimensions along a row of points."""
    if len(points) < 2:
        raise ValueError(f"Need ≥2 points for a dimension chain; got {len(points)}")

    _ensure_layer(msp.doc, layer)

    handles: list[str] = []
    for p1, p2 in zip(points, points[1:]):
        if direction == "horizontal":
            base_y = max(p1[1], p2[1]) + offset
            base = ((p1[0] + p2[0]) / 2.0, base_y)
            dim = msp.add_linear_dim(
                base=base, p1=p1, p2=p2, angle=0,
                dxfattribs={"layer": layer},
            )
        elif direction == "vertical":
            base_x = max(p1[0], p2[0]) + offset
            base = (base_x, (p1[1] + p2[1]) / 2.0)
            dim = msp.add_linear_dim(
                base=base, p1=p1, p2=p2, angle=90,
                dxfattribs={"layer": layer},
            )
        elif direction == "aligned":
            dim = msp.add_aligned_dim(
                p1=p1, p2=p2, distance=offset,
                dxfattribs={"layer": layer},
            )
        else:
            raise ValueError(
                f"direction must be 'horizontal', 'vertical', or 'aligned'; got {direction!r}"
            )
        dim.render()
        # ezdxf's add_*_dim returns DimStyleOverride; the entity is at .dimension
        handles.append(dim.dimension.dxf.handle)

    return {"handles": handles, "layer": layer, "count": len(handles)}


# ===========================================================================
# Building primitives — stair, column, slab, beam, roof, elevation, section
# ===========================================================================


def create_stair(
    msp: BaseLayout,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float = 36.0,
    num_treads: int = 12,
    tread_depth: float | None = None,
    layer: str = "A-FLOR-STRS",
    shape: str = "straight",
    landing_depth: float | None = None,
) -> dict[str, Any]:
    """Draw a stair (straight-run or U-shape).

    shape='straight' uses (x1,y1)→(x2,y2) as the centerline.
    shape='u' treats (x1,y1) as the insertion (bottom of flight 1) and
    (x2,y2) as the top of flight 1 — flight length = distance, width is
    given separately, and the second flight runs back parallel on the CCW
    side. Use `landing_depth` to control the landing size.
    """
    _ensure_layer(msp.doc, layer)
    if shape == "straight":
        s = stair_run(x1, y1, x2, y2, width, num_treads, tread_depth=tread_depth)
        outline = msp.add_lwpolyline(s["outline"], close=True, dxfattribs={"layer": layer})
        tread_handles: list[str] = []
        for sp, ep in s["tread_lines"]:
            line = msp.add_line(sp, ep, dxfattribs={"layer": layer})
            tread_handles.append(line.dxf.handle)
        arrow_handles: list[str] = []
        if s["arrow"] is not None:
            arrow_line = msp.add_line(s["arrow"][0], s["arrow"][1], dxfattribs={"layer": layer})
            arrow_handles.append(arrow_line.dxf.handle)
        if s["arrow_head"] is not None:
            head = msp.add_lwpolyline(s["arrow_head"], close=False, dxfattribs={"layer": layer})
            arrow_handles.append(head.dxf.handle)
        return {
            "handles": {
                "outline": outline.dxf.handle,
                "treads": tread_handles,
                "arrow": arrow_handles,
            },
            "layer": layer,
            "shape": "straight",
            "length": s["length"],
            "num_treads": s["num_treads"],
            "tread_depth": s["tread_depth"],
            "width": s["width"],
        }
    if shape in ("u", "u_shape"):
        dx = x2 - x1
        dy = y2 - y1
        flight_length = math.hypot(dx, dy)
        if flight_length == 0:
            raise ValueError("U-shape stair: (x1,y1) and (x2,y2) coincide")
        angle = math.degrees(math.atan2(dy, dx))
        u = stair_u_shape(
            x1, y1,
            flight_length=flight_length,
            width=width,
            num_treads_per_flight=num_treads,
            landing_depth=landing_depth,
            angle_deg=angle,
        )
        out_handles: dict = {"flight1": {}, "flight2": {}, "landing": None}
        for flight_key in ("flight1", "flight2"):
            flight = u[flight_key]
            outline = msp.add_lwpolyline(
                flight["outline"], close=True, dxfattribs={"layer": layer}
            )
            treads = []
            for sp, ep in flight["tread_lines"]:
                line = msp.add_line(sp, ep, dxfattribs={"layer": layer})
                treads.append(line.dxf.handle)
            arrow_handles: list[str] = []
            if flight["arrow"] is not None:
                line = msp.add_line(flight["arrow"][0], flight["arrow"][1], dxfattribs={"layer": layer})
                arrow_handles.append(line.dxf.handle)
            if flight["arrow_head"] is not None:
                head = msp.add_lwpolyline(flight["arrow_head"], close=False, dxfattribs={"layer": layer})
                arrow_handles.append(head.dxf.handle)
            out_handles[flight_key] = {
                "outline": outline.dxf.handle,
                "treads": treads,
                "arrow": arrow_handles,
            }
        landing = msp.add_lwpolyline(u["landing_corners"], close=True, dxfattribs={"layer": layer})
        out_handles["landing"] = landing.dxf.handle
        return {
            "handles": out_handles,
            "layer": layer,
            "shape": "u",
            "total_treads": u["total_treads"],
            "width": width,
        }
    raise ValueError(f"shape must be 'straight' or 'u'; got {shape!r}")


def create_column(
    msp: BaseLayout,
    x: float,
    y: float,
    shape: str = "square",
    size: float = 12.0,
    size_y: float | None = None,
    angle: float = 0.0,
    layer: str = "A-COLS",
) -> dict[str, Any]:
    """Draw a structural column at (x, y).

    shape: 'square' / 'rect' (rectangular) or 'round' (circle).
    """
    _ensure_layer(msp.doc, layer)
    spec = column_corners(x, y, shape, size, size_y=size_y, angle_deg=angle)
    if spec["shape"] == "rect":
        outline = msp.add_lwpolyline(
            spec["corners"], close=True, dxfattribs={"layer": layer}
        )
        return {
            "handles": {"outline": outline.dxf.handle},
            "layer": layer,
            "shape": spec["shape"],
            "center": spec["center"],
            "size_x": spec["size_x"],
            "size_y": spec["size_y"],
            "angle": spec["angle"],
        }
    # round
    circ = msp.add_circle(spec["center"], spec["radius"], dxfattribs={"layer": layer})
    return {
        "handles": {"outline": circ.dxf.handle},
        "layer": layer,
        "shape": "round",
        "center": spec["center"],
        "radius": spec["radius"],
    }


def create_slab(
    msp: BaseLayout,
    boundary: list[tuple[float, float]],
    edge_thickness: float = 0.0,
    layer: str = "A-FLOR",
) -> dict[str, Any]:
    """Draw a floor/slab outline. Optional inner polygon for edge thickness."""
    _ensure_layer(msp.doc, layer)
    s = slab_outline(boundary, edge_thickness=edge_thickness)
    outer = msp.add_lwpolyline(s["outer"], close=True, dxfattribs={"layer": layer})
    handles: dict[str, Any] = {"outer": outer.dxf.handle}
    if s["inner"] is not None:
        inner = msp.add_lwpolyline(s["inner"], close=True, dxfattribs={"layer": layer})
        handles["inner"] = inner.dxf.handle
    return {
        "handles": handles,
        "layer": layer,
        "area": s["area"],
        "centroid": s["centroid"],
    }


def create_beam(
    msp: BaseLayout,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float = 0.0,
    layer: str = "A-WALL-HEAD",
) -> dict[str, Any]:
    """Draw an overhead beam (HIDDEN by default via A-WALL-HEAD layer).

    width=0 → single centerline; width>0 → two parallel edges.
    """
    _ensure_layer(msp.doc, layer)
    b = beam_endpoints(x1, y1, x2, y2, width=width)
    handles: dict[str, Any] = {}
    if b["edges"] is None:
        line = msp.add_line(b["centerline"][0], b["centerline"][1], dxfattribs={"layer": layer})
        handles["centerline"] = line.dxf.handle
    else:
        edges: list[str] = []
        for s, e in b["edges"]:
            line = msp.add_line(s, e, dxfattribs={"layer": layer})
            edges.append(line.dxf.handle)
        handles["edges"] = edges
    return {
        "handles": handles,
        "layer": layer,
        "length": b["length"],
        "width": width,
    }


def create_roof_plan(
    msp: BaseLayout,
    boundary: list[tuple[float, float]],
    style: str = "gable",
    ridge_axis: str = "x",
    overhang: float = 12.0,
    eaves_layer: str = "A-ROOF",
    ridge_layer: str = "A-ROOF",
) -> dict[str, Any]:
    """Draw a roof plan: eaves polygon + ridge (and hip) lines."""
    _ensure_layer(msp.doc, eaves_layer)
    _ensure_layer(msp.doc, ridge_layer)
    r = roof_plan(boundary, style=style, ridge_axis=ridge_axis, overhang=overhang)
    eaves = msp.add_lwpolyline(r["eaves"], close=True, dxfattribs={"layer": eaves_layer})
    ridges: list[str] = []
    for s, e in r["ridges"]:
        line = msp.add_line(s, e, dxfattribs={"layer": ridge_layer})
        ridges.append(line.dxf.handle)
    hips: list[str] = []
    for s, e in r["hips"]:
        line = msp.add_line(s, e, dxfattribs={"layer": ridge_layer})
        hips.append(line.dxf.handle)
    return {
        "handles": {"eaves": eaves.dxf.handle, "ridges": ridges, "hips": hips},
        "eaves_layer": eaves_layer,
        "ridge_layer": ridge_layer,
        "style": r["style"],
        "footprint": r["footprint"],
    }


def create_elevation(
    msp: BaseLayout,
    origin_x: float,
    origin_y: float,
    wall_length: float,
    height: float,
    openings: list[dict] | None = None,
    wall_layer: str = "A-WALL",
    door_layer: str = "A-DOOR",
    window_layer: str = "A-GLAZ",
) -> dict[str, Any]:
    """Draw a 2D elevation view of a single wall with openings.

    Origin is the bottom-left corner of the wall on the elevation sheet.
    """
    _ensure_layer(msp.doc, wall_layer)
    _ensure_layer(msp.doc, door_layer)
    _ensure_layer(msp.doc, window_layer)
    e = elevation_view(
        wall_length=wall_length,
        height=height,
        floor_y=0.0,
        openings=openings,
    )

    def _shift(p):
        return (p[0] + origin_x, p[1] + origin_y)

    outline_pts = [_shift(p) for p in e["outline"]]
    outline = msp.add_lwpolyline(outline_pts, close=True, dxfattribs={"layer": wall_layer})

    floor_p1 = _shift(e["floor_line"][0])
    floor_p2 = _shift(e["floor_line"][1])
    floor_line = msp.add_line(floor_p1, floor_p2, dxfattribs={"layer": wall_layer})

    opening_handles: list[dict] = []
    for o in e["openings"]:
        corners_shifted = [_shift(p) for p in o["corners"]]
        target_layer = window_layer if o["type"] == "window" else door_layer
        rect = msp.add_lwpolyline(corners_shifted, close=True, dxfattribs={"layer": target_layer})
        entry: dict = {"corners": rect.dxf.handle, "type": o["type"]}
        if "panes" in o:
            pane_handles: list[str] = []
            for ps, pe in o["panes"]:
                pl = msp.add_line(_shift(ps), _shift(pe), dxfattribs={"layer": target_layer})
                pane_handles.append(pl.dxf.handle)
            entry["panes"] = pane_handles
        opening_handles.append(entry)

    return {
        "handles": {
            "outline": outline.dxf.handle,
            "floor_line": floor_line.dxf.handle,
            "openings": opening_handles,
        },
        "wall_layer": wall_layer,
        "wall_length": wall_length,
        "height": height,
        "opening_count": len(opening_handles),
    }


def create_section(
    msp: BaseLayout,
    origin_x: float,
    origin_y: float,
    section_width: float,
    floor_y: float,
    ceiling_y: float,
    elements: list[dict] | None = None,
    wall_layer: str = "A-WALL",
    slab_layer: str = "A-FLOR",
    door_layer: str = "A-DOOR",
    window_layer: str = "A-GLAZ",
) -> dict[str, Any]:
    """Draw a 2D building section."""
    _ensure_layer(msp.doc, wall_layer)
    _ensure_layer(msp.doc, slab_layer)
    _ensure_layer(msp.doc, door_layer)
    _ensure_layer(msp.doc, window_layer)

    s = section_view(
        floor_y=floor_y,
        ceiling_y=ceiling_y,
        section_width=section_width,
        elements=elements,
    )

    def _shift(p):
        return (p[0] + origin_x, p[1] + origin_y)

    fp1 = _shift(s["floor_line"][0])
    fp2 = _shift(s["floor_line"][1])
    floor_line = msp.add_line(fp1, fp2, dxfattribs={"layer": slab_layer})
    cp1 = _shift(s["ceiling_line"][0])
    cp2 = _shift(s["ceiling_line"][1])
    ceiling_line = msp.add_line(cp1, cp2, dxfattribs={"layer": slab_layer})

    wall_handles: list[str] = []
    for corners in s["walls"]:
        shifted = [_shift(p) for p in corners]
        pl = msp.add_lwpolyline(shifted, close=True, dxfattribs={"layer": wall_layer})
        wall_handles.append(pl.dxf.handle)

    opening_handles: list[dict] = []
    for o in s["openings"]:
        shifted = [_shift(p) for p in o["corners"]]
        target = window_layer if o["kind"] == "window" else door_layer
        pl = msp.add_lwpolyline(shifted, close=True, dxfattribs={"layer": target})
        opening_handles.append({"corners": pl.dxf.handle, "kind": o["kind"]})

    slab_handles: list[str] = []
    for corners in s["slabs"]:
        shifted = [_shift(p) for p in corners]
        pl = msp.add_lwpolyline(shifted, close=True, dxfattribs={"layer": slab_layer})
        slab_handles.append(pl.dxf.handle)

    return {
        "handles": {
            "floor_line": floor_line.dxf.handle,
            "ceiling_line": ceiling_line.dxf.handle,
            "walls": wall_handles,
            "openings": opening_handles,
            "slabs": slab_handles,
        },
        "wall_layer": wall_layer,
        "floor_y": floor_y,
        "ceiling_y": ceiling_y,
        "section_width": section_width,
    }
