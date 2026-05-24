---
name: architectural-drafting
description: Workflow guidance for producing architectural floor plans, sections, and drawing audits through the autocad-mcp server. Use when the user asks to draft, edit, or audit AutoCAD drawings — especially anything with walls, doors, windows, rooms, dimensions, or AIA/NCS layer standards. Covers both new-drawing creation and multi-DWG project awareness via `.acadproj.toml`.
---

# Architectural drafting with autocad-mcp

This skill teaches you to drive the `autocad-mcp` server like a junior architect: prefer composite operations over raw entities, keep drawings on the AIA layer standard, and use the `project` tool to reason across multiple DWGs.

It does **not** restate every tool's signature — the MCP descriptions already do that. Read this for workflow patterns, defaults, and pitfalls.

---

## When to use this skill

Trigger on any of:

- "draw a floor plan", "add a wall", "place a door", "label this room"
- "check this drawing against our standards", "audit the project layers"
- "is anything missing in this project?", "find broken xrefs"
- The conversation involves a `.dwg`, `.dxf`, or `.acadproj.toml` file.

Do **not** use for: P&IDs (use the `pid` tool directly), pure DWG conversion, or generic file management.

---

## Backend & environment quick-check

Before drawing anything, call `system(operation="status")` once to confirm which backend is live:

- **`com`** — full AutoCAD running. All operations work; entities appear immediately in AutoCAD.
- **`file_ipc`** — AutoCAD LT only. All `arch.*` and `library.*` operations are wired through `mcp_dispatch.lsp` + the `arch_tools.lsp` / `library_tools.lsp` companion files (auto-loaded via `findfile` when `lisp-code/` is on the Support File Search Path).
- **`ezdxf`** — headless, no AutoCAD. Build the drawing in memory, then `drawing(operation="save_as_dxf", data={"path": ...})` (DXF) or `drawing(operation="save", data={"path": "...dwg"})` for native DWG (requires `accoreconsole.exe` or ODA File Converter).

Choose your strategy based on the backend. If the user wants to *see* the drawing happen, you need `com` or `file_ipc`. If they want a reviewable artifact, `ezdxf` is fine.

---

## Workflow: start a new floor plan

Always in this order:

1. **`arch(operation="setup_layers")`** — creates the AIA layer set (`A-WALL`, `A-DOOR`, `A-GLAZ`, `A-AREA`, `A-ANNO-DIMS`, etc.). Idempotent; safe to call repeatedly.
2. **Walls.** For walls with openings, prefer `arch(operation="wall_with_openings", ...)` over separate `wall` + `door` + `window` calls — the composite operation auto-cuts the wall around each opening and inserts the door/window symbol at the right spot.
3. **Rooms.** `arch(operation="room", data={"boundary": [[x,y],...], "name": "LIVING ROOM"})` draws the closed boundary on `A-AREA`, computes the area, and labels it.
4. **Dimensions.** `arch(operation="dimension_chain", data={"points": [...], "direction": "horizontal", "offset": 24})` for the chain of linear dims along an exterior wall.
5. **Save.** `drawing(operation="save_as_dxf", data={"path": "..."})` for headless, or `drawing(operation="save", data={"path": "..."})` for full AutoCAD.

**Wall-with-openings example** — a 240"-long wall with one 36" door and one 30" window:

```
arch(operation="wall_with_openings", data={
  "x1": 0, "y1": 0, "x2": 240, "y2": 0, "thickness": 6,
  "openings": [
    {"type": "door",   "position": 60,  "width": 36, "swing": "left"},
    {"type": "window", "position": 144, "width": 30}
  ]
})
```

Positions are measured along the wall centerline from `(x1,y1)`. Each opening's `width` is the full opening width; the centerpoint sits at `position`.

---

## Workflow: audit an existing project

Use the `project` tool when the user has a multi-DWG project with an `.acadproj.toml` at its root (or is asking you to set one up).

