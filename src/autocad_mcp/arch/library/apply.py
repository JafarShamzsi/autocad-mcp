"""Apply an entity-spec list (from `symbols.py`) onto a block on each backend.

Three target shapes:

- `apply_to_ezdxf_block` — calls `add_line` / `add_circle` / ... on an ezdxf
  BlockLayout.
- `apply_to_com_block` — calls `AddLine` / `AddCircle` / ... on a COM block
  with the same VARIANT helpers used elsewhere in `com_backend.py`.
- `serialize_spec_for_lisp` — encodes the spec list as a compact pipe-style
  string that `lisp-code/library_tools.lsp` parses entity-by-entity. The
  file_ipc backend sends this to the dispatcher when it needs to define a
  library block on AutoCAD LT.

Entities are drawn on layer "0" so block inserts take on the insert's layer
(standard architectural convention).
"""

from __future__ import annotations

import math
from typing import Any


# ---------------------------------------------------------------------------
# ezdxf
# ---------------------------------------------------------------------------


def apply_to_ezdxf_block(block, spec_list: list[dict]) -> None:
    """Walk `spec_list` and emit primitives on the ezdxf BlockLayout.

    Unknown spec types raise ValueError — explicit failure beats silent skip
    when extending the library later.
    """
    for spec in spec_list:
        kind = spec.get("type")
        attribs = {"layer": "0"}
        if kind == "line":
            block.add_line(spec["start"], spec["end"], dxfattribs=attribs)
        elif kind == "circle":
            block.add_circle(spec["center"], spec["radius"], dxfattribs=attribs)
        elif kind == "arc":
            block.add_arc(
                center=spec["center"],
                radius=spec["radius"],
                start_angle=spec["start_angle"],
                end_angle=spec["end_angle"],
                dxfattribs=attribs,
            )
        elif kind == "polyline":
            block.add_lwpolyline(
                spec["points"],
                close=spec.get("closed", False),
                dxfattribs=attribs,
            )
        elif kind == "text":
            block.add_text(
                spec["text"],
                dxfattribs={
                    **attribs,
                    "insert": spec["insert"],
                    "height": spec.get("height", 2.5),
                },
            )
        else:
            raise ValueError(f"Unknown library entity spec type: {kind!r}")


# ---------------------------------------------------------------------------
# COM (AutoCAD IDispatch)
# ---------------------------------------------------------------------------


def apply_to_com_block(block, spec_list: list[dict], *, apoint, double_array) -> None:
    """Walk `spec_list` and emit primitives on a COM block.

    `apoint` and `double_array` are the same VARIANT helpers used elsewhere in
    `com_backend.py` (passed in to keep this module free of pywin32 imports
    on non-Windows systems).
    """
    for spec in spec_list:
        kind = spec.get("type")
        if kind == "line":
            sx, sy = spec["start"]
            ex, ey = spec["end"]
            ent = block.AddLine(apoint(sx, sy), apoint(ex, ey))
            ent.Layer = "0"
        elif kind == "circle":
            cx, cy = spec["center"]
            ent = block.AddCircle(apoint(cx, cy), float(spec["radius"]))
            ent.Layer = "0"
        elif kind == "arc":
            cx, cy = spec["center"]
            ent = block.AddArc(
                apoint(cx, cy),
                float(spec["radius"]),
                math.radians(float(spec["start_angle"])),
                math.radians(float(spec["end_angle"])),
            )
            ent.Layer = "0"
        elif kind == "polyline":
            flat: list[float] = []
            for pt in spec["points"]:
                flat.append(float(pt[0]))
                flat.append(float(pt[1]))
            ent = block.AddLightWeightPolyline(double_array(flat))
            ent.Closed = bool(spec.get("closed", False))
            ent.Layer = "0"
        elif kind == "text":
            ix, iy = spec["insert"]
            ent = block.AddText(
                str(spec["text"]),
                apoint(ix, iy),
                float(spec.get("height", 2.5)),
            )
            ent.Layer = "0"
        else:
            raise ValueError(f"Unknown library entity spec type: {kind!r}")


# ---------------------------------------------------------------------------
# LISP mini-language (for the file_ipc backend)
# ---------------------------------------------------------------------------


def _fmt(x: float) -> str:
    """Format a float as fixed-point decimal, no trailing zeros, no `e`-notation.

    Tiny near-zero floats from `math.cos(pi/2)*r` would otherwise serialize
    as `6.123e-17`, which AutoLISP's `atof` reads as 0 but produces
    confusing log output. Round-to-6-decimals then trim is unambiguous.
    """
    s = f"{x:.6f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def serialize_spec_for_lisp(spec_list: list[dict[str, Any]]) -> str:
    """Serialize an entity-spec list into the format the LISP renderer parses.

    Format: entities separated by `;`, fields by `,`.
    Per-entity layouts:

        L,sx,sy,ex,ey                              line
        C,cx,cy,r                                   circle
        A,cx,cy,r,start_deg,end_deg                arc
        P,closed,n,x1,y1,...,xn,yn                  polyline (closed = 0|1)
        T,ix,iy,height,text                         text — `text` is the
                                                    final field; must not
                                                    contain `,` or `;`.

    The matching parser is `mcp-lib-emit-entities` in
    `lisp-code/library_tools.lsp`. Float values are emitted via `_fmt` to
    avoid scientific notation that would lose precision through `atof`.
    """
    pieces: list[str] = []
    for spec in spec_list:
        kind = spec.get("type")
        if kind == "line":
            sx, sy = spec["start"]
            ex, ey = spec["end"]
            pieces.append(f"L,{_fmt(sx)},{_fmt(sy)},{_fmt(ex)},{_fmt(ey)}")
        elif kind == "circle":
            cx, cy = spec["center"]
            pieces.append(f"C,{_fmt(cx)},{_fmt(cy)},{_fmt(float(spec['radius']))}")
        elif kind == "arc":
            cx, cy = spec["center"]
            pieces.append(
                f"A,{_fmt(cx)},{_fmt(cy)},{_fmt(float(spec['radius']))},"
                f"{_fmt(float(spec['start_angle']))},{_fmt(float(spec['end_angle']))}"
            )
        elif kind == "polyline":
            pts = spec["points"]
            closed_flag = 1 if spec.get("closed") else 0
            flat = ",".join(f"{_fmt(x)},{_fmt(y)}" for x, y in pts)
            pieces.append(f"P,{closed_flag},{len(pts)},{flat}")
        elif kind == "text":
            ix, iy = spec["insert"]
            text = str(spec["text"])
            if "," in text or ";" in text:
                raise ValueError(
                    f"library symbol text {text!r} contains a reserved "
                    "delimiter (`,` or `;`); the LISP mini-language has no "
                    "escape mechanism yet"
                )
            pieces.append(
                f"T,{_fmt(ix)},{_fmt(iy)},{_fmt(float(spec.get('height', 2.5)))},{text}"
            )
        else:
            raise ValueError(f"Unknown library entity spec type: {kind!r}")
    return ";".join(pieces)
