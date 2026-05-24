"""Layer-tagged entity serializer for the file_ipc (LT) backend.

Takes the geometry-helper output (stair_run, column_corners, slab_outline,
beam_endpoints, roof_plan, elevation_view, section_view) and produces the
compact spec string that `lisp-code/arch_tools.lsp::mcp-arch-emit-entities`
parses + draws.

Spec mini-language (entities separated by `;`, fields by `,`):
    @<layer>@L,sx,sy,ex,ey                LINE
    @<layer>@C,cx,cy,r                     CIRCLE
    @<layer>@A,cx,cy,r,start_deg,end_deg  ARC
    @<layer>@P,closed,n,x1,y1,...,xn,yn   PLINE (closed = 0 | 1)
    @<layer>@T,ix,iy,height,text          TEXT

Numbers are formatted with six decimal places so the round-trip through
LISP's `atof` is loss-tolerant for typical building dimensions.
"""

from __future__ import annotations

import math
from typing import Any, Iterable


def _fmt(n: float) -> str:
    """Compact-ish 6-decimal float for the spec string."""
    return f"{float(n):.6f}"


def _line(layer: str, p1: tuple[float, float], p2: tuple[float, float]) -> str:
    return f"@{layer}@L,{_fmt(p1[0])},{_fmt(p1[1])},{_fmt(p2[0])},{_fmt(p2[1])}"


def _circle(layer: str, c: tuple[float, float], r: float) -> str:
    return f"@{layer}@C,{_fmt(c[0])},{_fmt(c[1])},{_fmt(r)}"


def _arc(
    layer: str, c: tuple[float, float], r: float, start_deg: float, end_deg: float,
) -> str:
    return (
        f"@{layer}@A,{_fmt(c[0])},{_fmt(c[1])},{_fmt(r)},"
        f"{_fmt(start_deg)},{_fmt(end_deg)}"
    )


def _polyline(layer: str, pts: list[tuple[float, float]], closed: bool) -> str:
    flat = ",".join(f"{_fmt(p[0])},{_fmt(p[1])}" for p in pts)
    return f"@{layer}@P,{1 if closed else 0},{len(pts)},{flat}"


def _text(
    layer: str, insert: tuple[float, float], height: float, text: str,
) -> str:
    if "," in text or ";" in text:
        raise ValueError(
            f"Text field cannot contain ',' or ';' in arch_tools spec: {text!r}"
        )
    return (
        f"@{layer}@T,{_fmt(insert[0])},{_fmt(insert[1])},{_fmt(height)},{text}"
    )


# ---------------------------------------------------------------------------
# Per-primitive serializers
# ---------------------------------------------------------------------------


def serialize_stair(
    stair: dict,
    layer: str = "A-FLOR-STRS",
) -> str:
    """Serialize a stair_run() result."""
    parts: list[str] = [_polyline(layer, stair["outline"], closed=True)]
    for sp, ep in stair["tread_lines"]:
        parts.append(_line(layer, sp, ep))
    if stair.get("arrow") is not None:
        parts.append(_line(layer, stair["arrow"][0], stair["arrow"][1]))
    if stair.get("arrow_head") is not None:
        parts.append(_polyline(layer, stair["arrow_head"], closed=False))
    return ";".join(parts)


def serialize_stair_u(
    u: dict,
    layer: str = "A-FLOR-STRS",
) -> str:
    """Serialize a stair_u_shape() result (both flights + landing)."""
    parts: list[str] = []
    for flight_key in ("flight1", "flight2"):
        flight = u[flight_key]
        parts.append(_polyline(layer, flight["outline"], closed=True))
        for sp, ep in flight["tread_lines"]:
            parts.append(_line(layer, sp, ep))
        if flight.get("arrow") is not None:
            parts.append(_line(layer, flight["arrow"][0], flight["arrow"][1]))
        if flight.get("arrow_head") is not None:
            parts.append(_polyline(layer, flight["arrow_head"], closed=False))
    parts.append(_polyline(layer, u["landing_corners"], closed=True))
    return ";".join(parts)


def serialize_column(spec: dict, layer: str = "A-COLS") -> str:
    """Serialize a column_corners() result."""
    if spec["shape"] == "rect":
        return _polyline(layer, spec["corners"], closed=True)
    return _circle(layer, spec["center"], spec["radius"])


def serialize_slab(spec: dict, layer: str = "A-FLOR") -> str:
    """Serialize a slab_outline() result."""
    parts: list[str] = [_polyline(layer, spec["outer"], closed=True)]
    if spec.get("inner"):
        parts.append(_polyline(layer, spec["inner"], closed=True))
    return ";".join(parts)


def serialize_beam(spec: dict, layer: str = "A-WALL-HEAD") -> str:
    """Serialize a beam_endpoints() result."""
    if spec["edges"] is None:
        return _line(layer, spec["centerline"][0], spec["centerline"][1])
    parts: list[str] = []
    for sp, ep in spec["edges"]:
        parts.append(_line(layer, sp, ep))
    return ";".join(parts)


