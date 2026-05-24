# Project Analysis — `autocad-mcp` and the road to "Claude Code for AutoCAD"

Date: 2026-05-10
Goal: Use this repo as inspiration to build a Claude-driven, collaborative AutoCAD workflow for **full AutoCAD on Windows**, focused on **architectural floor plans**, evolving from "Claude drafts, I review" → bidirectional pairing → project-level awareness.

---

## 1. What this repo actually is

A clean MCP server (Python, FastMCP) that lets an MCP client (Claude Code, Claude Desktop) drive AutoCAD LT through 8 consolidated tools.

### Architecture at a glance

```
MCP Client (Claude)
   │  stdio JSON-RPC
   ▼
autocad_mcp Python server (FastMCP, 8 tools)
   │
   ├── File IPC backend  ──►  C:/temp/*.json  ──►  mcp_dispatch.lsp inside AutoCAD LT
   │     PostMessageW(WM_CHAR) to MDIClient — focus-free
   │
   └── ezdxf backend    ──►  in-memory DXF (headless, any OS)
```

### Tool surface (8 tools, operation-dispatched)

| Tool         | What it does                                                  |
|--------------|---------------------------------------------------------------|
| `drawing`    | create / open / info / save / save_as_dxf / plot_pdf / purge / undo / redo / get_variables |
| `entity`     | CRUD + modify (line, circle, polyline, rect, arc, ellipse, mtext, hatch; copy/move/rotate/scale/mirror/offset/array/fillet/chamfer/erase) |
| `layer`      | list / create / set_current / set_properties / freeze / thaw / lock / unlock |
| `block`      | list / insert / insert_with_attributes / get/update_attribute / define |
| `annotation` | text + linear/aligned/angular/radius dimensions + leaders     |
| `pid`        | P&ID-specific (CTO library) — valves, instruments, pumps, tanks |
| `view`       | zoom_extents / zoom_window / get_screenshot                    |
| `system`     | status / health / runtime / init / **execute_lisp**            |

### Strengths to keep

1. **Two-backend abstraction** (`base.py` → `file_ipc.py` / `ezdxf_backend.py`). Clean, capability-flagged. Easy to add a third.
2. **Focus-free dispatch via `PostMessageW(WM_CHAR)` to MDIClient** — user keeps working in other windows while Claude operates AutoCAD. This is rare and worth preserving.
3. **AutoLISP dispatcher with a command whitelist** (no raw eval). Security-conscious. The `execute_lisp` escape hatch is opt-in.
4. **Atomic file IPC** (write `.tmp`, rename) — robust against partial reads.
5. **Win32 `PrintWindow` screenshot** that works when AutoCAD is minimized — a strong primitive for Claude's vision loop.
6. **ESC-prefix + UTF-8/cp1252 fallback + configurable timeout** in v3.1 — the hard-won details are already there.
7. **Tests with golden DXFs** — a real foundation to extend, not a pile of mocks.

### Gaps for *your* goal (full AutoCAD, architectural, project, collab)

| # | Gap | Why it matters for you |
|---|-----|------------------------|
| 1 | **No COM/ActiveX backend** for full AutoCAD | LT-only LISP can't reach 3D, layouts cleanly, XREFs, sheet sets, parametric constraints, reactors. Full AutoCAD via `pywin32`/`pythonnet` opens all of that. |
| 2 | **No reactor / event subscription** | Bidirectional pairing ("Claude watches my draw") needs AutoCAD to *push* events out. Today the flow is one-way: Claude → CAD. |
| 3 | **No project model** | Single drawing only. No notion of multi-DWG project, XREFs, support paths, sheet sets, drawing-standards (.dws). |
| 4 | **P&ID-biased symbol library** (CTO) | You're doing architectural — needs AIA/NCS layer naming, AEC blocks (doors, windows, plumbing fixtures, furniture). |
| 5 | **No semantic index of the drawing** | Can't ask "where are the kitchens" or "list every door wider than 36"" without scanning. |
| 6 | **No DWG read/write headlessly** | `ezdxf` is DXF-only. For project-level operations you'll want DWG (ODA File Converter or Aspose.CAD or Open Design Alliance SDK). |
| 7 | **No layout/paperspace + viewport ops** | Architectural sheets live in paperspace. LISP can do it but a typed API is much cleaner. |
| 8 | **No annotation scaling / annotative styles** | Standard practice in arch drafting; missing. |
| 9 | **No drawing-standards check (.dws)** | Layer/dimstyle/textstyle compliance is a daily reality in arch firms. |
| 10 | **No persistent project memory for Claude** | Each call is stateless. Claude forgets what a "Type A wall" or "the lobby" is between turns unless re-told. |
| 11 | **No Claude Code skill or subagent layered on top** | Just an MCP. The "Claude Code-feeling" UX needs a skill (workflows, checklists, templates) and ideally a specialized subagent. |
| 12 | **No drawing diff / change log** | "What changed since last save" is a great pairing signal — not built. |
| 13 | **Limited tests** | Two golden DXFs. Will be the bottleneck once you start adding tools fast. |

---

## 2. Proposed architecture for "Claude Code for AutoCAD" (architectural)

A 6-layer design. Each layer is independently shippable.

### Layer A — Core engine (replace/extend backends)

- **Add `com_backend.py`** for full AutoCAD via `pywin32` `win32com.client` (IDispatch into `AutoCAD.Application`). Capability flags expose 3D, layouts, XREF, sheet sets.
- **Keep `file_ipc.py`** for LT and as a fallback when COM isn't available.
- **Keep `ezdxf_backend.py`** for headless preview / CI / "show me before doing".
- **Optional `dotnet_backend.py`** later via `pythonnet` if reactors need to run in-process.

