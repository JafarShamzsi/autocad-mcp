# autocad-mcp — first 5 minutes

The plugin is installed. Now what?

This is a 60-second smoke test you can copy-paste into Claude.

---

## 1. Confirm the server is alive

```
system(operation="status")
```

You'll see one of:

- `backend: "com"` — full AutoCAD running. Best path; every tool works.
- `backend: "file_ipc"` — AutoCAD LT running. Most tools work; `pid` needs
  `lisp-code/pid_tools.lsp` on the AutoCAD Support File Search Path
  (the dispatcher auto-loads it if `findfile` resolves the name).
- `backend: "ezdxf"` — headless. Everything except live screenshots
  and `plot_pdf` still works; you'll save DXFs to disk and inspect.

If you see `ezdxf` but expected `com`, start AutoCAD and re-call
`system(operation="init")`.

## 2. Draw a tiny floor plan

```
arch(operation="setup_layers")
arch(operation="wall_with_openings", data={
  "x1": 0, "y1": 0, "x2": 240, "y2": 0, "thickness": 6,
  "openings": [
    {"type": "door",   "position": 60,  "width": 36, "swing": "left"},
    {"type": "window", "position": 144, "width": 30}
  ]
})
arch(operation="room", data={
  "boundary": [[0,0],[240,0],[240,120],[0,120]],
  "name": "LIVING"
})
arch(operation="dimension_chain", data={
  "points": [[0,0],[60,0],[144,0],[240,0]],
  "direction": "horizontal", "offset": 24
})
```

That gives you a wall with a door + window, a labelled room, and a
chain of horizontal dimensions across the openings.

## 3. Save it

```
drawing(operation="save_as_dxf", data={"path": "C:\\temp\\smoke.dxf"})
```

## 4. (Optional) Multi-DWG audit

If you have a project with several DWGs and an `.acadproj.toml` at
its root:

```
project(operation="load", data={"path": "C:\\path\\to\\project"})
project(operation="index")
project(operation="check_standard")
project(operation="find_broken_xrefs")
```

`check_standard` defaults to AIA layer compliance; point it at a firm
reference DXF with `data={"standard": "C:\\path\\to\\firm-standard.dxf"}`.

## 5. (Optional) Live pairing

If you want Claude to watch as you draw in AutoCAD:

```
events(operation="start_recording")
... user draws stuff in AutoCAD ...
events(operation="new")
```

`new` returns only events appended since the previous `new` call.

---

## Common first-day gotchas

- **`pid` ops return "pid_tools.lsp not loaded"** on file_ipc — the LISP
  companion isn't on AutoCAD's Support File Search Path. Either
  APPLOAD it once or add `<plugin>/lisp-code/` to
  Options → Files → Support File Search Path.
- **`system(operation="execute_lisp")` returns "execute_lisp is
  disabled"** — security gate. Set the `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`
  env var on the MCP server process and restart.
- **`plot_pdf` errors with "DWG To PDF.pc3 not available"** — configure
  that plotter in AutoCAD (`PLOTTERMANAGER` → "DWG To PDF.pc3").

For everything else, see the project's `README.md` and
`skills/architectural-drafting/SKILL.md`.
