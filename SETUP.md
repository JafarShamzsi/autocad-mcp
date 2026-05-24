# Setup — wire it into your MCP client + run the smoke tests

This doc walks you through getting the autocad-mcp server attached to an
MCP client (Claude Code, Claude Desktop, or any other) and verifying
each piece works end-to-end.

You already have everything **installed** — what's left is wiring the
server into a client + running the smoke recipes that exercise each
subsystem.

## What's already verified on this machine

`system(operation="preflight")` reports `ready: true`. Specifically:

| Component | Status |
|---|---|
| Python venv | `.venv/` with Python 3.14 |
| Package version | `autocad_mcp 3.2.0` |
| **Backend auto-selects** | `com` (full AutoCAD 2027 / R26.0 detected) |
| DWG converter | `accoreconsole.exe` at `C:\Program Files\Autodesk\AutoCAD 2027\` |
| `bin/dotnet/SheetSetReader.dll` | present, 7.5 KB |
| `bin/dotnet/MCPReactor.dll` | present, 11 KB |
| LISP companions | all 6 files present in `lisp-code/` |
| Asset catalog | **632 assets** across 19 categories (22 procedural + 610 vendor) |
| Test suite | **714 passed**, 14 deselected, 0 failures |
| MCP stdio server | launches + responds to `tools/list` correctly |

---

## 1. Wire it into Claude Code (or Claude Desktop)

### Option A — Claude Code's `.mcp.json` (already in this repo)

The repo ships a working config at `.mcp.json`:

```json
{
  "mcpServers": {
    "autocad": {
      "command": "uv",
      "args": ["run", "--project", "${CLAUDE_PLUGIN_ROOT}",
               "python", "-m", "autocad_mcp"],
      "env": {"AUTOCAD_MCP_BACKEND": "auto"}
    }
  }
}
```

If you're running Claude Code inside this directory, it'll pick this up
automatically. `uv run` re-syncs the venv on first launch (~5 s).

### Option B — Claude Desktop manual config

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "autocad": {
      "command": "C:\\Users\\Axel\\Developer\\autocad-mcp-main\\.venv\\Scripts\\python.exe",
      "args": ["-m", "autocad_mcp"],
      "env": {
        "AUTOCAD_MCP_BACKEND": "auto"
      }
    }
  }
}
```

Restart Claude Desktop. The `autocad` MCP server appears under "Connected
MCP Servers" in settings.

### Option C — Other MCP clients

Any MCP-stdio client works. The launch command is:

```
"C:\Users\Axel\Developer\autocad-mcp-main\.venv\Scripts\python.exe" -m autocad_mcp
```

---

## 2. Optional environment variables

Set these in the client's `env` block when you need them. None are
required for normal use.

| Variable | When to set | What it does |
|---|---|---|
| `AUTOCAD_MCP_BACKEND` | always | `auto` (default) / `com` / `file_ipc` / `ezdxf`. Auto picks COM when full AutoCAD is running. |
| `AUTOCAD_MCP_ENABLE_LISP_EXEC=1` | when you want `system.execute_lisp` or `drawing.batch` | Opt-in gate for arbitrary AutoLISP execution. Off by default. |
| `AUTOCAD_MCP_WINDOW_HINT` | when multiple Autodesk apps are running | Substring the AutoCAD window title must contain. E.g. `"AutoCAD LT"` to skip Architecture/MEP. |
| `AUTOCAD_MCP_ACCORECONSOLE` | only when accoreconsole is in a non-default path | Explicit path override. |
| `AUTOCAD_MCP_DWG_CACHE_SIZE=32` | tweak read perf | LRU cache size (default 32). Set to 0 to disable. |
| `AUTOCAD_MCP_PROJECT_ROOTS` | multi-user deployments | Path-separator-delimited allowlist for `project(load)`. |
| `AUTOCAD_MCP_ONLY_TEXT=1` | testing without screenshots | Disable PNG screenshot capture. |
| `AUTOCAD_MCP_IPC_DIR` | non-default temp dir | Where the file-IPC + reactor event log lives. |

---

## 3. AutoCAD LT users — load the LISP dispatcher once

Skip this if you're using full AutoCAD (the COM backend doesn't need
LISP). For **AutoCAD LT 2024+**:

1. Start AutoCAD LT and open a drawing.
2. Type `APPLOAD` at the command line.
3. Browse to `C:\Users\Axel\Developer\autocad-mcp-main\lisp-code\mcp_dispatch.lsp`, click **Load**.
4. You'll see `=== MCP Dispatch v3.2 loaded ===`.
5. **For auto-load every session:** in the APPLOAD dialog, click **Startup Suite → Contents... → Add...**, pick the same file.
6. **Trusted paths (SECURELOAD=2 by default):** add the repo to `TRUSTEDPATHS`:
   ```
   (setvar "TRUSTEDPATHS" (strcat (getvar "TRUSTEDPATHS") ";C:\\Users\\Axel\\Developer\\autocad-mcp-main\\lisp-code"))
   ```

---

## 4. Smoke-test recipes — run these from Claude

The first one you run from a fresh client should be **`preflight`**. It
reports everything in one shot; subsequent recipes test specific
subsystems.

### Recipe 0 — preflight check (always run this first)

> Run `system(operation="preflight")`.

Expected: `ready: true`, empty `warnings`. If `warnings` is non-empty,
fix what it tells you before running anything else.

### Recipe 1 — drawing primitives (works on any backend)

> Create a new drawing, draw a 100×100 wall, and report the entity count.