### Layer B — Real-time collaboration plane

- AutoCAD-side event source: a small **.NET plugin** (`acad-collab.dll`, NETLOAD'd) that subscribes to reactor events:
  - `Database.ObjectAppended/Modified/Erased`
  - `Document.CommandWillStart/Ended/Cancelled`
  - `Editor.SelectionChanged`
- Events stream out over a **named pipe** (or local websocket) to the MCP server.
- MCP server exposes them as MCP **resources** + SSE, so Claude sees a live feed of "user just drew a wall from (3,4) to (3,8) on layer A-WALL".

### Layer C — Project awareness

- **`.acadproj.toml`** — declares: project root, sheet set, DWG list, XREFs, support paths, standards file (.dws), block libraries, units, scale.
- **Indexer** — walks every DWG, extracts entities/blocks/attributes/layers/spaces into **SQLite + sentence-transformer embeddings**. Enables `find("kitchen")`, `find_doors_wider_than(36)`.
- **XREF tools** — attach / detach / bind / reload / list, with absolute-path normalization.
- **Sheet set basics** — open a `.dst`, list sheets, open sheet by name.
- **Standards check** — load `.dws`, diff layers/dimstyles/textstyles, report deltas.

### Layer D — Architectural domain pack

- **Symbol library** — door, window, fixture, furniture, ADA, electrical, plumbing blocks. Either ship a Creative-Commons set or wrap a vendor library you already have.
- **AIA / NCS layer standard** preset (`A-WALL`, `A-DOOR`, `A-GLAZ`, etc.) created on `drawing_create`.
- **Composite tools**:
  - `arch.wall(start, end, type, height)` — draws cleanup-aware double lines on `A-WALL`.
  - `arch.door(wall_entity, position, width, swing)` — cuts the wall, inserts the right block.
  - `arch.window(wall_entity, position, width, sill_height)` — same.
  - `arch.room(boundary)` — closed polyline on `A-AREA`, attaches space tag, computes area.
  - `arch.dimension_chain(points, layer="A-ANNO-DIMS")` — repeated linears with consistent style.
- **Title block templates** — load standard sheet templates (24x36, 30x42), populate sheet number, project name, revision.
- **Code/standards lite** — minimum corridor width, door clearance, ADA approach — flag violations.

### Layer E — Claude integration

- **Claude Code skill `architectural-drafting`** — `SKILL.md` with workflow guidance, checklists, when to draft vs. annotate, plotting prep.
- **Subagent `autocad-architect`** — specialized prompt with the symbol library, layer standard, and project memory baked in.
- **Memory file** — per-project notes Claude reads on start ("Type A walls = 2x6, GWB both sides", "ceiling height = 9'-0\"").
- **Plugin bundle** — package MCP + skill + subagent + sample blocks as a Claude Code plugin (`.plugin`) so it installs in one click.

### Layer F — UX polish

- **Auto-screenshot after every modification**, attached to the tool result (already supported, default-on).
- **"Preview before commit" mode** — Claude generates DXF in `ezdxf`, renders thumbnail, asks for approval, *then* applies via COM/IPC.
- **Undo grouping** — every Claude turn = one undo group (use `_undo begin/end`).
- **Drawing diff** — capture entity counts/handles before each turn; report "added 12 lines on A-WALL, modified 1 block on A-DOOR".
- **Conversation context tied to drawing** — when Claude opens a DWG, load its sidecar `.claude/{drawing}.md` for prior decisions.

---

## 3. Recommended path & milestones

A staged build. Each phase is independently usable.

| Phase | Outcome | Rough effort |
|------|---------|-------------|
| 0 — Setup & validate | Existing repo runs, full AutoCAD detected, gap list confirmed | 0.5 day |
| 1 — COM backend + arch primitives | `com_backend.py`, AIA layer preset, wall/door/window/room | 1–2 weeks |
| 2 — Project awareness | `.acadproj.toml`, indexer + SQLite, XREF tools, standards check | 1–2 weeks |
| 3 — Bidirectional pairing | .NET reactor plugin, event broker, MCP SSE, live feed in Claude | 2–3 weeks |
| 4 — Claude Code packaging | Skill + subagent + plugin bundle | 3–5 days |
| 5 — Hardening | Tests, perf, docs site, sample gallery | ongoing |

**My recommendation:** start at Phase 1 (COM backend + arch primitives) immediately — it's where you'll feel the win fastest. Ship Phase 0 as a smoke test in parallel.

---

## 4. Open decisions you'll want to make early

- **AIA vs. NCS layer naming** (or your firm's). Pick one and commit; layer assumptions thread through every tool.
- **DWG headless tool** — ODA File Converter (free), Aspose.CAD ($), or pure Open Design Alliance SDK (license)?
- **Symbol library source** — open-source AEC blocks vs. licensed library you already own.
- **Reactor language** — .NET (richest API, but requires AutoCAD .NET dev setup) vs. AutoLISP VLR reactors (limited but no extra build).
- **Bidirectional transport** — named pipe (Windows-native, fast) vs. local websocket (cross-language, debuggable).

---

## 5. Risks worth flagging now

- **AutoCAD COM is single-threaded apartment (STA)** — you'll need careful asyncio bridging. Don't call `acad.Document...` off the main thread.
- **PostMessage trick + COM may conflict** — if you mix backends in one session, sequence them.
- **`execute_lisp` is a power tool and a footgun** — Claude can do anything on the host. Keep the whitelist-first approach for default tools; gate `execute_lisp` behind explicit user confirmation.
- **DWG version compatibility** — full AutoCAD writes 2018 DWG by default; LT may be older. Pin a default and document it.
- **Project-index freshness** — DWGs change outside your control; build a file-watcher or cheap re-index on touch.
