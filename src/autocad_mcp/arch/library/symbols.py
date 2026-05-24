"""Starter AEC block library.

Each block is a list of entity-specs (plain dicts). Entities are drawn on
layer "0" so the user controls color/linetype via the block insert's layer.
Real-world dimensions in inches (matches the rest of the toolkit; metric
users can scale at insert time).

License: the symbols defined in this module are original work by the
autocad-mcp project, dedicated to the public domain under CC0 1.0
Universal — https://creativecommons.org/publicdomain/zero/1.0/. Copy,
modify, redistribute freely.

Supported entity spec types:
    {"type": "line",     "start": (x, y), "end": (x, y)}
    {"type": "circle",   "center": (x, y), "radius": float}
    {"type": "arc",      "center": (x, y), "radius": float,
                          "start_angle": deg, "end_angle": deg}
    {"type": "polyline", "points": [(x, y), ...], "closed": bool}
    {"type": "text",     "insert": (x, y), "text": str, "height": float}

Symbol naming convention: <DISCIPLINE>-<KIND>[-<VARIANT>] where DISCIPLINE
is the AIA discipline code (A/S/M/E/P/etc., plus PLMB / ELEC / ARCH for
common subcategories). Names are stable — once published, don't rename.
"""

from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


# ARCH-NORTH-ARROW: north-pointing arrowhead inside a circle.
# Inserted at the arrow's pivot; "up" in the block frame is +Y so users
# rotate via the insert's rotation angle to face true north.
def _north_arrow_spec() -> list[dict]:
    r = 6.0  # outer circle radius (inches)
    return [
        {"type": "circle", "center": (0.0, 0.0), "radius": r},
        # Arrowhead (triangle pointing up)
        {"type": "polyline", "closed": True, "points": [
            (0.0, r - 0.5),
            (-r * 0.35, -r + 1.5),
            (0.0, -r * 0.3),
            (r * 0.35, -r + 1.5),
        ]},
        # "N" letter above the circle
        {"type": "text", "insert": (-1.2, r + 1.0), "text": "N", "height": 2.5},
    ]


# ARCH-SCALE-BAR-10FT: graphic scale bar showing 0..10 ft in 2-ft increments.
# 1 unit = 1 foot here (scale bars are drawn in the same units as the
# drawing). Total length = 10.
def _scale_bar_10ft_spec() -> list[dict]:
    bar_height = 0.25
    segments = 5
    seg_length = 2.0
    items: list[dict] = []
    # Alternating filled/empty boxes
    for i in range(segments):
        x0 = i * seg_length
        x1 = (i + 1) * seg_length
        items.append({"type": "polyline", "closed": True, "points": [
            (x0, 0.0), (x1, 0.0), (x1, bar_height), (x0, bar_height),
        ]})
    # Tick marks + labels at each segment boundary (0, 2, 4, 6, 8, 10)
    for i in range(segments + 1):
        x = i * seg_length
        items.append({"type": "line", "start": (x, -0.2), "end": (x, bar_height + 0.2)})
        items.append({"type": "text", "insert": (x - 0.2, bar_height + 0.5),
                      "text": str(i * 2), "height": 0.5})
    items.append({"type": "text", "insert": (segments * seg_length + 0.5, 0.0),
                  "text": "FT", "height": 0.5})
    return items


# PLMB-TOILET: plan-view toilet. Standard "elongated" toilet bowl ~30" long.
# Origin at the center of the tank's back wall (so it inserts against a wall).
def _toilet_spec() -> list[dict]:
    tank_w = 18.0
    tank_d = 8.0
    bowl_w = 14.0
    bowl_len = 22.0
    items: list[dict] = []
    # Tank: rectangle, tank back at y=0, projects forward (negative y)
    items.append({"type": "polyline", "closed": True, "points": [
        (-tank_w / 2.0, 0.0), (tank_w / 2.0, 0.0),
        (tank_w / 2.0, -tank_d), (-tank_w / 2.0, -tank_d),
    ]})
    # Bowl: ellipse approximated as a closed polyline (oval).
    # Center at (0, -tank_d - bowl_len/2)
    cy = -tank_d - bowl_len / 2.0
    bowl_pts: list[tuple[float, float]] = []
    for i in range(32):
        theta = 2.0 * math.pi * i / 32.0
        bowl_pts.append((
            (bowl_w / 2.0) * math.cos(theta),
            cy + (bowl_len / 2.0) * math.sin(theta),
        ))
    items.append({"type": "polyline", "closed": True, "points": bowl_pts})
    return items


