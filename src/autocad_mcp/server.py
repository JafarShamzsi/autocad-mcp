"""AutoCAD MCP Server — consolidated tools with operation dispatch.

Tools: drawing, entity, layer, block, annotation, pid, arch, project, library,
       events, view, system
"""

from __future__ import annotations

import structlog
from mcp.server.fastmcp import FastMCP

from autocad_mcp.client import (
    _error,
    _json,
    _safe,
    add_screenshot_if_available,
    get_backend,
    invalidate_screenshot_cache,
)

# FastMCP validates return types via Pydantic. Tools that may return
# ImageContent (screenshot) alongside TextContent need a union return type.
ToolResult = str | list

log = structlog.get_logger()

mcp = FastMCP("autocad-mcp")


# ==========================================================================
# 1. drawing — File/drawing management
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Drawing Operations", "readOnlyHint": False})
@_safe("drawing")
async def drawing(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Drawing file management.

    Operations:
      create     — Create a new empty drawing. data: {name?, force?}
                   `force=true` is only honored on the file_ipc backend,
                   where `create` is destructive — it clobbers entities
                   in the active drawing. COM and ezdxf ignore `force`.
      open       — Open an existing drawing (.dxf or .dwg). data: {path}
                   On ezdxf, .dwg goes through accoreconsole / ODA. Check
                   `system(operation='dwg_status')` first if unsure.
      info       — Get drawing extents, entity count, layers, blocks.
      save       — Save current drawing. data: {path?} (saves to path if given, else QSAVE)
                   `.dwg` suffix triggers DWG conversion on ezdxf — preferred
                   over the explicit `save_as_dwg` only when the caller doesn't
                   want to be opinionated about format.
      save_as_dxf — Export as DXF. data: {path}
      save_as_dwg — Export as DWG. data: {path, version?}.
                   `version` accepts "2018"/"2013"/"2010"/"2007"/"2004"/"2000"
                   or an AutoCAD year ("2024" → "2018"). Defaults to "2018"
                   (broadly readable across every AutoCAD version since 2018).
                   On ezdxf, requires accoreconsole or ODA File Converter;
                   failure returns `backend_unavailable` with install guidance.
      plot_pdf   — Plot to PDF. data: {path}
      purge      — Purge unused objects.
      get_variables — Get system variables. data: {names: [...]}
      undo       — Undo last operation.
      redo       — Redo last undone operation.
      audit      — Check the drawing database for errors. `data: {fix?: bool=true}`
                   With `fix=true` (default), AutoCAD repairs whatever it can.
                   Returns the audit report text. COM + file_ipc only.
      recover    — Open a damaged DWG with the RECOVER command (the AutoCAD
                   equivalent of "salvage what you can"). `data: {path}`.
                   Use this instead of `drawing.open` when a previous open
                   showed a warning dialog or the file was authored on a
                   different release. COM + file_ipc only.
      batch      — Run a LISP script across many files headlessly via
                   accoreconsole.exe (no GUI, no flicker). `data: {paths:
                   [...], script: "(...)", save?: bool=true,
                   timeout_per_file?: float=60}`. Each file gets its own
                   subprocess (serial); per-file results land in
                   `payload.per_file`. Use for "rename layer Walls →
                   A-WALL-EXTR across every sheet" or "apply firm dimstyle
                   to all 50 drawings". Requires accoreconsole (full
                   AutoCAD installed) and `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`
                   (the same gate as `system.execute_lisp` — batch is just
                   execute_lisp times N files).
      list_layouts   — Enumerate layouts (model + every paperspace tab).
      create_layout  — data: {name, paper_size?: [w_in, h_in]}
                        Create a new paperspace layout. Defaults to ANSI D
                        (22×34) when paper_size is omitted.
      set_active_layout — data: {name} — switch to a layout by name.
      create_viewport   — data: {layout, x, y, width, height, view_extents?}
                        Add a rectangular viewport showing modelspace.
                        `view_extents = [x1, y1, x2, y2]` defines what the
                        viewport zooms to; omit for default extents.
    """
    data = data or {}
    backend = await get_backend()

    # Mark screenshot cache dirty for ops that mutate the active drawing.
    # Read-only ops (info, list_layouts, get_variables) skip this.
    if operation not in ("info", "list_layouts", "get_variables"):
        invalidate_screenshot_cache()

    if operation == "create":
        result = await backend.drawing_create(data.get("name"), bool(data.get("force", False)))
    elif operation == "info":
        result = await backend.drawing_info()
    elif operation == "save":
        result = await backend.drawing_save(data.get("path"))
    elif operation == "save_as_dxf":
        result = await backend.drawing_save_as_dxf(data["path"])
    elif operation == "save_as_dwg":
        # Enforce .dwg suffix and route through the version-aware writer.
        path = data["path"]
        if not path.lower().endswith(".dwg"):
            path = path + ".dwg"
        result = await backend.drawing_save_as_dwg(
            path, version=data.get("version"),
        )
    elif operation == "plot_pdf":
        result = await backend.drawing_plot_pdf(data["path"])
    elif operation == "purge":
        result = await backend.drawing_purge()
    elif operation == "get_variables":
        result = await backend.drawing_get_variables(data.get("names"))
    elif operation == "open":
        result = await backend.drawing_open(data["path"])
    elif operation == "undo":
        result = await backend.undo()
    elif operation == "redo":
        result = await backend.redo()
    elif operation == "audit":
        result = await backend.drawing_audit(fix=bool(data.get("fix", True)))
    elif operation == "recover":
        if "path" not in data:
            return _json({"error": "data.path is required for drawing.recover"})
        result = await backend.drawing_recover(data["path"])
    elif operation == "batch":
        # Batch = execute_lisp times N files. Apply the same env-var gate.
        from autocad_mcp.config import lisp_exec_enabled
        if not lisp_exec_enabled():
            return _json({
                "error": (
                    "drawing.batch is disabled. It runs arbitrary AutoLISP "
                    "against every file in `paths` via accoreconsole. To "
                    "enable, set AUTOCAD_MCP_ENABLE_LISP_EXEC=1 in the "
                    "server env. (Same gate as system.execute_lisp.)"
                ),
                "hint": "Restart the MCP server after setting the env var.",
                "category": "not_supported",
            })
        paths = data.get("paths")
        script = data.get("script")
        if not paths or not isinstance(paths, list):
            return _json({"error": "data.paths must be a non-empty list"})
        if not script or not isinstance(script, str):
            return _json({"error": "data.script must be a non-empty LISP string"})
        from autocad_mcp.batch import run_batch_script_async
        try:
            br = await run_batch_script_async(
                paths, script,
                save=bool(data.get("save", True)),
                timeout_per_file=float(data.get("timeout_per_file", 60.0)),
            )
        except FileNotFoundError as ex:
            return _json({
                "error": str(ex),
                "category": "backend_unavailable",
            })
        except ValueError as ex:
            return _json({"error": str(ex), "category": "invalid_arg"})
        # Invalidate the screenshot cache — batch may have mutated the
        # currently-active file (if it was in the paths list).
        invalidate_screenshot_cache()
        return _json({
            "ok": br.files_failed == 0,
            "payload": {
                "files_total": br.files_total,
                "files_ok": br.files_ok,
                "files_failed": br.files_failed,
                "total_duration_s": br.total_duration_s,
                "per_file": [
                    {
                        "path": f.path,
                        "ok": f.ok,
                        "duration_s": f.duration_s,
                        "returncode": f.returncode,
                        "error": f.error,
                        "stdout_tail": f.stdout_tail,
                    }
                    for f in br.per_file
                ],
            },
        })
    elif operation == "list_layouts":
        result = await backend.layout_list()
    elif operation == "create_layout":
        size = data.get("paper_size")
        result = await backend.layout_create(
            data["name"],
            paper_size=(size[0], size[1]) if size else None,
        )
    elif operation == "set_active_layout":
        result = await backend.layout_set_active(data["name"])
    elif operation == "create_viewport":
        ext = data.get("view_extents")
        result = await backend.layout_create_viewport(
            data["layout"], data["x"], data["y"], data["width"], data["height"],
            view_extents=tuple(ext) if ext else None,
        )
    else:
        return _json({"error": f"Unknown drawing operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 2. entity — Entity CRUD + modification
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Entity Operations", "readOnlyHint": False})
@_safe("entity")
async def entity(
    operation: str,
    x1: float | None = None,
    y1: float | None = None,
    x2: float | None = None,
    y2: float | None = None,
    points: list[list[float]] | None = None,
    layer: str | None = None,
    entity_id: str | None = None,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Entity creation, querying, and modification.

    Create operations:
      create_line       — x1, y1, x2, y2, layer?
      create_circle     — data: {cx, cy, radius}, layer?
      create_polyline   — points: [[x,y],...], data: {closed?}, layer?
      create_rectangle  — x1, y1, x2, y2, layer?
      create_arc        — data: {cx, cy, radius, start_angle, end_angle}, layer?
      create_ellipse    — data: {cx, cy, major_x, major_y, ratio}, layer?
      create_mtext      — data: {x, y, width, text, height?}, layer?
      create_hatch      — entity_id, data: {pattern?}

    Read operations:
      list              — layer?, data: {limit?, summary?} → list entities.
                          Default: returns up to `limit` (1000) full records
                          with `truncated: bool`. Pass `data={"limit": 0}`
                          for unbounded. **Pass `data={"summary": true}` to
                          get a compact `{total, by_layer, by_type, samples}`
                          payload** — perfect for "what's in this drawing"
                          on large files without dumping 50k JSON lines.
      count             — layer? → count entities
      get               — entity_id → entity details

    Modify operations:
      copy    — entity_id, data: {dx, dy}
      move    — entity_id, data: {dx, dy}
      rotate  — entity_id, data: {cx, cy, angle}
      scale   — entity_id, data: {cx, cy, factor}
      mirror  — entity_id, x1, y1, x2, y2
      offset  — entity_id, data: {distance}
                AutoCAD convention: positive distance offsets to the LEFT
                of the curve's direction (CCW perpendicular); negative
                offsets to the RIGHT. For LINEs, "left" is +90° from the
                vector (start → end).
      array   — entity_id, data: {rows, cols, row_dist, col_dist}
      fillet  — data: {id1, id2, radius}
      chamfer — data: {id1, id2, dist1, dist2}
      erase   — entity_id

    Selection + bulk operations:
      select  — data: {layer?, type?, bbox?, color?, handles?, limit?}
                Filter modelspace entities. Returns `{handles, count, by_layer,
                by_type, truncated}`. Filter args AND together; all optional.
                `layer`/`type` accept a string OR a list of strings (match any).
                `bbox` = [x_min, y_min, x_max, y_max] — entities whose own bbox
                *intersects* this region (AutoCAD crossing semantics).
                `color` accepts ACI int (1-255) or color name.
                `handles` narrows from a prior selection.
                Use this instead of `list` + filter-in-head when you need
                "all doors in this room", "every layer-A-WALL line", etc.
      osnap   — data: {x, y, modes?, tolerance?}
                Snap a query point to the nearest geometric feature.
                `modes` ⊆ {endpoint, midpoint, center, intersection,
                nearest, perpendicular} — defaults to the first four.
                `tolerance` is the search radius in drawing units (default 5).
                Returns `{x, y, mode_used, entity_handle, distance}` or
                `category=not_found` when nothing within tolerance.

    Bulk modify (any of copy/move/rotate/scale/erase + offset/array):
      Same `data` payload as the single-entity form, PLUS one of:
        * `entity_ids: ["1A2", "1A3", ...]`  — explicit handle list
        * `selection: {handles: [...]}`       — pass the result of `select`
      The op runs against each handle and returns `{processed, failed,
      handles_in, handles_out?}`. Failures are per-handle and don't abort
      the batch.

    3D primitives (COM-primary; ezdxf builds a polyface MESH for extrude
    and `not_supported` for the rest):
      region    — data: {entity_ids: [handles]} → one Region per closed
                  loop. Pass the result's handles to `extrude` / `revolve`.
      extrude   — entity_id, data: {height, taper_angle?} → 3DSOLID.
                  On ezdxf, only LWPOLYLINE/POLYLINE inputs work (taper
                  is ignored).
      revolve   — entity_id, data: {axis_x, axis_y, axis_dx, axis_dy,
                  axis_dz?, angle?: float=360} → 3DSOLID. COM-only.
      boolean   — data: {operation: "union"|"subtract"|"intersect",
                  target_id, tool_ids: [...]}. Tool solids are consumed
                  (deleted) by AutoCAD's boolean engine. COM-only.
    """
    data = data or {}
    backend = await get_backend()

    # Entity writes mutate the drawing → next screenshot must re-capture.
    # `list`/`count`/`get`/`select`/`osnap` are read-only and skip the
    # invalidation.
    if operation not in ("list", "count", "get", "select", "osnap"):
        invalidate_screenshot_cache()

    # --- Selection + snap (read-only) ---
    if operation == "select":
        result = await backend.entity_select(
            layer=data.get("layer"),
            type=data.get("type"),
            bbox=tuple(data["bbox"]) if data.get("bbox") else None,
            color=data.get("color"),
            handles=data.get("handles"),
            limit=int(data.get("limit", 5000)),
        )
        return await add_screenshot_if_available(result, include_screenshot)
    if operation == "osnap":
        result = await backend.entity_osnap(
            float(data["x"]), float(data["y"]),
            modes=data.get("modes"),
            tolerance=float(data.get("tolerance", 5.0)),
        )
        return await add_screenshot_if_available(result, include_screenshot)

    # --- Bulk modify dispatcher ---
    # When `data` carries `entity_ids` or a `selection` dict, route the
    # modify op (copy/move/rotate/scale/erase) through a per-handle loop
    # and return aggregate results. The single-entity path below stays
    # untouched.
    bulk_handles = _extract_bulk_handles(data, entity_id)
    if bulk_handles is not None and operation in _BULK_MODIFY_OPS:
        return await _run_bulk_modify(
            backend, operation, bulk_handles, data, x1, y1, x2, y2,
        )

    # --- Create ---
    if operation == "create_line":
        result = await backend.create_line(x1, y1, x2, y2, layer)
    elif operation == "create_circle":
        result = await backend.create_circle(data["cx"], data["cy"], data["radius"], layer)
    elif operation == "create_polyline":
        result = await backend.create_polyline(points or [], data.get("closed", False), layer)
    elif operation == "create_rectangle":
        result = await backend.create_rectangle(x1, y1, x2, y2, layer)
    elif operation == "create_arc":
        result = await backend.create_arc(data["cx"], data["cy"], data["radius"], data["start_angle"], data["end_angle"], layer)
    elif operation == "create_ellipse":
        result = await backend.create_ellipse(data["cx"], data["cy"], data["major_x"], data["major_y"], data["ratio"], layer)
    elif operation == "create_mtext":
        result = await backend.create_mtext(data["x"], data["y"], data["width"], data["text"], data.get("height", 2.5), layer)
    elif operation == "create_hatch":
        result = await backend.create_hatch(entity_id, data.get("pattern", "ANSI31"))
    # --- Read ---
    elif operation == "list":
        if data.get("summary"):
            # Compact "what's in this drawing" mode — counts by layer + type,
            # plus a small sample. Token-efficient on 50k-entity drawings
            # where the default list payload would dwarf Claude's context.
            result = await backend.entity_summary(layer)
        else:
            # `limit` defaults to 1000 on the backend; pass 0 for unlimited.
            # Without a cap, listing every entity in a 100k-entity DWG produces
            # JSON output that's expensive to marshal back through MCP.
            result = await backend.entity_list(
                layer,
                limit=int(data.get("limit", 1000)),
            )
    elif operation == "count":
        result = await backend.entity_count(layer)
    elif operation == "get":
        result = await backend.entity_get(entity_id)
    # --- Modify ---
    elif operation == "copy":
        result = await backend.entity_copy(entity_id, data["dx"], data["dy"])
    elif operation == "move":
        result = await backend.entity_move(entity_id, data["dx"], data["dy"])
    elif operation == "rotate":
        result = await backend.entity_rotate(entity_id, data["cx"], data["cy"], data["angle"])
    elif operation == "scale":
        result = await backend.entity_scale(entity_id, data["cx"], data["cy"], data["factor"])
    elif operation == "mirror":
        result = await backend.entity_mirror(entity_id, x1, y1, x2, y2)
    elif operation == "offset":
        result = await backend.entity_offset(entity_id, data["distance"])
    elif operation == "array":
        result = await backend.entity_array(entity_id, data["rows"], data["cols"], data["row_dist"], data["col_dist"])
    elif operation == "fillet":
        result = await backend.entity_fillet(data["id1"], data["id2"], data["radius"])
    elif operation == "chamfer":
        result = await backend.entity_chamfer(data["id1"], data["id2"], data["dist1"], data["dist2"])
    elif operation == "erase":
        result = await backend.entity_erase(entity_id)
    # --- 3D primitives ---
    elif operation == "region":
        ids = data.get("entity_ids") or []
        if not ids:
            return _json({"error": "data.entity_ids required for region"})
        result = await backend.entity_region(ids)
    elif operation == "extrude":
        result = await backend.entity_extrude(
            entity_id,
            float(data["height"]),
            float(data.get("taper_angle", 0.0)),
        )
    elif operation == "revolve":
        result = await backend.entity_revolve(
            entity_id,
            float(data["axis_x"]), float(data["axis_y"]),
            float(data["axis_dx"]), float(data["axis_dy"]),
            axis_dz=float(data.get("axis_dz", 0.0)),
            angle=float(data.get("angle", 360.0)),
        )
    elif operation == "boolean":
        result = await backend.entity_boolean(
            data["operation"], data["target_id"], data.get("tool_ids") or [],
        )
    else:
        return _json({"error": f"Unknown entity operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 3. layer — Layer management
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Layer Operations", "readOnlyHint": False})
@_safe("layer")
async def layer(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Layer creation and management.

    Operations:
      list            — List all layers with properties.
      create          — data: {name, color?, linetype?}
      set_current     — data: {name}
      set_properties  — data: {name, color?, linetype?, lineweight?}
      freeze          — data: {name}
      thaw            — data: {name}
      lock            — data: {name}
      unlock          — data: {name}
    """
    data = data or {}
    backend = await get_backend()

    if operation != "list":
        invalidate_screenshot_cache()

    if operation == "list":
        result = await backend.layer_list()
    elif operation == "create":
        result = await backend.layer_create(data["name"], data.get("color", "white"), data.get("linetype", "CONTINUOUS"))
    elif operation == "set_current":
        result = await backend.layer_set_current(data["name"])
    elif operation == "set_properties":
        result = await backend.layer_set_properties(data["name"], data.get("color"), data.get("linetype"), data.get("lineweight"))
    elif operation == "freeze":
        result = await backend.layer_freeze(data["name"])
    elif operation == "thaw":
        result = await backend.layer_thaw(data["name"])
    elif operation == "lock":
        result = await backend.layer_lock(data["name"])
    elif operation == "unlock":
        result = await backend.layer_unlock(data["name"])
    else:
        return _json({"error": f"Unknown layer operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 4. block — Block operations
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Block Operations", "readOnlyHint": False})
@_safe("block")
async def block(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Block definition, insertion, and attribute management.

    Operations:
      list                 — List all block definitions in the active drawing.
      insert               — data: {name, x, y, scale?, rotation?, block_id?}
      insert_with_attributes — data: {name, x, y, scale?, rotation?, attributes: {tag: value}}
      get_attributes       — data: {entity_id}
      update_attribute     — data: {entity_id, tag, value}
      define               — data: {name, entities: [{type, ...}]}
      list_from_file       — data: {path} — inspect blocks in an external
                              .dwg/.dxf without modifying the active drawing.
                              Use this to pick named blocks for `import_from_file`.
                              DWG support requires accoreconsole or ODA File Converter.
      import_from_file     — data: {path, names?} — copy block definitions
                              from an external .dwg/.dxf into the active drawing.
                              `names=null` imports every non-anonymous, non-xref
                              block. Existing block names are preserved (skip,
                              don't overwrite); call `purge` first if you want
                              to refresh a definition.
    """
    data = data or {}
    backend = await get_backend()

    if operation not in ("list", "list_from_file", "get_attributes"):
        invalidate_screenshot_cache()

    if operation == "list":
        result = await backend.block_list()
    elif operation == "insert":
        result = await backend.block_insert(
            data["name"], data["x"], data["y"],
            data.get("scale", 1.0), data.get("rotation", 0.0), data.get("block_id"),
        )
    elif operation == "insert_with_attributes":
        result = await backend.block_insert_with_attributes(
            data["name"], data["x"], data["y"],
            data.get("scale", 1.0), data.get("rotation", 0.0), data.get("attributes"),
        )
    elif operation == "get_attributes":
        result = await backend.block_get_attributes(data["entity_id"])
    elif operation == "update_attribute":
        result = await backend.block_update_attribute(data["entity_id"], data["tag"], data["value"])
    elif operation == "define":
        result = await backend.block_define(data["name"], data.get("entities", []))
    elif operation == "list_from_file":
        result = await backend.block_list_from_file(data["path"])
    elif operation == "import_from_file":
        result = await backend.block_import_from_file(
            data["path"], names=data.get("names"),
        )
    else:
        return _json({"error": f"Unknown block operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 5. annotation — Text, dimensions, leaders
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Annotation Operations", "readOnlyHint": False})
@_safe("annotation")
async def annotation(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Annotation: text, dimensions, and leaders.

    Operations:
      create_text             — data: {x, y, text, height?, rotation?, layer?}
      create_dimension_linear — data: {x1, y1, x2, y2, dim_x, dim_y}
      create_dimension_aligned — data: {x1, y1, x2, y2, offset}
      create_dimension_angular — data: {cx, cy, x1, y1, x2, y2}
      create_dimension_radius — data: {cx, cy, radius, angle}
      create_leader           — data: {points: [[x,y],...], text}
      create_table            — data: {x, y, rows, cols, row_height?, col_width?,
                                       data?: [[...],...], title?, header?: [...]}
                                Real AcDbTable on COM; a grid of LINE+TEXT on
                                ezdxf (no native table API). `rows`/`cols` are
                                total counts including title + header rows;
                                `data` fills whatever remains.
    """
    data = data or {}
    backend = await get_backend()
    invalidate_screenshot_cache()

    if operation == "create_text":
        result = await backend.create_text(
            data["x"], data["y"], data["text"],
            data.get("height", 2.5), data.get("rotation", 0.0), data.get("layer"),
        )
    elif operation == "create_dimension_linear":
        result = await backend.create_dimension_linear(
            data["x1"], data["y1"], data["x2"], data["y2"], data["dim_x"], data["dim_y"],
        )
    elif operation == "create_dimension_aligned":
        result = await backend.create_dimension_aligned(
            data["x1"], data["y1"], data["x2"], data["y2"], data["offset"],
        )
    elif operation == "create_dimension_angular":
        result = await backend.create_dimension_angular(
            data["cx"], data["cy"], data["x1"], data["y1"], data["x2"], data["y2"],
        )
    elif operation == "create_dimension_radius":
        result = await backend.create_dimension_radius(
            data["cx"], data["cy"], data["radius"], data["angle"],
        )
    elif operation == "create_leader":
        result = await backend.create_leader(data["points"], data["text"])
    elif operation == "create_table":
        result = await backend.create_table(
            data["x"], data["y"],
            int(data["rows"]), int(data["cols"]),
            row_height=float(data.get("row_height", 0.5)),
            col_width=float(data.get("col_width", 2.0)),
            data=data.get("data"),
            title=data.get("title"),
            header=data.get("header"),
        )
    else:
        return _json({"error": f"Unknown annotation operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 6. pid — P&ID operations (CTO library)
# ==========================================================================


@mcp.tool(annotations={"title": "P&ID Operations (CTO Library)", "readOnlyHint": False})
@_safe("pid")
async def pid(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """P&ID drawing with CTO symbol library.

    Operations:
      setup_layers     — Create standard P&ID layers.
      insert_symbol    — data: {category, symbol, x, y, scale?, rotation?}
      list_symbols     — data: {category}
      draw_process_line — data: {x1, y1, x2, y2}
      connect_equipment — data: {x1, y1, x2, y2}
      add_flow_arrow   — data: {x, y, rotation?}
      add_equipment_tag — data: {x, y, tag, description?}
      add_line_number  — data: {x, y, line_num, spec}
      insert_valve     — data: {x, y, valve_type, rotation?, attributes?}
      insert_instrument — data: {x, y, instrument_type, rotation?, tag_id?, range_value?}
      insert_pump      — data: {x, y, pump_type, rotation?, attributes?}
      insert_tank      — data: {x, y, tank_type, scale?, attributes?}
    """
    data = data or {}
    backend = await get_backend()
    if operation != "list_symbols":
        invalidate_screenshot_cache()

    if operation == "setup_layers":
        result = await backend.pid_setup_layers()
    elif operation == "insert_symbol":
        result = await backend.pid_insert_symbol(
            data["category"], data["symbol"], data["x"], data["y"],
            data.get("scale", 1.0), data.get("rotation", 0.0),
        )
    elif operation == "list_symbols":
        result = await backend.pid_list_symbols(data["category"])
    elif operation == "draw_process_line":
        result = await backend.pid_draw_process_line(data["x1"], data["y1"], data["x2"], data["y2"])
    elif operation == "connect_equipment":
        result = await backend.pid_connect_equipment(data["x1"], data["y1"], data["x2"], data["y2"])
    elif operation == "add_flow_arrow":
        result = await backend.pid_add_flow_arrow(data["x"], data["y"], data.get("rotation", 0.0))
    elif operation == "add_equipment_tag":
        result = await backend.pid_add_equipment_tag(data["x"], data["y"], data["tag"], data.get("description", ""))
    elif operation == "add_line_number":
        result = await backend.pid_add_line_number(data["x"], data["y"], data["line_num"], data["spec"])
    elif operation == "insert_valve":
        result = await backend.pid_insert_valve(
            data["x"], data["y"], data["valve_type"],
            data.get("rotation", 0.0), data.get("attributes"),
        )
    elif operation == "insert_instrument":
        result = await backend.pid_insert_instrument(
            data["x"], data["y"], data["instrument_type"],
            data.get("rotation", 0.0), data.get("tag_id", ""), data.get("range_value", ""),
        )
    elif operation == "insert_pump":
        result = await backend.pid_insert_pump(
            data["x"], data["y"], data["pump_type"],
            data.get("rotation", 0.0), data.get("attributes"),
        )
    elif operation == "insert_tank":
        result = await backend.pid_insert_tank(
            data["x"], data["y"], data["tank_type"],
            data.get("scale", 1.0), data.get("attributes"),
        )
    else:
        return _json({"error": f"Unknown pid operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 7. arch — Architectural primitives (AIA/NCS)
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Architectural Primitives (AIA/NCS)", "readOnlyHint": False})
@_safe("arch")
async def arch(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Architectural floor-plan primitives on AIA/NCS layers.

    Operations:
      setup_layers     — Create the AIA standard layer set (A-WALL, A-DOOR,
                         A-GLAZ, A-ANNO-DIMS, A-AREA, etc.) on the drawing.
                         Idempotent.
      wall             — Double-line wall as closed polygon along centerline.
                         data: {x1, y1, x2, y2, thickness?, layer?, justify?}
                         justify: 'center' (default), 'left', 'right'
      wall_with_openings — Wall split into segments around door/window openings,
                         with the matching door/window symbols inserted automatically.
                         data: {x1, y1, x2, y2, thickness?, openings: [
                             {position, width, type: 'door'|'window',
                              swing?, open_angle?, sill_thickness?},
                             ...
                         ], layer?, door_layer?, window_layer?, justify?}
      door             — Door leaf line + swing arc at hinge.
                         data: {x, y, width?, angle?, swing?, open_angle?, layer?,
                                room_side?: [x, y]}
                         angle = orientation of closed door (deg, CCW from +X)
                         swing: 'left' (default) or 'right'
                         room_side: optional point inside the room. When given,
                                    overrides `swing` so the door opens toward it.
      window           — Sill rectangle + center glass line between two points.
                         data: {x1, y1, x2, y2, sill_thickness?, layer?}
      room             — Closed boundary polyline + area label at centroid.
                         data: {boundary: [[x,y],...], name?, area_layer?,
                                text_layer?, text_height?}
      dimension_chain  — Consecutive linear dimensions along a row of points.
                         data: {points: [[x,y],...], offset?, direction?, layer?}
                         direction: 'horizontal' | 'vertical' | 'aligned'

      stair            — Stair with treads + walking-line arrow.
                         data: {x1, y1, x2, y2, width?, num_treads?, tread_depth?,
                                layer?, shape?='straight'|'u', landing_depth?}
      column           — Structural column (square/rect/round).
                         data: {x, y, shape?='square'|'rect'|'round',
                                size?, size_y?, angle?, layer?}
      slab             — Floor slab outline with optional inner edge polyline.
                         data: {boundary: [[x,y],...], edge_thickness?, layer?}
      beam             — Overhead beam centerline / parallel edges.
                         data: {x1, y1, x2, y2, width?, layer?}
      roof             — Roof-plan: eaves + ridge (+ hip lines).
                         data: {boundary: [[x,y],...], style?='gable'|'hip'|'flat',
                                ridge_axis?='x'|'y', overhang?, eaves_layer?, ridge_layer?}
      elevation        — 2D elevation view of a single wall.
                         data: {origin_x, origin_y, wall_length, height,
                                openings?: [{position, width, height, sill?, type?}, ...],
                                wall_layer?, door_layer?, window_layer?}
      section          — 2D section between floor and ceiling.
                         data: {origin_x, origin_y, section_width, floor_y, ceiling_y,
                                elements?: [{type, ...}, ...],
                                wall_layer?, slab_layer?, door_layer?, window_layer?}
      template         — Parametric whole-building composite.
                         data: {name: 'residential_1br'|'residential_2br'|
                                       'residential_3br_house'|'commercial_open_office',
                                x, y, params?: {width?, depth?, wall_thickness?, ...}}
      hatch_region     — Apply an AEC hatch pattern to a closed polygon.
                         data: {boundary: [[x,y],...], pattern: 'BRICK'|'CONCRETE'|
                                'INSULATION'|'WOOD'|'EARTH'|... , layer?, scale?, angle?}
                         Call `list_hatches` for the catalogue.
      list_hatches     — Return the curated AEC hatch pattern catalogue
                         (friendly name → AutoCAD pattern name, default layer).
    """
    data = data or {}
    backend = await get_backend()
    if operation != "list_hatches":
        invalidate_screenshot_cache()

    if operation == "setup_layers":
        result = await backend.arch_setup_layers()
    elif operation == "wall":
        result = await backend.arch_wall(
            data["x1"], data["y1"], data["x2"], data["y2"],
            thickness=data.get("thickness", 6.0),
            layer=data.get("layer", "A-WALL"),
            justify=data.get("justify", "center"),
        )
    elif operation == "wall_with_openings":
        result = await backend.arch_wall_with_openings(
            data["x1"], data["y1"], data["x2"], data["y2"],
            thickness=data.get("thickness", 6.0),
            openings=data.get("openings", []),
            layer=data.get("layer", "A-WALL"),
            door_layer=data.get("door_layer", "A-DOOR"),
            window_layer=data.get("window_layer", "A-GLAZ"),
            justify=data.get("justify", "center"),
        )
    elif operation == "door":
        result = await backend.arch_door(
            data["x"], data["y"],
            width=data.get("width", 36.0),
            angle=data.get("angle", 0.0),
            swing=data.get("swing", "left"),
            open_angle=data.get("open_angle", 90.0),
            layer=data.get("layer", "A-DOOR"),
            room_side=data.get("room_side"),
        )
    elif operation == "window":
        result = await backend.arch_window(
            data["x1"], data["y1"], data["x2"], data["y2"],
            sill_thickness=data.get("sill_thickness", 6.0),
            layer=data.get("layer", "A-GLAZ"),
        )
    elif operation == "room":
        result = await backend.arch_room(
            data["boundary"],
            name=data.get("name", ""),
            area_layer=data.get("area_layer", "A-AREA"),
            text_layer=data.get("text_layer", "A-AREA-IDEN"),
            text_height=data.get("text_height", 6.0),
        )
    elif operation == "dimension_chain":
        result = await backend.arch_dimension_chain(
            data["points"],
            offset=data.get("offset", 24.0),
            direction=data.get("direction", "horizontal"),
            layer=data.get("layer", "A-ANNO-DIMS"),
        )
    elif operation == "stair":
        result = await backend.arch_stair(
            data["x1"], data["y1"], data["x2"], data["y2"],
            width=data.get("width", 36.0),
            num_treads=int(data.get("num_treads", 12)),
            tread_depth=data.get("tread_depth"),
            layer=data.get("layer", "A-FLOR-STRS"),
            shape=data.get("shape", "straight"),
            landing_depth=data.get("landing_depth"),
        )
    elif operation == "column":
        result = await backend.arch_column(
            data["x"], data["y"],
            shape=data.get("shape", "square"),
            size=data.get("size", 12.0),
            size_y=data.get("size_y"),
            angle=data.get("angle", 0.0),
            layer=data.get("layer", "A-COLS"),
        )
    elif operation == "slab":
        result = await backend.arch_slab(
            data["boundary"],
            edge_thickness=data.get("edge_thickness", 0.0),
            layer=data.get("layer", "A-FLOR"),
        )
    elif operation == "beam":
        result = await backend.arch_beam(
            data["x1"], data["y1"], data["x2"], data["y2"],
            width=data.get("width", 0.0),
            layer=data.get("layer", "A-WALL-HEAD"),
        )
    elif operation == "roof":
        result = await backend.arch_roof(
            data["boundary"],
            style=data.get("style", "gable"),
            ridge_axis=data.get("ridge_axis", "x"),
            overhang=data.get("overhang", 12.0),
            eaves_layer=data.get("eaves_layer", "A-ROOF"),
            ridge_layer=data.get("ridge_layer", "A-ROOF"),
        )
    elif operation == "elevation":
        result = await backend.arch_elevation(
            data["origin_x"], data["origin_y"],
            data["wall_length"], data["height"],
            openings=data.get("openings"),
            wall_layer=data.get("wall_layer", "A-WALL"),
            door_layer=data.get("door_layer", "A-DOOR"),
            window_layer=data.get("window_layer", "A-GLAZ"),
        )
    elif operation == "section":
        result = await backend.arch_section(
            data["origin_x"], data["origin_y"],
            data["section_width"],
            data["floor_y"], data["ceiling_y"],
            elements=data.get("elements"),
            wall_layer=data.get("wall_layer", "A-WALL"),
            slab_layer=data.get("slab_layer", "A-FLOR"),
            door_layer=data.get("door_layer", "A-DOOR"),
            window_layer=data.get("window_layer", "A-GLAZ"),
        )
    elif operation == "template":
        result = await backend.arch_template(
            data["name"], data["x"], data["y"],
            params=data.get("params"),
        )
    elif operation == "hatch_region":
        result = await backend.arch_hatch_region(
            data["boundary"],
            pattern=data.get("pattern", "CONCRETE"),
            layer=data.get("layer"),
            scale=data.get("scale"),
            angle=data.get("angle"),
        )
    elif operation == "list_hatches":
        result = await backend.arch_list_hatches()
    else:
        return _json({"error": f"Unknown arch operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 8. project — Multi-drawing project model (.acadproj.toml + SQLite index)
# ==========================================================================


# Module-level singletons so successive tool calls keep working against
# the same loaded project + database connection without reloading.
# Guarded by `_project_lock` since FastMCP can dispatch tool calls concurrently
# and `index` (in particular) mutates the singleton mid-operation.
import asyncio as _asyncio

_project_state: dict = {"config": None, "conn": None, "db_path": None}
_project_lock = _asyncio.Lock()


def _project_close() -> None:
    conn = _project_state.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception as ex:
            # Connection already closed / corrupted — log and move on; we're
            # about to drop the reference either way.
            log.debug("project_conn_close_failed", error=str(ex))
    _project_state["conn"] = None


@mcp.tool(annotations={"title": "AutoCAD Project Operations", "readOnlyHint": False})
@_safe("project")
async def project(
    operation: str,
    data: dict | None = None,
) -> ToolResult:
    """Multi-drawing project: `.acadproj.toml` + DXF index → SQLite.

    Operations:
      load           — data: {path} — load .acadproj.toml from a file or
                        directory. Replaces any previously loaded project.
      index          — data: {force?: bool, db_path?: str} — walk every
                        drawing and (re)populate the SQLite index. Writes the
                        db next to the toml unless `db_path` overrides.
      info           — high-level summary: drawing/entity/layer counts.
      list_drawings  — one row per drawing.
      list_layers    — data: {drawing?, discipline?} — layers across project.
      find_entities  — data: {layer?, type?, drawing?, limit?} — entity search.
      check_standard — data: {standard?: 'aia' | <path-to-dxf>,
                              drawing?, include_extra?, limit?}
                        Audit layer compliance against the AIA preset (default)
                        or a reference DXF. Reports missing / wrong_color /
                        wrong_linetype / extra violations per drawing.
      list_xrefs     — data: {drawing?} — external references discovered in
                        the indexed drawings, with each one's resolved path
                        and `is_broken` flag.
      find_broken_xrefs — XREFs whose target file is missing on disk.
      list_dimstyles — data: {drawing?} — dimension styles aggregated by name;
                        `distinct_configs > 1` surfaces same-name drift.
      list_textstyles — data: {drawing?} — text styles aggregated by name;
                        `distinct_fonts > 1` surfaces same-name font drift.
      find_style_inconsistencies — no args; lists every dimstyle/textstyle
                        name whose configuration disagrees across drawings.
      list_sheets    — every sheet in the project's .dst (if configured).
      find_sheets_for_drawing — data: {drawing} — sheets whose underlying
                        DWG matches the given filename or absolute path.
      list_linetypes — data: {drawing?} — linetypes aggregated by name with
                        `distinct_configs` drift column.
      list_blocks    — data: {drawing?} — block definitions aggregated with
                        `distinct_entity_counts`.
      find_linetype_inconsistencies — linetype names whose pattern attrs
                        disagree across drawings.
      find_block_inconsistencies   — block names whose entity_count differs
                        across drawings (weak signal — review before "fixing").
      rebuild        — convenience: unconditional re-index. Equivalent to
                        `index` with `data={"force": true}`. Use after upgrading
                        the package across a SCHEMA_VERSION bump, or whenever
                        the on-disk db looks stale.
      sidecar        — data: {drawing|path} — read the per-drawing
                        `<stem>.claude.md` Markdown sidecar verbatim.
                        Conventional location for architect-authored
                        context ("Type A walls = 2x6 GWB", "ceiling = 9'-0\"").
                        Pass `drawing=<name>` to resolve against the loaded
                        project, or `path=<absolute>` for any drawing.
      xref_attach    — data: {path, name?, x?, y?, scale?, rotation?, overlay?}
                        Attach an external DWG as XREF to the active drawing.
                        `overlay=true` makes it a non-cascading overlay.
                        Routed to the backend; does not require a loaded
                        `.acadproj.toml`.
      xref_detach    — data: {name} — detach an XREF.
      xref_reload    — data: {name?} — reload one XREF (or all if name omitted).
                        COM-backend only; no-op on ezdxf.
      xref_bind      — data: {name} — bind an XREF into the local block table.
                        COM-backend only.
      unload         — close the current project / db connection.
    """
    async with _project_lock:
        return await _project_impl(operation, data)


async def _project_impl(operation: str, data: dict | None) -> ToolResult:
    """Core dispatch for the `project` tool — must run under _project_lock."""
    from pathlib import Path

    from autocad_mcp.project import (
        DXFIndexer,
        aia_standard,
        check_project as q_check_project,
        find_block_inconsistencies as q_find_block_inconsistencies,
        find_broken_xrefs as q_find_broken_xrefs,
        find_entities as q_find_entities,
        find_linetype_inconsistencies as q_find_linetype_inconsistencies,
        find_sheets_for_drawing as q_find_sheets_for_drawing,
        find_style_inconsistencies as q_find_style_inconsistencies,
        init_db,
        list_blocks as q_list_blocks,
        list_dimstyles as q_list_dimstyles,
        list_drawings as q_list_drawings,
        list_layers as q_list_layers,
        list_linetypes as q_list_linetypes,
        list_sheets as q_list_sheets,
        list_textstyles as q_list_textstyles,
        list_xrefs as q_list_xrefs,
        load_project,
        load_standard_from_dxf,
        project_info as q_project_info,
    )

    data = data or {}

    if operation == "load":
        path = data.get("path")
        if not path:
            return _json({"error": "data.path is required"})
        try:
            cfg = load_project(path)
        except PermissionError as ex:
            return _json({
                "error": str(ex),
                "category": "invalid_arg",
                "hint": (
                    "AUTOCAD_MCP_PROJECT_ROOTS is set; the requested path is "
                    "outside the allowlist. Loosen the env var or move the "
                    "project under an allowed root."
                ),
            })
        except FileNotFoundError as ex:
            return _json({"error": str(ex), "category": "not_found"})
        _project_close()
        _project_state["config"] = cfg
        _project_state["conn"] = None
        _project_state["db_path"] = cfg.root / ".acadproj.db"
        return _json({
            "ok": True,
            "payload": {
                "name": cfg.name,
                "root": str(cfg.root),
                "drawings": [
                    {"name": d.name, "file": str(d.file), "discipline": d.discipline}
                    for d in cfg.drawings
                ],
                "drawing_count": len(cfg.drawings),
                "layer_standard": cfg.layer_standard,
            },
        })

    if operation == "unload":
        _project_close()
        _project_state["config"] = None
        _project_state["db_path"] = None
        return _json({"ok": True, "payload": {"unloaded": True}})

    # `sidecar` resolves a drawing path (either explicit `path` or
    # `drawing` looked up in the loaded project) and returns the
    # `<stem>.claude.md` contents verbatim. Doesn't require an index;
    # the loaded `cfg` is consulted only when `drawing=<name>` is used.
    if operation == "sidecar":
        from autocad_mcp.project.sidecar import read_sidecar, sidecar_path

        path = data.get("path")
        drawing = data.get("drawing")
        if not path and not drawing:
            return _json({"error": "data.path or data.drawing required"})
        if path is None:
            cfg = _project_state.get("config")
            if cfg is None:
                return _json({
                    "error": "data.drawing requires a loaded project — call project(operation='load') first",
                    "category": "invalid_arg",
                })
            ref = next((d for d in cfg.drawings if d.name == drawing), None)
            if ref is None:
                return _json({
                    "error": f"No drawing named {drawing!r} in the loaded project",
                    "category": "not_found",
                })
            path = str(ref.file)
        text = read_sidecar(path)
        return _json({
            "ok": True,
            "payload": {
                "path": path,
                "sidecar_path": str(sidecar_path(path)),
                "exists": text is not None,
                "content": text,
            },
        })

    # XREF mutation routes through the backend — these operate on the
    # *active drawing*, not the indexed db, so they don't need a project
    # loaded. Keep them above the cfg-required guard.
    if operation in ("xref_attach", "xref_detach", "xref_reload", "xref_bind"):
        backend = await get_backend()
        if operation == "xref_attach":
            result = await backend.xref_attach(
                data["path"],
                name=data.get("name"),
                x=data.get("x", 0.0),
                y=data.get("y", 0.0),
                scale=data.get("scale", 1.0),
                rotation=data.get("rotation", 0.0),
                overlay=bool(data.get("overlay", False)),
            )
        elif operation == "xref_detach":
            result = await backend.xref_detach(data["name"])
        elif operation == "xref_reload":
            result = await backend.xref_reload(data.get("name"))
        else:  # xref_bind
            result = await backend.xref_bind(data["name"])
        return _json(result.to_dict())

    cfg = _project_state["config"]
    if cfg is None and operation != "load":
        return _json({"error": "No project loaded. Call project(operation='load', data={'path': ...}) first."})

    if operation == "index" or operation == "rebuild":
        db_path = Path(data.get("db_path") or _project_state["db_path"])
        # `rebuild` is sugar for "index with force=True". `index` honors
        # data.force; `rebuild` always drops the db first.
        force = bool(data.get("force", False)) or operation == "rebuild"
        if force and db_path.exists():
            _project_close()
            db_path.unlink()
        _project_close()
        idx = DXFIndexer(db_path)
        result = idx.index_project(cfg)
        # Keep the connection open for subsequent queries; close the indexer
        # wrapper but reattach the connection it created.
        _project_state["conn"] = idx.conn
        _project_state["db_path"] = db_path
        return _json({
            "ok": True,
            "payload": {
                "db_path": str(db_path),
                "indexed": result.indexed,
                "skipped": result.skipped,
                "errors": result.errors,
                "rebuilt": force,
            },
        })

    # Lazily open a read-only connection if `index` hasn't been run this session
    # but a previous run left a db file on disk.
    conn = _project_state.get("conn")
    if conn is None:
        db_path = _project_state.get("db_path")
        if db_path and Path(db_path).exists():
            conn = init_db(db_path)
            _project_state["conn"] = conn
        else:
            return _json({
                "error": "Project index not built. Call project(operation='index')."
            })

    if operation == "info":
        return _json({"ok": True, "payload": q_project_info(conn)})
    if operation == "list_drawings":
        return _json({"ok": True, "payload": {"drawings": q_list_drawings(conn)}})
    if operation == "list_layers":
        payload = q_list_layers(
            conn,
            drawing=data.get("drawing"),
            discipline=data.get("discipline"),
        )
        return _json({"ok": True, "payload": {"layers": payload}})
    if operation == "find_entities":
        # `limit` defaults to 1000 (was 200) — a single floor plan typically
        # has more than 200 entities on `A-WALL` alone, and the previous
        # default routinely truncated mid-query, sending clients into a
        # refine-and-retry loop. Hard cap of 5000 enforced inside
        # `q_find_entities` itself.
        requested = int(data.get("limit", 1000))
        payload = q_find_entities(
            conn,
            layer=data.get("layer"),
            type=data.get("type"),
            drawing=data.get("drawing"),
            limit=requested,
        )
        # Report when the cap kicked in so callers can refine the query
        # instead of silently dropping rows.
        return _json({
            "ok": True,
            "payload": {
                "entities": payload,
                "count": len(payload),
                "limit": requested,
                "truncated": len(payload) == min(max(1, requested), 5000),
            },
        })
    if operation == "list_sheets":
        payload = q_list_sheets(conn)
        return _json({
            "ok": True,
            "payload": {
                "sheets": payload,
                "count": len(payload),
                "missing_dwg_count": sum(1 for s in payload if not s["is_dwg_present"]),
            },
        })
    if operation == "find_sheets_for_drawing":
        drawing = data.get("drawing")
        if not drawing:
            return _json({"error": "data.drawing is required"})
        payload = q_find_sheets_for_drawing(conn, drawing)
        return _json({
            "ok": True,
            "payload": {"sheets": payload, "count": len(payload)},
        })
    if operation == "list_dimstyles":
        payload = q_list_dimstyles(conn, drawing=data.get("drawing"))
        return _json({
            "ok": True,
            "payload": {"dimstyles": payload, "count": len(payload)},
        })
    if operation == "list_textstyles":
        payload = q_list_textstyles(conn, drawing=data.get("drawing"))
        return _json({
            "ok": True,
            "payload": {"textstyles": payload, "count": len(payload)},
        })
    if operation == "find_style_inconsistencies":
        payload = q_find_style_inconsistencies(conn)
        return _json({"ok": True, "payload": payload})
    if operation == "list_linetypes":
        payload = q_list_linetypes(conn, drawing=data.get("drawing"))
        return _json({
            "ok": True,
            "payload": {"linetypes": payload, "count": len(payload)},
        })
    if operation == "list_blocks":
        payload = q_list_blocks(conn, drawing=data.get("drawing"))
        return _json({
            "ok": True,
            "payload": {"blocks": payload, "count": len(payload)},
        })
    if operation == "find_linetype_inconsistencies":
        payload = q_find_linetype_inconsistencies(conn)
        return _json({
            "ok": True,
            "payload": {"conflicts": payload, "count": len(payload)},
        })
    if operation == "find_block_inconsistencies":
        payload = q_find_block_inconsistencies(conn)
        return _json({
            "ok": True,
            "payload": {"conflicts": payload, "count": len(payload)},
        })
    if operation == "list_xrefs":
        payload = q_list_xrefs(conn, drawing=data.get("drawing"))
        return _json({
            "ok": True,
            "payload": {
                "xrefs": payload,
                "count": len(payload),
                "broken_count": sum(1 for x in payload if x["is_broken"]),
            },
        })
    if operation == "find_broken_xrefs":
        payload = q_find_broken_xrefs(conn)
        return _json({
            "ok": True,
            "payload": {"broken_xrefs": payload, "count": len(payload)},
        })
    if operation == "check_standard":
        which = data.get("standard", "aia")
        if which == "aia":
            standard = aia_standard()
        else:
            standard = load_standard_from_dxf(which)
        payload = q_check_project(
            conn,
            standard,
            drawing_name=data.get("drawing"),
            include_extra=bool(data.get("include_extra", True)),
            limit=int(data.get("limit", 500)),
        )
        return _json({"ok": True, "payload": payload})

    return _json({"error": f"Unknown project operation: {operation}"})


# ==========================================================================
# 9. library — Procedural AEC symbol library (CC0, built-in)
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Symbol Library", "readOnlyHint": False})
@_safe("library")
async def library(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Built-in procedural symbol library + external block-library index.

    The *procedural* library is the curated CC0 catalogue defined in
    `autocad_mcp.arch.library.symbols` — ~22 reusable blocks named
    `<DISCIPLINE>-<KIND>` (e.g. `PLMB-TOILET`, `ELEC-OUTLET-DUPLEX`).
    All block-internal entities live on layer "0" so the insert's layer
    controls color/linetype.

    The *external* library is a SQLite-backed index of every block found
    in a directory of .dwg / .dxf files (e.g. a vendor's furniture pack).
    Build it with `scan_external`, search it with `search_external`, then
    drop a result into the active drawing with `block(operation="import_from_file", data={"path": ..., "names": [...]})`.

    Operations (procedural):
      list         — names of every built-in procedural symbol.
      define       — data: {name} — define the block in the current drawing.
                     Idempotent.
      define_all   — define every procedural library block at once.
      insert       — data: {name, x, y, scale?, rotation?}
                     Defines the block on demand, then inserts it.

    Operations (external library):
      scan_external    — data: {roots: ["dir1", "dir2"], db_path?: str,
                                 recursive?: bool=true}
                         Walk each directory, extract every non-anonymous
                         non-XREF block from every .dwg/.dxf, write to the
                         SQLite index. Returns counts + per-file errors.
                         `db_path` defaults to `<tempdir>/autocad-mcp/block_library.db`.
      search_external  — data: {name?, prefix?, file?, limit?: 200, db_path?}
                         Search the index. `name` is a substring match on
                         block_name (case-insensitive); `prefix` is exact
                         match on discipline (FURN, PLMB, ELEC, etc.);
                         `file` is substring on source filename.
      stats_external   — data: {db_path?} — summary: total blocks, files
                         indexed, top discipline prefixes.

    Operations (structured asset catalog — `asset-library/`):
      catalog_reindex  — data: {root?} — rescan `<root>/asset-library/`, rebuild
                         the SQLite cache. Run once after dropping new assets in.
      catalog_list     — data: {root?} — return the category index +
                         counts (e.g. furniture/seating → 1, fixtures/plumbing → 5).
      catalog_search   — data: {query?, category?, discipline?, tag?,
                                 max_width?, limit?: 50, root?}
                         Keyword + filter search. `query` matches name +
                         tags + notes excerpt.
      catalog_get      — data: {asset_id, root?} — full asset record incl.
                         dimensions, default layer, file paths.
      catalog_insert   — data: {asset_id, x, y, scale?, rotation?, layer?, root?}
                         Two-phase: imports the block definition from the
                         asset's `block.dxf`, then inserts an instance on
                         the asset's default layer (or `layer` override).
      vendor_import    — data: {source: "<dir>", vendor_name?: str,
                                 recursive?: bool=true, min_entity_count?: int=3,
                                 root?, reindex?: bool=true}
                         Walk a directory of vendor `.dwg`/`.dxf` files
                         (e.g. `furniture and details/`) and turn every
                         block inside into a catalog asset. Each asset
                         points at the **source** DWG — no geometry is
                         copied. Category is inferred from filename;
                         tags from filename + block name. Assets land
                         under `<root>/vendor/<vendor_name>/...` (the
                         starter `.gitignore` excludes `vendor/`). After
                         importing, the catalog is reindexed unless
                         `reindex=false`.
    """
    data = data or {}
    backend = await get_backend()

    # Library reads (`list`, `search_external`, `stats_external`, every
    # `catalog_*` except `catalog_insert`) leave the drawing untouched.
    _READ_LIB_OPS = {
        "list", "search_external", "stats_external",
        "catalog_list", "catalog_search", "catalog_get", "catalog_stats",
    }
    if operation not in _READ_LIB_OPS:
        invalidate_screenshot_cache()

    if operation == "list":
        result = await backend.library_list()
    elif operation == "define":
        if not data.get("name"):
            return _json({"error": "data.name is required"})
        result = await backend.library_define(data["name"])
    elif operation == "define_all":
        result = await backend.library_define_all()
    elif operation == "insert":
        if not data.get("name"):
            return _json({"error": "data.name is required"})
        result = await backend.library_insert(
            data["name"],
            data["x"], data["y"],
            scale=data.get("scale", 1.0),
            rotation=data.get("rotation", 0.0),
        )
    elif operation in ("scan_external", "search_external", "stats_external"):
        # External library lives in its own SQLite db, independent of the
        # project index. Defaults to <tempdir>/autocad-mcp/block_library.db
        # so casual users don't have to specify a path.
        from autocad_mcp.config import default_ipc_dir
        from autocad_mcp.project.block_library import (
            init_library_db, scan_library, search_library, library_stats,
        )

        db_path = data.get("db_path") or str(default_ipc_dir() / "block_library.db")

        def _scan_sync() -> dict:
            conn = init_library_db(db_path)
            try:
                r = scan_library(
                    conn,
                    data.get("roots", []),
                    recursive=bool(data.get("recursive", True)),
                )
                return {
                    "db_path": db_path,
                    "files_scanned": r.files_scanned,
                    "blocks_indexed": r.blocks_indexed,
                    "files_failed": r.files_failed,
                }
            finally:
                conn.close()

        def _search_sync() -> dict:
            conn = init_library_db(db_path)
            try:
                rows = search_library(
                    conn,
                    name=data.get("name"),
                    prefix=data.get("prefix"),
                    file=data.get("file"),
                    limit=int(data.get("limit", 200)),
                )
                return {
                    "db_path": db_path,
                    "results": rows,
                    "count": len(rows),
                }
            finally:
                conn.close()

        def _stats_sync() -> dict:
            conn = init_library_db(db_path)
            try:
                s = library_stats(conn)
                s["db_path"] = db_path
                return s
            finally:
                conn.close()

        if operation == "scan_external":
            payload = await _asyncio.to_thread(_scan_sync)
        elif operation == "search_external":
            payload = await _asyncio.to_thread(_search_sync)
        else:
            payload = await _asyncio.to_thread(_stats_sync)
        return _json({"ok": True, "payload": payload})
    elif operation in (
        "catalog_reindex", "catalog_list", "catalog_search",
        "catalog_get", "catalog_insert", "catalog_stats",
        "vendor_import",
    ):
        from autocad_mcp.library.catalog import (
            AssetCatalog, default_library_root,
        )
        from autocad_mcp.library.insert import insert_asset

        root = data.get("root") or default_library_root()

        def _open_catalog() -> AssetCatalog:
            return AssetCatalog(root)

        if operation == "catalog_reindex":
            def _reindex_sync() -> dict:
                cat = _open_catalog()
                try:
                    return cat.reindex()
                finally:
                    cat.close()
            payload = await _asyncio.to_thread(_reindex_sync)
            return _json({"ok": True, "payload": payload})

        if operation == "catalog_list":
            def _list_sync() -> dict:
                cat = _open_catalog()
                try:
                    return {
                        "root": str(cat.root),
                        "categories": cat.list_categories(),
                    }
                finally:
                    cat.close()
            payload = await _asyncio.to_thread(_list_sync)
            return _json({"ok": True, "payload": payload})

        if operation == "catalog_stats":
            def _stats_cat_sync() -> dict:
                cat = _open_catalog()
                try:
                    return cat.stats()
                finally:
                    cat.close()
            payload = await _asyncio.to_thread(_stats_cat_sync)
            return _json({"ok": True, "payload": payload})

        if operation == "catalog_search":
            def _search_cat_sync() -> dict:
                cat = _open_catalog()
                try:
                    results = cat.search(
                        query=data.get("query"),
                        category=data.get("category"),
                        discipline=data.get("discipline"),
                        tag=data.get("tag"),
                        max_width=data.get("max_width"),
                        limit=int(data.get("limit", 50)),
                    )
                    return {
                        "root": str(cat.root),
                        "count": len(results),
                        "results": [_asset_to_dict(a) for a in results],
                    }
                finally:
                    cat.close()
            payload = await _asyncio.to_thread(_search_cat_sync)
            return _json({"ok": True, "payload": payload})

        if operation == "catalog_get":
            asset_id = data.get("asset_id")
            if not asset_id:
                return _json({"error": "data.asset_id is required"})

            def _get_sync() -> dict | None:
                cat = _open_catalog()
                try:
                    a = cat.get(asset_id)
                    return _asset_to_dict(a) if a else None
                finally:
                    cat.close()
            payload = await _asyncio.to_thread(_get_sync)
            if payload is None:
                return _json({
                    "error": f"No asset with id {asset_id!r}",
                    "category": "not_found",
                })
            return _json({"ok": True, "payload": payload})

        if operation == "catalog_insert":
            asset_id = data.get("asset_id")
            if not asset_id:
                return _json({"error": "data.asset_id is required"})
            # Open the catalog briefly to resolve the asset, then close
            # before doing the async backend work so we don't hold the
            # connection across the to_thread boundary.
            def _resolve_sync():
                cat = _open_catalog()
                try:
                    return cat.get(asset_id)
                finally:
                    cat.close()
            asset = await _asyncio.to_thread(_resolve_sync)
            if asset is None:
                return _json({
                    "error": f"No asset with id {asset_id!r}",
                    "category": "not_found",
                })
            # Insert needs x/y — validate before float() raises a KeyError
            # that surfaces as a confusing stacktrace through the MCP layer.
            if "x" not in data or "y" not in data:
                return _json({
                    "error": "data.x and data.y are required for catalog_insert",
                    "category": "invalid_arg",
                })
            try:
                ix, iy = float(data["x"]), float(data["y"])
            except (TypeError, ValueError) as ex:
                return _json({
                    "error": f"x and y must be numeric: {ex}",
                    "category": "invalid_arg",
                })
            result = await insert_asset(
                backend, asset,
                ix, iy,
                scale=float(data.get("scale", 1.0)),
                rotation=float(data.get("rotation", 0.0)),
                layer=data.get("layer"),
            )

        if operation == "vendor_import":
            source = data.get("source")
            if not source:
                return _json({"error": "data.source is required (path to vendor DWG dir)"})
            from autocad_mcp.library.vendor import import_vendor_library

            def _vendor_import_sync() -> dict:
                vr = import_vendor_library(
                    source_dir=source,
                    target_root=root,
                    vendor_name=data.get("vendor_name", "vendor"),
                    recursive=bool(data.get("recursive", True)),
                    min_entity_count=int(data.get("min_entity_count", 3)),
                    include_autonamed=bool(data.get("include_autonamed", False)),
                )
                payload = {
                    "target_root": vr.target_root,
                    "files_scanned": vr.files_scanned,
                    "files_failed": vr.files_failed,
                    "blocks_imported": vr.blocks_imported,
                    "blocks_skipped": vr.blocks_skipped,
                    # Sample the first 20 ids in the response; full list
                    # would dwarf the rest of the payload for big imports.
                    "sample_asset_ids": vr.asset_ids[:20],
                    "asset_ids_count": len(vr.asset_ids),
                }
                # Optional reindex so the new vendor assets are
                # immediately searchable.
                if data.get("reindex", True):
                    cat = _open_catalog()
                    try:
                        payload["reindex"] = cat.reindex()
                    finally:
                        cat.close()
                return payload

            payload = await _asyncio.to_thread(_vendor_import_sync)
            return _json({"ok": True, "payload": payload})
    else:
        return _json({"error": f"Unknown library operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ---------------------------------------------------------------------------
# Turn-scoped undo group + entity diff
# ---------------------------------------------------------------------------
#
# `system.turn_begin` / `turn_end` let the MCP client bracket a sequence of
# tool calls. Inside the bracket, AutoCAD's UNDO command records one group
# so a single Ctrl-Z reverts the whole turn. We also snapshot the entity
# inventory at begin and diff at end so the response reports
# `{added, erased, by_layer: {A-WALL: +8, A-DOOR: +4, ...}}` automatically.

_turn_state: dict = {
    "active": False,
    "label": None,
    "started_at": None,
    "snapshot": None,
    "undo_started": False,
}


async def _undo_group_begin(backend, label: str) -> tuple[bool, str | None]:
    """Start an AutoCAD undo group on the active backend.

    Uses the `_-UNDO _Begin` / `_End` command pair which AutoCAD has
    supported since the 90s and which works the same on COM and file_ipc.
    Returns `(ok, error)` — the caller decides whether to surface the
    error or push on without a group.
    """
    try:
        # Doesn't need the user-gated `execute_lisp` env var because the
        # payload is a fixed command string, not arbitrary user input.
        r = await backend.execute_lisp(f'(command "_.UNDO" "_BEGIN")')
        return (r.ok, r.error)
    except Exception as ex:
        return (False, str(ex))


async def _undo_group_end(backend) -> tuple[bool, str | None]:
    try:
        r = await backend.execute_lisp(f'(command "_.UNDO" "_END")')
        return (r.ok, r.error)
    except Exception as ex:
        return (False, str(ex))


def _compute_turn_diff(before: dict, after: dict) -> dict:
    """Diff two `entity_summary` payloads. The exact set of *modifications*
    is not visible from counts alone, so we report `added` / `erased` only
    and surface a per-layer delta for navigation."""
    before_layers = before.get("by_layer") or {}
    after_layers = after.get("by_layer") or {}
    all_layers = set(before_layers) | set(after_layers)
    by_layer_delta: dict[str, int] = {}
    for layer in all_layers:
        delta = after_layers.get(layer, 0) - before_layers.get(layer, 0)
        if delta != 0:
            by_layer_delta[layer] = delta
    total_before = int(before.get("total", 0))
    total_after = int(after.get("total", 0))
    net = total_after - total_before
    return {
        "total_before": total_before,
        "total_after": total_after,
        "net_change": net,
        # Net positive = added; net negative = erased. Exact split would
        # need reactor data — surface what we can derive from counts.
        "added_or_modified": max(0, net),
        "erased": max(0, -net),
        "by_layer": dict(sorted(by_layer_delta.items(), key=lambda kv: -abs(kv[1]))),
    }


# ---------------------------------------------------------------------------
# Bulk modify dispatcher
# ---------------------------------------------------------------------------
#
# `entity.{copy,move,rotate,scale,erase}` originally took a single
# `entity_id`. Cluster A adds two new shapes:
#   * `entity_ids = [...]`       — explicit list
#   * `selection = {handles: [...]}` — output of `entity.select`
# When either is present, we loop the op per-handle and report aggregate
# results: `{processed, failed, handles_in, handles_out?}`. Failures are
# per-handle and don't abort the batch.

# Ops that have meaningful per-handle semantics. `mirror` / `fillet` /
# `chamfer` take a different shape (two handles or two points), so they
# stay single-entity-only.
_BULK_MODIFY_OPS = {"copy", "move", "rotate", "scale", "erase", "offset", "array"}


def _extract_bulk_handles(data: dict, single_entity_id: str | None) -> list[str] | None:
    """Return a list of handles to operate on, or None for the single-entity
    fast path.

    Priority: `data.entity_ids` > `data.selection.handles`. A single
    `entity_id` argument is the legacy path and bypasses this dispatcher.
    """
    ids = data.get("entity_ids")
    if ids:
        return [str(h) for h in ids]
    sel = data.get("selection")
    if isinstance(sel, dict) and sel.get("handles"):
        return [str(h) for h in sel["handles"]]
    return None


async def _run_bulk_modify(
    backend, op: str, handles: list[str], data: dict,
    x1, y1, x2, y2,
) -> str:
    """Apply `op` to each handle in turn. Returns the JSON-encoded payload."""
    processed = 0
    failed: list[dict] = []
    handles_out: list[str] = []

    for h in handles:
        try:
            if op == "copy":
                r = await backend.entity_copy(h, data["dx"], data["dy"])
            elif op == "move":
                r = await backend.entity_move(h, data["dx"], data["dy"])
            elif op == "rotate":
                r = await backend.entity_rotate(
                    h, data["cx"], data["cy"], data["angle"],
                )
            elif op == "scale":
                r = await backend.entity_scale(
                    h, data["cx"], data["cy"], data["factor"],
                )
            elif op == "erase":
                r = await backend.entity_erase(h)
            elif op == "offset":
                r = await backend.entity_offset(h, data["distance"])
            elif op == "array":
                r = await backend.entity_array(
                    h, data["rows"], data["cols"],
                    data["row_dist"], data["col_dist"],
                )
            else:
                r = None
            if r is None or not r.ok:
                failed.append({
                    "handle": h,
                    "error": (r.error if r else "no result"),
                    "category": (r.category if r else None),
                })
                continue
            processed += 1
            # Copy / offset / array create new entities; capture their
            # handles when the per-op payload exposes them.
            new_handle = None
            if isinstance(r.payload, dict):
                new_handle = r.payload.get("handle") or r.payload.get("new_handle")
            if new_handle:
                handles_out.append(new_handle)
        except Exception as ex:
            failed.append({"handle": h, "error": str(ex)})

    payload = {
        "operation": op,
        "handles_in": handles,
        "processed": processed,
        "failed": failed,
    }
    if handles_out:
        payload["handles_out"] = handles_out
    return _json({"ok": len(failed) == 0, "payload": payload})


def _preflight_sync() -> dict:
    """Inventory every external dependency. Surface "missing X — run Y"
    style hints in `warnings` rather than failing.

    Sections:
      backend   — which backend auto-selects + whether AutoCAD is detected
      dwg       — converter availability (accoreconsole / ODA)
      dotnet    — DLL deployment (SheetSetReader + MCPReactor)
      lisp      — companion files presence on the Support File Search Path
      catalog   — asset-library state
      env       — relevant env vars
    """
    import os
    import sys
    from pathlib import Path

    from autocad_mcp import __version__
    from autocad_mcp.config import (
        IPC_DIR, _current_backend_env, _running_autocad_full_version,
        default_ipc_dir, lisp_exec_enabled,
    )
    from autocad_mcp.project.dwg_reader import available_dwg_converters

    warnings: list[str] = []
    repo_root = Path(__file__).resolve().parent.parent.parent

    # Backend selection
    backend_env = _current_backend_env()
    try:
        full_acad_version = _running_autocad_full_version()
    except Exception:
        full_acad_version = None
    if backend_env == "auto":
        # The actual auto-selection logic — duplicated here so preflight
        # can surface what will happen without spawning the backend.
        if full_acad_version:
            chosen = "com (full AutoCAD detected)"
        else:
            chosen = "file_ipc or ezdxf (depending on AutoCAD LT window)"
    else:
        chosen = backend_env

    # .NET DLLs
    bin_dir = repo_root / "bin" / "dotnet"
    sheetset_dll = bin_dir / "SheetSetReader.dll"
    reactor_dll = bin_dir / "MCPReactor.dll"
    if not sheetset_dll.exists():
        warnings.append(
            "SheetSetReader.dll missing — binary .dst sheet-set reading "
            "won't work. Build via `dotnet build -c Release "
            "dotnet/SheetSetReader/SheetSetReader.csproj` then copy to "
            "bin/dotnet/."
        )
    if not reactor_dll.exists():
        warnings.append(
            "MCPReactor.dll missing — the .NET reactor for "
            "`events.start_recording` won't NETLOAD; the LISP reactor "
            "(less reliable on AutoCAD 2024+) is the fallback."
        )

    # LISP files
    lisp_dir = repo_root / "lisp-code"
    lisp_files = [
        "mcp_dispatch.lsp", "mcp_reactors.lsp",
        "arch_tools.lsp", "library_tools.lsp",
        "pid_tools.lsp", "attribute_tools.lsp",
    ]
    lisp_present = {n: (lisp_dir / n).exists() for n in lisp_files}
    if not all(lisp_present.values()):
        missing = [n for n, ok in lisp_present.items() if not ok]
        warnings.append(f"LISP companion files missing: {missing}")

    # Converters
    converters = available_dwg_converters()
    if not any(converters.values()):
        warnings.append(
            "No DWG converter found. Without accoreconsole.exe (full "
            "AutoCAD 2018+) or ODA File Converter, headless DWG read / "
            "write is unavailable. `drawing.save_as_dwg` and `drawing.open` "
            "against .dwg paths will return `backend_unavailable`."
        )

    # Asset catalog
    try:
        from autocad_mcp.library.catalog import (
            AssetCatalog, default_library_root,
        )
        root = default_library_root()
        if not root.exists():
            warnings.append(
                "asset-library/ not present. Run "
                "`python -m autocad_mcp.library.seed` to generate the "
                "starter content."
            )
            cat_stats = {"present": False}
        else:
            cat = AssetCatalog(root)
            try:
                cat_stats = cat.stats()
                cat_stats["present"] = True
                if cat_stats["total_assets"] == 0:
                    warnings.append(
                        "asset-library/ exists but the catalog is empty. "
                        "Run `library(operation='catalog_reindex')` to populate."
                    )
            finally:
                cat.close()
    except Exception as ex:
        cat_stats = {"present": False, "error": str(ex)}

    # Env vars worth surfacing
    env = {
        "AUTOCAD_MCP_BACKEND": os.environ.get("AUTOCAD_MCP_BACKEND", "auto"),
        "AUTOCAD_MCP_IPC_DIR": str(IPC_DIR),
        "AUTOCAD_MCP_ENABLE_LISP_EXEC": (
            "enabled" if lisp_exec_enabled() else "disabled (default)"
        ),
        "AUTOCAD_MCP_WINDOW_HINT": os.environ.get("AUTOCAD_MCP_WINDOW_HINT") or None,
        "AUTOCAD_MCP_PROJECT_ROOTS": os.environ.get("AUTOCAD_MCP_PROJECT_ROOTS") or None,
    }

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.executable,
        "backend": {
            "env": backend_env,
            "auto_selects": chosen,
            "full_autocad_registered": full_acad_version,
        },
        "dwg": {
            "converters": converters,
            "can_read_dwg": bool(any(converters.values())),
            "can_write_dwg": bool(any(converters.values())),
            "preferred": (
                "accoreconsole" if converters["accoreconsole"]
                else "oda_file_converter" if converters["oda_file_converter"]
                else None
            ),
        },
        "dotnet": {
            "sheetset_reader_dll": (
                str(sheetset_dll) if sheetset_dll.exists() else None
            ),
            "mcp_reactor_dll": (
                str(reactor_dll) if reactor_dll.exists() else None
            ),
        },
        "lisp": {
            "dir": str(lisp_dir),
            "files": lisp_present,
        },
        "catalog": cat_stats,
        "env_vars": env,
        "warnings": warnings,
        "ready": not warnings,
    }


def _tool_help_one(tool, *, summary_only: bool = False) -> dict:
    """Format a FastMCP `Tool` for the `tool_help` payload.

    `summary_only=True` returns just the index shape (name + title +
    one-line summary). Falsy → returns the full docstring + parameter
    schema so callers see every operation enumerated.
    """
    desc = tool.description or ""
    # First non-blank line is the headline. Falls back to the tool name
    # if the docstring is empty.
    summary = next(
        (line.strip() for line in desc.splitlines() if line.strip()),
        tool.name,
    )
    annotations = getattr(tool, "annotations", None)
    title = None
    if annotations is not None:
        try:
            title = annotations.title if hasattr(annotations, "title") else None
        except Exception:
            title = None
    out: dict = {
        "name": tool.name,
        "title": title,
        "summary": summary,
    }
    if not summary_only:
        out["description"] = desc
        out["parameters"] = tool.parameters
    return out


def _asset_to_dict(a) -> dict:
    """Serialize an Asset for JSON output. Paths → strings."""
    return {
        "id": a.id,
        "name": a.name,
        "category": a.category,
        "discipline": a.discipline,
        "tags": a.tags,
        "size": {
            "width": a.width, "depth": a.depth, "height": a.height,
            "units": a.units,
        } if any([a.width, a.depth, a.height]) else None,
        "default_layer": a.default_layer,
        "license": a.license,
        "inserts_as": a.inserts_as,
        "block_file": str(a.block_file) if a.block_file else None,
        "block_name": a.block_name,
        "thumbnail": str(a.thumbnail) if a.thumbnail else None,
        "notes_excerpt": a.notes_excerpt,
    }


# ==========================================================================
# 9b. style — DIMSTYLE / TEXTSTYLE / MLEADERSTYLE
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Style Operations", "readOnlyHint": False})
@_safe("style")
async def style(
    operation: str,
    data: dict | None = None,
) -> ToolResult:
    """Dimstyle / textstyle / mleaderstyle management.

    Operations:
      list_dimstyles         — Enumerate dimstyles with headline DXF attribs
                                (`dimscale`, `dimtxt`, `dimasz`, `dimdec`, …).
                                Response also carries `current` (`$DIMSTYLE`).
      get_dimstyle           — data: {name} — full attribute dict for one dimstyle.
      create_dimstyle        — data: {name, attribs: {dimscale, dimtxt, ...}}
                                Create or overwrite. Existing dimstyles get their
                                listed attribs applied; unspecified attribs are
                                preserved.
      set_current_dimstyle   — data: {name} — sets `$DIMSTYLE`.

      list_textstyles        — Enumerate textstyles with `font`, `height`,
                                `width`, `oblique`. Response carries `current`.
      get_textstyle          — data: {name}
      create_textstyle       — data: {name, font?, height?, width_factor?, oblique?}
      set_current_textstyle  — data: {name}

      list_mleaderstyles     — Enumerate mleader styles (no per-style attribs
                                yet — those are too varied across versions).
      set_current_mleaderstyle — data: {name}

    Use `system.tool_help` to discover this tool's parameters from the
    MCP runtime. For a quick standards-enforcement check, pair
    `list_dimstyles` with `project.find_style_inconsistencies` —
    the project tool flags same-name / different-attribs drift across
    drawings, this tool lets you fix them.
    """
    data = data or {}
    backend = await get_backend()
    # Style writes affect the next drawn entity but don't invalidate the
    # current screenshot — the in-window geometry hasn't changed.
    if operation in (
        "create_dimstyle", "set_current_dimstyle",
        "create_textstyle", "set_current_textstyle",
        "set_current_mleaderstyle",
    ):
        invalidate_screenshot_cache()

    if operation == "list_dimstyles":
        result = await backend.style_list_dimstyles()
    elif operation == "get_dimstyle":
        result = await backend.style_get_dimstyle(data["name"])
    elif operation == "create_dimstyle":
        result = await backend.style_create_dimstyle(
            data["name"], data.get("attribs", {}),
        )
    elif operation == "set_current_dimstyle":
        result = await backend.style_set_current_dimstyle(data["name"])
    elif operation == "list_textstyles":
        result = await backend.style_list_textstyles()
    elif operation == "get_textstyle":
        result = await backend.style_get_textstyle(data["name"])
    elif operation == "create_textstyle":
        result = await backend.style_create_textstyle(
            data["name"],
            font=data.get("font"),
            height=float(data.get("height", 0.0)),
            width_factor=float(data.get("width_factor", 1.0)),
            oblique=float(data.get("oblique", 0.0)),
        )
    elif operation == "set_current_textstyle":
        result = await backend.style_set_current_textstyle(data["name"])
    elif operation == "list_mleaderstyles":
        result = await backend.style_list_mleaderstyles()
    elif operation == "set_current_mleaderstyle":
        result = await backend.style_set_current_mleaderstyle(data["name"])
    else:
        return _json({"error": f"Unknown style operation: {operation}"})

    return _json(result.to_dict())


# ==========================================================================
# 9c. express — Express Tools wrappers (full AutoCAD only)
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Express Tools", "readOnlyHint": False})
@_safe("express")
async def express(
    operation: str,
    data: dict | None = None,
) -> ToolResult:
    """Wrappers around the AutoCAD Express Tools commands.

    Express Tools ship with **full AutoCAD only** — AutoCAD LT has none
    of these. On the ezdxf backend, `layiso` has a useful headless
    emulation (toggles layer.is_off); the rest return `not_supported`.

    Operations:
      burst       — data: {entity_ids: [handles]} — EXPLODE INSERTs but
                    keep attribute values as TEXT/MTEXT entities. Vanilla
                    EXPLODE drops the attribute strings; BURST keeps them.
      flatten     — data: {entity_ids?: [...], z?: float=0} — collapse
                    every entity (or those in `entity_ids`) to Z=0. The
                    `z` param is COM-side ignored today; AutoCAD's
                    FLATTEN always flattens to 0.
      overkill    — data: {entity_ids?: [...]} — remove duplicate /
                    overlapping entities. Massive cleanup for imported
                    drawings.
      layiso      — data: {layers: [name, name, ...]} — isolate the named
                    layers, turn off everything else. ezdxf-friendly via
                    `Layer.off()`; COM uses `-LAYER _OFF *` + `_ON <name>`.
      txt2mtxt    — data: {entity_ids: [handles]} — combine TEXT entities
                    into one MTEXT (Express Tools TXT2MTXT).
    """
    data = data or {}
    backend = await get_backend()
    invalidate_screenshot_cache()

    if operation == "burst":
        result = await backend.express_burst(data.get("entity_ids", []))
    elif operation == "flatten":
        result = await backend.express_flatten(
            entity_ids=data.get("entity_ids"),
            z=float(data.get("z", 0.0)),
        )
    elif operation == "overkill":
        result = await backend.express_overkill(
            entity_ids=data.get("entity_ids"),
        )
    elif operation == "layiso":
        result = await backend.express_layiso(data.get("layers") or [])
    elif operation == "txt2mtxt":
        result = await backend.express_txt2mtxt(data.get("entity_ids", []))
    else:
        return _json({"error": f"Unknown express operation: {operation}"})

    return _json(result.to_dict())


# ==========================================================================
# 10. events — AutoCAD reactor event stream (Claude watches the draw)
# ==========================================================================


# Module-level singleton tail; one log per process is the right shape.
# Caveat: `EventTail()` resolves `AUTOCAD_MCP_IPC_DIR` once at first
# instantiation. If the env var is changed after the server has started,
# this singleton's `path` is stale until the process restarts — by design,
# since changing the IPC dir mid-session would also break the LISP side.
_event_tail = None


def _get_event_tail():
    """Lazy-construct the global EventTail. Reset by tests via monkeypatch."""
    global _event_tail
    if _event_tail is None:
        from autocad_mcp.events import EventTail
        _event_tail = EventTail()
    return _event_tail


@mcp.tool(annotations={"title": "AutoCAD Event Stream", "readOnlyHint": False})
@_safe("events")
async def events(
    operation: str,
    data: dict | None = None,
) -> ToolResult:
    """Bidirectional pairing: surface user actions in AutoCAD as a stream.

    Two reactor implementations write the same JSON-Lines log at
    `<ipc-dir>/autocad_mcp_events.log`; `start_recording` picks whichever
    is appropriate for the active backend:

    * `.NET ManagedReactor` (bin/dotnet/MCPReactor.dll) — preferred on the
      COM backend. Reliable `ObjectAppended` / `ObjectModified` /
      `ObjectErased` events on AutoCAD 2024+ where the equivalent LISP
      reactor callbacks fire unpredictably. Also adds `selection_changed`
      events that the LISP reactor can't reach. NETLOADed on demand.
    * `LISP reactor` (lisp-code/mcp_reactors.lsp) — fallback for the
      file_ipc backend (AutoCAD LT). Uses VLR reactors plus a forward-scan
      workaround at command boundaries.

    Either way, every record carries:
      `{ts, ts_local, kind, handle, entity_type, layer, ...}`
    so `EventTail`-based readers don't care which reactor produced the log.

    Operations:
      start_recording — load + attach the right reactor. Idempotent.
                        Accepts `data: {prefer: "dotnet"|"lisp"|"auto"}`;
                        default `auto` picks .NET on COM, LISP on file_ipc.
      stop_recording  — detach the reactors.
      recent          — data: {limit?} — return the last N events.
      new             — return only events since the previous `new` call
                        (uses a byte-offset cursor; survives log truncation).
      clear           — truncate the event log.
    """
    data = data or {}

    if operation == "recent":
        tail = _get_event_tail()
        limit = int(data.get("limit", 50))
        events_list = tail.read_recent(limit)
        return _json({
            "ok": True,
            "payload": {"events": events_list, "count": len(events_list)},
        })
    if operation == "new":
        tail = _get_event_tail()
        events_list = tail.read_new()
        return _json({
            "ok": True,
            "payload": {"events": events_list, "count": len(events_list)},
        })
    if operation == "clear":
        tail = _get_event_tail()
        tail.clear()
        return _json({"ok": True, "payload": {"cleared": True}})

    if operation in ("start_recording", "stop_recording"):
        backend = await get_backend()
        prefer = (data.get("prefer") or "auto").lower()
        impl = _choose_reactor_impl(backend, prefer)

        if operation == "start_recording":
            if impl == "dotnet":
                result = await _start_dotnet_reactor(backend)
                impl_used = "dotnet"
                if not result.ok and prefer == "auto":
                    # Auto-mode fall-through: if NETLOAD failed (DLL missing,
                    # AutoCAD doesn't have a doc, etc.), retry with LISP.
                    log.warning(
                        "dotnet_reactor_attach_failed_fallback_lisp",
                        error=result.error,
                    )
                    result = await _start_lisp_reactor(backend)
                    impl_used = "lisp"
            else:
                result = await _start_lisp_reactor(backend)
                impl_used = "lisp"
        else:
            # Detach: send the detach command for whichever reactor is loaded.
            # Both detach commands are no-ops when the reactor isn't attached,
            # so blasting both is safe and survives an unknown prior state.
            if impl == "dotnet":
                result = await backend.execute_lisp('(command "MCPDETACHREACTOR") ')
                impl_used = "dotnet"
            else:
                result = await backend.execute_lisp(
                    "(if c:mcp-detach-reactors (c:mcp-detach-reactors))"
                )
                impl_used = "lisp"

        if not result.ok:
            return _json({"ok": False, "error": result.error})
        return _json({
            "ok": True,
            "payload": {
                "operation": operation,
                "reactor_impl": impl_used,
                "log_path": str(_get_event_tail().path),
            },
        })

    return _json({"error": f"Unknown events operation: {operation}"})


# --- Reactor implementation selection ---


def _dotnet_reactor_path():
    """Path to the prebuilt MCPReactor.dll. Returns None when missing."""
    from pathlib import Path
    here = Path(__file__).resolve()
    # src/autocad_mcp/server.py → repo root is 3 levels up.
    candidate = here.parent.parent.parent / "bin" / "dotnet" / "MCPReactor.dll"
    return candidate if candidate.exists() else None


def _choose_reactor_impl(backend, prefer: str) -> str:
    """Decide between the .NET and LISP reactor implementations.

    `prefer = "auto"`: .NET if (backend=COM AND DLL exists), else LISP.
    `prefer = "dotnet"` / `"lisp"`: honor the override; user knows what
        they're doing (e.g. testing the fallback path).
    """
    if prefer == "dotnet":
        return "dotnet"
    if prefer == "lisp":
        return "lisp"
    # Auto: .NET requires the COM backend (NETLOAD + command method
    # invocation only works through AutoCAD's full .NET runtime, which
    # the LT-targeted file_ipc backend doesn't expose). Defensive
    # `getattr` so unusual backend mocks (tests) fall through cleanly.
    backend_name = getattr(backend, "name", "")
    if backend_name == "com" and _dotnet_reactor_path() is not None:
        return "dotnet"
    return "lisp"


async def _start_dotnet_reactor(backend):
    """NETLOAD MCPReactor.dll into AutoCAD, then run MCPATTACHREACTOR."""
    dll = _dotnet_reactor_path()
    if dll is None:
        from autocad_mcp.backends.base import CommandResult
        return CommandResult(
            ok=False,
            error=(
                "MCPReactor.dll not found at bin/dotnet/. Build it via "
                "`dotnet build -c Release dotnet/MCPReactor/MCPReactor.csproj` "
                "or set prefer='lisp' to use the AutoLISP reactor instead."
            ),
            category="backend_unavailable",
        )
    posix = str(dll).replace("\\", "/")
    code = (
        f'(command "_NETLOAD" "{posix}")'
        '(command "MCPATTACHREACTOR")'
    )
    return await backend.execute_lisp(code)


async def _start_lisp_reactor(backend):
    """Load mcp_reactors.lsp and attach. Pre-existing behavior."""
    from autocad_mcp.config import LISP_DIR
    reactor_path = (LISP_DIR / "mcp_reactors.lsp").as_posix()
    code = (
        f'(if (not c:mcp-attach-reactors) (load "{reactor_path}"))'
        "(c:mcp-attach-reactors)"
    )
    return await backend.execute_lisp(code)


# ==========================================================================
# 11. view — Viewport and screenshot
# ==========================================================================


# `readOnlyHint` is False — named-view save / layer-state save / delete
# are write operations even though zoom/screenshot are read-only.
@mcp.tool(annotations={"title": "AutoCAD View Operations", "readOnlyHint": False})
@_safe("view")
async def view(
    operation: str,
    x1: float | None = None,
    y1: float | None = None,
    x2: float | None = None,
    y2: float | None = None,
    data: dict | None = None,
) -> ToolResult:
    """Viewport, screenshot, named views, layer states.

    Operations:
      zoom_extents          — Zoom to show all entities.
      zoom_window           — Zoom to window: x1, y1, x2, y2
      get_screenshot        — Capture current view as PNG image.

      list_named_views      — Enumerate saved named views with their stored
                              centers + extents.
      save_named_view       — data: {name, x1, y1, x2, y2} — save the rectangle
                              as a named view. Overwrites if `name` exists.
      restore_named_view    — data: {name} — switch to a saved view. On the
                              ezdxf headless backend, returns the stored
                              extents instead of changing a viewport.
      delete_named_view     — data: {name}

      list_layer_states     — Enumerate saved layer-state names (the LAS
                              facility). COM-only; ezdxf returns
                              `category=not_supported`.
      save_layer_state      — data: {name} — snapshot every layer's
                              on/off/freeze/lock/color into a named state.
      restore_layer_state   — data: {name} — apply a saved state.
      delete_layer_state    — data: {name}
    """
    data = data or {}
    backend = await get_backend()

    if operation == "zoom_extents":
        result = await backend.zoom_extents()
        return _json(result.to_dict())
    elif operation == "zoom_window":
        result = await backend.zoom_window(x1, y1, x2, y2)
        return _json(result.to_dict())
    elif operation == "get_screenshot":
        result = await backend.get_screenshot()
        if result.ok and result.payload:
            from mcp.types import ImageContent, TextContent

            return [
                TextContent(type="text", text=_json({"ok": True, "screenshot": "attached"})),
                ImageContent(type="image", data=result.payload, mimeType="image/png"),
            ]
        return _json(result.to_dict())
    elif operation == "list_named_views":
        result = await backend.view_list_named()
        return _json(result.to_dict())
    elif operation == "save_named_view":
        result = await backend.view_save_named(
            data["name"], data["x1"], data["y1"], data["x2"], data["y2"],
        )
        return _json(result.to_dict())
    elif operation == "restore_named_view":
        result = await backend.view_restore_named(data["name"])
        return _json(result.to_dict())
    elif operation == "delete_named_view":
        result = await backend.view_delete_named(data["name"])
        return _json(result.to_dict())
    elif operation == "list_layer_states":
        result = await backend.view_list_layer_states()
        return _json(result.to_dict())
    elif operation == "save_layer_state":
        result = await backend.view_save_layer_state(data["name"])
        return _json(result.to_dict())
    elif operation == "restore_layer_state":
        result = await backend.view_restore_layer_state(data["name"])
        return _json(result.to_dict())
    elif operation == "delete_layer_state":
        result = await backend.view_delete_layer_state(data["name"])
        return _json(result.to_dict())
    else:
        return _json({"error": f"Unknown view operation: {operation}"})


# ==========================================================================
# 12. system — Server management
# ==========================================================================


# `readOnlyHint` is intentionally False — `init`, `execute_lisp`, and any
# operation that re-creates the backend are not read-only. MCP clients that
# auto-approve "read-only" tools should still prompt for this one.
@mcp.tool(annotations={"title": "AutoCAD MCP System", "readOnlyHint": False})
@_safe("system")
async def system(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Server status and management.

    Operations:
      status        — Backend info, capabilities, health check.
      health        — Quick health check (ping backend).
      get_backend   — Return current backend name and capabilities.
      runtime       — Return process/runtime details for spawn diagnostics.
      dwg_status    — Report whether headless DWG read/write is available
                      and which converter (accoreconsole / ODA) was found.
                      Use this before relying on `drawing.save` with a .dwg
                      extension or `project.index` on a project that
                      references .dwg files.
      preflight     — One-shot health check covering every external
                       dependency: which backend will auto-select, which
                       DWG converters are reachable, .NET DLLs deployed,
                       asset library state, LISP companion-file presence,
                       AutoCAD register-y presence. Call this once after
                       install to verify everything's wired before
                       hitting the more specific tools.
      tool_help     — Schema-rich tool discovery. `data: {tool?: str}`.
                      With no tool: lists every MCP tool with its title +
                      one-line summary + parameter schema. With `tool="..."`:
                      returns that tool's full docstring (which enumerates
                      operations) plus the parameter schema. Use this once
                      at session start so you don't have to memorize every
                      operation name.
      init          — Re-initialize the backend.
      turn_begin    — Start an "undo turn": every entity-touching tool call
                       between `turn_begin` and `turn_end` joins one AutoCAD
                       undo group, so a single Ctrl-Z reverts the whole
                       sequence. Optionally pass `data: {label?: str}` for
                       a human-readable name. Snapshots the entity count
                       per layer so `turn_end` can diff. Idempotent within
                       one process — calling `turn_begin` while already in
                       a turn is a no-op (logs at debug).
      turn_end      — Close the current undo turn and return the entity
                       diff: `{added, modified, erased, by_layer, duration_s}`.
                       The diff is computed from a turn-start snapshot;
                       only `added` and `erased` are exact, `modified` is
                       a best-effort delta (entity count is stable so it's
                       inferred — set `data: {use_reactor: true}` to read
                       precise counts from the reactor event log instead).
      execute_lisp  — Execute arbitrary AutoLISP code. data: {code}

    SECURITY: `execute_lisp` is gated behind the `AUTOCAD_MCP_ENABLE_LISP_EXEC`
    env var. Set it to `1`/`true`/`yes` to opt in. The operation accepts any
    AutoLISP expression and forwards it directly to AutoCAD — that includes
    file deletion via `(vl-file-delete ...)`, system-variable mutation,
    arbitrary command invocation, and side effects on every open drawing.
    Only enable it for trusted MCP clients on workstations where this surface
    is acceptable.
    """
    data = data or {}

    if operation == "status" or operation == "get_backend":
        backend = await get_backend()
        result = await backend.status()
        return await add_screenshot_if_available(result, include_screenshot)
    elif operation == "health":
        try:
            backend = await get_backend()
            result = await backend.status()
            return _json({"ok": result.ok, "backend": backend.name})
        except Exception as e:
            return _json({"ok": False, "error": str(e)})
    elif operation == "runtime":
        import os
        import sys

        return _json(
            {
                "ok": True,
                "platform": sys.platform,
                "python": sys.executable,
                "cwd": os.getcwd(),
                "backend_env": os.environ.get("AUTOCAD_MCP_BACKEND", "auto"),
                "wsl_interop": bool(os.environ.get("WSL_INTEROP")),
            }
        )
    elif operation == "dwg_status":
        # Report which DWG converters (accoreconsole / ODA File Converter)
        # are reachable on this machine. Anything but the ezdxf backend
        # talks to AutoCAD directly and doesn't need a converter, but the
        # ezdxf backend leans on these for headless DWG read and write.
        from autocad_mcp.project.dwg_reader import available_dwg_converters

        converters = await _asyncio.to_thread(available_dwg_converters)
        can_read_dwg = bool(converters["accoreconsole"] or converters["oda_file_converter"])
        can_write_dwg = can_read_dwg  # both paths support both directions
        return _json({
            "ok": True,
            "payload": {
                "converters": converters,
                "can_read_dwg": can_read_dwg,
                "can_write_dwg": can_write_dwg,
                "preferred": (
                    "accoreconsole" if converters["accoreconsole"]
                    else "oda_file_converter" if converters["oda_file_converter"]
                    else None
                ),
                "hint": (
                    None if can_read_dwg
                    else "No DWG converter found. Install full AutoCAD (for "
                         "accoreconsole.exe) or the free ODA File Converter "
                         "from opendesign.com to enable headless DWG support."
                ),
            },
        })
    elif operation == "preflight":
        # One-shot health probe. Surfaces every external dependency so
        # the user can diagnose "why isn't it working" without running
        # six separate ops. Always returns ok=true — `payload.warnings`
        # is the actual signal.
        payload = await _asyncio.to_thread(_preflight_sync)
        return _json({"ok": True, "payload": payload})
    elif operation == "tool_help":
        # Walk FastMCP's tool registry. `data.tool` narrows to a single
        # tool — otherwise return the index of every tool plus a one-line
        # summary (first non-blank line of the docstring).
        tools = mcp._tool_manager.list_tools()
        target = (data.get("tool") or "").strip()
        if target:
            for t in tools:
                if t.name == target:
                    return _json({
                        "ok": True,
                        "payload": _tool_help_one(t),
                    })
            return _json({
                "error": f"No tool named {target!r}",
                "category": "not_found",
                "available_tools": sorted(t.name for t in tools),
            })
        return _json({
            "ok": True,
            "payload": {
                "tools": [_tool_help_one(t, summary_only=True) for t in tools],
                "count": len(tools),
                "tip": (
                    "Pass data={'tool': '<name>'} to system.tool_help for "
                    "the full docstring of one tool (operations list lives "
                    "in the docstring)."
                ),
            },
        })
    elif operation == "init":
        # Force re-initialization
        from autocad_mcp import client
        client._backend = None
        backend = await get_backend()
        result = await backend.status()
        return _json(result.to_dict())
    elif operation == "turn_begin":
        backend = await get_backend()
        label = data.get("label") or "claude-turn"
        if _turn_state.get("active"):
            return _json({
                "ok": True,
                "payload": {
                    "already_in_turn": True,
                    "label": _turn_state["label"],
                    "started_at": _turn_state["started_at"],
                },
            })
        # Snapshot per-layer entity counts so `turn_end` can diff.
        snap = await backend.entity_summary(None)
        snap_payload = snap.payload if snap.ok else {}
        # Begin AutoCAD's undo group so Ctrl-Z reverts the whole turn.
        ok, err = await _undo_group_begin(backend, label)
        if not ok:
            log.debug("turn_begin_undo_begin_failed", error=err)
        import time as _time
        _turn_state.update({
            "active": True,
            "label": label,
            "started_at": _time.time(),
            "snapshot": snap_payload,
            "undo_started": ok,
        })
        return _json({
            "ok": True,
            "payload": {
                "label": label,
                "started_at": _turn_state["started_at"],
                "snapshot": {
                    "total": snap_payload.get("total", 0),
                    "by_layer_keys": list((snap_payload.get("by_layer") or {}).keys()),
                },
                "undo_group_started": ok,
            },
        })
    elif operation == "turn_end":
        backend = await get_backend()
        if not _turn_state.get("active"):
            return _json({
                "ok": True,
                "payload": {"in_turn": False, "note": "no active turn"},
            })
        import time as _time
        # Close the undo group on the backend (best effort).
        if _turn_state.get("undo_started"):
            ok, err = await _undo_group_end(backend)
            if not ok:
                log.debug("turn_end_undo_end_failed", error=err)
        # Diff entity counts: now vs. snapshot.
        snap_before = _turn_state.get("snapshot") or {}
        snap_after_r = await backend.entity_summary(None)
        snap_after = snap_after_r.payload if snap_after_r.ok else {}
        diff = _compute_turn_diff(snap_before, snap_after)
        duration = _time.time() - _turn_state["started_at"]
        label = _turn_state["label"]
        _turn_state.update({"active": False, "label": None,
                             "started_at": None, "snapshot": None,
                             "undo_started": False})
        return _json({
            "ok": True,
            "payload": {
                "label": label,
                "duration_s": round(duration, 3),
                "diff": diff,
            },
        })
    elif operation == "execute_lisp":
        from autocad_mcp.config import lisp_exec_enabled

        if not lisp_exec_enabled():
            return _json({
                "error": (
                    "execute_lisp is disabled. It can run arbitrary AutoLISP "
                    "(file deletion, command invocation, etc.). To enable, "
                    "set AUTOCAD_MCP_ENABLE_LISP_EXEC=1 in the server env."
                ),
                "hint": "Restart the MCP server after setting the env var.",
            })
        backend = await get_backend()
        if not data.get("code"):
            return _json({"error": "data.code is required"})
        result = await backend.execute_lisp(data["code"])
        return await add_screenshot_if_available(result, include_screenshot)
    else:
        return _json({"error": f"Unknown system operation: {operation}"})


# ==========================================================================
# Main entry point
# ==========================================================================


def main():
    """Run the MCP server on stdio transport."""
    import logging
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    from autocad_mcp import __version__
    log.info("autocad_mcp_starting", version=__version__)
    mcp.run(transport="stdio")