1. **`project(operation="load", data={"path": "..."})`** — point at the toml file or its directory.
2. **`project(operation="index")`** — builds the SQLite index. Re-run with `data={"force": true}` after schema upgrades or to wipe stale data.
3. **`project(operation="info")`** — quick sanity check: drawing count, total entities, last-indexed timestamp.
4. **`project(operation="check_standard")`** — defaults to the AIA layer standard. Returns `missing` / `wrong_color` / `wrong_linetype` / `extra` violations per drawing. Pass `data={"standard": "<path-to-firm-standard.dxf>"}` to use the firm's own reference DXF.
5. **`project(operation="list_xrefs")`** — discovered external references with each one's `is_broken` flag. Use **`find_broken_xrefs`** when the audit goal is specifically "what's missing on the file server".

If the user doesn't have an `.acadproj.toml`, offer to draft one. Template lives at `templates/sample-acadproj.toml` in this skill.

---

## Primitive cheat sheet

For floor-plan work, prefer the `arch` tool over `entity` or `block`:

| Want…                          | Use                                                                    |
|--------------------------------|------------------------------------------------------------------------|
| Wall (no openings)             | `arch(operation="wall", ...)`                                          |
| Wall with door(s) and window(s)| `arch(operation="wall_with_openings", ...)` — **strongly preferred**   |
| Standalone door symbol         | `arch(operation="door", ...)`                                          |
| Standalone window symbol       | `arch(operation="window", ...)`                                        |
| Room boundary + area label     | `arch(operation="room", ...)`                                          |
| Chain of dimensions            | `arch(operation="dimension_chain", ...)`                               |
| Stair (straight or U-shape)    | `arch(operation="stair", ...)`                                         |
| Structural column              | `arch(operation="column", ...)`                                        |
| Floor / building slab          | `arch(operation="slab", ...)`                                          |
| Overhead beam (HIDDEN)         | `arch(operation="beam", ...)`                                          |
| Roof plan (gable/hip/flat)     | `arch(operation="roof", ...)`                                          |
| 2D elevation of a wall         | `arch(operation="elevation", ...)`                                     |
| Building section               | `arch(operation="section", ...)`                                       |
| Whole-building starter layout  | `arch(operation="template", data={"name": "residential_2br", ...})`    |
| Any other geometry             | `entity(...)` — falls through to raw lines, circles, polylines, etc.   |
| AEC asset by description (sofa, queen bed, 36″ toilet, fridge…) | **`library(operation="catalog_search", data={"query": "..."})`** then `catalog_insert` on the best hit. Prefer this over `library.insert` whenever the user describes what they want rather than naming a specific procedural symbol. |
| Specific procedural symbol by exact name | `library(operation="insert", data={"name": "PLMB-TOILET", ...})` — used as fallback when `catalog_search` returns no results. |

If you find yourself manually drawing two parallel lines to make a wall, **stop** — use `arch.wall` instead. The composite operation keeps walls on `A-WALL`, applies consistent thickness, and is reversible as a single undo step.

### Asset-insert decision tree

When the user asks to "place X" or "add X" — anything that isn't pure
geometry — follow this order:

1. **Search the catalog first.** `library(operation="catalog_search", data={"query": "<the user's words>"})`. Returns up to 50 hits with full metadata. If the top hit obviously matches what was asked for, call `catalog_insert` against its `asset_id`.
2. **Fall back to the procedural library** if step 1 returns nothing useful. The 22 built-in symbols (`PLMB-TOILET`, `FURN-SOFA`, …) are listed via `library(operation="list")`. Insert with `library(operation="insert", data={"name": "...", "x": ..., "y": ...})`.
3. **Fall back to `arch.*` primitives** if it's a structural/geometric thing — a wall, slab, dimension, etc.
4. **Raw `entity.*`** as the last resort, when nothing higher-level fits.

For *exploration* requests ("show me all chairs", "what cottages do we have"),
hand off to the `asset-library-browse` skill instead of doing it inline.

---

## AIA layer reference