Expected sequence:
- `drawing(create)` →  `{ok: true}`
- `entity(create_line, x1=0, y1=0, x2=100, y2=0)` → handle returned
- `entity(create_line, x1=0, y1=0, x2=0, y2=100)` → handle returned
- `entity(list, data={summary: true})` → `{total: 2, by_layer, by_type, samples}`

### Recipe 2 — architectural composite (validates arch.* + auto-layers)

> Build a 240×144-inch room with one 36-inch door and one 60-inch window
> in the south wall.

Expected:
- `arch(setup_layers)` → creates the AIA layer set
- `arch(wall_with_openings, data={x1: 0, y1: 0, x2: 240, y2: 0, thickness: 6, openings: [{type: "door", position: 60, width: 36}, {type: "window", position: 144, width: 60}]})`
- `arch(wall, data={x1: 240, y1: 0, x2: 240, y2: 144, ...})` ×3 more
- `arch(room, data={boundary: [[0,0], [240,0], [240,144], [0,144]], name: "TEST ROOM"})` → area label

### Recipe 3 — asset catalog (validates the seeded library)

> Find me a queen bed and place it at (100, 80).

Expected:
- `library(catalog_search, data={query: "queen bed"})` → hits include `furniture/beds/bed-queen`
- `library(catalog_insert, data={asset_id: "furniture/beds/bed-queen", x: 100, y: 80})` → INSERT entity created on `I-FURN`

### Recipe 4 — selection + bulk modify (Cluster A)

> Rotate every door in this drawing 90 degrees.

Expected:
- `entity(select, data={layer: "A-DOOR"})` → handles list
- `entity(rotate, data={selection: <that list>, cx: 0, cy: 0, angle: 90})` → `{processed: N, failed: []}`

### Recipe 5 — DWG round-trip (validates accoreconsole)

> Save the current drawing as `test.dwg`, then open it again and report the entity count.

Expected:
- `drawing(save_as_dwg, data={path: "C:/temp/test.dwg"})` → `{format: "dwg", version: "2018"}`
- `drawing(open, data={path: "C:/temp/test.dwg"})` → same handle count

### Recipe 6 — batch op (validates Cluster B; requires `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`)

> Run an AUDIT across these two files headlessly.

Expected:
- `drawing(batch, data={paths: ["a.dwg", "b.dwg"], script: '(command "_AUDIT" "Y")'})` → `{files_total: 2, files_ok: 2, total_duration_s: ~3.8}`

### Recipe 7 — turn-scoped undo (Cluster D)

> Wrap a sequence of operations in an undo turn so Ctrl-Z reverts them all together.

Expected:
- `system(turn_begin, data={label: "draw-test-room"})`
- (any sequence of `arch.*` / `entity.*` calls)
- `system(turn_end)` → `{diff: {added_or_modified: N, by_layer: {...}}}`

In AutoCAD itself, **one** Ctrl-Z reverts the whole sequence.

### Recipe 8 — live reactor (Cluster G5 / events)

> Start recording, draw something in AutoCAD by hand, then show me the events.

Expected:
- `events(start_recording)` → `{reactor_impl: "dotnet"}` (on COM) or `"lisp"` (on LT)
- *User draws a wall in AutoCAD*
- `events(recent, data={limit: 10})` → events with `kind: "object_appended"`, `entity_type: "AcDbLine"`, etc.

### Recipe 9 — 3D extrude (Cluster E; works on both backends)

> Extrude a closed rectangle to height 9 feet.

Expected:
- `entity(create_polyline, points=[[0,0],[120,0],[120,144],[0,144]], data={closed: true})` → handle
- `entity(extrude, entity_id=<handle>, data={height: 108})` → 3DSOLID (COM) or POLYFACE (ezdxf)

---

## 5. Useful one-shot ops to know about

| Op | Why |
|---|---|
| `system(tool_help)` | Index of every tool with summaries. |
| `system(tool_help, data={tool: "entity"})` | Full docstring + JSON schema for one tool. |
| `system(preflight)` | Health check. Always run after upgrading. |
| `system(dwg_status)` | Just the DWG converter info. |
| `system(status)` | Which backend is live + capabilities. |
| `system(init)` | Force-reattach the backend (after AutoCAD crash). |

---

## 6. Verify the install ran the right tests

```powershell
cd C:\Users\Axel\Developer\autocad-mcp-main
.venv\Scripts\python.exe -m pytest tests/ --tb=short -q
```

Expected last line: `714 passed, 14 deselected, ... in ~25s`.

The 14 deselected are integration tests that need a live AutoCAD. Run
them with:

```powershell
.venv\Scripts\python.exe -m pytest tests/ -m integration -v
```

---

## 7. Troubleshooting

**"`uv` not found"** — Use the absolute python path from Option B instead.

**"No AutoCAD found"** — `system(preflight)` will tell you what's
missing. The most common case is AutoCAD not registered in the
`HKLM\SOFTWARE\Autodesk\AutoCAD` key — restart AutoCAD once or run it
as administrator the first time.

**`drawing.save_as_dwg` returns `backend_unavailable`** — accoreconsole
isn't on the PATH `find_accoreconsole()` probes. Check
`system(dwg_status)`; if it says `accoreconsole: null`, either install
full AutoCAD or set `AUTOCAD_MCP_ACCORECONSOLE` to its absolute path.

**"AutoCAD COM disconnected"** — AutoCAD crashed or was closed.
`client.py::get_backend()` detects this and re-attaches on the next
call, but if you want to force-reset, call `system(init)`.

**`events.start_recording` doesn't fire** — the .NET reactor needs
AutoCAD's command-line to be receptive. Make sure no modal dialog is
open. Manually load the reactor via NETLOAD in AutoCAD with
`bin/dotnet/MCPReactor.dll`, then run `MCPATTACHREACTOR`.