# PLMB-SINK: plan-view bathroom sink. Square basin with a smaller circle
# for the drain. ~20" wide. Origin at back-center against the wall.
def _sink_spec() -> list[dict]:
    w = 20.0
    d = 16.0
    items: list[dict] = []
    # Outer rim
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Inner basin (inset 2" all around)
    inset = 2.0
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0 + inset, -inset), (w / 2.0 - inset, -inset),
        (w / 2.0 - inset, -d + inset), (-w / 2.0 + inset, -d + inset),
    ]})
    # Drain circle in the center
    items.append({"type": "circle", "center": (0.0, -d / 2.0), "radius": 0.75})
    return items


# ELEC-OUTLET-DUPLEX: 110V duplex outlet symbol.
# Convention: a small circle on the wall with two short slots indicating
# "duplex". Origin on the wall side; circle protrudes outward.
def _outlet_duplex_spec() -> list[dict]:
    r = 1.5
    items: list[dict] = []
    # Outer circle
    items.append({"type": "circle", "center": (0.0, r), "radius": r})
    # Two short parallel "slots" inside, vertical
    slot_h = 1.0
    slot_dx = 0.4
    items.append({"type": "line", "start": (-slot_dx, r - slot_h / 2.0),
                  "end":   (-slot_dx, r + slot_h / 2.0)})
    items.append({"type": "line", "start": (slot_dx, r - slot_h / 2.0),
                  "end":   (slot_dx, r + slot_h / 2.0)})
    return items


# PLMB-BATHTUB: 60" x 30" alcove bathtub. Origin at one inside corner so
# the long side aligns with a wall.
def _bathtub_spec() -> list[dict]:
    w = 60.0
    d = 30.0
    wall_thk = 2.0  # bath surround edge
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (0.0, 0.0), (w, 0.0), (w, -d), (0.0, -d),
    ]})
    # Inside tub (rounded by approximation with corner offset)
    items.append({"type": "polyline", "closed": True, "points": [
        (wall_thk, -wall_thk), (w - wall_thk, -wall_thk),
        (w - wall_thk, -(d - wall_thk)), (wall_thk, -(d - wall_thk)),
    ]})
    # Drain
    items.append({"type": "circle", "center": (w - 6.0, -d / 2.0), "radius": 1.0})
    return items


# PLMB-SHOWER: 36" x 36" square shower stall.
def _shower_spec() -> list[dict]:
    s = 36.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (0.0, 0.0), (s, 0.0), (s, -s), (0.0, -s),
    ]})
    # Drain in center
    items.append({"type": "circle", "center": (s / 2.0, -s / 2.0), "radius": 2.0})
    # Diagonal indicating drain slope
    items.append({"type": "line", "start": (4.0, -4.0), "end": (s - 4.0, -(s - 4.0))})
    items.append({"type": "line", "start": (s - 4.0, -4.0), "end": (4.0, -(s - 4.0))})
    return items


# PLMB-KITCHEN-SINK: 33" x 22" double-bowl kitchen sink.
def _kitchen_sink_spec() -> list[dict]:
    w = 33.0
    d = 22.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    inset = 2.0
    bowl_w = (w - 3.0 * inset) / 2.0
    # Left bowl
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0 + inset, -inset),
        (-w / 2.0 + inset + bowl_w, -inset),
        (-w / 2.0 + inset + bowl_w, -(d - inset)),
        (-w / 2.0 + inset, -(d - inset)),
    ]})
    # Right bowl
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0 + 2 * inset + bowl_w, -inset),
        (w / 2.0 - inset, -inset),
        (w / 2.0 - inset, -(d - inset)),
        (-w / 2.0 + 2 * inset + bowl_w, -(d - inset)),
    ]})
    items.append({"type": "circle", "center": (-(bowl_w / 2.0 + inset / 2.0), -d / 2.0), "radius": 0.75})
    items.append({"type": "circle", "center": (bowl_w / 2.0 + inset / 2.0, -d / 2.0), "radius": 0.75})
    return items


