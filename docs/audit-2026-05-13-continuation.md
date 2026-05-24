# autocad-mcp Audit — 2026-05-13 continuation

Follow-up to `docs/audit-2026-05-13.md`. User scoped a fresh session
around **DWG support for drawing every kind of building**, which is
substantially wider than the audit's "still open" infrastructure items
(H4 .NET pipe trigger, G5 .NET reactor, F1/F5/F6 forward-looking work —
all explicitly deferred by the prior audit).

The user's priority shifted the work: rather than chip at deferred
infrastructure, **expand the `arch` toolbox** so the server can sketch
and modify residential + commercial buildings, plus **close the DWG
round-trip** so headless `ezdxf` can save native DWG.

## What this session shipped

### New `arch` primitives (all three backends)

| Operation    | What it draws                                                                              |
|--------------|--------------------------------------------------------------------------------------------|
| `stair`      | Treads + outline + walking-line arrow; `shape='straight'` or `'u'` (with landing)          |
| `column`     | `square` / `rect` / `round` structural column at a point                                   |
| `slab`       | Floor / building footprint with optional inner edge polyline                               |
| `beam`       | Overhead beam centerline (or parallel edges if `width > 0`) on `A-WALL-HEAD` (HIDDEN)      |
| `roof`       | Eaves polygon + ridge + hip lines; `style='gable' / 'hip' / 'flat'`                        |
| `elevation`  | 2D elevation view of a single wall with door/window openings (panes drawn for windows)     |
| `section`    | 2D section between floor + ceiling lines with walls, openings, and slabs                   |
| `template`   | Whole-building composite: `residential_1br/2br/3br_house`, `commercial_open_office`        |

All composites use the same pattern as the existing primitives: pure
geometry in `arch/geometry.py`, ezdxf composite in `arch/primitives.py`,
COM backend computes geometry and issues IDispatch calls, file_ipc
serializes via the new `arch_tools.lsp` renderer.

### `arch_tools.lsp` (new file)

A layer-tagged entity renderer that mirrors `library_tools.lsp`. The Python
side computes geometry → serializes to a compact spec string with a
per-entity `@layer@` tag → ships it via one `arch-emit` dispatch. The
LISP side parses the spec, ensures each tagged layer exists, sets it as
current, and emits the entity. This means every new arch primitive — and
every future one — needs **no new LISP** as long as it decomposes into
LINE / CIRCLE / ARC / PLINE / TEXT.

Spec mini-language:

```
@<layer>@L,sx,sy,ex,ey                 LINE
@<layer>@C,cx,cy,r                      CIRCLE
@<layer>@A,cx,cy,r,start_deg,end_deg   ARC
@<layer>@P,closed,n,x1,y1,...,xn,yn    PLINE (closed = 0 | 1)
@<layer>@T,ix,iy,height,text           TEXT
```

Auto-loaded by `mcp_dispatch.lsp` via `findfile`, same as
`pid_tools.lsp` and `library_tools.lsp`. Falls back with an actionable
error pointing at the Support File Search Path setting.

Bug caught and fixed during review: `mcp-arch-extract-layer` initially
declared its locals as `/ first-at second-at layer body` but used a
loop variable `i` not in the locals — leaked into the global LISP
environment. Fixed before the file shipped.

### Building templates (`arch/templates.py`)

Four parametric whole-building composites:

| Template                    | Default size | Rooms                                                |
|-----------------------------|-------------|------------------------------------------------------|
| `residential_1br`           | 25′ × 35′    | Living/Dining, Kitchen, Bath, Bedroom               |
| `residential_2br`           | 35′ × 40′    | Living/Dining, Kitchen, Bath, Bedroom 1, Bedroom 2  |
| `residential_3br_house`     | 50′ × 35′    | Living, Dining, Kitchen, 3 Bedrooms (one with bath) |
| `commercial_open_office`    | 60′ × 40′    | Open office, Conference, Bath + structural column grid |

