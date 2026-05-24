"""Pure geometric helpers for architectural primitives.

No DXF or COM dependency — these functions compute the points/angles each
primitive needs, so the same math is reused by both the ezdxf and COM
backends.
"""

from __future__ import annotations

import math


Point = tuple[float, float]


def wall_corners(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float,
    justify: str = "center",
) -> tuple[list[Point], float]:
    """Compute the four corners of a wall rectangle along a centerline.

    Args:
        x1, y1, x2, y2: centerline endpoints.
        thickness: total wall thickness.
        justify: "center" / "left" / "right" relative to direction (x1,y1)→(x2,y2).

    Returns:
        (corners, length) — corners is a 4-element list of (x,y), CCW order.

    Raises:
        ValueError: zero-length centerline or invalid justify.
    """
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("Wall has zero length (start and end points coincide)")

    nx = -dy / length
    ny = dx / length

    if justify == "center":
        off_l = off_r = thickness / 2.0
    elif justify == "left":
        off_l, off_r = thickness, 0.0
    elif justify == "right":
        off_l, off_r = 0.0, thickness
    else:
        raise ValueError(f"justify must be 'center', 'left', or 'right'; got {justify!r}")

    p1 = (x1 + nx * off_l, y1 + ny * off_l)
    p2 = (x2 + nx * off_l, y2 + ny * off_l)
    p3 = (x2 - nx * off_r, y2 - ny * off_r)
    p4 = (x1 - nx * off_r, y1 - ny * off_r)
    return [p1, p2, p3, p4], length


def wall_segments_with_openings(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float,
    openings: list[dict],
    justify: str = "center",
) -> tuple[list[list[Point]], list[dict], float]:
    """Compute wall geometry for a wall containing one or more openings.

    The wall centerline runs from (x1, y1) to (x2, y2). Each opening occupies
    an interval [position - width/2, position + width/2] measured along the
    centerline from the start. The function splits the wall into the solid
    segments *between* openings, leaving gaps where doors and windows go.

    Args:
        x1, y1, x2, y2: centerline endpoints.
        thickness: total wall thickness.
        openings: list of dicts with keys:
            - "position" (float, required): centerline distance from (x1,y1)
            - "width" (float, required): opening width along the centerline
            - "type" (str, optional): "door" | "window" | other — passed through
            - any other keys are passed through to the output dict unchanged
        justify: "center" / "left" / "right" — see wall_corners().

    Returns:
        (segments, resolved_openings, total_length)
          segments  — list of 4-point corner lists, one per solid wall segment.
                      Empty list if openings cover the whole wall.
          resolved_openings — same opening dicts plus computed:
                      "centerline_start" (Point), "centerline_end" (Point),
                      "midpoint" (Point), "angle_deg" (float, CCW from +X).
          total_length — math.hypot of centerline.

    Raises:
        ValueError: zero-length wall, invalid justify, opening outside the
            wall, or openings overlap each other.
    """
    dx = x2 - x1
    dy = y2 - y1
    total_length = math.hypot(dx, dy)
    if total_length == 0:
        raise ValueError("Wall has zero length (start and end points coincide)")

    # Sort + validate openings along the centerline.
    sorted_openings = sorted(openings, key=lambda o: float(o["position"]))
    intervals: list[tuple[float, float, dict]] = []
    last_end = 0.0
    for o in sorted_openings:
        pos = float(o["position"])
        width = float(o["width"])
        if width <= 0:
            raise ValueError(f"Opening width must be > 0; got {width}")
        start = pos - width / 2.0
        end = pos + width / 2.0
        if start < 0 or end > total_length:
            raise ValueError(
                f"Opening at position={pos} width={width} extends outside the "
                f"wall (centerline length={total_length})"
            )
        if start < last_end:
            raise ValueError(
                f"Opening at position={pos} overlaps the previous opening "
                f"(starts at {start:.3f}, previous ended at {last_end:.3f})"
            )
        intervals.append((start, end, o))
        last_end = end

    # Direction + perpendicular unit vectors.
    ux = dx / total_length
    uy = dy / total_length
    angle_deg = math.degrees(math.atan2(dy, dx))

    nx = -uy
    ny = ux

    if justify == "center":
        off_l = off_r = thickness / 2.0
    elif justify == "left":
        off_l, off_r = thickness, 0.0
    elif justify == "right":
        off_l, off_r = 0.0, thickness
    else:
        raise ValueError(f"justify must be 'center', 'left', or 'right'; got {justify!r}")

    def _corners(seg_start: float, seg_end: float) -> list[Point]:
        s_x = x1 + ux * seg_start
        s_y = y1 + uy * seg_start
        e_x = x1 + ux * seg_end
        e_y = y1 + uy * seg_end
        return [
            (s_x + nx * off_l, s_y + ny * off_l),
            (e_x + nx * off_l, e_y + ny * off_l),
            (e_x - nx * off_r, e_y - ny * off_r),
            (s_x - nx * off_r, s_y - ny * off_r),
        ]

    # Build segments between openings.
    segments: list[list[Point]] = []
    cursor = 0.0
    for start, end, _ in intervals:
        if start > cursor:
            segments.append(_corners(cursor, start))
        cursor = end
    if cursor < total_length:
        segments.append(_corners(cursor, total_length))

    # Resolve each opening to its centerline coordinates.
    resolved: list[dict] = []
    for start, end, o in intervals:
        out = dict(o)
        out["centerline_start"] = (x1 + ux * start, y1 + uy * start)
        out["centerline_end"] = (x1 + ux * end, y1 + uy * end)
        midpoint_t = (start + end) / 2.0
        out["midpoint"] = (x1 + ux * midpoint_t, y1 + uy * midpoint_t)
        out["angle_deg"] = angle_deg
        resolved.append(out)

    return segments, resolved, total_length


