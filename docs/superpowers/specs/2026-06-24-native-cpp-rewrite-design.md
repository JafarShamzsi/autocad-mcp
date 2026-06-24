# Native C++ Rewrite Design for AutoCAD MCP

Date: 2026-06-24

## Summary

Rewrite the AutoCAD MCP server as a native C++ application that can fully
replace the current Python server while preserving the current MCP API. The
rewrite should improve startup time, live AutoCAD control, memory ownership,
Windows integration, and long-running reliability without breaking existing
clients that call the 14 consolidated tools.

The final system is a native `autocad-mcp-native.exe` stdio MCP server with
native backend implementations for full AutoCAD, AutoCAD LT, and headless
project/DXF workflows. The Python implementation remains the parity oracle
during migration and is removed from the default launch path only after the
native server proves complete coverage.

## Current System Evidence

The current repository is a Python FastMCP server with three backends and one
public API:

- 14 MCP tools: `drawing`, `entity`, `layer`, `block`, `annotation`, `pid`,
  `arch`, `project`, `library`, `style`, `express`, `events`, `view`,
  `system`.
- Backend priority in auto mode: `com`, then `file_ipc`, then `ezdxf`.
- Backend contract: `CommandResult`, `ErrorCategory`, `BackendCapabilities`,
  and a large `AutoCADBackend` async interface.
- COM backend: full AutoCAD automation through IDispatch on Windows.
- File IPC backend: AutoCAD LT automation through JSON command/result files
  plus a Win32 trigger into `mcp_dispatch.lsp`.
- Headless backend: DXF generation and inspection through `ezdxf`, with DWG
  read/write mediated by `accoreconsole.exe` or ODA File Converter.
- Project and library workflows use SQLite indexes, sidecar files, sheet-set
  readers, external block libraries, standards checks, and XREF metadata.
- Events use either a .NET `MCPReactor.dll` for full AutoCAD or a LISP reactor
  for LT-compatible event logging.

The default Python test suite is the behavioral contract while the native
server is built. Tests that require AutoCAD, `accoreconsole.exe`, ODA File
Converter, or prebuilt .NET DLLs stay gated by environment checks.

## Goals

1. Provide a real native C++ MCP server, not a permanent Python wrapper.
2. Preserve all current MCP tool names, operation names, arguments, result
   shapes, and stable error categories.
3. Implement native backend selection and backend status reporting.
4. Make full AutoCAD automation faster and more reliable through a C++ COM STA
   worker with deterministic lifetime management.
5. Preserve AutoCAD LT support through the existing LISP dispatcher protocol,
   implemented in C++ with robust file, timeout, encoding, and Win32 handling.
6. Preserve headless workflows for CI, preview, project indexing, and asset
   library operations.
7. Expand toward a comprehensive AutoCAD MCP surface after parity: richer 3D,
   layouts, sheet sets, standards, project memory, batch operations, and
   event-driven collaboration.
8. Avoid memory leaks and lifetime bugs through RAII, bounded caches, and
   ownership rules enforced in review and tests.
9. Test along the migration path so every implementation phase produces a
   usable, inspectable server.

## Non-Goals

- Do not redesign the public MCP API before compatibility is achieved.
- Do not remove the Python server until the native server has parity evidence.
- Do not require ObjectARX as the first milestone. ObjectARX can become a later
  high-capability backend, but the initial native server should work with COM
  and the existing LT dispatcher.
- Do not silently enable arbitrary AutoLISP execution. Dangerous operations
  remain gated by explicit environment variables.

## Approved Approach

Use a compatibility-first native rewrite.

The first native executable exposes the same 14 MCP tools and routes every
known operation. Early operations may return accurate `not_supported` results
while the backend implementation is being ported, but the protocol, discovery,
schemas, result envelopes, and error categories must be stable from the first
milestone. Each later milestone replaces one Python subsystem with native C++
behavior and adds parity tests.

This approach keeps the server usable during migration and makes regressions
visible. It also allows native performance work to land where it matters most:
MCP dispatch, COM control, file IPC, project indexing, caches, and batch
operations.