Each takes `(x, y)` as the south-west corner + a params dict for
width/depth/wall_thickness/door_width/window_width. The templates run
against an in-memory ezdxf doc and the COM / file_ipc backends rebuild
the entity stream on the live drawing — keeps template logic in one
Python module instead of three.

### Library expansion (`arch/library/symbols.py`)

Added 17 procedural CC0 symbols:

- **Plumbing:** `PLMB-BATHTUB`, `PLMB-SHOWER`, `PLMB-KITCHEN-SINK`
- **Appliances:** `APPL-RANGE`, `APPL-REFRIGERATOR`, `APPL-DISHWASHER`
- **Furniture:** `FURN-BED-QUEEN`, `FURN-BED-KING`, `FURN-DRESSER`,
  `FURN-NIGHTSTAND`, `FURN-DESK`, `FURN-CHAIR-OFFICE`, `FURN-CHAIR-DINING`,
  `FURN-TABLE-DINING`, `FURN-SOFA`, `FURN-FILE-CABINET`
- **Annotation:** `ARCH-STAIR-MARKER`

Library catalogue is now 22 symbols (up from 5). All still on layer "0"
so the insert's layer controls coloring/linetype.

### DXF → DWG write (`project/dwg_reader.py`)

Symmetric counterpart to the existing DWG → DXF read path. `write_dwg(doc, path)`
saves an ezdxf document to a temp DXF, then converts to DWG via the
best available converter (accoreconsole DWGOUT-style script → ODA
File Converter). `EzdxfBackend.drawing_save` auto-detects `.dwg`
extension and routes through the new writer; falls back to a
`backend_unavailable` `CommandResult` with a clear error message when
no converter is on the system.

This closes the headless round-trip: a Claude session running on a
machine with no AutoCAD but with `accoreconsole` (full AutoCAD install)
or the free ODA File Converter can now both read .dwg and write .dwg
without any COM glue.

## File-by-file changes

```
src/autocad_mcp/arch/geometry.py        +358 lines (stair_run, stair_u_shape,
                                                   column_corners, slab_outline,
                                                   beam_endpoints, roof_plan,
                                                   elevation_view, section_view,
                                                   _inset_polygon helper)
src/autocad_mcp/arch/primitives.py      +280 lines (create_stair, create_column,
                                                   create_slab, create_beam,
                                                   create_roof_plan,
                                                   create_elevation, create_section)
src/autocad_mcp/arch/templates.py       new file (~480 lines)
src/autocad_mcp/arch/lisp_render.py     new file (~240 lines)
src/autocad_mcp/arch/library/symbols.py +250 lines (17 new symbols)
src/autocad_mcp/arch/__init__.py        +new exports
src/autocad_mcp/backends/base.py        +8 new abstract methods
src/autocad_mcp/backends/ezdxf_backend.py  +new arch ops + DWG save path
src/autocad_mcp/backends/com_backend.py +~400 lines (8 new arch ops)
src/autocad_mcp/backends/file_ipc.py    +~180 lines (arch-emit wrappers)
src/autocad_mcp/project/dwg_reader.py   +write_dwg + accoreconsole/ODA writers
src/autocad_mcp/server.py               +8 new arch operation branches
lisp-code/arch_tools.lsp                new file (~140 lines)
lisp-code/mcp_dispatch.lsp              +dispatcher entry + auto-load block
tests/test_arch_buildings.py            new file (~440 lines, 83 tests)
tests/test_arch_buildings_lisp.py       new file (~145 lines, 16 tests)
tests/test_library.py                   +"APPL" added to discipline allowlist
README.md                               +arch ops table + library catalogue refresh
skills/architectural-drafting/SKILL.md  +new arch ops + DWG write note +
                                          backend-status note
```

## Test status

```
uv run pytest tests/                429 → 528 (+99 tests, none deselected new)
uv run pytest tests/ -m integration 14 deselected (live AutoCAD suite unchanged)
```

99 new default tests (+1 modified to widen the library prefix allowlist).
Coverage spans:

