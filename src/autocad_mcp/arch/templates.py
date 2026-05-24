"""Building templates — parametric composites that emit a whole floor plan.

Each template takes a footprint origin (insertion point) + params dict and
emits walls, rooms, doors, windows (and optionally furniture) in one call.
Templates are designed to be tweaked by the caller — every dimension is a
named param with a sensible default.

License: same CC0 dedication as the rest of arch_mcp's procedural library.
Templates are "starter compositions", not architect-stamped designs — they
won't pass code review, but they're a fast way to get a recognizable layout
on screen for sketching/iteration purposes.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from ezdxf.layouts import BaseLayout

from autocad_mcp.arch.primitives import (
    create_door,
    create_room,
    create_slab,
    create_wall_with_openings,
    create_window,
)


def _outer_walls(
    msp: BaseLayout,
    origin: tuple[float, float],
    width: float,
    depth: float,
    thickness: float,
    openings_per_side: dict[str, list[dict]] | None = None,
) -> dict[str, Any]:
    """Build a rectangular building perimeter with optional per-side openings.

    `openings_per_side` keys: 'south', 'east', 'north', 'west'. Values are
    lists in the same shape `arch_wall_with_openings` accepts.
    """
    x0, y0 = origin
    x1 = x0 + width
    y1 = y0 + depth
    openings_per_side = openings_per_side or {}
    handles: dict[str, Any] = {}

    south = create_wall_with_openings(
        msp, x0, y0, x1, y0,
        thickness=thickness,
        openings=openings_per_side.get("south", []),
    )
    east = create_wall_with_openings(
        msp, x1, y0, x1, y1,
        thickness=thickness,
        openings=openings_per_side.get("east", []),
    )
    north = create_wall_with_openings(
        msp, x1, y1, x0, y1,
        thickness=thickness,
        openings=openings_per_side.get("north", []),
    )
    west = create_wall_with_openings(
        msp, x0, y1, x0, y0,
        thickness=thickness,
        openings=openings_per_side.get("west", []),
    )
    handles["walls"] = {
        "south": south["handles"],
        "east": east["handles"],
        "north": north["handles"],
        "west": west["handles"],
    }
    return handles


def _partition(
    msp: BaseLayout,
    x1: float, y1: float, x2: float, y2: float,
    thickness: float,
    openings: list[dict] | None = None,
) -> dict[str, Any]:
    info = create_wall_with_openings(
        msp, x1, y1, x2, y2,
        thickness=thickness,
        openings=openings or [],
    )
    return info["handles"]


def _residential_1br(
    msp: BaseLayout,
    x: float,
    y: float,
    params: dict,
) -> dict[str, Any]:
    """One-bedroom apartment template ~25' x 35'.

    Layout (south = front):
        - Living/dining at south
        - Kitchen along west wall
        - Bedroom at north
        - Bathroom in middle east
    """
    width = float(params.get("width", 25.0 * 12))  # inches
    depth = float(params.get("depth", 35.0 * 12))
    wall = float(params.get("wall_thickness", 6.0))
    door_w = float(params.get("door_width", 32.0))
    win_w = float(params.get("window_width", 36.0))

    handles: dict[str, Any] = {}

    # Front entry door on south, two windows on long sides
    south_openings = [
        {"position": width * 0.3, "width": door_w, "type": "door", "swing": "left"},
        {"position": width * 0.75, "width": win_w, "type": "window"},
    ]
    east_openings = [
        {"position": depth * 0.25, "width": win_w, "type": "window"},
        {"position": depth * 0.75, "width": win_w, "type": "window"},
    ]
    north_openings = [
        {"position": width * 0.5, "width": win_w, "type": "window"},
    ]
    west_openings = [
        {"position": depth * 0.7, "width": win_w, "type": "window"},
    ]

    perimeter = _outer_walls(
        msp, (x, y), width, depth, wall,
        openings_per_side={
            "south": south_openings,
            "east": east_openings,
            "north": north_openings,
            "west": west_openings,
        },
    )
    handles.update(perimeter)

    # Interior partitions: split into 4 rough rooms
    # Vertical wall at ~60% width separating kitchen+bath from living+bedroom
    bath_x = x + width * 0.55
    bath_y_low = y + depth * 0.40
    bath_y_high = y + depth * 0.65

    # Wall between living (south) and bedroom (north) — horizontal at ~55% depth
    horiz_y = y + depth * 0.55
    horiz_walls = [
        _partition(
            msp, x, horiz_y, bath_x, horiz_y,
            thickness=wall,
            openings=[{"position": (bath_x - x) * 0.5, "width": door_w,
                       "type": "door", "swing": "left"}],
        ),
        _partition(
            msp, bath_x, horiz_y, x + width, horiz_y,
            thickness=wall,
            openings=[],
        ),
    ]
    # Bath box: a small enclosed room
    bath_walls = [
        _partition(
            msp, bath_x, bath_y_low, x + width, bath_y_low,
            thickness=wall,
            openings=[{"position": (width - (bath_x - x)) * 0.5, "width": door_w,
                       "type": "door", "swing": "left"}],
        ),
        _partition(
            msp, bath_x, bath_y_low, bath_x, bath_y_high,
            thickness=wall,
        ),
        _partition(
            msp, bath_x, bath_y_high, x + width, bath_y_high,
            thickness=wall,
        ),
    ]

    # Room labels
    rooms = {}
    rooms["living"] = create_room(
        msp,
        boundary=[(x, y), (bath_x, y), (bath_x, horiz_y), (x, horiz_y)],
        name="LIVING/DINING",
    )
    rooms["kitchen"] = create_room(
        msp,
        boundary=[(bath_x, y), (x + width, y), (x + width, bath_y_low), (bath_x, bath_y_low)],
        name="KITCHEN",
    )
    rooms["bath"] = create_room(
        msp,
        boundary=[(bath_x, bath_y_low), (x + width, bath_y_low),
                  (x + width, bath_y_high), (bath_x, bath_y_high)],
        name="BATH",
    )
    rooms["bedroom"] = create_room(
        msp,
        boundary=[(x, horiz_y), (x + width, horiz_y),
                  (x + width, y + depth), (x, y + depth)],
        name="BEDROOM",
    )

    handles["partitions"] = horiz_walls + bath_walls
    handles["rooms"] = {k: v["handles"] for k, v in rooms.items()}

    return {
        "handles": handles,
        "template": "residential_1br",
        "footprint": [(x, y), (x + width, y), (x + width, y + depth), (x, y + depth)],
        "rooms": [
            {"name": "LIVING/DINING", "area": rooms["living"]["area"]},
            {"name": "KITCHEN", "area": rooms["kitchen"]["area"]},
            {"name": "BATH", "area": rooms["bath"]["area"]},
            {"name": "BEDROOM", "area": rooms["bedroom"]["area"]},
        ],
    }


def _residential_2br(
    msp: BaseLayout,
    x: float,
    y: float,
    params: dict,
) -> dict[str, Any]:
    """Two-bedroom apartment ~35' x 40'.

    Layout: Living/Dining south; Kitchen southeast; central bath; two bedrooms north.
    """
    width = float(params.get("width", 35.0 * 12))
    depth = float(params.get("depth", 40.0 * 12))
    wall = float(params.get("wall_thickness", 6.0))
    door_w = float(params.get("door_width", 32.0))
    win_w = float(params.get("window_width", 36.0))

    handles: dict[str, Any] = {}
    perimeter = _outer_walls(
        msp, (x, y), width, depth, wall,
        openings_per_side={
            "south": [
                {"position": width * 0.25, "width": door_w, "type": "door", "swing": "left"},
                {"position": width * 0.6, "width": win_w, "type": "window"},
                {"position": width * 0.85, "width": win_w, "type": "window"},
            ],
            "east": [
                {"position": depth * 0.3, "width": win_w, "type": "window"},
                {"position": depth * 0.8, "width": win_w, "type": "window"},
            ],
            "north": [
                {"position": width * 0.25, "width": win_w, "type": "window"},
                {"position": width * 0.75, "width": win_w, "type": "window"},
            ],
            "west": [
                {"position": depth * 0.3, "width": win_w, "type": "window"},
                {"position": depth * 0.8, "width": win_w, "type": "window"},
            ],
        },
    )
    handles.update(perimeter)

    mid_y = y + depth * 0.5
    bath_x_low = x + width * 0.35
    bath_x_high = x + width * 0.55
    bedroom_split_x = x + width * 0.5

    partitions = []
    # East-west wall splitting south and north halves
    partitions.append(_partition(
        msp, x, mid_y, bath_x_low, mid_y,
        thickness=wall,
        openings=[{"position": (bath_x_low - x) * 0.5, "width": door_w,
                   "type": "door", "swing": "left"}],
    ))
    partitions.append(_partition(
        msp, bath_x_high, mid_y, x + width, mid_y,
        thickness=wall,
        openings=[{"position": (x + width - bath_x_high) * 0.3, "width": door_w,
                   "type": "door", "swing": "right"}],
    ))

    # Bath enclosure
    bath_y_low = mid_y - depth * 0.12
    bath_y_high = mid_y + depth * 0.12
    partitions.append(_partition(msp, bath_x_low, bath_y_low, bath_x_high, bath_y_low,
                                  thickness=wall))
    partitions.append(_partition(msp, bath_x_low, bath_y_high, bath_x_high, bath_y_high,
                                  thickness=wall))
    partitions.append(_partition(msp, bath_x_low, bath_y_low, bath_x_low, bath_y_high,
                                  thickness=wall,
                                  openings=[{"position": (bath_y_high - bath_y_low) * 0.5,
                                              "width": door_w, "type": "door", "swing": "left"}]))
    partitions.append(_partition(msp, bath_x_high, bath_y_low, bath_x_high, bath_y_high,
                                  thickness=wall))

    # Bedroom split north
    partitions.append(_partition(
        msp, bedroom_split_x, mid_y, bedroom_split_x, y + depth,
        thickness=wall,
    ))

    # Kitchen partition (south-east): vertical at width*0.55, from south up to mid_y
    kitchen_x = x + width * 0.6
    partitions.append(_partition(
        msp, kitchen_x, y, kitchen_x, mid_y,
        thickness=wall,
        openings=[{"position": (mid_y - y) * 0.8, "width": door_w,
                   "type": "door", "swing": "left"}],
    ))

    # Room labels
    rooms = {}
    rooms["living"] = create_room(
        msp,
        boundary=[(x, y), (kitchen_x, y), (kitchen_x, mid_y), (x, mid_y)],
        name="LIVING/DINING",
    )
    rooms["kitchen"] = create_room(
        msp,
        boundary=[(kitchen_x, y), (x + width, y), (x + width, mid_y), (kitchen_x, mid_y)],
        name="KITCHEN",
    )
    rooms["bath"] = create_room(
        msp,
        boundary=[(bath_x_low, bath_y_low), (bath_x_high, bath_y_low),
                  (bath_x_high, bath_y_high), (bath_x_low, bath_y_high)],
        name="BATH",
    )
    rooms["bedroom1"] = create_room(
        msp,
        boundary=[(x, mid_y), (bedroom_split_x, mid_y),
                  (bedroom_split_x, y + depth), (x, y + depth)],
        name="BEDROOM 1",
    )
    rooms["bedroom2"] = create_room(
        msp,
        boundary=[(bedroom_split_x, mid_y), (x + width, mid_y),
                  (x + width, y + depth), (bedroom_split_x, y + depth)],
        name="BEDROOM 2",
    )

    handles["partitions"] = partitions
    handles["rooms"] = {k: v["handles"] for k, v in rooms.items()}

    return {
        "handles": handles,
        "template": "residential_2br",
        "footprint": [(x, y), (x + width, y), (x + width, y + depth), (x, y + depth)],
        "rooms": [
            {"name": name, "area": rm["area"]}
            for name, rm in [
                ("LIVING/DINING", rooms["living"]),
                ("KITCHEN", rooms["kitchen"]),
                ("BATH", rooms["bath"]),
                ("BEDROOM 1", rooms["bedroom1"]),
                ("BEDROOM 2", rooms["bedroom2"]),
            ]
        ],
    }


def _residential_3br_house(
    msp: BaseLayout,
    x: float,
    y: float,
    params: dict,
) -> dict[str, Any]:
    """3-bedroom single-family house ~50' x 35'.

    Layout: south = living/dining/kitchen; north = three bedrooms + 2 baths.
    """
    width = float(params.get("width", 50.0 * 12))
    depth = float(params.get("depth", 35.0 * 12))
    wall = float(params.get("wall_thickness", 6.0))
    door_w = float(params.get("door_width", 32.0))
    win_w = float(params.get("window_width", 36.0))

    handles: dict[str, Any] = {}

    perimeter = _outer_walls(
        msp, (x, y), width, depth, wall,
        openings_per_side={
            "south": [
                {"position": width * 0.2, "width": door_w, "type": "door", "swing": "left"},
                {"position": width * 0.5, "width": win_w * 1.5, "type": "window"},
                {"position": width * 0.85, "width": win_w, "type": "window"},
            ],
            "east": [
                {"position": depth * 0.25, "width": win_w, "type": "window"},
                {"position": depth * 0.75, "width": win_w, "type": "window"},
            ],
            "north": [
                {"position": width * 0.2, "width": win_w, "type": "window"},
                {"position": width * 0.5, "width": win_w, "type": "window"},
                {"position": width * 0.8, "width": win_w, "type": "window"},
            ],
            "west": [
                {"position": depth * 0.25, "width": win_w, "type": "window"},
                {"position": depth * 0.75, "width": win_w, "type": "window"},
            ],
        },
    )
    handles.update(perimeter)

    mid_y = y + depth * 0.5
    partitions: list = []

    # East-west wall splitting public/private — hallway access via door
    partitions.append(_partition(
        msp, x, mid_y, x + width, mid_y,
        thickness=wall,
        openings=[{"position": width * 0.5, "width": door_w * 1.2,
                   "type": "door", "swing": "left"}],
    ))

    # Kitchen partition (north-east corner of south section)
    kitchen_x = x + width * 0.65
    partitions.append(_partition(
        msp, kitchen_x, y, kitchen_x, mid_y,
        thickness=wall,
        openings=[{"position": (mid_y - y) * 0.5, "width": door_w * 1.5,
                   "type": "door", "swing": "left"}],
    ))

    # North half: three bedrooms + two baths
    # vertical walls at width*0.25 and width*0.65
    bd_split1_x = x + width * 0.3
    bd_split2_x = x + width * 0.65
    partitions.append(_partition(
        msp, bd_split1_x, mid_y, bd_split1_x, y + depth,
        thickness=wall,
        openings=[{"position": (y + depth - mid_y) * 0.3, "width": door_w,
                   "type": "door", "swing": "left"}],
    ))
    partitions.append(_partition(
        msp, bd_split2_x, mid_y, bd_split2_x, y + depth,
        thickness=wall,
        openings=[{"position": (y + depth - mid_y) * 0.3, "width": door_w,
                   "type": "door", "swing": "right"}],
    ))

    # Master bath box (north-east): a rectangle inside bedroom3
    mb_x_low = x + width * 0.75
    mb_x_high = x + width - wall
    mb_y_low = mid_y + wall
    mb_y_high = mid_y + depth * 0.18
    partitions.append(_partition(msp, mb_x_low, mb_y_low, mb_x_low, mb_y_high,
                                  thickness=wall,
                                  openings=[{"position": (mb_y_high - mb_y_low) * 0.5,
                                              "width": door_w, "type": "door", "swing": "left"}]))
    partitions.append(_partition(msp, mb_x_low, mb_y_high, mb_x_high, mb_y_high,
                                  thickness=wall))

    # Common bath box (between bedrooms 1 and 2)
    cb_x_low = bd_split1_x + wall
    cb_x_high = cb_x_low + width * 0.15
    cb_y_low = mid_y + wall
    cb_y_high = mid_y + depth * 0.18
    partitions.append(_partition(msp, cb_x_high, cb_y_low, cb_x_high, cb_y_high,
                                  thickness=wall,
                                  openings=[{"position": (cb_y_high - cb_y_low) * 0.5,
                                              "width": door_w, "type": "door", "swing": "left"}]))
    partitions.append(_partition(msp, cb_x_low, cb_y_high, cb_x_high, cb_y_high,
                                  thickness=wall))

    # Rooms
    rooms = {}
    rooms["living"] = create_room(
        msp,
        boundary=[(x, y), (x + width * 0.5, y),
                  (x + width * 0.5, mid_y), (x, mid_y)],
        name="LIVING",
    )
    rooms["dining"] = create_room(
        msp,
        boundary=[(x + width * 0.5, y), (kitchen_x, y),
                  (kitchen_x, mid_y), (x + width * 0.5, mid_y)],
        name="DINING",
    )
    rooms["kitchen"] = create_room(
        msp,
        boundary=[(kitchen_x, y), (x + width, y),
                  (x + width, mid_y), (kitchen_x, mid_y)],
        name="KITCHEN",
    )
    rooms["bedroom1"] = create_room(
        msp,
        boundary=[(x, mid_y), (bd_split1_x, mid_y),
                  (bd_split1_x, y + depth), (x, y + depth)],
        name="BEDROOM 1",
    )
    rooms["bedroom2"] = create_room(
        msp,
        boundary=[(bd_split1_x, mid_y), (bd_split2_x, mid_y),
                  (bd_split2_x, y + depth), (bd_split1_x, y + depth)],
        name="BEDROOM 2",
    )
    rooms["bedroom3"] = create_room(
        msp,
        boundary=[(bd_split2_x, mid_y), (x + width, mid_y),
                  (x + width, y + depth), (bd_split2_x, y + depth)],
        name="MASTER BEDROOM",
    )

    handles["partitions"] = partitions
    handles["rooms"] = {k: v["handles"] for k, v in rooms.items()}

    return {
        "handles": handles,
        "template": "residential_3br_house",
        "footprint": [(x, y), (x + width, y), (x + width, y + depth), (x, y + depth)],
        "rooms": [
            {"name": rm["name"], "area": rm["area"]}
            for rm in rooms.values()
        ],
    }


def _commercial_open_office(
    msp: BaseLayout,
    x: float,
    y: float,
    params: dict,
) -> dict[str, Any]:
    """Open commercial office plate ~60' x 40' with a 2-row column grid.

    Layout:
      - Open plate with structural columns on a regular grid.
      - Front entry on south, exits on east/west.
      - Two enclosed rooms (conference + bathroom) along north wall.
    """
    width = float(params.get("width", 60.0 * 12))
    depth = float(params.get("depth", 40.0 * 12))
    wall = float(params.get("wall_thickness", 8.0))
    door_w = float(params.get("door_width", 36.0))
    win_w = float(params.get("window_width", 48.0))
    col_size = float(params.get("column_size", 18.0))
    col_grid_x = int(params.get("col_grid_x", 4))
    col_grid_y = int(params.get("col_grid_y", 2))

    from autocad_mcp.arch.primitives import create_column

    handles: dict[str, Any] = {}

    perimeter = _outer_walls(
        msp, (x, y), width, depth, wall,
        openings_per_side={
            "south": [
                {"position": width * 0.5, "width": door_w * 1.5, "type": "door"},
                {"position": width * 0.15, "width": win_w, "type": "window"},
                {"position": width * 0.85, "width": win_w, "type": "window"},
            ],
            "east": [
                {"position": depth * 0.5, "width": door_w, "type": "door"},
                {"position": depth * 0.2, "width": win_w, "type": "window"},
                {"position": depth * 0.8, "width": win_w, "type": "window"},
            ],
            "west": [
                {"position": depth * 0.5, "width": door_w, "type": "door"},
                {"position": depth * 0.2, "width": win_w, "type": "window"},
                {"position": depth * 0.8, "width": win_w, "type": "window"},
            ],
            "north": [
                {"position": width * 0.2, "width": win_w, "type": "window"},
                {"position": width * 0.5, "width": win_w * 2, "type": "window"},
                {"position": width * 0.8, "width": win_w, "type": "window"},
            ],
        },
    )
    handles.update(perimeter)

    # Column grid: place columns inside the perimeter on a regular grid.
    cols: list = []
    margin_x = width / (col_grid_x + 1)
    margin_y = depth / (col_grid_y + 1)
    for i in range(1, col_grid_x + 1):
        for j in range(1, col_grid_y + 1):
            cx = x + i * margin_x
            cy = y + j * margin_y
            col = create_column(msp, cx, cy, shape="square", size=col_size)
            cols.append(col["handles"])
    handles["columns"] = cols

    # Conference room enclosure on north side, west half
    cr_x_low = x + wall * 2
    cr_x_high = x + width * 0.35
    cr_y_low = y + depth - depth * 0.30
    cr_y_high = y + depth - wall
    partitions = []
    partitions.append(_partition(
        msp, cr_x_low, cr_y_low, cr_x_high, cr_y_low,
        thickness=wall,
        openings=[{"position": (cr_x_high - cr_x_low) * 0.5, "width": door_w,
                   "type": "door", "swing": "left"}],
    ))
    partitions.append(_partition(msp, cr_x_high, cr_y_low, cr_x_high, cr_y_high,
                                  thickness=wall))

    # Bathroom on north side, east half
    br_x_low = x + width * 0.65
    br_x_high = x + width - wall * 2
    br_y_low = y + depth - depth * 0.25
    br_y_high = y + depth - wall
    partitions.append(_partition(
        msp, br_x_low, br_y_low, br_x_high, br_y_low,
        thickness=wall,
        openings=[{"position": (br_x_high - br_x_low) * 0.3, "width": door_w,
                   "type": "door", "swing": "left"}],
    ))
    partitions.append(_partition(msp, br_x_low, br_y_low, br_x_low, br_y_high,
                                  thickness=wall))

    rooms = {}
    rooms["open"] = create_room(
        msp,
        boundary=[(x, y), (x + width, y), (x + width, cr_y_low), (x, cr_y_low)],
        name="OPEN OFFICE",
    )
    rooms["conference"] = create_room(
        msp,
        boundary=[(cr_x_low, cr_y_low), (cr_x_high, cr_y_low),
                  (cr_x_high, cr_y_high), (cr_x_low, cr_y_high)],
        name="CONFERENCE",
    )
    rooms["bath"] = create_room(
        msp,
        boundary=[(br_x_low, br_y_low), (br_x_high, br_y_low),
                  (br_x_high, br_y_high), (br_x_low, br_y_high)],
        name="BATH",
    )

    handles["partitions"] = partitions
    handles["rooms"] = {k: v["handles"] for k, v in rooms.items()}

    return {
        "handles": handles,
        "template": "commercial_open_office",
        "footprint": [(x, y), (x + width, y), (x + width, y + depth), (x, y + depth)],
        "rooms": [
            {"name": rm["name"], "area": rm["area"]}
            for rm in rooms.values()
        ],
        "column_grid": [col_grid_x, col_grid_y],
    }


TEMPLATES: dict[str, Callable[[BaseLayout, float, float, dict], dict[str, Any]]] = {
    "residential_1br": _residential_1br,
    "residential_2br": _residential_2br,
    "residential_3br_house": _residential_3br_house,
    "commercial_open_office": _commercial_open_office,
}


def template_names() -> list[str]:
    """Sorted list of available template names."""
    return sorted(TEMPLATES.keys())


def apply_template(
    msp: BaseLayout,
    name: str,
    x: float,
    y: float,
    params: dict | None = None,
) -> dict[str, Any]:
    """Apply a named template at (x, y). Raises ValueError on unknown name."""
    fn = TEMPLATES.get(name)
    if fn is None:
        raise ValueError(
            f"Unknown template {name!r}. Available: {sorted(TEMPLATES.keys())}"
        )
    from autocad_mcp.arch.layers import setup_aia_layers
    try:
        setup_aia_layers(msp.doc)
    except Exception:
        pass
    return fn(msp, x, y, params or {})