# APPL-RANGE: 30" stove / range. Origin at back-center against wall.
def _range_spec() -> list[dict]:
    w = 30.0
    d = 25.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Four burners (front-left, front-right, back-left, back-right)
    burner_r = 4.0
    burner_back_y = -7.0
    burner_front_y = -18.0
    burner_dx = 9.0
    for bx in (-burner_dx, burner_dx):
        for by in (burner_back_y, burner_front_y):
            items.append({"type": "circle", "center": (bx, by), "radius": burner_r})
    return items


# APPL-REFRIGERATOR: 36" wide standard fridge. Origin at back-center.
def _refrigerator_spec() -> list[dict]:
    w = 36.0
    d = 30.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Door swing arc (single door, hinge on left)
    items.append({"type": "arc", "center": (-w / 2.0, -d),
                  "radius": w, "start_angle": 270, "end_angle": 360})
    # Door swing line
    items.append({"type": "line", "start": (-w / 2.0, -d), "end": (w / 2.0, -d)})
    return items


# APPL-DISHWASHER: 24" undercounter dishwasher. Origin at back-center.
def _dishwasher_spec() -> list[dict]:
    w = 24.0
    d = 24.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # "DW" text on the front
    items.append({"type": "text", "insert": (-3.0, -d / 2.0 - 1.0), "text": "DW", "height": 3.0})
    return items


# FURN-BED-QUEEN: 60" x 80" queen bed. Origin at headboard center.
def _bed_queen_spec() -> list[dict]:
    w = 60.0
    d = 80.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Headboard line (slightly thicker visual)
    items.append({"type": "line", "start": (-w / 2.0, -2.0), "end": (w / 2.0, -2.0)})
    # Pillows: two rectangles at the headboard end
    pw = w / 2.0 - 4.0
    items.append({"type": "polyline", "closed": True, "points": [
        (-pw, -3.0), (-2.0, -3.0), (-2.0, -12.0), (-pw, -12.0),
    ]})
    items.append({"type": "polyline", "closed": True, "points": [
        (2.0, -3.0), (pw, -3.0), (pw, -12.0), (2.0, -12.0),
    ]})
    return items


# FURN-BED-KING: 76" x 80" king bed.
def _bed_king_spec() -> list[dict]:
    w = 76.0
    d = 80.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    items.append({"type": "line", "start": (-w / 2.0, -2.0), "end": (w / 2.0, -2.0)})
    pw = w / 2.0 - 4.0
    items.append({"type": "polyline", "closed": True, "points": [
        (-pw, -3.0), (-2.0, -3.0), (-2.0, -12.0), (-pw, -12.0),
    ]})
    items.append({"type": "polyline", "closed": True, "points": [
        (2.0, -3.0), (pw, -3.0), (pw, -12.0), (2.0, -12.0),
    ]})
    return items


# FURN-DRESSER: 60" x 18" dresser. Origin back-center.
def _dresser_spec() -> list[dict]:
    w = 60.0
    d = 18.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Drawer divisions
    drawer_w = w / 3.0
    for i in range(1, 3):
        x = -w / 2.0 + i * drawer_w
        items.append({"type": "line", "start": (x, 0.0), "end": (x, -d)})
    return items


# FURN-NIGHTSTAND: 22" x 18" nightstand.
def _nightstand_spec() -> list[dict]:
    w = 22.0
    d = 18.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    return items


# FURN-DESK: 60" x 30" office desk. Origin at back-center.
def _desk_spec() -> list[dict]:
    w = 60.0
    d = 30.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Drawer pedestal on the right
    ped_w = 18.0
    items.append({"type": "polyline", "closed": True, "points": [
        (w / 2.0 - ped_w, -2.0),
        (w / 2.0 - 2.0, -2.0),
        (w / 2.0 - 2.0, -(d - 2.0)),
        (w / 2.0 - ped_w, -(d - 2.0)),
    ]})
    return items


# FURN-CHAIR-OFFICE: 24" diameter office chair.
def _chair_office_spec() -> list[dict]:
    r = 12.0
    items: list[dict] = []
    items.append({"type": "circle", "center": (0.0, 0.0), "radius": r})
    items.append({"type": "circle", "center": (0.0, 0.0), "radius": r * 0.4})
    items.append({"type": "line", "start": (-r, 0.0), "end": (r, 0.0)})
    items.append({"type": "line", "start": (0.0, -r), "end": (0.0, r)})
    return items