## Repository Layout

Add a native source tree without disturbing the current Python package:

```text
cpp/
  CMakeLists.txt
  README.md
  src/
    main.cpp
    protocol/
      JsonRpc.hpp
      JsonRpc.cpp
      McpServer.hpp
      McpServer.cpp
      StdioTransport.hpp
      StdioTransport.cpp
    core/
      BackendCapabilities.hpp
      CommandResult.hpp
      Config.hpp
      Config.cpp
      Errors.hpp
      Logger.hpp
      Paths.hpp
      Version.hpp
    tools/
      ToolRegistry.hpp
      ToolRegistry.cpp
      DrawingTool.cpp
      EntityTool.cpp
      LayerTool.cpp
      BlockTool.cpp
      AnnotationTool.cpp
      PidTool.cpp
      ArchTool.cpp
      ProjectTool.cpp
      LibraryTool.cpp
      StyleTool.cpp
      ExpressTool.cpp
      EventsTool.cpp
      ViewTool.cpp
      SystemTool.cpp
    backends/
      IBackend.hpp
      BackendFactory.hpp
      BackendFactory.cpp
      ComBackend.hpp
      ComBackend.cpp
      FileIpcBackend.hpp
      FileIpcBackend.cpp
      DxfBackend.hpp
      DxfBackend.cpp
    domain/
      arch/
      pid/
      project/
      library/
      styles/
      geometry/
    platform/
      win32/
        ComApartment.hpp
        ComPtr.hpp
        Handle.hpp
        Process.hpp
        Registry.hpp
        Screenshot.hpp
        TempFile.hpp
  tests/
    protocol_tests.cpp
    tool_registry_tests.cpp
    config_tests.cpp
    backend_contract_tests.cpp
```

The top-level repository can keep Python packaging during migration. Native
build files live under `cpp/` until the native server becomes the default.

## Public MCP Contract

The native server must support MCP over stdio with:

- `initialize`
- `tools/list`
- `tools/call`
- standard JSON-RPC request, response, and error framing
- deterministic single-response behavior for every request
- compact JSON result text matching the existing Python result envelope

All 14 current tools must be registered. Tool names and read/write intent must
match the current server. Tool descriptions should be generated from a shared
operation registry so `system(tool_help)` and MCP discovery stay consistent.

The native result envelope is:

```json
{"ok":true,"payload":{}}
```

or:

```json
{"ok":false,"error":"message","category":"invalid_arg"}
```

Stable error categories are:

- `not_supported`
- `not_found`
- `invalid_arg`
- `backend_unavailable`
- `timeout`
- `internal_error`

## Backend Contract

Define a C++ `IBackend` interface that mirrors the current backend contract at
the operation level. It should use typed values internally and JSON only at the
tool boundary.

Core backend interface requirements:

- `name()`
- `capabilities()`
- `initialize()`
- `status()`
- drawing operations
- entity creation, query, modification, 3D, and selection operations
- layer, block, style, view, annotation, P&ID, architecture, project, library,
  express, event, and system support as capability-specific methods

Default interface methods return `not_supported` with a clear detail string.
This preserves parity while implementation proceeds incrementally.

## Backend Selection

Native auto mode keeps the existing priority:

1. `com`: full AutoCAD installed and reachable.
2. `file_ipc`: Windows AutoCAD LT window available and dispatcher expected.
3. `dxf`: headless fallback.

Explicit backend selection uses `AUTOCAD_MCP_BACKEND` with values:

- `auto`
- `com`
- `file_ipc`
- `dxf`
- `ezdxf` as a compatibility alias for `dxf`

`system(status)` must report the selected backend, capabilities, environment
diagnostics, and actionable hints when a requested backend is unavailable.

## COM Backend Design

The COM backend is the primary native backend for full AutoCAD.

Design requirements:

- Run all AutoCAD COM calls on a dedicated single-threaded apartment worker.
- Initialize COM with RAII and uninitialize it deterministically.
- Use smart COM wrappers for `IDispatch`, `VARIANT`, `BSTR`, and SAFEARRAY
  lifetimes.