def door_geometry(
    x: float,
    y: float,
    width: float,
    angle: float,
    swing: str,
    open_angle: float,
    room_side: tuple[float, float] | None = None,
) -> tuple[Point, Point, float, float, str]:
    """Compute door leaf endpoint + arc start/end angles + resolved swing.

    Args:
        x, y: hinge point.
        width: door width.
        angle: orientation of closed door, degrees CCW from +X.
        swing: "left" (CCW) or "right" (CW).
        open_angle: degrees the door is shown open.
        room_side: optional point inside the room. When given, `swing` is
            overridden — the door is auto-oriented to sweep toward this
            point. This matches the architectural convention "doors open
            into the room" and lets callers describe rooms semantically
            (a point) rather than geometrically (CCW/CW).

    Tiebreak rule: when `room_side` lies exactly on the closed-door axis
    (the cross-product = 0, within a small float tolerance), the function
    falls back to `swing` (the caller's value). This is documented so
    callers can pass an explicit fallback when the geometry is symmetric.

    Returns:
        ((hinge), (leaf_end), arc_start_deg, arc_end_deg, resolved_swing)
        — `resolved_swing` is whichever of "left" / "right" was actually
        used (after room_side override, if any).
    """
    if swing not in ("left", "right"):
        raise ValueError(f"swing must be 'left' or 'right'; got {swing!r}")

    if room_side is not None:
        # Decide which side of the closed-door line the room_side point lies on.
        # Closed door points along `angle` from the hinge.
        rad = math.radians(angle)
        dir_x = math.cos(rad)
        dir_y = math.sin(rad)
        # Vector from hinge to room_side
        rsx = room_side[0] - x
        rsy = room_side[1] - y
        # 2D cross product (dir × room_side): positive = left (CCW), negative = right (CW).
        cross = dir_x * rsy - dir_y * rsx
        # Tolerance covers float noise near zero. Anything within this band
        # falls through to the caller-supplied `swing` (documented tiebreak).
        if cross > 1e-9:
            swing = "left"
        elif cross < -1e-9:
            swing = "right"
        # Otherwise: collinear — keep the caller's `swing` as the deterministic
        # tiebreak. Documented in the docstring above.

    leaf_dir = angle + (open_angle if swing == "left" else -open_angle)
    leaf_rad = math.radians(leaf_dir)
    lx = x + width * math.cos(leaf_rad)
    ly = y + width * math.sin(leaf_rad)

    if swing == "left":
        arc_start, arc_end = angle, angle + open_angle
    else:
        arc_start, arc_end = angle - open_angle, angle

    # Normalize to [0, 360) so callers get the same range AutoCAD writes
    # to disk after a save+reload roundtrip.
    arc_start = arc_start % 360.0
    arc_end = arc_end % 360.0

    return (x, y), (lx, ly), arc_start, arc_end, swing


def window_corners(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    sill_thickness: float,
) -> tuple[list[Point], float]:
    """Compute the four corners of a window sill rectangle along a centerline.

    Returns:
        (corners, length) — corners is a 4-element list of (x,y).

    Raises:
        ValueError: zero-length window.
    """
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("Window has zero length (start and end points coincide)")

    nx = -dy / length
    ny = dx / length
    half = sill_thickness / 2.0

    p1 = (x1 + nx * half, y1 + ny * half)
    p2 = (x2 + nx * half, y2 + ny * half)
    p3 = (x2 - nx * half, y2 - ny * half)
    p4 = (x1 - nx * half, y1 - ny * half)
    return [p1, p2, p3, p4], length