def serialize_roof(
    spec: dict,
    eaves_layer: str = "A-ROOF",
    ridge_layer: str = "A-ROOF",
) -> str:
    """Serialize a roof_plan() result."""
    parts: list[str] = [_polyline(eaves_layer, spec["eaves"], closed=True)]
    for sp, ep in spec["ridges"]:
        parts.append(_line(ridge_layer, sp, ep))
    for sp, ep in spec["hips"]:
        parts.append(_line(ridge_layer, sp, ep))
    return ";".join(parts)


def serialize_elevation(
    spec: dict,
    origin_x: float,
    origin_y: float,
    wall_layer: str = "A-WALL",
    door_layer: str = "A-DOOR",
    window_layer: str = "A-GLAZ",
) -> str:
    """Serialize an elevation_view() result; shifted by (origin_x, origin_y)."""
    def shift(p: tuple[float, float]) -> tuple[float, float]:
        return (p[0] + origin_x, p[1] + origin_y)

    parts: list[str] = [
        _polyline(wall_layer, [shift(p) for p in spec["outline"]], closed=True),
        _line(wall_layer, shift(spec["floor_line"][0]), shift(spec["floor_line"][1])),
    ]
    for o in spec["openings"]:
        target = window_layer if o["type"] == "window" else door_layer
        parts.append(_polyline(target, [shift(p) for p in o["corners"]], closed=True))
        for ps, pe in o.get("panes", []):
            parts.append(_line(target, shift(ps), shift(pe)))
    return ";".join(parts)


def serialize_section(
    spec: dict,
    origin_x: float,
    origin_y: float,
    wall_layer: str = "A-WALL",
    slab_layer: str = "A-FLOR",
    door_layer: str = "A-DOOR",
    window_layer: str = "A-GLAZ",
) -> str:
    """Serialize a section_view() result; shifted by (origin_x, origin_y)."""
    def shift(p: tuple[float, float]) -> tuple[float, float]:
        return (p[0] + origin_x, p[1] + origin_y)

    parts: list[str] = [
        _line(slab_layer, shift(spec["floor_line"][0]), shift(spec["floor_line"][1])),
        _line(slab_layer, shift(spec["ceiling_line"][0]), shift(spec["ceiling_line"][1])),
    ]
    for corners in spec["walls"]:
        parts.append(_polyline(wall_layer, [shift(p) for p in corners], closed=True))
    for o in spec["openings"]:
        target = window_layer if o["kind"] == "window" else door_layer
        parts.append(_polyline(target, [shift(p) for p in o["corners"]], closed=True))
    for corners in spec["slabs"]:
        parts.append(_polyline(slab_layer, [shift(p) for p in corners], closed=True))
    return ";".join(parts)


def serialize_template(
    msp,  # ezdxf BaseLayout
    template_name: str,
    x: float,
    y: float,
    params: dict | None = None,
) -> tuple[str, dict]:
    """Run a template on a temp ezdxf doc, then walk the modelspace and
    encode every entity as a layer-tagged spec.

    Returns (spec_string, info_dict). The caller (file_ipc backend) ships
    the spec to LISP via arch-emit; the info dict (template name, rooms,
    footprint) goes back to the MCP caller as the response payload.
    """
    import ezdxf as _ezdxf
    from autocad_mcp.arch.templates import TEMPLATES, apply_template

    if template_name not in TEMPLATES:
        raise ValueError(
            f"Unknown template {template_name!r}. Available: {sorted(TEMPLATES.keys())}"
        )

    if msp is None:
        tmp_doc = _ezdxf.new("R2013")
        tmp_msp = tmp_doc.modelspace()
    else:
        tmp_msp = msp

    info = apply_template(tmp_msp, template_name, x, y, params or {})
    parts: list[str] = []
    for ent in tmp_msp:
        etype = ent.dxftype()
        layer = ent.dxf.layer or "0"
        if etype == "LINE":
            parts.append(_line(
                layer,
                (ent.dxf.start[0], ent.dxf.start[1]),
                (ent.dxf.end[0], ent.dxf.end[1]),
            ))
        elif etype == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in ent.get_points()]
            parts.append(_polyline(layer, pts, closed=bool(ent.closed)))
        elif etype == "ARC":
            parts.append(_arc(
                layer,
                (ent.dxf.center[0], ent.dxf.center[1]),
                float(ent.dxf.radius),
                float(ent.dxf.start_angle),
                float(ent.dxf.end_angle),
            ))
        elif etype == "CIRCLE":
            parts.append(_circle(
                layer,
                (ent.dxf.center[0], ent.dxf.center[1]),
                float(ent.dxf.radius),
            ))
        elif etype == "MTEXT":
            text = (ent.text or "").replace(",", " ").replace(";", " ").replace("\\P", " ")
            parts.append(_text(
                layer,
                (ent.dxf.insert[0], ent.dxf.insert[1]),
                float(ent.dxf.char_height),
                text,
            ))
        elif etype == "TEXT":
            text = (ent.dxf.text or "").replace(",", " ").replace(";", " ")
            parts.append(_text(
                layer,
                (ent.dxf.insert[0], ent.dxf.insert[1]),
                float(ent.dxf.height),
                text,
            ))
    spec = ";".join(parts)
    return spec, {
        "template": info["template"],
        "rooms": info["rooms"],
        "footprint": info["footprint"],
    }