- Serialize operations through the STA worker to avoid cross-thread COM calls.
- Retry rejected calls that match AutoCAD busy/RPC rejection behavior.
- Surface disconnected AutoCAD as `backend_unavailable` with restart guidance.
- Group multi-operation tool calls into undo groups where supported.
- Keep screenshots on the Win32 side through native `PrintWindow` or a better
  AutoCAD-native capture path if available.
- Keep raw LISP execution behind `AUTOCAD_MCP_ENABLE_LISP_EXEC`.

The first COM milestone implements status, drawing info, variables, create,
save, open, line/circle/polyline/text creation, layers, zoom, screenshot, and
basic entity listing. Later milestones port blocks, dimensions, tables, XREFs,
layouts, styles, 3D operations, batch, and events.

## File IPC Backend Design

The File IPC backend preserves AutoCAD LT support.

Design requirements:

- Use the existing command/result JSON file protocol.
- Use atomic writes: write temp file, flush, close, then rename.
- Use a bounded polling timeout from `AUTOCAD_MCP_IPC_TIMEOUT`.
- Decode result JSON strictly and map malformed output to `internal_error`.
- Trigger AutoCAD LT through Win32 APIs without stealing focus.
- Preserve `AUTOCAD_MCP_IPC_DIR` behavior.
- Keep dispatcher and companion LISP files compatible.
- Handle encoding explicitly: UTF-8 first, Windows fallback only where needed.
- Clean up stale command/result files safely.

File IPC should initially reuse the current LISP operation map. Later native
work can improve the protocol with named pipes only if LT behavior needs more
reliability than the file protocol can provide.

## Headless Dxf Backend Design

The native headless backend replaces the Python `ezdxf` dependency over time.
Because robust DXF/DWG support is large, this backend is staged.

Stage 1:

- Provide status and capability reporting.
- Preserve tool discovery and return accurate `not_supported` for unported
  operations.
- Use external converters for DWG diagnostics through native process helpers.

Stage 2:

- Implement enough DXF read/write for current geometry tests: layers, lines,
  circles, arcs, polylines, text, blocks, basic styles, xrefs, layouts, and
  simple hatches.
- Preserve output compatibility with AutoCAD-readable DXF.

Stage 3:

- Port project indexing, external block scanning, standards checks, and asset
  catalog operations to native SQLite-backed code.
- Add optional support for a licensed DWG SDK if the user chooses one.

The native server should not claim full headless DWG editing unless backed by
AutoCAD, ODA, or another reliable DWG-capable engine.

## Domain Modules

Domain code should be backend-independent where possible:

- `domain/geometry`: points, vectors, matrices, bounding boxes, intersections,
  offsets, fillets, chamfers, area, centroid, transforms.
- `domain/arch`: AIA/NCS layers, walls, openings, doors, windows, rooms,
  stairs, columns, slabs, beams, roofs, elevations, sections, templates,
  hatches.
- `domain/pid`: symbols, process lines, valves, instruments, pumps, tanks,
  tags, line numbers.
- `domain/project`: `.acadproj.toml`, SQLite index schema, drawing metadata,
  layers, entities, xrefs, sheets, styles, standards checks.
- `domain/library`: procedural symbol library, asset catalog, external block
  indexer, vendor import metadata, catalog insert.
- `domain/styles`: dimstyles, textstyles, mleaderstyles, linetypes.

Each domain operation should produce a backend-neutral command plan when that
is practical. Backends then realize the plan through COM, file IPC, or DXF
writers. This reduces duplicate geometry logic and keeps behavior consistent.

## Data Flow

1. MCP client sends JSON-RPC over stdio.
2. `StdioTransport` reads exactly one message and passes it to `JsonRpc`.
3. `McpServer` validates method and routes `tools/call` to `ToolRegistry`.
4. Tool router validates operation arguments and selects the current backend.
5. Domain code builds typed operations or query requests.
6. Backend executes the operation.
7. Backend returns `CommandResult`.
8. Tool router optionally appends screenshot content when requested and
   supported.