def fillet_line_line(
    line1: tuple[Point, Point],
    line2: tuple[Point, Point],
    radius: float,
) -> dict:
    """Compute fillet geometry between two coplanar lines.

    Both lines are given as `((x1, y1), (x2, y2))`. The two lines must share
    an endpoint (the "corner") — typically `line1[1] == line2[0]`, but any
    matching endpoint pair works.

    Returns a dict:
        {
            "trim1": Point,            # new endpoint for line1 (replaces the corner end)
            "trim2": Point,            # new endpoint for line2 (replaces the corner end)
            "arc_center": Point,
            "arc_radius": float,
            "arc_start_deg": float,    # CCW from +X, AutoCAD convention
            "arc_end_deg": float,
            "corner": Point,           # original shared endpoint, for reference
        }

    Raises:
        ValueError: lines don't share an endpoint, are parallel, or the
            radius is too large for the meeting angle.
    """
    if radius <= 0:
        raise ValueError(f"Fillet radius must be > 0; got {radius}")

    corner, far1, far2 = _shared_corner(line1, line2)

    # Unit vectors from the corner pointing away from it along each line.
    d1 = _unit_vector(corner, far1)
    d2 = _unit_vector(corner, far2)

    # Angle between the two outgoing rays (in radians, 0..π).
    cos_theta = max(-1.0, min(1.0, d1[0] * d2[0] + d1[1] * d2[1]))
    theta = math.acos(cos_theta)
    if theta < 1e-9 or math.isclose(theta, math.pi, abs_tol=1e-9):
        raise ValueError("Lines are parallel or collinear — no fillet possible")

    half = theta / 2.0
    # Distance from corner to each tangent point along the lines.
    trim_dist = radius / math.tan(half)
    # Distance from corner to arc center along the bisector.
    center_dist = radius / math.sin(half)

    # Trim points along each ray.
    trim1 = (corner[0] + d1[0] * trim_dist, corner[1] + d1[1] * trim_dist)
    trim2 = (corner[0] + d2[0] * trim_dist, corner[1] + d2[1] * trim_dist)

    # Bisector direction (interior of the angle).
    bisector_x = d1[0] + d2[0]
    bisector_y = d1[1] + d2[1]
    bisector_len = math.hypot(bisector_x, bisector_y)
    if bisector_len < 1e-12:
        # Lines point in opposite directions — covered by parallel check above.
        raise ValueError("Bisector degenerate")
    bx = bisector_x / bisector_len
    by = bisector_y / bisector_len
    arc_center = (corner[0] + bx * center_dist, corner[1] + by * center_dist)

    # Arc start/end angles (vectors from arc center to each trim point).
    a1 = math.degrees(math.atan2(trim1[1] - arc_center[1], trim1[0] - arc_center[0]))
    a2 = math.degrees(math.atan2(trim2[1] - arc_center[1], trim2[0] - arc_center[0]))

    # AutoCAD arcs are CCW from start_angle to end_angle. Pick the order that
    # produces the *short* arc — the fillet should never sweep > π.
    start, end = _short_ccw_arc(a1, a2)

    return {
        "trim1": trim1,
        "trim2": trim2,
        "arc_center": arc_center,
        "arc_radius": radius,
        "arc_start_deg": start,
        "arc_end_deg": end,
        "corner": corner,
    }


def chamfer_line_line(
    line1: tuple[Point, Point],
    line2: tuple[Point, Point],
    dist1: float,
    dist2: float,
) -> dict:
    """Compute chamfer geometry between two coplanar lines that share an endpoint.

    Returns a dict with trim1 (new endpoint for line1), trim2 (new endpoint
    for line2), and corner (original shared endpoint). The chamfer itself is
    a straight line between trim1 and trim2.
    """
    if dist1 <= 0 or dist2 <= 0:
        raise ValueError(f"Chamfer distances must be > 0; got {dist1!r}, {dist2!r}")

    corner, far1, far2 = _shared_corner(line1, line2)
    d1 = _unit_vector(corner, far1)
    d2 = _unit_vector(corner, far2)

    # Parallel check (cross product magnitude ≈ 0)
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) < 1e-9:
        raise ValueError("Lines are parallel or collinear — no chamfer possible")

    trim1 = (corner[0] + d1[0] * dist1, corner[1] + d1[1] * dist1)
    trim2 = (corner[0] + d2[0] * dist2, corner[1] + d2[1] * dist2)
    return {"trim1": trim1, "trim2": trim2, "corner": corner}


# Tolerance used by `_shared_corner` to treat endpoints as coincident.
_ENDPOINT_TOLERANCE = 1e-6


