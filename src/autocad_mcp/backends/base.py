"""Abstract base class for AutoCAD backends + CommandResult envelope."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


# Categorical error codes that survive wording changes. Add new values
# carefully — `client._error` and any MCP clients may key off these.
ErrorCategory = Literal[
    "not_supported",       # operation isn't implemented on the current backend
    "not_found",           # entity / drawing / file doesn't exist
    "invalid_arg",         # caller passed bad input (out of range, wrong type, etc.)
    "backend_unavailable", # backend couldn't initialize / lost connection
    "timeout",             # operation timed out
    "internal_error",      # unexpected backend exception
]


@dataclass
class CommandResult:
    """Structured result envelope from backend operations.

    `category` is a stable enum for failure modes so MCP clients can branch
    without doing substring matching on `error`. New code should set it on
    every failure result; old call sites without a category fall back to the
    `_error` helper's substring heuristics.
    """

    ok: bool
    payload: Any = None
    error: str | None = None
    category: ErrorCategory | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            d["payload"] = self.payload
        else:
            d["error"] = self.error
            if self.category is not None:
                d["category"] = self.category
        return d


def not_supported(detail: str | None = None) -> CommandResult:
    """Build the standard not_supported result, optionally with extra detail."""
    msg = "Not supported on this backend"
    if detail:
        msg = f"{msg}: {detail}"
    return CommandResult(ok=False, error=msg, category="not_supported")


@dataclass
class BackendCapabilities:
    """Declares what a backend supports."""

    can_read_drawing: bool = False
    can_modify_entities: bool = False
    can_create_entities: bool = True
    can_screenshot: bool = False
    can_save: bool = False
    can_plot_pdf: bool = False
    can_zoom: bool = False
    can_query_entities: bool = False
    can_file_operations: bool = False
    can_undo: bool = False


class AutoCADBackend(ABC):
    """Abstract interface for AutoCAD operation backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier: 'file_ipc' or 'ezdxf'."""

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Declare supported operations."""

    @abstractmethod
    async def initialize(self) -> CommandResult:
        """Initialize the backend. Called once at startup."""

    @abstractmethod
    async def status(self) -> CommandResult:
        """Return backend health/status info."""

    # --- Drawing management ---

    async def drawing_info(self) -> CommandResult:
        return not_supported()

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        return not_supported()

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        return not_supported()

    async def drawing_save_as_dwg(
        self, path: str, version: str | None = None,
    ) -> CommandResult:
        """Save the active drawing as DWG.

        `version` accepts the DWG format generation ("2018"/"2013"/.../"2000")
        or an AutoCAD year ("2024" → "2018"). Unknown values fall back to
        "2018" after logging at debug level — see
        `autocad_mcp.project.dwg_reader._normalize_dwg_version` for the
        exact mapping table.
        """
        return not_supported()

    async def drawing_create(self, name: str | None = None, force: bool = False) -> CommandResult:
        return not_supported()

    async def drawing_purge(self) -> CommandResult:
        return not_supported()

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        return not_supported()

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        return not_supported()

    async def drawing_open(self, path: str) -> CommandResult:
        return not_supported()

    async def drawing_audit(self, fix: bool = True) -> CommandResult:
        """Run AutoCAD's AUDIT against the active drawing. `fix=True` repairs
        recoverable errors; `fix=False` reports without modifying. COM and
        file_ipc only — headless ezdxf has no equivalent."""
        return not_supported()

    async def drawing_recover(self, path: str) -> CommandResult:
        """Open a damaged DWG via the RECOVER command (salvages what AUDIT
        would reject). COM and file_ipc only."""
        return not_supported()

    # --- Undo / Redo ---

    async def undo(self) -> CommandResult:
        return not_supported()

    async def redo(self) -> CommandResult:
        return not_supported()

    # --- Freehand LISP execution ---

    async def execute_lisp(self, code: str) -> CommandResult:
        return not_supported()

    # --- Entity operations ---

    async def create_line(self, x1: float, y1: float, x2: float, y2: float, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_circle(self, cx: float, cy: float, radius: float, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_polyline(self, points: list[list[float]], closed: bool = False, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_rectangle(self, x1: float, y1: float, x2: float, y2: float, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_arc(self, cx: float, cy: float, radius: float, start_angle: float, end_angle: float, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_ellipse(self, cx: float, cy: float, major_x: float, major_y: float, ratio: float, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_mtext(self, x: float, y: float, width: float, text: str, height: float = 2.5, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_hatch(self, entity_id: str, pattern: str = "ANSI31") -> CommandResult:
        return not_supported()

    async def entity_list(
        self, layer: str | None = None, limit: int = 1000,
    ) -> CommandResult:
        return not_supported()

    async def entity_count(self, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def entity_summary(self, layer: str | None = None) -> CommandResult:
        """Compact entity inventory: total, by_layer, by_type, sample.

        Token-efficient alternative to `entity_list` for large drawings.
        Layer-scoped when `layer` is provided.
        """
        return not_supported()

    async def entity_select(
        self,
        *,
        layer: str | list[str] | None = None,
        type: str | list[str] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        color: int | str | None = None,
        handles: list[str] | None = None,
        limit: int = 5000,
    ) -> CommandResult:
        """Filter modelspace entities; return matching handles + summary.

        Filter args AND together. All are optional — calling with no
        filter returns every entity (capped by `limit`). The result is
        meant as input to bulk operations (`entity_*` ops accepting a
        `selection`). Reasoning surface for filtering:

        `layer`/`type`: string OR list of strings — match any.
        `bbox`: `(x_min, y_min, x_max, y_max)` — entities whose own bbox
            *intersects* the query bbox (AutoCAD "crossing" semantics).
        `color`: ACI int (1-255) or named color ("red", "yellow", ...).
        `handles`: pre-existing handle list to narrow further — lets a
            caller chain two selections.

        Result payload: `{count, handles, by_layer, by_type, truncated}`.
        """
        return not_supported()

    async def entity_osnap(
        self,
        x: float, y: float,
        modes: list[str] | None = None,
        tolerance: float = 5.0,
    ) -> CommandResult:
        """Snap a query point to the nearest geometric feature.

        `modes` accepts any subset of:
          endpoint, midpoint, center, intersection, nearest, perpendicular
        Defaults to `[endpoint, midpoint, center, intersection]` — the
        common architectural-drafting set.

        `tolerance` is the search radius in drawing units. Returns
        `{x, y, mode_used, entity_handle, distance}` for the best hit,
        or `category=not_found` when nothing within tolerance.
        """
        return not_supported()

    async def entity_get(self, entity_id: str) -> CommandResult:
        return not_supported()

    async def entity_erase(self, entity_id: str) -> CommandResult:
        return not_supported()

    async def entity_copy(self, entity_id: str, dx: float, dy: float) -> CommandResult:
        return not_supported()

    async def entity_move(self, entity_id: str, dx: float, dy: float) -> CommandResult:
        return not_supported()

    async def entity_rotate(self, entity_id: str, cx: float, cy: float, angle: float) -> CommandResult:
        return not_supported()

    async def entity_scale(self, entity_id: str, cx: float, cy: float, factor: float) -> CommandResult:
        return not_supported()

    async def entity_mirror(self, entity_id: str, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return not_supported()

    async def entity_offset(self, entity_id: str, distance: float) -> CommandResult:
        return not_supported()

    async def entity_array(self, entity_id: str, rows: int, cols: int, row_dist: float, col_dist: float) -> CommandResult:
        return not_supported()

    async def entity_fillet(self, entity_id1: str, entity_id2: str, radius: float) -> CommandResult:
        return not_supported()

    async def entity_chamfer(self, entity_id1: str, entity_id2: str, dist1: float, dist2: float) -> CommandResult:
        return not_supported()

    # --- 3D primitives (full AutoCAD only) ---

    async def entity_extrude(
        self,
        entity_id: str,
        height: float,
        taper_angle: float = 0.0,
    ) -> CommandResult:
        """Extrude a closed 2D curve / region into a 3D solid.

        `entity_id` must be a Region (created via `entity_region`) or a
        closed curve AutoCAD can convert. `height` is the extrusion
        distance along the entity's Z axis; positive = +Z direction.
        `taper_angle` is the draft angle in degrees (0 = straight prism).
        """
        return not_supported()

    async def entity_revolve(
        self,
        entity_id: str,
        axis_x: float, axis_y: float,
        axis_dx: float, axis_dy: float, axis_dz: float = 0.0,
        angle: float = 360.0,
    ) -> CommandResult:
        """Revolve a Region around an axis to produce a 3D solid.

        `(axis_x, axis_y, 0)` is a point on the axis; `(axis_dx, axis_dy,
        axis_dz)` is the axis direction. `angle` is the sweep in degrees
        (360 = full revolution).
        """
        return not_supported()

    async def entity_region(self, entity_ids: list[str]) -> CommandResult:
        """Build one or more Region entities from a set of closed curves.

        Pass the handles of LINEs / LWPOLYLINEs / ARCs / CIRCLEs / ELLIPSEs
        that together form one or more closed loops. AutoCAD's `AddRegion`
        returns a Region per closed loop discovered.
        """
        return not_supported()

    async def entity_boolean(
        self,
        operation: str,
        target_id: str,
        tool_ids: list[str],
    ) -> CommandResult:
        """Boolean operation on 3D solids (or Regions).

        `operation` ∈ {"union", "subtract", "intersect"}.
        `target_id` is the solid that absorbs the result.
        `tool_ids` is the list of solids unioned-with / subtracted-from /
        intersected-with the target. The tool solids are consumed
        (deleted) by AutoCAD's boolean engine.
        """
        return not_supported()

    # --- Express Tools wrappers (full AutoCAD only) ---

    async def express_burst(self, entity_ids: list[str]) -> CommandResult:
        """Explode INSERTs while preserving their attribute *values* as
        TEXT/MTEXT entities (Express Tools BURST). Vanilla EXPLODE loses
        the attribute strings — BURST keeps them on the same insertion
        point and layer."""
        return not_supported()

    async def express_flatten(
        self,
        entity_ids: list[str] | None = None,
        z: float = 0.0,
    ) -> CommandResult:
        """Project every entity (or those in `entity_ids`) onto a horizontal
        plane at Z = `z`. Strips elevations / thicknesses. Common cleanup
        before plotting field-captured drawings."""
        return not_supported()

    async def express_overkill(self, entity_ids: list[str] | None = None) -> CommandResult:
        """Remove duplicate / overlapping entities (Express Tools OVERKILL).
        Massively helpful on imported / merged drawings where the same
        line was drawn 3 times."""
        return not_supported()

    async def express_layiso(self, layers: list[str]) -> CommandResult:
        """Isolate the named layers — turn off everything else. Reverse
        with `layer.thaw` on the remaining layers."""
        return not_supported()

    async def express_txt2mtxt(self, entity_ids: list[str]) -> CommandResult:
        """Combine multiple TEXT entities into one MTEXT (Express Tools
        TXT2MTXT). Useful when imported drawings have one TEXT-per-line
        for what should be a single paragraph."""
        return not_supported()

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        return not_supported()

    async def layer_create(self, name: str, color: str | int = "white", linetype: str = "CONTINUOUS") -> CommandResult:
        return not_supported()

    async def layer_set_current(self, name: str) -> CommandResult:
        return not_supported()

    async def layer_set_properties(self, name: str, color: str | int | None = None, linetype: str | None = None, lineweight: str | None = None) -> CommandResult:
        return not_supported()

    async def layer_freeze(self, name: str) -> CommandResult:
        return not_supported()

    async def layer_thaw(self, name: str) -> CommandResult:
        return not_supported()

    async def layer_lock(self, name: str) -> CommandResult:
        return not_supported()

    async def layer_unlock(self, name: str) -> CommandResult:
        return not_supported()

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        return not_supported()

    async def block_insert(self, name: str, x: float, y: float, scale: float = 1.0, rotation: float = 0.0, block_id: str | None = None) -> CommandResult:
        return not_supported()

    async def block_insert_with_attributes(self, name: str, x: float, y: float, scale: float = 1.0, rotation: float = 0.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return not_supported()

    async def block_get_attributes(self, entity_id: str) -> CommandResult:
        return not_supported()

    async def block_update_attribute(self, entity_id: str, tag: str, value: str) -> CommandResult:
        return not_supported()

    async def block_define(self, name: str, entities: list[dict]) -> CommandResult:
        return not_supported()

    # --- Block import from external DWG/DXF (asset reuse) ---

    async def block_list_from_file(self, path: str) -> CommandResult:
        """List every non-anonymous block defined inside `path` (.dwg or .dxf).

        Pure inspection — does not modify the active drawing. Used by clients
        to pick blocks for `block_import_from_file`.
        """
        return not_supported()

    async def block_import_from_file(
        self,
        path: str,
        names: list[str] | None = None,
    ) -> CommandResult:
        """Copy block definitions from `path` into the active drawing.

        `names=None` imports every non-anonymous block. Existing block names
        in the active drawing are preserved — the importer skips duplicates
        rather than overwriting. Returns the list of names imported / skipped.
        """
        return not_supported()

    # --- Style management (DIMSTYLE / TEXTSTYLE / MLEADERSTYLE) ---

    async def style_list_dimstyles(self) -> CommandResult:
        """List every dimstyle name plus a small subset of attribs."""
        return not_supported()

    async def style_get_dimstyle(self, name: str) -> CommandResult:
        """Return the full DXF attribute dictionary of one dimstyle."""
        return not_supported()

    async def style_create_dimstyle(self, name: str, attribs: dict) -> CommandResult:
        """Create or overwrite a dimstyle. `attribs` is a dict of DXF
        attribute names (`dimscale`, `dimtxt`, `dimasz`, `dimdec`, ...)."""
        return not_supported()

    async def style_set_current_dimstyle(self, name: str) -> CommandResult:
        """Set the active dimstyle (writes `$DIMSTYLE` / CDIMSTYLE)."""
        return not_supported()

    async def style_list_textstyles(self) -> CommandResult:
        """List every textstyle name + font + fixed height."""
        return not_supported()

    async def style_get_textstyle(self, name: str) -> CommandResult:
        return not_supported()

    async def style_create_textstyle(
        self, name: str, font: str | None = None,
        height: float = 0.0, width_factor: float = 1.0,
        oblique: float = 0.0,
    ) -> CommandResult:
        """Create or overwrite a textstyle."""
        return not_supported()

    async def style_set_current_textstyle(self, name: str) -> CommandResult:
        """Set the active textstyle (writes `$TEXTSTYLE` / CTEXTSTYLE)."""
        return not_supported()

    async def style_list_mleaderstyles(self) -> CommandResult:
        return not_supported()

    async def style_set_current_mleaderstyle(self, name: str) -> CommandResult:
        return not_supported()

    # --- Named views ---

    async def view_list_named(self) -> CommandResult:
        return not_supported()

    async def view_save_named(
        self, name: str,
        x1: float, y1: float, x2: float, y2: float,
    ) -> CommandResult:
        """Save the rectangle (x1,y1)-(x2,y2) as a named view."""
        return not_supported()

    async def view_restore_named(self, name: str) -> CommandResult:
        return not_supported()

    async def view_delete_named(self, name: str) -> CommandResult:
        return not_supported()

    # --- Layer states ---

    async def view_list_layer_states(self) -> CommandResult:
        """List saved layer-state names (the AutoCAD LAS facility)."""
        return not_supported()

    async def view_save_layer_state(self, name: str) -> CommandResult:
        return not_supported()

    async def view_restore_layer_state(self, name: str) -> CommandResult:
        return not_supported()

    async def view_delete_layer_state(self, name: str) -> CommandResult:
        return not_supported()

    # --- Annotation ---

    async def create_text(self, x: float, y: float, text: str, height: float = 2.5, rotation: float = 0.0, layer: str | None = None) -> CommandResult:
        return not_supported()

    async def create_dimension_linear(self, x1: float, y1: float, x2: float, y2: float, dim_x: float, dim_y: float) -> CommandResult:
        return not_supported()

    async def create_dimension_aligned(self, x1: float, y1: float, x2: float, y2: float, offset: float) -> CommandResult:
        return not_supported()

    async def create_dimension_angular(self, cx: float, cy: float, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return not_supported()

    async def create_dimension_radius(self, cx: float, cy: float, radius: float, angle: float) -> CommandResult:
        return not_supported()

    async def create_leader(self, points: list[list[float]], text: str) -> CommandResult:
        return not_supported()

    async def create_table(
        self,
        x: float, y: float,
        rows: int, cols: int,
        row_height: float = 0.5,
        col_width: float = 2.0,
        data: list[list[str]] | None = None,
        title: str | None = None,
        header: list[str] | None = None,
    ) -> CommandResult:
        """Create a TABLE-shaped entity at (x, y).

        On COM, uses AutoCAD's native `AddTable` — produces a real
        editable TABLE entity. On ezdxf, renders a "grid table" out of
        LINE + TEXT entities (no real TABLE entity exists in the headless
        ezdxf API), which prints/plots identically but isn't a true
        AutoCAD table object.

        `data` is rows-of-cells (list of list of strings). `title`
        appears in the top row above the headers; `header` is the second
        row. Both optional — when omitted, only `data` rows are drawn.

        `rows` / `cols` are the total counts including title + header
        rows; `data` fills whatever rows remain after those.
        """
        return not_supported()

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        return not_supported()

    async def pid_insert_symbol(self, category: str, symbol: str, x: float, y: float, scale: float = 1.0, rotation: float = 0.0) -> CommandResult:
        return not_supported()

    async def pid_list_symbols(self, category: str) -> CommandResult:
        return not_supported()

    async def pid_draw_process_line(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return not_supported()

    async def pid_connect_equipment(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return not_supported()

    async def pid_add_flow_arrow(self, x: float, y: float, rotation: float = 0.0) -> CommandResult:
        return not_supported()

    async def pid_add_equipment_tag(self, x: float, y: float, tag: str, description: str = "") -> CommandResult:
        return not_supported()

    async def pid_add_line_number(self, x: float, y: float, line_num: str, spec: str) -> CommandResult:
        return not_supported()

    async def pid_insert_valve(self, x: float, y: float, valve_type: str, rotation: float = 0.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return not_supported()

    async def pid_insert_instrument(self, x: float, y: float, instrument_type: str, rotation: float = 0.0, tag_id: str = "", range_value: str = "") -> CommandResult:
        return not_supported()

    async def pid_insert_pump(self, x: float, y: float, pump_type: str, rotation: float = 0.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return not_supported()

    async def pid_insert_tank(self, x: float, y: float, tank_type: str, scale: float = 1.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return not_supported()

    # --- Architectural primitives (AIA/NCS) ---

    async def arch_setup_layers(self) -> CommandResult:
        return not_supported()

    async def arch_wall(
        self, x1: float, y1: float, x2: float, y2: float,
        thickness: float = 6.0, layer: str = "A-WALL", justify: str = "center",
    ) -> CommandResult:
        return not_supported()

    async def arch_wall_with_openings(
        self, x1: float, y1: float, x2: float, y2: float,
        thickness: float = 6.0,
        openings: list[dict] | None = None,
        layer: str = "A-WALL",
        door_layer: str = "A-DOOR",
        window_layer: str = "A-GLAZ",
        justify: str = "center",
    ) -> CommandResult:
        return not_supported()

    async def arch_door(
        self, x: float, y: float, width: float = 36.0, angle: float = 0.0,
        swing: str = "left", open_angle: float = 90.0, layer: str = "A-DOOR",
        room_side: tuple[float, float] | None = None,
    ) -> CommandResult:
        return not_supported()

    async def arch_window(
        self, x1: float, y1: float, x2: float, y2: float,
        sill_thickness: float = 6.0, layer: str = "A-GLAZ",
    ) -> CommandResult:
        return not_supported()

    async def arch_room(
        self, boundary: list[list[float]], name: str = "",
        area_layer: str = "A-AREA", text_layer: str = "A-AREA-IDEN",
        text_height: float = 6.0,
    ) -> CommandResult:
        return not_supported()

    async def arch_dimension_chain(
        self, points: list[list[float]], offset: float = 24.0,
        direction: str = "horizontal", layer: str = "A-ANNO-DIMS",
    ) -> CommandResult:
        return not_supported()

    async def arch_stair(
        self, x1: float, y1: float, x2: float, y2: float,
        width: float = 36.0, num_treads: int = 12,
        tread_depth: float | None = None,
        layer: str = "A-FLOR-STRS",
        shape: str = "straight",
        landing_depth: float | None = None,
    ) -> CommandResult:
        return not_supported()

    async def arch_column(
        self, x: float, y: float,
        shape: str = "square", size: float = 12.0,
        size_y: float | None = None, angle: float = 0.0,
        layer: str = "A-COLS",
    ) -> CommandResult:
        return not_supported()

    async def arch_slab(
        self, boundary: list[list[float]],
        edge_thickness: float = 0.0,
        layer: str = "A-FLOR",
    ) -> CommandResult:
        return not_supported()

    async def arch_beam(
        self, x1: float, y1: float, x2: float, y2: float,
        width: float = 0.0,
        layer: str = "A-WALL-HEAD",
    ) -> CommandResult:
        return not_supported()

    async def arch_roof(
        self, boundary: list[list[float]],
        style: str = "gable",
        ridge_axis: str = "x",
        overhang: float = 12.0,
        eaves_layer: str = "A-ROOF",
        ridge_layer: str = "A-ROOF",
    ) -> CommandResult:
        return not_supported()

    async def arch_elevation(
        self, origin_x: float, origin_y: float,
        wall_length: float, height: float,
        openings: list[dict] | None = None,
        wall_layer: str = "A-WALL",
        door_layer: str = "A-DOOR",
        window_layer: str = "A-GLAZ",
    ) -> CommandResult:
        return not_supported()

    async def arch_section(
        self, origin_x: float, origin_y: float,
        section_width: float,
        floor_y: float, ceiling_y: float,
        elements: list[dict] | None = None,
        wall_layer: str = "A-WALL",
        slab_layer: str = "A-FLOR",
        door_layer: str = "A-DOOR",
        window_layer: str = "A-GLAZ",
    ) -> CommandResult:
        return not_supported()

    async def arch_template(
        self, name: str, x: float, y: float,
        params: dict | None = None,
    ) -> CommandResult:
        return not_supported()

    async def arch_hatch_region(
        self,
        boundary: list[list[float]],
        pattern: str = "CONCRETE",
        layer: str | None = None,
        scale: float | None = None,
        angle: float | None = None,
    ) -> CommandResult:
        """Hatch a closed polygon using a friendly pattern name.

        `pattern` is a key from `autocad_mcp.arch.hatches.HATCH_LIBRARY`
        (e.g. "CONCRETE", "BRICK", "INSULATION"). Per-pattern defaults
        for scale / angle / layer apply unless overridden.
        """
        return not_supported()

    # --- XREF mutation (attach / detach / reload) ---

    async def xref_attach(
        self,
        path: str,
        name: str | None = None,
        x: float = 0.0,
        y: float = 0.0,
        scale: float = 1.0,
        rotation: float = 0.0,
        overlay: bool = False,
    ) -> CommandResult:
        """Attach an external DWG as an XREF to the active drawing.

        `overlay=True` attaches as overlay (won't be nested when this
        drawing is referenced elsewhere); default is "attachment" which
        cascades. Returns the resolved XREF name and the INSERT handle.
        """
        return not_supported()

    async def xref_detach(self, name: str) -> CommandResult:
        """Detach an XREF by name from the active drawing."""
        return not_supported()

    async def xref_reload(self, name: str | None = None) -> CommandResult:
        """Reload one XREF (by `name`) or every loaded XREF (`name=None`)."""
        return not_supported()

    async def xref_bind(self, name: str) -> CommandResult:
        """Bind an XREF — make its contents part of the active drawing.

        After bind, the XREF is no longer external; entities are copied
        into the local block table with name `<xref>$0$<sym>` mangling.
        """
        return not_supported()

    # --- Layout / paperspace ---

    async def layout_list(self) -> CommandResult:
        """List every layout in the active drawing (model + paper)."""
        return not_supported()

    async def layout_create(
        self, name: str, paper_size: tuple[float, float] | None = None,
    ) -> CommandResult:
        """Create a new paperspace layout.

        `paper_size = (width, height)` in inches, defaulting to ANSI D
        (22×34). Returns the layout's tab name.
        """
        return not_supported()

    async def layout_set_active(self, name: str) -> CommandResult:
        """Set the active layout (model or paperspace)."""
        return not_supported()

    async def layout_create_viewport(
        self,
        layout: str,
        x: float, y: float, width: float, height: float,
        view_extents: tuple[float, float, float, float] | None = None,
    ) -> CommandResult:
        """Create a rectangular viewport on `layout` showing modelspace.

        `view_extents = (x1, y1, x2, y2)` defines the modelspace window
        the viewport should show; if None, AutoCAD's `ZoomExtents` is
        applied to the new viewport.
        """
        return not_supported()

    async def arch_list_hatches(self) -> CommandResult:
        """Return the friendly pattern names available to `arch_hatch_region`."""
        from autocad_mcp.arch.hatches import HATCH_LIBRARY
        return CommandResult(ok=True, payload={
            "patterns": [
                {
                    "name": k,
                    "autocad_name": v.name,
                    "description": v.description,
                    "discipline": v.discipline,
                    "default_layer": v.layer_hint,
                }
                for k, v in HATCH_LIBRARY.items()
            ],
            "count": len(HATCH_LIBRARY),
        })

    # --- Symbol library (procedural AEC blocks) ---

    async def library_list(self) -> CommandResult:
        return not_supported()

    async def library_define(self, name: str) -> CommandResult:
        return not_supported()

    async def library_define_all(self) -> CommandResult:
        return not_supported()

    async def library_insert(
        self, name: str, x: float, y: float,
        scale: float = 1.0, rotation: float = 0.0,
    ) -> CommandResult:
        return not_supported()

    # --- View ---

    async def zoom_extents(self) -> CommandResult:
        return not_supported()

    async def zoom_window(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return not_supported()

    async def get_screenshot(self) -> CommandResult:
        """Return base64 PNG in payload."""
        return not_supported()