9. `McpServer` writes a single JSON-RPC response.

No backend should write directly to stdio. All stdio traffic must go through
the protocol layer to avoid corrupting MCP framing.

## Error Handling

Error handling must be explicit and category-preserving:

- Bad user arguments: `invalid_arg`.
- Missing files, drawings, handles, blocks, layers, styles: `not_found`.
- Unsupported backend operations: `not_supported`.
- AutoCAD not running, COM disconnected, missing converter: `backend_unavailable`.
- IPC or subprocess timeout: `timeout`.
- Unexpected failures: `internal_error` with a short diagnostic.

Native exceptions should be caught at the tool boundary and converted into
structured results. Destructors must not throw.

## Memory and Lifetime Rules

All native code must follow these rules:

- No owning raw pointers.
- Prefer values, `std::unique_ptr`, and `std::vector`.
- Use `std::shared_ptr` only for true shared ownership.
- Wrap every OS handle in an RAII type.
- Wrap COM initialization, BSTR, VARIANT, SAFEARRAY, and IDispatch pointers in
  RAII types.
- Bound every cache by entry count or bytes.
- Avoid global mutable state except for a small process-level backend manager
  guarded by clear synchronization.
- Avoid detached threads. Worker threads must have deterministic shutdown.
- Treat all file paths and external process output as untrusted input.
- Do not hold locks while calling COM, waiting on processes, or doing blocking
  file IPC polls unless the lock protects that exact operation.

## Security and Permissions

Security behavior must match or tighten the current server:

- `system(execute_lisp)` is disabled unless `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`.
- `drawing(batch)` uses the same gate as raw LISP execution.
- `project(load)` respects `AUTOCAD_MCP_PROJECT_ROOTS` when set.
- File operations normalize paths before checking allowlists.
- No command-line construction through unsanitized string concatenation when a
  structured process API can pass arguments directly.
- Result JSON from LISP and external tools is treated as untrusted.
- The server should never execute scripts embedded in vendor assets.

## Testing Strategy

Testing happens in layers:

1. Protocol tests: JSON-RPC parse/write, stdio message framing, initialize,
   tools/list, tools/call, unknown methods, malformed JSON.
2. Core tests: `CommandResult`, error categories, capability serialization,
   env config, path normalization, backend selection.
3. Tool tests: all 14 tool names registered, every current operation name is
   known, invalid args return `invalid_arg`, unported operations return
   `not_supported`.
4. File IPC tests: command file shape, atomic writes, timeout handling,
   result parsing, stale file cleanup.
5. COM tests: compile-time tests always; live AutoCAD tests gated by explicit
   environment or test marker.
6. DXF tests: generated files open in AutoCAD-compatible DXF readers and match
   expected entity/layer/style counts.
7. Project/library tests: SQLite schema creation, indexing, queries,
   standards checks, catalog operations, vendor metadata.
8. Memory tests: sanitizer builds where available, leak checks on CI runners
   that support them, stress tests for repeated MCP calls and backend resets.
9. Parity tests: drive equivalent requests through Python and native servers
   and compare stable JSON payloads for operations that should match.

During migration, CI should keep running the current Python default tests and
add native tests as soon as the scaffold exists.

## Build and Toolchain

Use CMake as the native build entrypoint. The preferred Windows compiler is
MSVC through Visual Studio Build Tools. Clang-cl is acceptable if it can link
against Windows SDK and COM libraries.

Initial build requirements:

- CMake 3.25 or newer.
- C++20.
- Windows SDK for COM and Win32 backends.
- SQLite library for project and catalog modules.
- A JSON library with predictable ownership and no hidden runtime dependency.

Dependency policy:

- Prefer small, well-maintained libraries.
- Vendor dependencies only when package resolution is unreliable for users.
- Keep public server launch independent of Python once native parity is
  achieved.
- Document every dependency in `cpp/README.md`.

## Migration Plan

