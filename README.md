# AutoCAD MCP Server

MCP server for AutoCAD automation, architectural drafting, and headless DXF generation.

Three backends, one API:

| Backend       | Runtime          | Requires AutoCAD?                        | Screenshot          |
|---------------|------------------|------------------------------------------|---------------------|
| **COM**       | Windows Python   | Yes — full AutoCAD 2018+ (Windows)       | Win32 PrintWindow   |
| **File IPC**  | Windows Python   | Yes — AutoCAD LT 2024+ (Windows)         | Win32 PrintWindow   |
| **ezdxf**     | Any platform     | No (headless)                            | matplotlib render   |

The server exposes **14 consolidated tools** (`drawing`, `entity`, `layer`, `block`, `annotation`, `pid`, `arch`, `project`, `library`, `style`, `express`, `events`, `view`, `system`) over the MCP stdio transport. An MCP client (Claude Desktop, Claude Code, etc.) connects and drives AutoCAD through natural-language requests.

In `auto` mode the backend priority is **COM** (full AutoCAD running) → **File IPC** (LT window available) → **ezdxf** (headless fallback).

## Prerequisites

- **Windows 10/11** (COM and File IPC backends use Win32 APIs)
- **AutoCAD** — pick one of:
  - **Full AutoCAD 2018 or newer** (Windows) → COM backend, full feature set including 3D, paperspace, XREFs.
  - **AutoCAD LT 2024 or newer** (Windows) → File IPC backend via the bundled AutoLISP dispatcher. (LT 2024 added AutoLISP support; LT for Mac is **not** supported.)
- **Python 3.10+** (Windows native — not WSL Python)
- **uv** package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/)) — or use `pip install -e ".[dev]"` against a venv.

> The ezdxf headless backend works on any platform (Linux, macOS, WSL) for offline DXF generation without AutoCAD installed.

## Quick Start

Two install paths. Pick one, then load the LISP dispatcher in AutoCAD (shared step).

### Path A — Install as a Claude Code plugin (recommended)

```powershell
claude plugin install https://github.com/puran-water/autocad-mcp
```

That's it — the plugin ships:
- The MCP server (auto-registered as `autocad` via `.mcp.json`; uses `uv run` so the venv self-bootstraps on first launch).
- The `architectural-drafting` skill (namespaced as `autocad-mcp:architectural-drafting`).
- The `autocad-architect` subagent.