- Every new geometry helper (positive paths, edge cases, raises)
- Every new ezdxf composite primitive
- Every new backend method on `EzdxfBackend`
- Every new operation through `server.py::arch` (operation-string dispatch)
- Every new operation through `FileIPCBackend` (monkey-patched `_dispatch`
  asserts the LISP spec string is well-formed and the correct dispatch
  command is sent)
- `arch_tools.lsp` static structure (file ships, exports the expected
  defuns, sets the `*mcp-arch-tools-loaded*` flag, is referenced by the
  dispatcher)
- DWG write path with converters mocked away (graceful failure)

## What's still open after this session

### Unchanged from prior audits

- **H4 (partial)** — .NET named-pipe trigger for file_ipc on full AutoCAD
  2027. Same shape as before; not session-scoped. The new arch.* support
  doesn't change this — file_ipc users on full AutoCAD are still pushed
  toward COM.
- **G5 (partial)** — .NET `ManagedReactor` for the missing
  `vlr-objectappended` callback on AutoCAD 2024+.
- **F1, F5, F6** — Forward-looking items the 2026-05-13 audit deferred
  for valid reasons.

### New, intentionally out of scope this session

- **3D / multi-story support.** The new `template` operation drops a
  single-floor footprint; multi-story (`/level=ground/upper`) is a
  separate composite. Section view supports a single floor band.
- **Native DWG modification from ezdxf.** The new `write_dwg` is a
  one-way write (memory → file). Reading a .dwg, modifying it, and
  saving the modifications back to .dwg requires either the COM
  backend (which already supports this) or a full DXF↔DWG round-trip.
- **Furniture beyond the 17 added.** No `FURN-COUCH-SECTIONAL`,
  `FURN-WASHER-DRYER`, `APPL-WATER-HEATER`, etc. Add on demand.

## Behavior changes (existing-user surprises)

- **`arch.*` now works on file_ipc.** Existing LT users who got
  `not_supported` for `arch.wall` etc. previously will now see actual
  drawings appear when `arch_tools.lsp` is on the support path. If
  `arch_tools.lsp` isn't loadable, the new operations (`stair` /
  `column` / `slab` / `beam` / `roof` / `elevation` / `section` /
  `template`) return a clear "arch_tools.lsp not loaded" error pointing
  at the support path setting. The existing wall/door/window/room/dim
  operations are unaffected — they have their own LISP impls in
  `mcp_dispatch.lsp` and remain working without `arch_tools.lsp`.
- **`drawing.save` / `drawing.save_as_dxf` with a `.dwg` extension** now
  routes through the new writer instead of writing a DXF file with a
  `.dwg` suffix. If no converter is installed, the operation now fails
  with `category="backend_unavailable"` instead of silently producing a
  malformed `.dwg` file. Existing code passing `.dxf` is unaffected.

## Distribution + install notes

`arch_tools.lsp` is shipped alongside `pid_tools.lsp` and
`library_tools.lsp` in `lisp-code/`. The same Support File Search Path
setup that makes pid + library work on LT also makes arch work — no new
install steps for LT users. Documented in the README's "Load the LISP
dispatcher in AutoCAD LT" section (no edit needed; it already says "add
the lisp-code/ directory to the Support File Search Path").

For DWG write: same converter requirements as DWG read — either full
AutoCAD's `accoreconsole.exe` (preferred) or the free ODA File
Converter. The `available_dwg_converters()` diagnostic still surfaces
which path is in use.

## Summary

| Severity | Resolved this session | Open |
|----------|----------------------|------|
| HIGH     | 0 (none new)         | 1 (H4 unchanged) |
| MED      | 0 (none new)         | 1 (G5 unchanged) |
| LOW      | 0 (none new)         | 0 |
| **Total**| **0**                | **2** |

This session is **feature-shaped, not bug-shaped** — it doesn't close
audit findings, it expands the `arch` toolbox in the direction the user
asked for. The repo went from "walls/doors/windows + a starter symbol
library" to "walls/doors/windows + stairs/columns/slabs/beams/roofs +
elevation + section + four whole-building templates + 22 procedural
symbols", on all three backends, with 99 new tests.