# FURN-CHAIR-DINING: 18" x 18" dining chair.
def _chair_dining_spec() -> list[dict]:
    s = 18.0
    h = s / 2.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-h, -h), (h, -h), (h, h), (-h, h),
    ]})
    items.append({"type": "line", "start": (-h, h - 2.0), "end": (h, h - 2.0)})
    return items


# FURN-TABLE-DINING: 60" x 36" dining table. Origin at center.
def _table_dining_spec() -> list[dict]:
    w = 60.0
    d = 36.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, -d / 2.0), (w / 2.0, -d / 2.0),
        (w / 2.0, d / 2.0), (-w / 2.0, d / 2.0),
    ]})
    return items


# FURN-SOFA: 84" x 36" three-seat sofa. Origin at back-center.
def _sofa_spec() -> list[dict]:
    w = 84.0
    d = 36.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Arm rests on either end
    arm = 6.0
    items.append({"type": "line", "start": (-w / 2.0 + arm, 0.0),
                  "end": (-w / 2.0 + arm, -d)})
    items.append({"type": "line", "start": (w / 2.0 - arm, 0.0),
                  "end": (w / 2.0 - arm, -d)})
    # Three seat cushions
    seat_w = (w - 2 * arm) / 3.0
    for i in range(1, 3):
        x = -w / 2.0 + arm + i * seat_w
        items.append({"type": "line", "start": (x, -arm), "end": (x, -d + 2.0)})
    # Back cushion line
    items.append({"type": "line", "start": (-w / 2.0 + arm, -arm),
                  "end": (w / 2.0 - arm, -arm)})
    return items


# FURN-FILE-CABINET: 18" x 30" 4-drawer file cabinet.
def _file_cabinet_spec() -> list[dict]:
    w = 18.0
    d = 30.0
    items: list[dict] = []
    items.append({"type": "polyline", "closed": True, "points": [
        (-w / 2.0, 0.0), (w / 2.0, 0.0),
        (w / 2.0, -d), (-w / 2.0, -d),
    ]})
    # Drawer divisions
    for i in range(1, 4):
        y = -i * d / 4.0
        items.append({"type": "line", "start": (-w / 2.0, y), "end": (w / 2.0, y)})
    return items


# ARCH-STAIR-MARKER: simple "UP" stair indicator.
def _stair_marker_spec() -> list[dict]:
    return [
        {"type": "text", "insert": (0.0, 0.0), "text": "UP", "height": 4.0},
    ]


SYMBOL_LIBRARY: dict[str, list[dict]] = {
    "ARCH-NORTH-ARROW":       _north_arrow_spec(),
    "ARCH-SCALE-BAR-10FT":    _scale_bar_10ft_spec(),
    "ARCH-STAIR-MARKER":      _stair_marker_spec(),
    "PLMB-TOILET":            _toilet_spec(),
    "PLMB-SINK":              _sink_spec(),
    "PLMB-BATHTUB":           _bathtub_spec(),
    "PLMB-SHOWER":            _shower_spec(),
    "PLMB-KITCHEN-SINK":      _kitchen_sink_spec(),
    "APPL-RANGE":             _range_spec(),
    "APPL-REFRIGERATOR":      _refrigerator_spec(),
    "APPL-DISHWASHER":        _dishwasher_spec(),
    "FURN-BED-QUEEN":         _bed_queen_spec(),
    "FURN-BED-KING":          _bed_king_spec(),
    "FURN-DRESSER":           _dresser_spec(),
    "FURN-NIGHTSTAND":        _nightstand_spec(),
    "FURN-DESK":              _desk_spec(),
    "FURN-CHAIR-OFFICE":      _chair_office_spec(),
    "FURN-CHAIR-DINING":      _chair_dining_spec(),
    "FURN-TABLE-DINING":      _table_dining_spec(),
    "FURN-SOFA":              _sofa_spec(),
    "FURN-FILE-CABINET":      _file_cabinet_spec(),
    "ELEC-OUTLET-DUPLEX":     _outlet_duplex_spec(),
}


def symbol_names() -> list[str]:
    """Sorted list of library block names."""
    return sorted(SYMBOL_LIBRARY.keys())