def _shared_corner(
    line1: tuple[Point, Point],
    line2: tuple[Point, Point],
) -> tuple[Point, Point, Point]:
    """Find the shared endpoint of two lines and the two "far" endpoints.

    Returns (corner, far_on_line1, far_on_line2).
    Raises ValueError if no endpoint pair is within `_ENDPOINT_TOLERANCE`.
    """
    p1a, p1b = line1
    p2a, p2b = line2
    for a, far1 in ((p1a, p1b), (p1b, p1a)):
        for b, far2 in ((p2a, p2b), (p2b, p2a)):
            if math.hypot(a[0] - b[0], a[1] - b[1]) < _ENDPOINT_TOLERANCE:
                return a, far1, far2
    raise ValueError("Lines don't share an endpoint within tolerance")


def _unit_vector(p_from: Point, p_to: Point) -> Point:
    dx = p_to[0] - p_from[0]
    dy = p_to[1] - p_from[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        raise ValueError("Zero-length vector")
    return (dx / length, dy / length)


def _short_ccw_arc(a1_deg: float, a2_deg: float) -> tuple[float, float]:
    """Return (start, end) in degrees so the CCW sweep from start→end is
    the *short* arc between the two angles. Both outputs are normalized to
    `[0, 360)` to match AutoCAD's on-disk representation (so consumers can
    safely do `if arc.start_angle < 0` without false negatives after a
    save+reload normalization)."""
    a1 = a1_deg % 360.0
    a2 = a2_deg % 360.0
    sweep = (a2 - a1) % 360.0
    if sweep > 180.0:
        # Going the other way is shorter — swap start/end.
        return a2, a1
    return a1, a2


def polygon_area(pts: list[Point]) -> float:
    """Signed shoelace area (absolute value returned)."""
    n = len(pts)
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_centroid(pts: list[Point]) -> Point:
    """Centroid of a simple polygon via the shoelace-weighted formula.

    Falls back to the arithmetic mean for degenerate (collinear) polygons.
    """
    n = len(pts)
    cx = cy = a = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    a /= 2.0
    if a == 0:
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    return (cx / (6.0 * a), cy / (6.0 * a))


# ===========================================================================
# Building primitives — stair, column, slab, beam, roof, section, elevation
# ===========================================================================


def stair_run(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
    num_treads: int,
    tread_depth: float | None = None,
    direction_arrow: bool = True,
) -> dict:
    """Compute a single-flight (straight-run) stair laid along a centerline.

    The centerline runs from (x1,y1) (bottom of flight) to (x2,y2) (top).
    Treads are perpendicular line segments crossing the full width. The
    walking line (arrow) runs from bottom to top along the centerline.

    Args:
        x1, y1, x2, y2: centerline endpoints (bottom → top).
        width: stair width (perpendicular to centerline).
        num_treads: number of treads (visible plan-view risers = num_treads).
        tread_depth: optional override; if None, equals centerline_len / num_treads.
        direction_arrow: include the walking-line arrow.

    Returns:
        {
            "outline": [Point, Point, Point, Point],  # CCW rectangle
            "tread_lines": [((sx,sy),(ex,ey)), ...],  # one segment per riser
            "arrow":      [(start_x, start_y), (head_x, head_y)] | None,
            "arrow_head": [(p1_x,p1_y),(tip_x,tip_y),(p2_x,p2_y)] | None,
            "length":     float,
            "num_treads": int,
            "tread_depth": float,
            "width":      float,
        }

    Raises:
        ValueError: zero-length flight, non-positive width, or num_treads < 1.
    """
    if num_treads < 1:
        raise ValueError(f"num_treads must be >= 1; got {num_treads}")
    if width <= 0:
        raise ValueError(f"width must be > 0; got {width}")

    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("Stair has zero length (start and end points coincide)")

    ux = dx / length
    uy = dy / length
    nx = -uy
    ny = ux

    half_w = width / 2.0
    depth = float(tread_depth) if tread_depth is not None else length / num_treads

    outline = [
        (x1 + nx * half_w, y1 + ny * half_w),
        (x2 + nx * half_w, y2 + ny * half_w),
        (x2 - nx * half_w, y2 - ny * half_w),
        (x1 - nx * half_w, y1 - ny * half_w),
    ]

    tread_lines: list[tuple[Point, Point]] = []
    for i in range(1, num_treads):
        t = i * depth
        if t > length:
            break
        cx = x1 + ux * t
        cy = y1 + uy * t
        s = (cx + nx * half_w, cy + ny * half_w)
        e = (cx - nx * half_w, cy - ny * half_w)
        tread_lines.append((s, e))

    arrow: list[Point] | None = None
    arrow_head: list[Point] | None = None
    if direction_arrow:
        margin = min(depth * 0.4, length * 0.1)
        arrow_start = (x1 + ux * margin, y1 + uy * margin)
        arrow_end = (x2 - ux * margin, y2 - uy * margin)
        arrow = [arrow_start, arrow_end]
        head_len = min(depth * 0.6, length * 0.08)
        head_w = head_len * 0.5
        ax = arrow_end[0] - ux * head_len
        ay = arrow_end[1] - uy * head_len
        arrow_head = [
            (ax + nx * head_w, ay + ny * head_w),
            arrow_end,
            (ax - nx * head_w, ay - ny * head_w),
        ]

    return {
        "outline": outline,
        "tread_lines": tread_lines,
        "arrow": arrow,
        "arrow_head": arrow_head,
        "length": length,
        "num_treads": num_treads,
        "tread_depth": depth,
        "width": width,
    }


def stair_u_shape(
    x: float,
    y: float,
    flight_length: float,
    width: float,
    num_treads_per_flight: int,
    landing_depth: float | None = None,
    angle_deg: float = 0.0,
) -> dict:
    """Compute a U-shape stair: two parallel flights joined by a landing.

    Layout: insert at the bottom of the first flight; flights run in the
    +angle direction. Second flight runs back parallel, offset by width
    perpendicular (CCW side). Landing sits at the top of flight 1.

    Args:
        x, y: insertion point (bottom of flight 1).
        flight_length: length of each flight along the walk direction.
        width: stair width (each flight).
        num_treads_per_flight: integer count.
        landing_depth: depth of the landing along walk direction.
                       Defaults to `width` (square landing).
        angle_deg: rotation of the entire assembly (degrees, CCW from +X).

    Returns:
        {
            "flight1": stair_run(...) dict,
            "flight2": stair_run(...) dict (reverse direction),
            "landing_corners": [Point, Point, Point, Point],
            "total_treads": int,
            "footprint":     [Point, Point, Point, Point],  # outer outline
        }

    Raises:
        ValueError: invalid args.
    """
    if flight_length <= 0:
        raise ValueError(f"flight_length must be > 0; got {flight_length}")
    if width <= 0:
        raise ValueError(f"width must be > 0; got {width}")
    if num_treads_per_flight < 1:
        raise ValueError(f"num_treads_per_flight must be >= 1; got {num_treads_per_flight}")

    depth = float(landing_depth) if landing_depth is not None else width

    rad = math.radians(angle_deg)
    ux = math.cos(rad)
    uy = math.sin(rad)
    nx = -uy
    ny = ux

    # Flight 1: from (x,y) along +u
    f1_end_x = x + ux * flight_length
    f1_end_y = y + uy * flight_length
    flight1 = stair_run(x, y, f1_end_x, f1_end_y, width, num_treads_per_flight)

    # Landing: starts at the top of flight 1, extends `depth` along +u,
    # and spans 2*width perpendicular (covers both flights).
    landing_total_w = 2.0 * width
    half_lw = landing_total_w / 2.0
    l_back_cx = f1_end_x
    l_back_cy = f1_end_y
    l_front_cx = l_back_cx + ux * depth
    l_front_cy = l_back_cy + uy * depth

    landing_corners = [
        (l_back_cx + nx * half_lw, l_back_cy + ny * half_lw),
        (l_front_cx + nx * half_lw, l_front_cy + ny * half_lw),
        (l_front_cx - nx * half_lw, l_front_cy - ny * half_lw),
        (l_back_cx - nx * half_lw, l_back_cy - ny * half_lw),
    ]

    # Flight 2: starts at the landing's far side, runs back to base,
    # centerline offset by `width` perpendicular (CCW side = -nx*width).
    f2_start_x = l_front_cx - nx * width
    f2_start_y = l_front_cy - ny * width
    f2_end_x = x - nx * width
    f2_end_y = y - ny * width
    flight2 = stair_run(f2_start_x, f2_start_y, f2_end_x, f2_end_y, width, num_treads_per_flight)

    # Footprint: rectangle from (x - nx*width - perpendicular_half_band) to top corners.
    # Simpler: outer corners of the two flights + landing.
    base_w_half = width
    half_outer = 1.5 * width  # spans both flights' outer edges from centerline of the pair
    # Centerline of the *pair* of flights is offset -nx*width/2 from flight1.
    mid_cx = x - nx * (width / 2.0)
    mid_cy = y - ny * (width / 2.0)
    far_mid_cx = mid_cx + ux * (flight_length + depth)
    far_mid_cy = mid_cy + uy * (flight_length + depth)
    footprint = [
        (mid_cx + nx * width, mid_cy + ny * width),       # base CCW corner
        (far_mid_cx + nx * width, far_mid_cy + ny * width),  # top CCW corner
        (far_mid_cx - nx * width, far_mid_cy - ny * width),  # top CW corner
        (mid_cx - nx * width, mid_cy - ny * width),       # base CW corner
    ]

    return {
        "flight1": flight1,
        "flight2": flight2,
        "landing_corners": landing_corners,
        "total_treads": 2 * num_treads_per_flight,
        "footprint": footprint,
    }


def column_corners(
    x: float,
    y: float,
    shape: str,
    size: float,
    size_y: float | None = None,
    angle_deg: float = 0.0,
) -> dict:
    """Compute a column footprint at (x, y).

    Args:
        x, y: column center.
        shape: 'rect' or 'square' (returns corner polygon) or 'round'
               (returns center + radius for circle entity).
        size: square side length, or rect width, or round diameter.
        size_y: only for rect — height (perpendicular to width). Required
                when shape='rect'.
        angle_deg: rotation for rect/square shapes.

    Returns:
        For rect/square: {"shape": "rect", "corners": [Point]*4,
                          "center": (x,y), "size_x": w, "size_y": h, "angle": deg}
        For round:       {"shape": "round", "center": (x,y), "radius": float}

    Raises:
        ValueError: invalid shape or missing size_y for rect.
    """
    if shape in ("round", "circle"):
        return {
            "shape": "round",
            "center": (x, y),
            "radius": size / 2.0,
        }
    if shape in ("square",):
        size_x = size_y = size
    elif shape in ("rect", "rectangle"):
        if size_y is None:
            raise ValueError("shape='rect' requires size_y")
        size_x = size
    else:
        raise ValueError(f"shape must be 'square', 'rect', or 'round'; got {shape!r}")

    hx = size_x / 2.0
    hy = size_y / 2.0
    rad = math.radians(angle_deg)
    c = math.cos(rad)
    s = math.sin(rad)
    local = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    corners = [(x + lx * c - ly * s, y + lx * s + ly * c) for lx, ly in local]
    return {
        "shape": "rect",
        "corners": corners,
        "center": (x, y),
        "size_x": size_x,
        "size_y": size_y,
        "angle": angle_deg,
    }


def slab_outline(
    boundary: list[Point],
    edge_thickness: float = 0.0,
) -> dict:
    """Compute a slab/floor outline. Optionally adds an inner offset to
    represent slab edge thickness in plan view.

    Args:
        boundary: list of (x,y) points defining the outer outline (>=3).
        edge_thickness: inset for a second (inner) polyline. 0 = no inner outline.

    Returns:
        {
            "outer": [Point, ...],
            "inner": [Point, ...] | None,
            "area":  float,
            "centroid": Point,
        }

    Notes:
        For a non-convex polygon, the offset can self-intersect. Callers
        should keep `edge_thickness` small relative to the smallest dimension.
    """
    if len(boundary) < 3:
        raise ValueError(f"slab boundary needs >= 3 points; got {len(boundary)}")
    outer = [(p[0], p[1]) for p in boundary]
    area = polygon_area(outer)
    cx, cy = polygon_centroid(outer)
    inner = None
    if edge_thickness > 0:
        inner = _inset_polygon(outer, edge_thickness)
    return {
        "outer": outer,
        "inner": inner,
        "area": area,
        "centroid": (cx, cy),
    }


def beam_endpoints(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float = 0.0,
) -> dict:
    """Compute a beam projected onto a plan.

    Args:
        x1, y1, x2, y2: beam centerline (typically drawn HIDDEN above plane).
        width: if 0, return a single centerline; if > 0, return a pair of
               parallel lines offset by width/2 each side.

    Returns:
        {
            "centerline": ((x1,y1), (x2,y2)),
            "edges":      [((s1x,s1y),(e1x,e1y)), ((s2x,s2y),(e2x,e2y))] | None,
            "length":     float,
        }
    """
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("Beam has zero length")
    if width <= 0:
        return {
            "centerline": ((x1, y1), (x2, y2)),
            "edges": None,
            "length": length,
        }
    nx = -dy / length
    ny = dx / length
    half = width / 2.0
    edge_a = ((x1 + nx * half, y1 + ny * half), (x2 + nx * half, y2 + ny * half))
    edge_b = ((x1 - nx * half, y1 - ny * half), (x2 - nx * half, y2 - ny * half))
    return {
        "centerline": ((x1, y1), (x2, y2)),
        "edges": [edge_a, edge_b],
        "length": length,
    }


def roof_plan(
    boundary: list[Point],
    style: str = "gable",
    ridge_axis: str = "x",
    overhang: float = 12.0,
) -> dict:
    """Compute roof-plan lines (eaves outline + ridge + hip lines).

    Args:
        boundary: building footprint (will be offset outward by `overhang`).
        style: 'gable' (single ridge along ridge_axis) or 'hip' (four ridges
               meeting near the center) or 'flat' (no ridge lines).
        ridge_axis: 'x' or 'y' — orientation of the gable ridge.
        overhang: outward offset of the eave line from the footprint.

    Returns:
        {
            "footprint": [Point, ...],
            "eaves":     [Point, ...],  # offset polygon (closed)
            "ridges":    [((sx,sy),(ex,ey)), ...],
            "hips":      [((sx,sy),(ex,ey)), ...],  # diagonals for 'hip'
            "style":     str,
        }
    """
    if len(boundary) < 3:
        raise ValueError(f"roof boundary needs >= 3 points; got {len(boundary)}")
    footprint = [(p[0], p[1]) for p in boundary]

    eaves = _inset_polygon(footprint, -abs(overhang)) if overhang > 0 else list(footprint)

    xs = [p[0] for p in eaves]
    ys = [p[1] for p in eaves]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    mid_x = (min_x + max_x) / 2.0
    mid_y = (min_y + max_y) / 2.0

    ridges: list[tuple[Point, Point]] = []
    hips: list[tuple[Point, Point]] = []

    if style == "flat":
        pass
    elif style == "gable":
        if ridge_axis == "x":
            ridges.append(((min_x, mid_y), (max_x, mid_y)))
        elif ridge_axis == "y":
            ridges.append(((mid_x, min_y), (mid_x, max_y)))
        else:
            raise ValueError(f"ridge_axis must be 'x' or 'y'; got {ridge_axis!r}")
    elif style == "hip":
        width_x = max_x - min_x
        width_y = max_y - min_y
        if width_x >= width_y:
            half = width_y / 2.0
            ridges.append(((min_x + half, mid_y), (max_x - half, mid_y)))
            hips.append(((min_x, min_y), (min_x + half, mid_y)))
            hips.append(((min_x, max_y), (min_x + half, mid_y)))
            hips.append(((max_x, min_y), (max_x - half, mid_y)))
            hips.append(((max_x, max_y), (max_x - half, mid_y)))
        else:
            half = width_x / 2.0
            ridges.append(((mid_x, min_y + half), (mid_x, max_y - half)))
            hips.append(((min_x, min_y), (mid_x, min_y + half)))
            hips.append(((max_x, min_y), (mid_x, min_y + half)))
            hips.append(((min_x, max_y), (mid_x, max_y - half)))
            hips.append(((max_x, max_y), (mid_x, max_y - half)))
    else:
        raise ValueError(f"style must be 'gable', 'hip', or 'flat'; got {style!r}")

    return {
        "footprint": footprint,
        "eaves": eaves,
        "ridges": ridges,
        "hips": hips,
        "style": style,
    }


def elevation_view(
    wall_length: float,
    height: float,
    floor_y: float = 0.0,
    openings: list[dict] | None = None,
) -> dict:
    """Compute a 2D elevation view of a single wall with openings.

    Coordinate system: x = horizontal position along the wall (0..wall_length),
    y = vertical (floor_y..floor_y+height). Openings are rectangles cut into
    the wall.

    Args:
        wall_length: horizontal extent.
        height: top of the wall above floor.
        floor_y: vertical position of the floor line.
        openings: each dict has:
            - "position" (float, required): horizontal center along wall
            - "width" (float, required)
            - "height" (float, required)
            - "sill"  (float, optional, default 0): vertical position of opening bottom
            - "type"  (str, optional): 'door' (default) or 'window'

    Returns:
        {
            "outline": [Point, Point, Point, Point],  # CCW wall rectangle
            "floor_line": (Point, Point),
            "openings": [
                {"corners": [Point]*4, "type": str, "midpoint": Point, "panes": [Line]?},
                ...
            ],
        }
    """
    if wall_length <= 0:
        raise ValueError(f"wall_length must be > 0; got {wall_length}")
    if height <= 0:
        raise ValueError(f"height must be > 0; got {height}")
    top = floor_y + height
    outline = [
        (0.0, floor_y), (wall_length, floor_y),
        (wall_length, top), (0.0, top),
    ]
    floor_line = ((0.0, floor_y), (wall_length, floor_y))

    resolved: list[dict] = []
    for o in (openings or []):
        pos = float(o["position"])
        w = float(o["width"])
        h = float(o["height"])
        sill = float(o.get("sill", 0.0))
        kind = o.get("type", "door")
        x0 = pos - w / 2.0
        x1 = pos + w / 2.0
        y0 = floor_y + sill
        y1 = y0 + h
        if x0 < 0 or x1 > wall_length:
            raise ValueError(
                f"Opening at position={pos} width={w} extends outside the wall"
            )
        if y1 > top:
            raise ValueError(f"Opening at sill={sill} height={h} extends above the wall top")
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        entry: dict = {
            "corners": corners,
            "type": kind,
            "midpoint": (pos, (y0 + y1) / 2.0),
        }
        if kind == "window":
            mid_x = pos
            mid_y = (y0 + y1) / 2.0
            entry["panes"] = [
                ((x0, mid_y), (x1, mid_y)),
                ((mid_x, y0), (mid_x, y1)),
            ]
        resolved.append(entry)

    return {
        "outline": outline,
        "floor_line": floor_line,
        "openings": resolved,
    }


def section_view(
    floor_y: float,
    ceiling_y: float,
    section_width: float,
    elements: list[dict] | None = None,
) -> dict:
    """Compute a 2D section view between a floor and ceiling line.

    Each element in `elements` is one of:
        {"type": "wall", "x": float, "thickness": float}       # vertical wall
        {"type": "opening", "x": float, "width": float, "sill": float, "head": float, "kind": "door"|"window"}
        {"type": "floor_slab", "thickness": float}             # below floor_y
        {"type": "roof_slab", "thickness": float, "pitch": float}  # above ceiling_y

    Args:
        floor_y: y-coordinate of the floor surface.
        ceiling_y: y-coordinate of the ceiling/roof underside.
        section_width: horizontal extent (x: 0..section_width).
        elements: structural elements to project.

    Returns:
        {
            "floor_line": (Point, Point),
            "ceiling_line": (Point, Point),
            "walls":  [[Point]*4, ...],
            "openings": [{"corners": [Point]*4, "kind": str}, ...],
            "slabs":  [[Point]*4, ...],
        }
    """
    if ceiling_y <= floor_y:
        raise ValueError(f"ceiling_y ({ceiling_y}) must be > floor_y ({floor_y})")
    if section_width <= 0:
        raise ValueError(f"section_width must be > 0; got {section_width}")

    floor_line = ((0.0, floor_y), (section_width, floor_y))
    ceiling_line = ((0.0, ceiling_y), (section_width, ceiling_y))
    walls: list[list[Point]] = []
    openings_out: list[dict] = []
    slabs: list[list[Point]] = []

    for el in (elements or []):
        kind = el.get("type")
        if kind == "wall":
            x = float(el["x"])
            t = float(el["thickness"])
            walls.append([
                (x - t / 2.0, floor_y),
                (x + t / 2.0, floor_y),
                (x + t / 2.0, ceiling_y),
                (x - t / 2.0, ceiling_y),
            ])
        elif kind == "opening":
            x = float(el["x"])
            w = float(el["width"])
            sill = float(el.get("sill", 0.0))
            head = float(el.get("head", ceiling_y - floor_y))
            y0 = floor_y + sill
            y1 = floor_y + head
            openings_out.append({
                "corners": [
                    (x - w / 2.0, y0),
                    (x + w / 2.0, y0),
                    (x + w / 2.0, y1),
                    (x - w / 2.0, y1),
                ],
                "kind": el.get("kind", "door"),
            })
        elif kind == "floor_slab":
            t = float(el["thickness"])
            slabs.append([
                (0.0, floor_y - t), (section_width, floor_y - t),
                (section_width, floor_y), (0.0, floor_y),
            ])
        elif kind == "roof_slab":
            t = float(el["thickness"])
            slabs.append([
                (0.0, ceiling_y), (section_width, ceiling_y),
                (section_width, ceiling_y + t), (0.0, ceiling_y + t),
            ])

    return {
        "floor_line": floor_line,
        "ceiling_line": ceiling_line,
        "walls": walls,
        "openings": openings_out,
        "slabs": slabs,
    }


def _inset_polygon(pts: list[Point], distance: float) -> list[Point]:
    """Offset a simple polygon by `distance` (positive = inward, negative = outward).

    Per-edge perpendicular offset, segment-intersected at each corner.
    Uses winding direction (signed area) to pick the inward-normal sign:
    a CCW polygon (signed area > 0) gets its left-of-edge normal as inward;
    a CW polygon's right-of-edge normal is inward. This means callers don't
    have to think about orientation — positive `distance` always shrinks
    the polygon's enclosed area.

    Falls back to the input on a degenerate corner. Not topologically
    robust for self-intersecting offsets — callers must keep `distance`
    small relative to the polygon's smallest dimension.
    """
    n = len(pts)
    if n < 3:
        return list(pts)

    signed_area = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        signed_area += x1 * y2 - x2 * y1
    sign = 1.0 if signed_area >= 0 else -1.0

    offset_edges: list[tuple[Point, Point]] = []
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length == 0:
            return list(pts)
        nx = -dy / length * sign
        ny = dx / length * sign
        offset_edges.append((
            (x1 + nx * distance, y1 + ny * distance),
            (x2 + nx * distance, y2 + ny * distance),
        ))

    result: list[Point] = []
    for i in range(n):
        prev = offset_edges[(i - 1) % n]
        curr = offset_edges[i]
        intersect = _line_line_intersection(prev, curr)
        result.append(intersect or curr[0])
    return result


def _line_line_intersection(
    line1: tuple[Point, Point],
    line2: tuple[Point, Point],
) -> Point | None:
    """Infinite-line intersection. Returns None if parallel."""
    (x1, y1), (x2, y2) = line1
    (x3, y3), (x4, y4) = line2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