### Milestone 1: Native MCP Skeleton

- Add `cpp/` CMake scaffold.
- Implement stdio JSON-RPC.
- Implement MCP `initialize`, `tools/list`, and `tools/call`.
- Register all 14 current tools.
- Add operation registries for every current operation name.
- Return `not_supported` for unimplemented backend operations.
- Add native protocol/core/tool tests.

### Milestone 2: Core Backend Manager

- Implement config, backend detection, result formatting, error mapping, and
  capability reporting.
- Implement `system(status)`, `system(health)`, `system(runtime)`,
  `system(dwg_status)`, `system(preflight)`, and `system(tool_help)`.
- Add converter detection for `accoreconsole.exe` and ODA File Converter.

### Milestone 3: File IPC Native Backend

- Port command/result file protocol.
- Port Win32 AutoCAD LT window detection and trigger behavior.
- Exercise current LISP dispatcher without changing its public command map.
- Add offline protocol tests plus optional live LT smoke tests.

### Milestone 4: COM Native Backend

- Implement COM STA worker and AutoCAD attach logic.
- Port drawing, layer, basic entity, view, and screenshot operations first.
- Add COM disconnection handling and busy-call retry.
- Add live full-AutoCAD integration tests gated by environment.

### Milestone 5: Domain Geometry and Architecture

- Port backend-neutral geometry primitives.
- Port AIA/NCS layer setup and architectural composite operations.
- Compare output against current Python behavior for generated geometry.

### Milestone 6: Project, Library, and Headless DXF

- Port SQLite project index and asset catalog.
- Port external block scanning, vendor import metadata, standards checks, and
  sheet/XREF queries.
- Implement native DXF read/write coverage needed by CI and preview workflows.

### Milestone 7: Events, Advanced AutoCAD, and Full Replacement

- Port event log reader and .NET reactor loading path.
- Port 3D operations, layouts, named views, layer states, tables, XREF bind,
  batch, and remaining Express/style operations.
- Switch plugin config to launch native executable by default.
- Keep Python launch as a temporary fallback until release validation passes.

## Completion Criteria

The rewrite is complete only when current evidence proves:

- Native executable builds from a clean checkout.
- Native server launches from MCP stdio without Python.
- Native `tools/list` exposes all 14 tools.
- Native `tools/call` supports every current operation with equivalent payload
  or a documented backend-specific capability difference.
- Full AutoCAD COM backend covers the live AutoCAD feature set currently
  supported by Python.
- AutoCAD LT file IPC backend covers current LISP dispatcher support.
- Headless backend covers current CI/project/library/preview workflows.
- Existing default tests pass or have native parity replacements with equal
  behavioral coverage.
- Live integration gates are documented and pass on a machine with AutoCAD and
  required converters.
- Memory and lifetime tests show no leaks across repeated server startup,
  backend reset, COM attach/detach, file IPC calls, and project indexing.
- Plugin and manual MCP setup docs point to the native executable by default.

## Risks and Mitigations

- COM automation is STA-sensitive. Mitigation: one dedicated STA worker and no
  direct COM calls from tool threads.
- Native DXF/DWG parity is large. Mitigation: stage DXF support and keep DWG
  claims tied to AutoCAD or a reliable converter/SDK.
- A big-bang port would hide regressions. Mitigation: register full API early
  and port behavior in tested slices.
- Toolchain may be missing on contributor machines. Mitigation: document MSVC
  Build Tools setup and keep Python server usable during migration.
- Native memory bugs are possible. Mitigation: RAII rules, sanitizer builds,
  bounded caches, stress tests, and review checklist.

## Open Implementation Decisions

These are decisions for the implementation plan, not unresolved requirements:

- Select the JSON library after checking available package options and license.
- Select SQLite integration method after checking whether a system SQLite or
  vendored amalgamation is preferable for Windows users.
- Decide whether native DXF support is built in-house first or uses a
  permissively licensed library.
- Decide when ObjectARX becomes worthwhile as an additional backend after COM
  parity is stable.

