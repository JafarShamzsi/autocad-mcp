"""Generate a modern 120 m² single-storey house plan.

Brief: 2 bedrooms + 1 guest room, 2 bathrooms, kitchen with chimney,
modern decor, interior walls 100 mm. Drawing units = millimeters
($INSUNITS = 4). Outer envelope 12 000 × 10 000 mm (120 m² gross).

This script drives the `autocad_mcp` package's ezdxf-backed primitives
directly (the same code path the MCP `arch.*` tools take). It produces a
DXF and optionally converts it to DWG via `accoreconsole`.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import ezdxf

# Make sure we are running with the project package importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from autocad_mcp.arch.layers import setup_aia_layers  # noqa: E402
from autocad_mcp.arch.primitives import (  # noqa: E402
    create_dimension_chain,
    create_door,
    create_wall,
    create_wall_with_openings,
    create_window,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXT_WT = 250.0   # exterior wall thickness (mm)
INT_WT = 100.0   # interior wall thickness (mm) — user requirement
W = 12000.0      # exterior width
H = 10000.0      # exterior depth

# Door / opening defaults
DOOR_W = 900.0
INT_DOOR_W = 800.0
ARCH_W = 1500.0  # wide passage
WIN_H = 1200.0   # info only — used in labels


# ---------------------------------------------------------------------------
# Custom layer additions on top of the AIA preset
# ---------------------------------------------------------------------------

EXTRA_LAYERS = [
    ("A-FURN-SOFT", 30,  "Furniture — soft (sofa, chairs, beds)"),
    ("A-FURN-CASE", 30,  "Furniture — casework / wardrobes"),
    ("A-FURN-DINE", 30,  "Dining table + chairs"),
    ("A-EQPM-KIT",  6,   "Kitchen equipment"),
    ("A-FLOR-PLMB", 4,   "Plumbing fixtures"),
    ("A-FLOR-CHIM", 251, "Chimney / fireplace masonry"),
    ("A-FLOR-RUG",  9,   "Rugs / floor accents"),
    ("A-DECO-PLNT", 3,   "Plants"),
    ("A-DECO-ACC",  9,   "Decor accents (TV, lamps, etc.)"),
    ("A-ANNO-ROOM", 1,   "Room labels"),
    ("A-ANNO-TTLB", 7,   "Title block / titles"),
]


def ensure_extra_layers(doc) -> None:
    for name, color, desc in EXTRA_LAYERS:
        if name not in doc.layers:
            doc.layers.add(name, dxfattribs={"color": color}).description = desc


# ---------------------------------------------------------------------------
# Small geometry helpers
# ---------------------------------------------------------------------------

def rect(msp, x1, y1, x2, y2, layer):
    return msp.add_lwpolyline(
        [(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
        close=True,
        dxfattribs={"layer": layer},
    )


def line(msp, p1, p2, layer):
    return msp.add_line(p1, p2, dxfattribs={"layer": layer})


def circle(msp, cx, cy, r, layer):
    return msp.add_circle((cx, cy), r, dxfattribs={"layer": layer})


def label(msp, x, y, text, height=180.0, layer="A-ANNO-ROOM"):
    return msp.add_mtext(
        text,
        dxfattribs={
            "insert": (x, y),
            "char_height": height,
            "attachment_point": 5,   # MIDDLE_CENTER
            "layer": layer,
        },
    )


def hatch_rect(msp, x1, y1, x2, y2, pattern, scale, angle, layer):
    h = msp.add_hatch(color=7, dxfattribs={"layer": layer})
    h.set_pattern_fill(pattern, scale=scale, angle=angle)
    h.paths.add_polyline_path(
        [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], is_closed=True
    )
    return h


def hatch_poly(msp, pts, pattern, scale, angle, layer):
    h = msp.add_hatch(color=7, dxfattribs={"layer": layer})
    h.set_pattern_fill(pattern, scale=scale, angle=angle)
    h.paths.add_polyline_path(pts, is_closed=True)
    return h


# ---------------------------------------------------------------------------
# Document setup
# ---------------------------------------------------------------------------

def new_doc():
    doc = ezdxf.new("R2013", setup=True)
    doc.header["$INSUNITS"] = 4         # millimeters
    doc.header["$MEASUREMENT"] = 1      # metric
    doc.header["$LUNITS"] = 2           # decimal
    doc.header["$AUNITS"] = 0           # decimal degrees
    # Default text style: ARIAL fallback to txt
    setup_aia_layers(doc)
    ensure_extra_layers(doc)
    return doc


# ---------------------------------------------------------------------------
# Building the house
# ---------------------------------------------------------------------------

def build_exterior(msp):
    """Outer load-bearing walls (250 mm) with windows + the front door."""
    # SOUTH wall: (0,0) → (W,0). Direction +X. Left swing = into house (+Y).
    create_wall_with_openings(
        msp,
        x1=0, y1=0, x2=W, y2=0,
        thickness=EXT_WT,
        layer="A-WALL-EXTR",
        openings=[
            # Two big living-room windows
            {"type": "window", "position": 1500.0, "width": 1400.0,
             "sill_thickness": EXT_WT},
            {"type": "window", "position": 3500.0, "width": 1400.0,
             "sill_thickness": EXT_WT},
            # FRONT DOOR — opens into foyer
            {"type": "door", "position": 6950.0, "width": DOOR_W,
             "swing": "left"},
            # Kitchen window
            {"type": "window", "position": 10500.0, "width": 1200.0,
             "sill_thickness": EXT_WT},
        ],
    )

    # EAST wall: (W,0) → (W,H). Direction +Y. Left swing = into house (-X).
    # Skip a window where the chimney sits (y ≈ 2700..3700).
    create_wall_with_openings(
        msp,
        x1=W, y1=0, x2=W, y2=H,
        thickness=EXT_WT,
        layer="A-WALL-EXTR",
        openings=[
            # Kitchen side window
            {"type": "window", "position": 1500.0, "width": 1000.0,
             "sill_thickness": EXT_WT},
            # Guest room window
            {"type": "window", "position": 8500.0, "width": 1200.0,
             "sill_thickness": EXT_WT},
        ],
    )

    # NORTH wall: (W,H) → (0,H). Direction -X. Left swing = -Y (into house).
    create_wall_with_openings(
        msp,
        x1=W, y1=H, x2=0, y2=H,
        thickness=EXT_WT,
        layer="A-WALL-EXTR",
        openings=[
            # Guest window
            {"type": "window", "position": 1500.0, "width": 1200.0,
             "sill_thickness": EXT_WT},
            # Bedroom 2 window
            {"type": "window", "position": 5500.0, "width": 1200.0,
             "sill_thickness": EXT_WT},
            # Master bedroom big window
            {"type": "window", "position": 8500.0, "width": 1600.0,
             "sill_thickness": EXT_WT},
            # Master bath small window
            {"type": "window", "position": 11100.0, "width": 600.0,
             "sill_thickness": EXT_WT},
        ],
    )

    # WEST wall: (0,H) → (0,0). Direction -Y. Left swing = +X (into house).
    create_wall_with_openings(
        msp,
        x1=0, y1=H, x2=0, y2=0,
        thickness=EXT_WT,
        layer="A-WALL-EXTR",
        openings=[
            # Master bedroom side window  (y ≈ 7000..8000)
            {"type": "window", "position": 2500.0, "width": 1000.0,
             "sill_thickness": EXT_WT},
            # Living room side window     (y ≈ 2500..3500)
            {"type": "window", "position": 7000.0, "width": 1000.0,
             "sill_thickness": EXT_WT},
        ],
    )


def build_interior(msp):
    """Interior partitions (100 mm) with door swings."""

    # Main east-west corridor wall at y=6000 — separates public S from private N
    # Direction +X. Left = +Y (north / into bedrooms).
    create_wall_with_openings(
        msp,
        x1=0, y1=6000, x2=W, y2=6000,
        thickness=INT_WT,
        layer="A-WALL-INTR",
        openings=[
            {"type": "door", "position": 2400.0, "width": INT_DOOR_W,
             "swing": "left"},   # → master bedroom
            {"type": "door", "position": 6750.0, "width": INT_DOOR_W,
             "swing": "left"},   # → bedroom 2
            {"type": "door", "position": 10300.0, "width": INT_DOOR_W,
             "swing": "left"},   # → guest room
        ],
    )

    # Living / Foyer divider at x=5000, full height 0..6000. Wide archway.
    # Direction +Y. Left = -X (into living). "Archway" type = no symbol.
    create_wall_with_openings(
        msp,
        x1=5000, y1=0, x2=5000, y2=6000,
        thickness=INT_WT,
        layer="A-WALL-INTR",
        openings=[
            {"type": "archway", "position": 3000.0, "width": ARCH_W},
        ],
    )

    # Foyer / Kitchen divider at x=8500, 0..6000. Wide archway to kitchen.
    create_wall_with_openings(
        msp,
        x1=8500, y1=0, x2=8500, y2=6000,
        thickness=INT_WT,
        layer="A-WALL-INTR",
        openings=[
            {"type": "archway", "position": 1500.0, "width": ARCH_W},
        ],
    )

    # Foyer / Bath 2 divider at y=4000, x=5000..8500.
    # Direction +X. Left = +Y (into bath 2). Door swings into bathroom.
    create_wall_with_openings(
        msp,
        x1=5000, y1=4000, x2=8500, y2=4000,
        thickness=INT_WT,
        layer="A-WALL-INTR",
        openings=[
            {"type": "door", "position": 1250.0, "width": INT_DOOR_W,
             "swing": "left"},   # → bath 2
        ],
    )

    # North zone partition: Master / Bedroom 2 at x=4500, y=6000..10000.
    create_wall(
        msp,
        x1=4500, y1=6000, x2=4500, y2=H,
        thickness=INT_WT,
        layer="A-WALL-INTR",
    )

    # North zone partition: Bedroom 2 / Guest at x=8500, y=6000..10000.
    create_wall(
        msp,
        x1=8500, y1=6000, x2=8500, y2=H,
        thickness=INT_WT,
        layer="A-WALL-INTR",
    )

    # Master ensuite east wall at x=2500, y=8500..10000.
    # Direction +Y. Left = -X (into ensuite). Door swings into master bedroom.
    create_wall_with_openings(
        msp,
        x1=2500, y1=8500, x2=2500, y2=H,
        thickness=INT_WT,
        layer="A-WALL-INTR",
        openings=[
            {"type": "door", "position": 600.0, "width": INT_DOOR_W,
             "swing": "right"},  # swings into master bedroom
        ],
    )

    # Master ensuite south wall at y=8500, x=0..2500. No opening.
    create_wall(
        msp,
        x1=0, y1=8500, x2=2500, y2=8500,
        thickness=INT_WT,
        layer="A-WALL-INTR",
    )


def build_chimney(msp):
    """Chimney attached to the kitchen east wall — exterior brick stack
    + interior chimney breast + range hood + cooktop."""
    # Exterior brick stack bumping out (east of the building)
    rect(msp, W,        2700, W + 600, 3700, layer="A-FLOR-CHIM")
    hatch_rect(msp, W,  2700, W + 600, 3700, pattern="AR-B816",
               scale=20.0, angle=0.0, layer="A-WALL-PATT")

    # Interior chimney breast (inside the kitchen, 350 mm deep)
    rect(msp, W - 350,  2600, W - EXT_WT, 3800, layer="A-FLOR-CHIM")
    hatch_rect(msp, W - 350, 2600, W - EXT_WT, 3800, pattern="AR-B816",
               scale=15.0, angle=0.0, layer="A-WALL-PATT")

    # Range hood line (dashed projection — use HIDDEN linetype trick: a
    # short polyline on the header layer)
    msp.add_lwpolyline(
        [(W - 1100, 2400), (W - 1100, 4000),
         (W - EXT_WT, 4000), (W - EXT_WT, 2400)],
        close=False,
        dxfattribs={"layer": "A-WALL-HEAD"},
    )

    # Cooktop / stove under the hood
    stove_x1, stove_y1 = W - 950, 2750
    stove_x2, stove_y2 = W - 350, 3650
    rect(msp, stove_x1, stove_y1, stove_x2, stove_y2, layer="A-EQPM-KIT")
    # Four burners
    for cx, cy in [(W - 800, 2950), (W - 800, 3450),
                   (W - 500, 2950), (W - 500, 3450)]:
        circle(msp, cx, cy, 100, layer="A-EQPM-KIT")
        circle(msp, cx, cy, 70,  layer="A-EQPM-KIT")

    # "Chimney" tag
    label(msp, W + 300, 3200, "CHIMNEY", height=120,
          layer="A-ANNO-TTLB")


def build_living_dining(msp):
    """Open living / dining area west of x=5000, south of y=6000."""
    base = "A-FURN-SOFT"

    # Rug (light hatched square in living zone)
    hatch_rect(msp, 600, 600, 3800, 3400, pattern="ANSI31",
               scale=80.0, angle=0.0, layer="A-FLOR-RUG")

    # Sectional sofa — L-shape against west and south walls
    # Long arm along the west wall (north-south), back at west wall
    rect(msp, 250 + EXT_WT/2, 600, 1100, 3400, layer=base)   # west piece
    rect(msp, 1100, 600, 3700, 1300, layer=base)            # south piece
    # Cushion lines on sofa
    line(msp, (1100, 1300), (1100, 3400), "A-FURN-SOFT")
    line(msp, (1100, 1300), (3700, 1300), "A-FURN-SOFT")

    # Coffee table — rounded rectangle approximated as ellipse + rect
    rect(msp, 1600, 1800, 3200, 2600, layer=base)
    # Small accent circle (centerpiece) on table
    circle(msp, 2400, 2200, 120, layer="A-DECO-ACC")

    # TV unit against east interior wall (x=5000-INT_WT/2 inside surface ≈ 4950)
    rect(msp, 4700, 1700, 4950, 3500, layer="A-FURN-CASE")
    # TV screen
    rect(msp, 4720, 2400, 4940, 2900, layer="A-DECO-ACC")

    # Dining table — round, ø 1400, between living and the corridor wall
    circle(msp, 2400, 4500, 700, layer="A-FURN-DINE")
    # 4 chairs
    for ang in (0, 90, 180, 270):
        a = math.radians(ang)
        cx = 2400 + 1000 * math.cos(a)
        cy = 4500 + 1000 * math.sin(a)
        circle(msp, cx, cy, 220, layer="A-FURN-DINE")

    # Pendant light over table (small concentric circles)
    circle(msp, 2400, 4500, 80, layer="A-LITE")
    circle(msp, 2400, 4500, 40, layer="A-LITE")

    # Plant in NW corner
    circle(msp, 700, 5500, 250, layer="A-DECO-PLNT")
    circle(msp, 700, 5500, 350, layer="A-DECO-PLNT")


def build_kitchen(msp):
    """Modern kitchen east of x=8500, with island + counters."""
    # Inner kitchen rectangle (counters wrap N + E)
    # North counter (along corridor wall)
    rect(msp, 8550 + INT_WT/2, 5350, W - EXT_WT/2 - 50, 5950,
         layer="A-EQPM-KIT")
    # Sink in north counter
    rect(msp, 9300, 5500, 10300, 5900, layer="A-FLOR-PLMB")
    # Sink basins
    rect(msp, 9380, 5560, 9780, 5840, layer="A-FLOR-PLMB")
    rect(msp, 9820, 5560, 10220, 5840, layer="A-FLOR-PLMB")
    # Faucet
    circle(msp, 9800, 5950, 40, layer="A-FLOR-PLMB")

    # Refrigerator at west end of north counter
    rect(msp, 8650, 5400, 9300, 5950, layer="A-EQPM-KIT")
    # Door swing line
    line(msp, (8650, 5400), (8975, 5400), "A-EQPM-KIT")
    line(msp, (8975, 5400), (8975, 5950), "A-EQPM-KIT")

    # Kitchen island in the middle
    island_x1, island_y1 = 9300, 3200
    island_x2, island_y2 = 11000, 4400
    rect(msp, island_x1, island_y1, island_x2, island_y2, layer="A-EQPM-KIT")
    # Counter-stool circles along south side of island
    for sx in (9700, 10150, 10600):
        circle(msp, sx, 2900, 200, layer="A-FURN-SOFT")

    # Pendant lights over island
    for sx in (9700, 10150, 10600):
        circle(msp, sx, 3800, 100, layer="A-LITE")

    # Pantry/storage along the interior west wall of the kitchen (x≈8550..9300, y=600..3000)
    rect(msp, 8600, 600, 9300, 3000, layer="A-FURN-CASE")
    # Sliding-door indicator
    line(msp, (8600, 1800), (9300, 1800), "A-FURN-CASE")
    label(msp, 8950, 1800, "STORAGE", height=110,
          layer="A-ANNO-TTLB")


def build_master_bedroom(msp):
    """Master bedroom + ensuite (west of x=4500, north of y=6000)."""
    # Bed — queen, headboard against north wall
    bed_x1, bed_y1 = 2800, H - EXT_WT/2 - 2050
    bed_x2, bed_y2 = 4350, H - EXT_WT/2 - 50
    rect(msp, bed_x1, bed_y1, bed_x2, bed_y2, layer="A-FURN-SOFT")
    # Pillows
    rect(msp, bed_x1 + 80, bed_y2 - 500, bed_x1 + 800, bed_y2 - 100,
         layer="A-FURN-SOFT")
    rect(msp, bed_x2 - 800, bed_y2 - 500, bed_x2 - 80, bed_y2 - 100,
         layer="A-FURN-SOFT")
    # Duvet line
    line(msp, (bed_x1, bed_y2 - 700), (bed_x2, bed_y2 - 700), "A-FURN-SOFT")

    # Nightstands
    rect(msp, 2350, H - EXT_WT/2 - 550, 2750, H - EXT_WT/2 - 50,
         layer="A-FURN-CASE")
    rect(msp, 4400, H - EXT_WT/2 - 550, 4400 + 50, H - EXT_WT/2 - 50,
         layer="A-FURN-CASE")  # against east-master wall (visual sliver)
    # Lamp circles on nightstands
    circle(msp, 2550, H - EXT_WT/2 - 300, 80, layer="A-LITE")

    # Wardrobe along east master wall (x=4350..4450 = thin sliver before partition)
    rect(msp, 2800, 6200, 4400, 6800, layer="A-FURN-CASE")
    # Doors on wardrobe (vertical splits)
    for x in (3200, 3600, 4000):
        line(msp, (x, 6200), (x, 6800), "A-FURN-CASE")

    # Rug under bed
    hatch_rect(msp, 2600, 6900, 4400, 8300, pattern="ANSI31",
               scale=80.0, angle=45.0, layer="A-FLOR-RUG")

    # --- Master Ensuite (x=0..2500, y=8500..H) ---
    # Toilet (against west wall)
    rect(msp, 250, 9450, 600, 9950, layer="A-FLOR-PLMB")
    circle(msp, 425, 9620, 130, layer="A-FLOR-PLMB")

    # Vanity / sink against north wall
    rect(msp, 800, 9550, 1700, 9950, layer="A-FLOR-PLMB")
    # Sink basin
    rect(msp, 1000, 9650, 1500, 9900, layer="A-FLOR-PLMB")
    circle(msp, 1250, 9775, 25, layer="A-FLOR-PLMB")  # drain

    # Shower stall (square 900x900)
    rect(msp, 1500, 8650, 2400, 9450, layer="A-FLOR-PLMB")
    # Tile hatch
    hatch_rect(msp, 1500, 8650, 2400, 9450, pattern="ANSI37",
               scale=15.0, angle=0.0, layer="A-FLOR-PLMB")
    # Shower drain
    circle(msp, 1950, 9050, 40, layer="A-FLOR-PLMB")


def build_bedroom_2(msp):
    """Bedroom 2 — x=4500..8500, y=6000..H."""
    # Bed (double, ø 1400×2000) — headboard against west wall (V_n1)
    rect(msp, 4700, 6900, 6700, 8300, layer="A-FURN-SOFT")
    # Pillow
    rect(msp, 4750, 7900, 6650, 8250, layer="A-FURN-SOFT")
    line(msp, (4700, 7700), (6700, 7700), "A-FURN-SOFT")

    # Nightstand east of bed
    rect(msp, 6800, 7200, 7300, 7800, layer="A-FURN-CASE")
    circle(msp, 7050, 7500, 80, layer="A-LITE")

    # Wardrobe against north wall
    rect(msp, 7200, 9250, 8400, H - EXT_WT/2 - 50, layer="A-FURN-CASE")
    for x in (7600, 8000):
        line(msp, (x, 9250), (x, H - EXT_WT/2 - 50), "A-FURN-CASE")

    # Desk along east wall
    rect(msp, 8050, 6200, 8400, 7100, layer="A-FURN-CASE")
    # Chair
    circle(msp, 7800, 6650, 220, layer="A-FURN-SOFT")

    # Rug under bed
    hatch_rect(msp, 4600, 6400, 7000, 8600, pattern="ANSI31",
               scale=80.0, angle=45.0, layer="A-FLOR-RUG")


def build_guest_room(msp):
    """Guest room — x=8500..W, y=6000..H."""
    # Bed (single, 1200×2000) — headboard against west wall
    rect(msp, 8700, 7100, 10700, 8300, layer="A-FURN-SOFT")
    rect(msp, 8750, 7900, 10650, 8250, layer="A-FURN-SOFT")
    line(msp, (8700, 7700), (10700, 7700), "A-FURN-SOFT")

    # Nightstand
    rect(msp, 10800, 7400, 11250, 7900, layer="A-FURN-CASE")
    circle(msp, 11025, 7650, 80, layer="A-LITE")

    # Wardrobe along east wall
    rect(msp, W - EXT_WT/2 - 650, 6200, W - EXT_WT/2 - 50, 7400,
         layer="A-FURN-CASE")
    for y in (6500, 6800, 7100):
        line(msp, (W - EXT_WT/2 - 650, y), (W - EXT_WT/2 - 50, y),
             "A-FURN-CASE")

    # Small reading chair + side table
    circle(msp, 9100, 6700, 350, layer="A-FURN-SOFT")
    rect(msp, 8800, 6400, 9000, 6600, layer="A-FURN-CASE")

    # Rug
    hatch_rect(msp, 8700, 6300, 11000, 8500, pattern="ANSI31",
               scale=80.0, angle=45.0, layer="A-FLOR-RUG")


def build_bath2(msp):
    """Bath 2 (main bathroom) — x=5000..8500, y=4000..6000."""
    # Toilet (north wall side)
    rect(msp, 5200, 5300, 5700, 5900, layer="A-FLOR-PLMB")
    circle(msp, 5450, 5500, 180, layer="A-FLOR-PLMB")

    # Vanity (south wall side)
    rect(msp, 5200, 4100, 6800, 4600, layer="A-FLOR-PLMB")
    # Two basins
    rect(msp, 5350, 4200, 5950, 4550, layer="A-FLOR-PLMB")
    rect(msp, 6050, 4200, 6650, 4550, layer="A-FLOR-PLMB")
    circle(msp, 5650, 4375, 30, layer="A-FLOR-PLMB")
    circle(msp, 6350, 4375, 30, layer="A-FLOR-PLMB")
    # Mirrors above vanity (header line)
    msp.add_lwpolyline(
        [(5350, 4080), (6650, 4080)],
        close=False, dxfattribs={"layer": "A-WALL-HEAD"},
    )

    # Bathtub (east side of bathroom, against east interior wall)
    rect(msp, 7300, 4150, 8400, 5900, layer="A-FLOR-PLMB")
    # Inner tub (rounded ends approximated)
    rect(msp, 7400, 4250, 8300, 5800, layer="A-FLOR-PLMB")
    # Drain
    circle(msp, 7850, 4450, 50, layer="A-FLOR-PLMB")
    # Tile hatch
    hatch_rect(msp, 5050, 4050, 8450, 5950, pattern="ANSI37",
               scale=20.0, angle=0.0, layer="A-FLOR-PLMB")


def build_foyer(msp):
    """Foyer / entry — x=5000..8500, y=0..4000."""
    # Welcome rug
    hatch_rect(msp, 6300, 250, 7600, 1800, pattern="ANSI31",
               scale=80.0, angle=0.0, layer="A-FLOR-RUG")

    # Console table against east interior wall (V_s2 at x=8500)
    rect(msp, 8050, 2400, 8400, 3400, layer="A-FURN-CASE")
    # Decorative bowl + vase on console
    circle(msp, 8225, 2700, 90, layer="A-DECO-ACC")
    circle(msp, 8225, 3100, 100, layer="A-DECO-ACC")

    # Plant by the door
    circle(msp, 5700, 600, 200, layer="A-DECO-PLNT")
    circle(msp, 5700, 600, 280, layer="A-DECO-PLNT")

    # Shoe bench against west interior wall (V_s1 at x=5000)
    rect(msp, 5050 + INT_WT/2, 2200, 5400, 3400, layer="A-FURN-CASE")


def build_labels(msp):
    """Room names + areas. Uses A-ANNO-ROOM."""
    rooms = [
        # (cx, cy, text)
        (2400, 2500, "LIVING\\P\\PROOM"),
        (2400, 4800, "DINING"),
        (10250, 1700, "KITCHEN"),
        (6750, 1500, "FOYER"),
        (6750, 5000, "BATH 2"),
        (1600, 7400, "MASTER\\PBEDROOM"),
        (1250, 9100, "MASTER\\PBATH"),
        (6500, 6700, "BEDROOM 2"),
        (10100, 6700, "GUEST\\PROOM"),
    ]
    for cx, cy, t in rooms:
        label(msp, cx, cy, t, height=200, layer="A-ANNO-ROOM")

    # Approximate room areas in m² (interior, ignoring wall thickness)
    area_lines = [
        (2400, 2100, "≈ 27 m²"),
        (10250, 1300, "≈ 19 m²"),
        (6750, 1100, "≈ 14 m²"),
        (6750, 4600, "≈ 7 m²"),
        (1600, 7000, "≈ 13 m²"),
        (1250, 8700, "≈ 4 m²"),
        (6500, 6300, "≈ 15 m²"),
        (10100, 6300, "≈ 12 m²"),
    ]
    for cx, cy, t in area_lines:
        label(msp, cx, cy, t, height=140, layer="A-ANNO-TEXT")


def build_titleblock(msp):
    """North arrow + title block + total area note."""
    # North arrow at lower-right outside the building
    nx, ny = W + 1800, 8500
    circle(msp, nx, ny, 400, layer="A-ANNO-SYMB")
    msp.add_lwpolyline(
        [(nx, ny + 380), (nx - 130, ny - 320), (nx, ny - 160),
         (nx + 130, ny - 320)],
        close=True, dxfattribs={"layer": "A-ANNO-SYMB"},
    )
    label(msp, nx, ny + 600, "N", height=300, layer="A-ANNO-SYMB")

    # Title block / drawing info
    info = (
        "MODERN HOUSE — FLOOR PLAN\\P"
        "120 m²  ·  2 BED  ·  1 GUEST  ·  2 BATH\\P"
        "Interior walls: 100 mm  ·  Exterior walls: 250 mm\\P"
        "Units: millimeters  ·  Scale 1:100"
    )
    label(msp, W / 2, -1400, info, height=260, layer="A-ANNO-TTLB")

    # Scale bar (10 m long, 5 segments × 2 m)
    bx0, by0 = -2000, -1400
    h_seg = 120
    for i in range(5):
        x0 = bx0 + i * 2000
        x1 = x0 + 2000
        pts = [(x0, by0), (x1, by0), (x1, by0 + h_seg), (x0, by0 + h_seg)]
        msp.add_lwpolyline(pts, close=True,
                           dxfattribs={"layer": "A-ANNO-SYMB"})
        if i % 2 == 0:
            hatch_poly(msp, pts, "SOLID", 1.0, 0.0, "A-ANNO-SYMB")
    for i in range(6):
        x = bx0 + i * 2000
        line(msp, (x, by0 - 80), (x, by0 + h_seg + 80), "A-ANNO-SYMB")
        label(msp, x, by0 + h_seg + 320, f"{i*2}", height=180,
              layer="A-ANNO-SYMB")
    label(msp, bx0 + 5000, by0 - 350, "SCALE BAR  (m)", height=180,
          layer="A-ANNO-TTLB")


def build_dimensions(msp):
    """Overall + room-by-room dimensions along the south side."""
    # Overall width chain along the south of the building (offset below)
    create_dimension_chain(
        msp,
        points=[(0, 0), (W, 0)],
        offset=1200.0,
        direction="horizontal",
        layer="A-ANNO-DIMS",
    )
    # Overall depth chain along the west side (offset to the left)
    create_dimension_chain(
        msp,
        points=[(0, H), (0, 0)],
        offset=1200.0,
        direction="vertical",
        layer="A-ANNO-DIMS",
    )
    # Room-grid divisions on the south side (one offset row beneath)
    create_dimension_chain(
        msp,
        points=[(0, 0), (5000, 0), (8500, 0), (W, 0)],
        offset=600.0,
        direction="horizontal",
        layer="A-ANNO-DIMS",
    )
    # Room-grid divisions on the east side
    create_dimension_chain(
        msp,
        points=[(W, H), (W, 6000), (W, 0)],
        offset=600.0,
        direction="vertical",
        layer="A-ANNO-DIMS",
    )


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_dxf(doc, dxf_path: Path) -> None:
    dxf_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dxf_path))


def try_save_dwg(doc, dwg_path: Path) -> str:
    """Convert ezdxf doc → DWG via the package's vetted converter."""
    try:
        from autocad_mcp.project.dwg_reader import write_dwg
    except Exception as ex:
        return f"write_dwg unavailable: {ex}"
    try:
        out = write_dwg(doc, str(dwg_path), version="2018")
        return f"DWG saved: {out}"
    except Exception as ex:
        return f"DWG conversion failed: {ex}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    out_dir = REPO_ROOT
    dxf_path = out_dir / "House_120m2_Modern.dxf"
    dwg_path = out_dir / "House_120m2_Modern.dwg"

    doc = new_doc()
    msp = doc.modelspace()

    build_exterior(msp)
    build_interior(msp)
    build_chimney(msp)

    build_living_dining(msp)
    build_kitchen(msp)
    build_master_bedroom(msp)
    build_bedroom_2(msp)
    build_guest_room(msp)
    build_bath2(msp)
    build_foyer(msp)

    build_labels(msp)
    build_titleblock(msp)
    build_dimensions(msp)

    # Set the active viewport to fit the whole drawing
    try:
        doc.set_modelspace_vport(height=14000, center=(W / 2, H / 2 - 500))
    except Exception:
        pass

    save_dxf(doc, dxf_path)
    print(f"DXF saved: {dxf_path}")

    dwg_status = try_save_dwg(doc, dwg_path)
    print(dwg_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