`arch(operation="setup_layers")` creates these. Use them in the `layer` argument of any tool that takes one; never invent ad-hoc names like `WALLS` or `MyDoors`:

| Layer            | Color | Purpose                              |
|------------------|-------|--------------------------------------|
| `A-WALL`         | 4 cyan| Wall — full height                   |
| `A-WALL-PATT`    | 251   | Wall hatch / fill                    |
| `A-WALL-HEAD`    | 4     | Wall above (header), dashed          |
| `A-DOOR`         | 2 yel | Door — leaf, frame, swing            |
| `A-GLAZ`         | 5 blu | Glazing / window                     |
| `A-FLOR-FIXT`    | 3 grn | Plumbing fixtures                    |
| `A-FLOR-CASE`    | 30 org| Casework / millwork                  |
| `A-FLOR-STRS`    | 6 mag | Stairs                               |
| `A-AREA`         | 1 red | Room boundary                        |
| `A-AREA-IDEN`    | 1     | Room identification / tag            |
| `A-ANNO-DIMS`    | 2     | Dimensions                           |
| `A-ANNO-TEXT`    | 7 wht | General text / notes                 |
| `A-ANNO-NOTE`    | 7     | Keynotes                             |
| `A-ANNO-SYMB`    | 4     | Symbols (north arrow, scale)         |
| `A-ANNO-TTLB`    | 7     | Title block                          |
| `A-GRID`         | 8 gry | Column / planning grid (centerline)  |
| `A-CLNG`         | 6     | Ceiling                              |
| `A-ROOF`         | 6     | Roof                                 |

Full list lives in `src/autocad_mcp/arch/layers.py::AIA_LAYERS`.

---

## Common pitfalls

- **Opening overlap.** `arch.wall_with_openings` validates that openings don't overlap and fit inside the wall. If you get a `ValueError: opening overlap`, you're trying to place a door and window too close together — check the `position` and `width` of each.
- **Raw entities drift off-standard.** Calling `entity(operation="create_line", layer="WALLS")` puts the line on a non-AIA layer, and a later `project.check_standard` will flag it as an `extra` violation. Always use AIA names.
- **Outdated project index.** After the package is upgraded, an existing `.acadproj.db` may be at an older schema. The tool raises `SchemaVersionMismatch` with a "run `force: true`" hint — pass that to the user and re-run `project.index` with `data={"force": true}`.
- **Door swing side.** `swing="left"` makes the door arc sweep CCW from the closed position; `swing="right"` is CW. This is geometric, not "which side the room is on" — verify with the user if it matters for code compliance (egress, accessibility approach).
- **Wall justification.** `justify="center"` (default) splits thickness equally about the centerline. For finish-wall-to-finish-wall dimensions, you may want `justify="left"` or `justify="right"` so dimensions match the outer face.

---

## Pre-commit checklist for architectural drawings

Before declaring a drawing done:

- [ ] `arch.setup_layers` was called (drawing has the full AIA layer set).
- [ ] All walls are on `A-WALL` (run `entity(operation="list", layer="A-WALL")` to confirm count).
- [ ] All doors are on `A-DOOR`, all windows on `A-GLAZ`.
- [ ] Each enclosed space has a `room` boundary with a name label.
- [ ] At least one `dimension_chain` exists along each exterior wall.
- [ ] If part of a project: `project.check_standard` returns zero `wrong_color` / `wrong_linetype` violations on the standard layers. `extra` violations are informational — discuss with the user before "fixing" them (sometimes firms legitimately add discipline-specific sub-layers).
- [ ] If part of a project with XREFs: `project.find_broken_xrefs` returns an empty list.

---

## What this skill deliberately does *not* cover

- 3D modeling, sections, elevations — the arch primitives are floor-plan only for now.
- Annotation scaling and annotative styles — set manually if needed.
- Plotting prep (page setups, plot styles) — covered by `drawing(operation="plot_pdf")` but not by this skill.
- AutoCAD command-line workflows — use `system(operation="execute_lisp")` as an escape hatch only when no MCP tool fits.