Prerequisites: `uv` on `PATH` (the plugin uses `uv run` to launch the server cross-platform; install from <https://docs.astral.sh/uv/getting-started/installation/>).

For local development on the plugin itself:

```powershell
git clone https://github.com/puran-water/autocad-mcp.git
cd autocad-mcp
uv sync
claude --plugin-dir .
```

### Path B — Manual MCP config (other MCP clients, or no Claude Code)

If you're wiring the server into Claude Desktop, a different MCP client, or want
full control over the config:

#### B.1. Clone and install

```powershell
git clone https://github.com/puran-water/autocad-mcp.git
cd autocad-mcp
uv sync
```

#### B.2. Configure your MCP client

Add to your MCP client configuration (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "C:\\path\\to\\autocad-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "autocad_mcp"],
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

**Key points:**

- The `command` must point to the **Windows Python** inside the project venv (not WSL python).
- `AUTOCAD_MCP_BACKEND` can be `auto` (default — tries File IPC, falls back to ezdxf), `file_ipc` (requires AutoCAD), or `ezdxf` (headless only).

#### Running from WSL

If your MCP client runs in WSL (e.g. Claude Code), launch the server through `cmd.exe` so it runs as a native Windows process:

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "type": "stdio",
      "command": "cmd.exe",
      "args": ["/d", "/s", "/c", "cd /d C:\\path\\to\\autocad-mcp && .venv\\Scripts\\python.exe -m autocad_mcp"],
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

### Shared step — Load the LISP dispatcher in AutoCAD LT

> Skip if you're only using the COM backend (full AutoCAD) or the ezdxf
> headless backend. The dispatcher is for AutoCAD LT, which has no COM surface.

Open AutoCAD LT and load `mcp_dispatch.lsp` using **APPLOAD**:

1. Type `APPLOAD` in the AutoCAD command line
2. Browse to `<repo>/lisp-code/mcp_dispatch.lsp`
3. Click **Load**
4. You should see: `=== MCP Dispatch v3.2 loaded ===` and `Ready for commands via (c:mcp-dispatch)`

> **Tip:** Add the file to your AutoCAD Startup Suite (in the APPLOAD dialog) so it loads automatically with every drawing.

> **Auto-loading companions (`pid_tools.lsp`):** `mcp_dispatch.lsp` tries
> to `findfile` `pid_tools.lsp` on startup so file_ipc users don't have
> to APPLOAD two files. `findfile` only searches AutoCAD's *Support File
> Search Path* (Options → Files → Support File Search Path) — that's a
> separate list from `TRUSTEDPATHS`. Add the `lisp-code/` directory to
> both lists if you want the companion to auto-load every session;
> otherwise APPLOAD `pid_tools.lsp` once per session manually.

#### Trusted paths (SECURELOAD)

Modern AutoCAD ships with `SECURELOAD=2`, which prompts the first time you load an
unsigned `.lsp` from a path outside the trusted list. You'll see a "Security – Unsigned
Executable File" dialog. Pick one:

- **Click Load** in the dialog (one-shot — re-prompts next session).
- **Add the repo to `TRUSTEDPATHS`** (persistent): in the AutoCAD command line, type
  `(setvar "TRUSTEDPATHS" (strcat (getvar "TRUSTEDPATHS") ";C:\\path\\to\\autocad-mcp\\lisp-code"))`.
- **Temporarily** set `SECURELOAD` to `0` for local dev (not recommended on shared machines).

> **File IPC vs full AutoCAD:** the File IPC backend is designed for AutoCAD LT. On
> full AutoCAD 2027, the `PostMessageW` keystroke trigger is unreliable even though
> initialisation succeeds — auto-mode prefers COM precisely for this reason. Force
> COM with `AUTOCAD_MCP_BACKEND=com` if you're on full AutoCAD.

### Verify

From your MCP client, call:

```
system(operation="status")
```

You should see `backend: "com"` (full AutoCAD), `backend: "file_ipc"` (AutoCAD LT), or `backend: "ezdxf"` (headless).

## Tools

### `drawing` — File/drawing management

| Operation       | Description                                | COM | File IPC | ezdxf       |
|-----------------|--------------------------------------------|-----|----------|-------------|
| `create`        | New / reset drawing                        | Yes | Yes      | Yes         |
| `open`          | Open an existing drawing (`.dxf` or `.dwg`) | Yes | Yes     | Yes (DXF native, DWG via converter) |
| `info`          | Get entity count and layers                | Yes | Yes      | Yes         |
| `save`          | Save current drawing (to path if given). `.dwg` suffix routes through the DWG converter on ezdxf. | Yes | Yes | Yes |
| `save_as_dxf`   | Export as DXF                              | Yes | Yes      | Yes         |
| `save_as_dwg`   | Export as DWG. `data: {path, version?}`. `version` accepts the DWG format generation (`2018`/`2013`/.../`2000`) or an AutoCAD year (`2024` → `2018`). Defaults to `2018` (broadly readable across every AutoCAD release since 2018). On ezdxf needs accoreconsole or ODA File Converter. | Yes (native SaveAs + AcSaveAsType constants) | Yes | Yes (via converter) |
| `plot_pdf`      | Plot to PDF                                | Yes | Yes      | No          |
| `purge`         | Purge unused objects                       | Yes | Yes      | Yes         |
| `get_variables` | Get system variables by name               | Yes | Yes      | Yes         |
| `undo` / `redo` | Single-step undo / redo                    | Yes | Yes      | No          |
| `audit`         | Run AUDIT on the active drawing (`data.fix?: bool=true` repairs recoverable errors) | Yes | Yes (via execute_lisp) | No |
| `recover`       | Open a damaged DWG with RECOVER. `data: {path}` — use instead of `open` when a previous load warned about errors | Yes | Yes | No |
| `batch`         | **Run a LISP script across N files headlessly via accoreconsole.exe.** `data: {paths: [...], script: "...", save?: bool=true, timeout_per_file?: float=60}`. Each file gets its own subprocess (serial). Per-file results land in `payload.per_file`. Requires full AutoCAD installed (for accoreconsole) and `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`. Backend-agnostic — works whether you're attached via COM, file_ipc, or ezdxf. | Yes (any backend) | Yes (any) | Yes (any) |

> **Headless DWG support.** The COM and File IPC backends save DWG natively
> through AutoCAD. The ezdxf backend has no AutoCAD process and so leans on
> a converter — either `accoreconsole.exe` (preferred; installed with full
> AutoCAD 2018+) or the free [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter).
> Call **`system(operation="dwg_status")`** to confirm which converter is
> available on the current machine before relying on DWG read/write. If
> neither is installed, `drawing.save_as_dwg` and `drawing.open` against a
> `.dwg` path return `category="backend_unavailable"` with install guidance.
>
> **DWG read cache.** Reading a `.dwg` shells out to the converter (~2 sec
> per file). The reader caches converted documents by `(absolute_path, mtime_ns, size)`;
> the second access of an unchanged file is ~0 ms. Cache capacity defaults
> to 32 entries — override via `AUTOCAD_MCP_DWG_CACHE_SIZE`. Call
> `clear_dwg_cache()` programmatically (or just mutate the file) to evict.
>
> **DWG-first save.** `drawing.save(path="myfile")` (no extension) defaults
> to `.dwg` when a converter is available, `.dxf` otherwise. Users who
> specifically want DXF call `save_as_dxf` or pass a `.dxf` suffix
> explicitly.
>
> **Project-scale batch ops.** `drawing(operation="batch", data={paths, script})`
> runs an AutoLISP script across many DWGs headlessly via accoreconsole
> (no GUI flicker, ~1.9 s per file in our smoke test). Backend-agnostic;
> use for "rename layer Walls → A-WALL-EXTR across every sheet" / "apply
> firm dimstyle to 50 drawings" / "AUDIT + QSAVE this folder". Gated
> behind `AUTOCAD_MCP_ENABLE_LISP_EXEC=1` since the script is arbitrary.
> Example:
>
> ```python
> drawing(operation="batch", data={
>   "paths": ["sheet-01.dwg", "sheet-02.dwg", ...],
>   "script": '(command "_-LAYER" "_R" "Walls" "")'
>              '(command "_-LAYER" "_N" "A-WALL-EXTR" "")',
> })
> # → {files_total, files_ok, files_failed, total_duration_s,
> #    per_file: [{path, ok, duration_s, returncode, error?, stdout_tail?}]}
> ```

### `entity` — Entity CRUD + modification

**Create:** `create_line`, `create_circle`, `create_polyline`, `create_rectangle`, `create_arc`, `create_ellipse`, `create_mtext`, `create_hatch`

**Read:** `list`, `count`, `get`

> `entity(operation="list", data={"summary": true})` returns a token-efficient `{total, by_layer, by_type, samples}` payload — preferred over the default record-by-record list when the drawing has thousands of entities. Pass `layer` to scope the summary.

**Selection + snap:** `select`, `osnap`

> **`entity(operation="select", data={layer?, type?, bbox?, color?, handles?, limit?})`** filters modelspace entities and returns `{handles, count, by_layer, by_type, truncated}`. Filter args AND together; `layer`/`type` accept a string OR list (match any); `bbox = [x_min, y_min, x_max, y_max]` uses AutoCAD's crossing semantics (any overlap); `handles` narrows from a prior selection. Use this instead of `list` + filter-in-head for "all doors", "every wall on layer X", "everything within this region".
>
> **`entity(operation="osnap", data={x, y, modes?, tolerance?})`** snaps a query point to the nearest geometric feature. `modes ⊆ {endpoint, midpoint, center, intersection}` (defaults to all four); `tolerance` is the search radius in drawing units (default 5). Returns `{x, y, mode_used, entity_handle, distance}` or `category=not_found` when nothing's within tolerance.

**Modify:** `copy`, `move`, `rotate`, `scale`, `mirror`, `offset`\*, `array`, `fillet`\*, `chamfer`\*, `erase`

> \* `offset` works on COM and File IPC; `fillet` / `chamfer` work on COM, File IPC, and ezdxf (LINE+LINE only — LINE+ARC and ARC+ARC are deferred).

**3D primitives** (full AutoCAD via COM; ezdxf does extrude only):

| Op | Behavior |
|---|---|
| `region` | `data: {entity_ids}` — build Region(s) from closed curves. COM-only. Returns the new region handles to pass into `extrude` / `revolve`. |
| `extrude` | `entity_id`, `data: {height, taper_angle?}` — produce a 3DSOLID (COM) or polyface MESH (ezdxf, LWPOLYLINE/POLYLINE only, taper ignored). |
| `revolve` | `entity_id`, `data: {axis_x, axis_y, axis_dx, axis_dy, axis_dz?, angle?: 360}` — produce a 3DSOLID by revolving around the given axis. COM-only. |
| `boolean` | `data: {operation: "union"|"subtract"|"intersect", target_id, tool_ids}` — boolean op on 3D solids. Tool solids are consumed by AutoCAD's engine. COM-only. |

Architectural workflow for a section view: `entity.select(layer="A-WALL")` → `entity.region(entity_ids=...)` → `entity.extrude(handle, height=108)` → `entity.boolean(operation="subtract", target_id=wall_solid, tool_ids=[door_solid, window_solid])`.
>
> **Bulk modify.** Every modify op accepts EITHER a single `entity_id` (legacy) OR a list — pass `entity_ids=[...]` directly or `selection={handles: [...]}` from `entity.select`. The op runs per-handle and returns `{processed, failed, handles_in, handles_out?}` — one bad handle doesn't abort the batch. Example: rotate all doors 90° in one round trip:
>
> ```python
> sel = entity(operation="select", data={"type": "INSERT", "layer": "A-DOOR"})
> entity(operation="rotate", data={
>   "selection": sel["payload"],   # carries handles
>   "cx": 0, "cy": 0, "angle": 90,
> })
> ```

### `layer` — Layer management

`list`, `create`, `set_current`, `set_properties`, `freeze`, `thaw`, `lock`, `unlock`

### `block` — Block operations

| Operation | What it does |
|-----------|--------------|
| `list` | List block definitions in the active drawing |
| `insert` | Insert an existing block as an INSERT entity |
| `insert_with_attributes` | Insert with `{tag: value}` attribute payload |
| `get_attributes` / `update_attribute` | Read / mutate attributes on an INSERT |
| `define` | Define a new block inline (ezdxf-native; LISP-mediated on COM / file_ipc) |
| `list_from_file` | **Inspect blocks in an external `.dwg` / `.dxf`** without modifying the active drawing. DWG support routes through accoreconsole / ODA File Converter. |
| `import_from_file` | **Copy block definitions** from an external file into the active drawing. `data: {path, names?}`. Skips block names that already exist; pass `purge` first to refresh. |

Use this surface to draw rooms / floor plans with assets from vendor block libraries: list what's in the file, pick the ones you want, import their definitions, then `block(operation="insert", ...)` to place each one.

### `annotation` — Text, dimensions, leaders, tables

`create_text`, `create_dimension_linear`, `create_dimension_aligned`, `create_dimension_angular`, `create_dimension_radius`, `create_leader`, `create_table`

> **`annotation(operation="create_table", data={x, y, rows, cols, title?, header?, data?, row_height?, col_width?})`** — drops a schedule / table at `(x, y)`. On COM the result is a real AcDbTable entity (editable in AutoCAD); on ezdxf it's a grid of LINE + TEXT entities (plots identically, not editable as a table). Door schedule example:
>
> ```python
> annotation(operation="create_table", data={
>   "x": 100, "y": 80, "rows": 5, "cols": 3,
>   "row_height": 0.5, "col_width": 2.0,
>   "title": "DOOR SCHEDULE",
>   "header": ["TAG", "WIDTH", "HEIGHT"],
>   "data": [
>     ["D01", '3\'-0"', '6\'-8"'],
>     ["D02", '2\'-8"', '6\'-8"'],
>     ["D03", '3\'-0"', '6\'-8"'],
>   ],
> })
> ```

### `pid` — P&ID operations (CTO symbol library)

`setup_layers`, `insert_symbol`, `list_symbols`, `draw_process_line`, `connect_equipment`, `add_flow_arrow`, `add_equipment_tag`, `add_line_number`, `insert_valve`, `insert_instrument`, `insert_pump`, `insert_tank`

> **By default, every backend draws simplified ISA-5.1-shaped placeholder
> geometry** — outlined diamond for a valve, circle-with-crosshair for an
> instrument, rectangle for a tank. This is intentional so the `pid` tool
> works without any third-party install. When you want real ISA-compliant
> symbols, install the [CAD Tools Online](https://www.cadtoolsonline.com/)
> (CTO) P&ID Symbol Library at `C:\PIDv4-CTO\` (override with
> `CTO_LIBRARY_PATH`). The COM backend already picks up CTO; on file_ipc,
> `lisp-code/pid_tools.lsp` (auto-loaded by `mcp_dispatch.lsp` when
> `lisp-code/` is on AutoCAD's *Support File Search Path*) detects CTO
> via `findfile` and uses real CTO blocks with attribute support whenever
> the .dwg is available.

### `arch` — Architectural primitives on the AIA/NCS layer standard

Composite operations for floor-plan work — prefer these over raw `entity.*` calls when drafting architectural drawings.

| Operation              | What it does                                                                                  |
|------------------------|-----------------------------------------------------------------------------------------------|
| `setup_layers`         | Create the AIA standard layer set (`A-WALL`, `A-DOOR`, `A-GLAZ`, `A-AREA`, `A-ANNO-*`, …)     |
| `wall`                 | Double-line wall as a closed polygon along a centerline                                       |
| `wall_with_openings`   | Wall auto-cut into segments around door/window openings, with the symbols inserted in place   |
| `door`                 | Door leaf line + swing arc at a hinge                                                          |
| `window`               | Window sill rectangle + glass line                                                             |
| `room`                 | Closed boundary polyline + computed area + name/area label at the centroid                    |
| `dimension_chain`      | Consecutive linear (or aligned) dimensions across a row of points                              |
| `stair`                | Stair with treads + walking-line arrow (`shape: 'straight' \| 'u'`)                            |
| `column`               | Structural column (`shape: 'square' \| 'rect' \| 'round'`)                                     |
| `slab`                 | Floor / building footprint with optional inner edge polyline                                   |
| `beam`                 | Overhead beam (HIDDEN-by-default on `A-WALL-HEAD`)                                             |
| `roof`                 | Roof plan: eaves + ridge (+ hip lines) — `style: 'gable' \| 'hip' \| 'flat'`                   |
| `elevation`            | 2D elevation view of a single wall with openings                                              |
| `section`              | 2D building section between floor + ceiling lines                                              |
| `template`             | Whole-building composite (`residential_1br/2br/3br_house`, `commercial_open_office`)           |
| `hatch_region`         | Apply an AEC hatch pattern (BRICK, CONCRETE, INSULATION, WOOD, EARTH, …) to a closed polygon |
| `list_hatches`         | Return the curated AEC hatch catalogue                                                         |

**Available on all three backends** (COM, file_ipc, ezdxf) as of v3.3. File IPC support uses
the new `arch_tools.lsp` companion (auto-loaded via `findfile` like `pid_tools.lsp` and
`library_tools.lsp`), which renders Python-computed geometry per-entity onto its target layer.

> **Drawing units (F6).** `arch.room` (and any other area-bearing op going forward) picks
> `SF` vs `m²` based on the drawing's `$INSUNITS` header. Imperial-inches drawings
> divide raw area by 144, metric-mm drawings divide by 1 000 000, metric-meter drawings
> pass through, and any other value falls back to "SF" with no conversion to preserve
> the historic default. The ezdxf backend's `drawing.create` defaults to `$INSUNITS=2`
> (feet) so existing imperial workflows are unaffected.

### `project` — Multi-drawing project model + audits

Backed by `.acadproj.toml` + a SQLite index. Answers "what's on `A-WALL` across every floor plan?" / "are any of our XREFs broken?" without re-opening DWGs.

Both `.dxf` and `.dwg` are supported. DWG indexing requires one of:
- **Full AutoCAD 2018+** installed — provides `accoreconsole.exe` (preferred path, no separate install). The DXFOUT prompt sequence used to convert DWG→DXF is verified against the 2018+ command flow; older AutoCAD versions may need adjustment.
- **ODA File Converter** — free, [download from Open Design Alliance](https://www.opendesign.com/guestfiles/oda_file_converter).

| Operation             | What it does                                                                       |
|-----------------------|------------------------------------------------------------------------------------|
| `load`                | Load `.acadproj.toml` from a file or directory                                     |
| `index`               | Walk every drawing and (re)populate the SQLite index next to the toml              |
| `info`                | High-level summary: drawing / entity / layer counts; last-indexed timestamp        |
| `list_drawings`       | One row per drawing                                                                |
| `list_layers`         | Layers aggregated across the project (filterable by drawing / discipline)          |
| `find_entities`       | Entity search by layer / type / drawing                                            |
| `check_standard`      | Layer compliance against the AIA preset or a firm-specific reference DXF           |
| `list_xrefs`          | All external references discovered in the indexed drawings                         |
| `find_broken_xrefs`   | XREFs whose target file is missing on disk                                         |
| `xref_attach`         | `data: {path, name?, x?, y?, scale?, rotation?, overlay?}` — attach an external DWG as XREF to the active drawing |
| `xref_detach`         | `data: {name}` — detach an XREF (guarded; refuses to delete regular blocks)        |
| `xref_reload`         | `data: {name?}` — reload one or all XREFs (COM-backend only)                       |
| `xref_bind`           | `data: {name}` — bind an XREF into the local block table (COM-backend only)        |
| `sidecar`             | `data: {path|drawing}` — read the per-drawing `<stem>.claude.md` Markdown sidecar verbatim. Convention for architect-authored context (`Type A walls = 2x6 GWB`, `ceiling height = 9'-0"`). Auto-surfaced as `payload.sidecar` on `drawing.open`. |
| `list_dimstyles` / `list_textstyles` / `list_linetypes` / `list_blocks` | Aggregates with `distinct_configs` drift columns         |
| `find_style_inconsistencies` | Per-name dimstyle/textstyle drift across drawings                           |
| `find_linetype_inconsistencies` | Linetype names with diverging pattern attributes across drawings         |
| `find_block_inconsistencies` | Block names with differing `entity_count` across drawings (weak signal)     |
| `list_sheets`         | Every sheet in the project's `.dst` sheet set (if configured)                      |
| `find_sheets_for_drawing` | Sheets whose underlying DWG matches a given name / absolute path               |
| `unload`              | Close the project and its db connection                                            |

See `.claude/skills/architectural-drafting/templates/sample-acadproj.toml` for a starter template.

### `library` — Procedural symbol library

Built-in CC0 AEC blocks defined in code (no external assets to ship). The catalogue covers
the most common floor-plan symbols (22 as of v3.3):

- **Annotations:** `ARCH-NORTH-ARROW`, `ARCH-SCALE-BAR-10FT`, `ARCH-STAIR-MARKER`
- **Plumbing:** `PLMB-TOILET`, `PLMB-SINK`, `PLMB-BATHTUB`, `PLMB-SHOWER`, `PLMB-KITCHEN-SINK`
- **Appliances:** `APPL-RANGE`, `APPL-REFRIGERATOR`, `APPL-DISHWASHER`
- **Furniture:** `FURN-BED-QUEEN`, `FURN-BED-KING`, `FURN-DRESSER`, `FURN-NIGHTSTAND`,
  `FURN-DESK`, `FURN-CHAIR-OFFICE`, `FURN-CHAIR-DINING`, `FURN-TABLE-DINING`,
  `FURN-SOFA`, `FURN-FILE-CABINET`
- **Electrical:** `ELEC-OUTLET-DUPLEX`

All block-internal entities live on layer `0` so the insert's layer controls
coloring/linetype.

| Operation     | What it does                                                              |
|---------------|---------------------------------------------------------------------------|
| `list`        | Names of every built-in procedural symbol                                 |
| `define`      | `data: {name}` — define the block in the current drawing (idempotent)     |
| `define_all`  | Define every built-in library block at once                               |
| `insert`      | `data: {name, x, y, scale?, rotation?}` — auto-define + insert            |
| `scan_external` | `data: {roots: [dir, ...], db_path?, recursive?}` — **Index a directory of vendor DWGs** so Claude can search them. Walks every `.dwg` / `.dxf`, extracts every non-anonymous non-XREF block, writes to SQLite. |
| `search_external` | `data: {name?, prefix?, file?, limit?}` — Search the indexed library. `name` is substring; `prefix` is exact discipline (FURN/PLMB/ELEC/etc.); `file` is substring on source filename. |
| `stats_external` | High-level summary: total blocks, files indexed, top discipline prefixes. |
| `catalog_reindex` / `catalog_list` / `catalog_search` / `catalog_get` / `catalog_insert` / `catalog_stats` | **Structured asset catalog** — `asset-library/` filesystem layout with `meta.toml` + `block.dxf` per asset. Searchable by name, tags, category, dimensions. Insert composes block-import + insert in one call. See **`docs/library-spec.md`** for the data model and **`asset-library/CONTRIBUTING-ASSETS.md`** for the asset-authoring recipe. Seed 22 starter CC0 assets with `python -m autocad_mcp.library.seed`. |
| `vendor_import` | **Bulk-import a directory of vendor DWGs.** `data: {source, vendor_name?, min_entity_count?, include_autonamed?, recursive?}`. Walks each DWG, materializes every meaningful block as a catalog asset whose `meta.toml` points at the source file (no duplication). Filters AutoCAD auto-named placeholders (`A$*`) by default. Smoke-tested against 88 DWGs / ~50 MB → 610 quality assets indexed. Vendor output lives at `asset-library/vendor/...` which is gitignored. |

**External library workflow:**
1. `library(operation="scan_external", data={"roots": ["C:/my-blocks/"]})` — one-time (or after vendor updates).
2. `library(operation="search_external", data={"name": "chair"})` — find what's available.
3. `block(operation="import_from_file", data={"path": "<from search>", "names": ["<picked names>"]})` — pull definitions into the active drawing.
4. `block(operation="insert", data={"name": ..., "x": ..., "y": ...})` — place them.

Available on all three backends (COM, file_ipc, ezdxf). File IPC support uses
`lisp-code/library_tools.lsp` to render Python-serialized block specs into AutoCAD blocks.

### `events` — Bidirectional pairing (Claude watches the draw)

AutoCAD's reactor system, surfaced as a JSON-Lines event stream Claude can poll.
Two reactor implementations ship; `events.start_recording` picks the right one
for the active backend:

* **`.NET ManagedReactor`** (`bin/dotnet/MCPReactor.dll`) — preferred on the
  COM backend (full AutoCAD). Reliable `ObjectAppended` / `ObjectModified` /
  `ObjectErased` events on AutoCAD 2024+ where the equivalent LISP callbacks
  fire unpredictably. Also adds `selection_changed` events the LISP reactor
  can't reach. NETLOADed on demand via the AutoCAD command line.
* **LISP reactor** (`lisp-code/mcp_reactors.lsp`) — fallback for AutoCAD LT
  (file_ipc backend) and any environment where the .NET DLL isn't deployable.
  Uses VLR reactors plus a forward-scan workaround at command boundaries.

Either way the event log lives at `<ipc-dir>/autocad_mcp_events.log` and uses
the same JSON-Lines schema:

```json
{"ts": 1745432123.456, "ts_local": 1745410523.456, "kind": "object_appended",
 "handle": "1A4", "entity_type": "AcDbLine", "layer": "A-WALL"}
```

| Operation         | What it does                                                          |
|-------------------|-----------------------------------------------------------------------|
| `start_recording` | Load + attach the appropriate reactor (idempotent). Optional `data: {prefer: "auto"|"dotnet"|"lisp"}` overrides the default selection. |
| `stop_recording`  | Detach the reactor                                                    |
| `new`             | Return only the events since the previous `new` call (byte-cursor)    |
| `recent`          | `data: {limit?}` — return the last N events without advancing cursor  |
| `clear`           | Truncate the event log                                                |

Typical loop: call `start_recording` once at session start, then poll `new` between
your turns to see what the user drew. Survives log truncation and AutoCAD restarts.

**Smoke-test the .NET path:** with AutoCAD running and a document open, run
`.venv\Scripts\python.exe scripts\smoke_dotnet_reactor.py`. Output reports
which reactor was selected and shows the events captured for a synthetic
line-draw operation.

### `view` — Viewport, screenshot, named views, layer states

| Operation | Description |
|-----------|-------------|
| `zoom_extents` | Zoom to show all entities |
| `zoom_window` | Zoom to a specified window |
| `get_screenshot` | Capture current AutoCAD view as PNG |
| `list_named_views` | Enumerate saved named views with their stored center + extents |
| `save_named_view` | `data: {name, x1, y1, x2, y2}` — save a window as a named view (overwrites) |
| `restore_named_view` | `data: {name}` — switch to a saved view |
| `delete_named_view` | `data: {name}` |
| `list_layer_states` | List saved layer-state names (LAS facility). COM-only; ezdxf returns `not_supported` |
| `save_layer_state` | `data: {name}` — snapshot every layer's on/off/freeze/lock/color |
| `restore_layer_state` | `data: {name}` |
| `delete_layer_state` | `data: {name}` |

Screenshots use `PrintWindow` (Win32) for COM and File IPC backends — works even when AutoCAD is minimized or in the background. The ezdxf backend renders via matplotlib.

### `express` — Express Tools wrappers (full AutoCAD only)

Wrappers around AutoCAD's Express Tools commands. **Express Tools ship with full AutoCAD only** — AutoCAD LT has none of these. On the ezdxf backend, `layiso` has a useful headless emulation; the rest return `category=not_supported`.

| Operation | Description |
|-----------|-------------|
| `burst` | `data: {entity_ids}` — EXPLODE INSERTs while keeping attribute *values* as TEXT/MTEXT. Vanilla EXPLODE drops the strings; BURST keeps them. |
| `flatten` | `data: {entity_ids?, z?: 0}` — collapse entities to Z=0. Cleanup pass for field-captured drawings with stray elevations. |
| `overkill` | `data: {entity_ids?}` — remove duplicate / overlapping entities. Major cleanup after imports / merges. |
| `layiso` | `data: {layers: [name, ...]}` — isolate the listed layers, turn off everything else. ezdxf-friendly via `Layer.off()`; COM uses `_-LAYER` directly. |
| `txt2mtxt` | `data: {entity_ids}` — combine multiple TEXT entities into one MTEXT. Useful when imported drawings have one TEXT per paragraph line. |

### `style` — Dimstyles, textstyles, mleaderstyles

| Operation | Description |
|-----------|-------------|
| `list_dimstyles` / `get_dimstyle` / `create_dimstyle` / `set_current_dimstyle` | Manage dimension styles. `create_dimstyle` takes `data: {name, attribs: {dimscale, dimtxt, dimasz, dimdec, ...}}`. Overwrites preserve unspecified attribs. |
| `list_textstyles` / `get_textstyle` / `create_textstyle` / `set_current_textstyle` | Manage text styles. `create_textstyle` takes `data: {name, font?, height?, width_factor?, oblique?}`. |
| `list_mleaderstyles` / `set_current_mleaderstyle` | Multileader-style enumeration + current pointer. Create-mleaderstyle deferred (too many params across AutoCAD versions). |

Use this tool to enforce firm dimension / text standards across a project. Pair `list_dimstyles` with `project.find_style_inconsistencies` — the project tool flags same-name / different-attribs drift across drawings; this tool lets you fix the drift.

### `system` — Server management

`status`, `health`, `get_backend`, `runtime`, `dwg_status`, `tool_help`, `init`, `turn_begin`, `turn_end`, `execute_lisp`

**`tool_help`** is the introspection entry point. Call `system(operation="tool_help")` for an index of every tool with one-line summaries; call `system(operation="tool_help", data={"tool": "entity"})` for the full docstring (which enumerates every operation) plus JSON parameter schema. Use it once at session start so you don't have to memorize op names.

**`turn_begin` / `turn_end`** bracket a sequence of tool calls into one AutoCAD undo group. Inside the bracket, every entity-touching call joins one `_-UNDO _Begin` / `_End` group, so a single Ctrl-Z reverts the whole turn. `turn_end` also returns an entity diff:
```json
{
  "label": "claude-turn",
  "duration_s": 1.234,
  "diff": {
    "total_before": 421, "total_after": 437, "net_change": 16,
    "added_or_modified": 16, "erased": 0,
    "by_layer": {"A-WALL": 8, "A-DOOR": 4, "A-AREA": 2, "A-ANNO-DIMS": 2}
  }
}
```
Calling `turn_begin` while already in a turn is a no-op (returns `already_in_turn: true`). Calling `turn_end` without a begin returns `in_turn: false`.

> The `system` tool is intentionally tagged `readOnlyHint=False` — `init` recreates the backend and `execute_lisp` runs arbitrary code. MCP clients that auto-approve "read-only" tools still prompt for this one. Per-operation auto-approval ("`status` is safe, `execute_lisp` is not") would need per-tool permission grants in the Claude Code plugin spec, which isn't surfaced today; the right shape there is to split `system` into two tools once the spec supports it.

> `execute_lisp` runs arbitrary AutoLISP code (COM and File IPC backends). Pass `data: {code: "(+ 1 2)"}`. This turns the server into an extensible automation platform — any valid AutoLISP expression can be executed.
>
> **Security:** `execute_lisp` is **disabled by default** because it can do anything AutoLISP can — including `(vl-file-delete ...)` against arbitrary paths, mutation of any system variable, and invocation of any LISP-registered command. Set `AUTOCAD_MCP_ENABLE_LISP_EXEC=1` in the server env to opt in. Other tools (`events.start_recording`, etc.) that load fixed LISP files internally are unaffected by this gate.

## Claude Code skill

A workflow skill ships in `.claude/skills/architectural-drafting/SKILL.md`. It teaches Claude how to use the `arch` + `project` tools well for real arch drafting tasks — when to prefer composite operations, the AIA layer reference, common pitfalls, and a pre-commit checklist. A matching subagent definition lives in `.claude/agents/autocad-architect.md`.

## Architecture

```
MCP Client (Claude)
    │  stdio (JSON-RPC)
    ▼
Python MCP Server (autocad_mcp)
    │
    ├── COM Backend ──────► pywin32 IDispatch ──► AutoCAD.Application (full AutoCAD)
    │   Pinned single-thread STA executor; retry on RPC_E_CALL_REJECTED
    │
    ├── File IPC Backend ─► C:/temp/*.json ──► mcp_dispatch.lsp (AutoCAD LT)
    │   PostMessageW(WM_CHAR) to MDIClient — no focus steal
    │
    └── ezdxf Backend ────► in-memory DXF (headless, no AutoCAD needed)
```

The COM backend talks directly to a running full AutoCAD via IDispatch — single-threaded apartment, all calls pinned to one worker thread to satisfy STA. The File IPC backend sends keystrokes to AutoCAD LT's MDIClient window via `PostMessageW(WM_CHAR)`, triggering the `(c:mcp-dispatch)` AutoLISP command. Neither approach steals window focus — you can continue working in other applications while automation runs.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOCAD_MCP_BACKEND` | `auto` | Backend selection: `auto`, `com`, `file_ipc`, `ezdxf`. In auto mode the priority is `com` (full AutoCAD running) → `file_ipc` (AutoCAD LT window) → `ezdxf` (headless fallback). |
| `AUTOCAD_MCP_IPC_DIR` | `<tempdir>/autocad-mcp` | Directory for IPC command/result JSON files. Both the Python and LISP sides read this env var and default to `<system tempdir>/autocad-mcp` so a fresh install needs no per-machine config. |
| `AUTOCAD_MCP_IPC_TIMEOUT` | `10.0` | IPC command timeout in seconds (1-300) |
| `AUTOCAD_MCP_ENABLE_LISP_EXEC` | `0` | Set to `1` to enable `system(operation="execute_lisp")`. Disabled by default because it runs arbitrary AutoLISP — anything from changing system variables to deleting files via `(vl-file-delete ...)`. |
| `AUTOCAD_MCP_ONLY_TEXT` | `false` | Disable screenshot capture (text feedback only) |
| `AUTOCAD_MCP_WINDOW_HINT` | *(empty)* | Optional case-insensitive substring the window title must contain — disambiguates when multiple Autodesk apps are running (e.g., set to `"AutoCAD LT"` to skip AutoCAD Architecture/MEP). |
| `AUTOCAD_MCP_ACCORECONSOLE` | *(auto)* | Explicit path to `accoreconsole.exe`. If unset, the project tool searches the standard install roots (`C:\Program Files\Autodesk\AutoCAD <year>\` and friends). |
| `CTO_LIBRARY_PATH` | `C:/PIDv4-CTO` | Root of the CAD Tools Online P&ID symbol library. Override if you installed CTO elsewhere. |
| `AUTOCAD_MCP_PROJECT_ROOTS` | *(unset)* | Optional path-separator-delimited allowlist of directories `project(operation="load")` will accept `.acadproj.toml` from. Unset means no restriction (the right default for a single-user workstation). Set on multi-user deployments to prevent the MCP client from asking the server to load arbitrary filesystem paths. |

> **Note:** If you set `AUTOCAD_MCP_IPC_DIR` to a non-default value, the LISP side picks it up automatically on next load — no manual edit of `mcp_dispatch.lsp` required.

> **File IPC backend limitation:** the file IPC backend is designed for AutoCAD LT. On full AutoCAD 2027, the `PostMessageW(WM_CHAR)` trigger to MDIClient is unreliable even though window detection succeeds. Use the COM backend on full AutoCAD; auto-mode does this for you.

## Development

```powershell
uv sync
uv run pytest tests/ -v
```

## AutoCAD LT AutoLISP Compatibility

AutoLISP was added to AutoCAD LT in the **2024 release (Windows only)**. AutoCAD LT for Mac does not support AutoLISP.

| Supported (LT 2024+ Windows) | Not Supported |
|-------------------------------|---------------|
| `.lsp` / `.fas` / `.vlx` / `.dcl` | VLIDE (Visual LISP IDE) |
| All `vl-*` utility functions | `vlax-*` (ActiveX/COM) |
| File I/O (`open`, `read-line`, etc.) | Express Tools |
| Entity access (`entget`, `entmod`, etc.) | 3D operations |
| Selection sets | AutoLISP on Mac |

The `mcp_dispatch.lsp` dispatcher is fully compatible with LT 2024+.

## What's New in v3.2

- **COM backend for full AutoCAD** — `com_backend.py` attaches to a running AutoCAD via pywin32 IDispatch. All entity / layer / annotation / arch operations work; calls are pinned to a single-thread executor (STA) and auto-retry on `RPC_E_CALL_REJECTED`. `detect_backend()` now prefers `com` → `file_ipc` → `ezdxf` in auto mode.
- **`arch` tool — architectural primitives** — composite operations on the AIA/NCS layer standard: `wall`, `wall_with_openings` (auto-cuts walls around doors/windows), `door`, `window`, `room` (auto area + label), `dimension_chain`, plus `stair`, `column`, `slab`, `beam`, `roof`, `elevation`, `section`, and parametric `template`. **Now shipped on all three backends** (COM, file_ipc, ezdxf) — file_ipc routes through the `arch_tools.lsp` companion.
- **`project` tool — multi-drawing project model** — `.acadproj.toml` loader + SQLite indexer + queries: `info`, `list_drawings`, `list_layers`, `find_entities`, `check_standard` (AIA preset or firm-specific DXF), `list_xrefs`, `find_broken_xrefs`. Schema-versioned with strict mismatch handling.
- **`architectural-drafting` Claude Code skill + subagent** — workflow guidance + autocad-architect subagent under `.claude/skills/` and `.claude/agents/`.
- **Tests** — extensive default suite plus opt-in integration suite (against live AutoCAD) covering full COM round-trip, XREF audit, standards-compliance, and skill-discoverability. Run integration tests with `pytest -m integration`.

## What's New in v3.1

- **`execute_lisp`** — Run arbitrary AutoLISP code via temp file pattern.
- **Undo / Redo** — Single-step undo and redo via `drawing` tool.
- **Drawing open / create / save with path** — programmatic file ops.
- **`get_variables` fix** — respects the `names` parameter.
- **Polyline / leader fix** — point arrays via semicolon-delimited format.
- **ESC prefix, UTF-8 / cp1252 fallback, configurable IPC timeout, thread-safe backend init.**

## License

MIT
