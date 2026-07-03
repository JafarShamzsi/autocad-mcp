# AutoCAD MCP Native C++ Server

This directory contains the staged native rewrite of the AutoCAD MCP server.
The current Python implementation remains the behavioral oracle until this
server reaches parity.

## Build

```powershell
.\scripts\build_native.ps1
```

The preferred Windows toolchain is MSVC from Visual Studio Build Tools with a
Windows SDK. The wrapper locates `vcvars64.bat`, configures the native target
with CMake/NMake, builds `cpp/build/autocad-mcp-native.exe`, and runs CTest.

Manual build from a Visual Studio Developer Prompt:

```powershell
cmake -S cpp -B cpp/build -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build
ctest --test-dir cpp/build --output-on-failure
```

Run a protocol-level smoke test:

```powershell
uv run python scripts/smoke_native_mcp.py
```

## Current Native Coverage

- Native stdio MCP framing.
- `initialize`, `tools/list`, and `tools/call`.
- The same 14 tool names as the Python server.
- Operation registries for the current public API.
- Stable result/error envelope compatible with the Python server.
- Backend selection and diagnostics through `system.status`, `system.runtime`,
  `system.dwg_status`, `system.preflight`, and `system.tool_help`.
- Native File IPC transport for AutoCAD LT:
  - `autocad_mcp_cmd_{id}.json` / `autocad_mcp_result_{id}.json` protocol.
  - Atomic command publish, bounded polling, request cleanup, stale cleanup.
  - Win32 AutoCAD/LT window discovery and `(c:mcp-dispatch)` trigger.
  - `system.execute_lisp` remains gated by `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`.
- Native COM backend for full AutoCAD:
  - RAII-managed STA COM attachment with retry for busy AutoCAD RPC calls.
  - Drawing create/open/info/save/save_as_dxf/save_as_dwg, purge/audit/recover,
    undo/redo, PDF plotting, layouts, and paperspace viewport creation.
  - Entity select/osnap/create/list/count/get/erase/copy/move/rotate/scale/
    mirror/offset/array, hatches, regions, extrusion, revolve, and boolean
    solids.
  - Layer list/create/current/properties/freeze/thaw/lock/unlock.
  - Block list/insert/insert_with_attributes and live XREF list/attach/detach/
    reload/bind/broken-reference scan.
  - Annotation text, mtext, aligned/linear/angular/radius dimensions, leaders,
    and tables.
  - Dimstyle/textstyle/mleaderstyle list/get/create/current helpers.
  - Native Win32 `PrintWindow` + WIC PNG screenshot capture for
    `view.get_screenshot`.
- Native headless DXF backend:
  - Drawing create/open/info/save/save_as_dxf.
  - Drawing purge/audit/recover and undo/redo-safe no-op responses.
  - Entity select/osnap/create/list/count/get/erase/copy/move/rotate/scale/mirror/offset/array for LINE, CIRCLE,
    LWPOLYLINE, RECTANGLE, ARC, ELLIPSE, MTEXT, TEXT, simple LEADER,
    DIMENSION stubs, and grid TABLE emulation via LINE+TEXT.
  - Layer list/create/current/properties/freeze/thaw/lock/unlock.
  - Block list/define/insert/insert_with_attributes/get_attributes/update_attribute,
    with `BLOCKS`, `INSERT`, and `ATTRIB` DXF output.
  - Procedural P&ID helpers for layers, process lines, flow arrows,
    equipment tags, line numbers, valves, instruments, pumps, and tanks.
  - Procedural AIA/NCS architectural helpers for layer setup, walls, doors,
    windows, rooms, dimension chains, stairs, columns, slabs, beams, roofs,
    elevation/section rectangles, templates, and hatch-region outlines.
  - Stateful style helpers for dimstyles/textstyles/mleaderstyle defaults.
  - Headless named view and layer-state storage; `view.get_screenshot` is
    still intentionally unsupported until a native renderer is added.
  - In-process event recording cursors for `events.start_recording`,
    `events.recent`, `events.new`, and `events.clear`.
  - Native starter `library.list`, `library.define`, `library.define_all`, and
    `library.insert` for common AEC/P&ID symbols backed by block definitions.
  - Native-lite `project` queries for active drawing info, drawings/layers,
    entity search by layer, AIA layer checks, blocks, styles, linetypes,
    XREF/sheet empty surfaces, and Markdown sidecar reads.
  - Headless express-tool emulations for `layiso`, `overkill`, `burst`,
    `txt2mtxt`, and `flatten` (2D no-op).
  - Dependency-free ASCII DXF writer/parser for the native-supported entity
    set.

## Launch

Run the native server directly:

```powershell
.\cpp\build\autocad-mcp-native.exe
```

Or use the MCP-safe wrapper, which builds the executable if missing and sends
build logs to stderr so stdout remains valid MCP framing:

```powershell
.\scripts\run_native_mcp.ps1
```

Manual MCP config for a local native build:

```json
{
  "mcpServers": {
    "autocad-native": {
      "command": "C:\\path\\to\\autocad-mcp\\cpp\\build\\autocad-mcp-native.exe",
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

For clients that can load an alternate config file, `.mcp.native.json` contains
an `autocad-native` server profile that launches the wrapper.

The Python server remains the default packaged launch path until native SQLite
project indexing, external library catalog/import, live Express Tools parity,
and the remaining advanced drafting edge cases are complete.
