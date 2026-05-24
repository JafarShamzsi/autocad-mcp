"""File-based IPC backend for AutoCAD LT.

Protocol (IPC dir resolves from `AUTOCAD_MCP_IPC_DIR`, defaulting to
`<system tempdir>/autocad-mcp`):
1. Python writes JSON command to <ipc-dir>/autocad_mcp_cmd_{request_id}.json
2. Python types the fixed string "(c:mcp-dispatch)" + Enter
3. LISP reads cmd, dispatches via command map, writes result to
   <ipc-dir>/autocad_mcp_result_{request_id}.json
4. Python polls for result file (100ms intervals, 10s timeout)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import structlog

from autocad_mcp.backends.base import (
    AutoCADBackend,
    BackendCapabilities,
    CommandResult,
    not_supported,
)
from autocad_mcp.config import IPC_DIR, IPC_TIMEOUT, LISP_DIR

log = structlog.get_logger()

# IPC settings
POLL_INTERVAL = 0.1  # seconds
TIMEOUT = IPC_TIMEOUT  # seconds (configurable via AUTOCAD_MCP_IPC_TIMEOUT)
STALE_THRESHOLD = 60.0  # clean up files older than this


def find_autocad_window() -> int | None:
    """Find an AutoCAD window handle by walking visible windows.

    The default match `"autocad" in title AND ("drawing" or ".dwg" in title)`
    catches AutoCAD LT, full AutoCAD, AutoCAD Architecture / MEP and similar
    Autodesk-vertical products. When the user has multiple Autodesk apps
    open and wants to disambiguate, they can set the
    `AUTOCAD_MCP_WINDOW_HINT` env var to an additional case-insensitive
    substring the title must contain (e.g. `"AutoCAD LT"` to exclude
    AutoCAD Architecture).

    Selection is deterministic: when multiple windows match, prefers (in
    order) the foreground window, then sorts the rest by HWND so the same
    window wins every call within a session.
    """
    if sys.platform != "win32":
        return None
    try:
        import win32gui

        hint = os.environ.get("AUTOCAD_MCP_WINDOW_HINT", "").strip().lower()
        windows: list[int] = []

        def callback(hwnd, result):
            if win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd).lower()
                if "autocad" in text and ("drawing" in text or ".dwg" in text):
                    if not hint or hint in text:
                        result.append(hwnd)
            return True

        win32gui.EnumWindows(callback, windows)
        if not windows:
            return None
        # Prefer whatever AutoCAD window the user has in the foreground.
        try:
            fg = win32gui.GetForegroundWindow()
            if fg in windows:
                return fg
        except Exception:
            pass
        # Otherwise pick the lowest HWND — deterministic across calls so a
        # session that succeeds once keeps targeting the same window.
        return min(windows)
    except ImportError:
        return None


class FileIPCBackend(AutoCADBackend):
    """File-based IPC with AutoCAD LT via mcp_dispatch.lsp."""

    def __init__(self):
        self._hwnd: int | None = None
        self._command_hwnd: int | None = None
        self._ipc_dir = Path(IPC_DIR)
        self._screenshot_provider = None
        self._lock = asyncio.Lock()  # Single in-flight command

    @property
    def name(self) -> str:
        return "file_ipc"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=True,
            can_zoom=True,
            can_query_entities=True,
            can_file_operations=True,
            can_undo=True,
        )

    async def initialize(self) -> CommandResult:
        """Find AutoCAD window and verify dispatcher is loaded.

        Refuses upfront on full AutoCAD 2025+ — the modern UI is rendered
        via DirectX rather than Win32 EDIT controls, so PostMessageW to
        MDIClient never reaches the command line (H4). Auto-mode picks
        COM in that case; this check is for explicit
        AUTOCAD_MCP_BACKEND=file_ipc users.
        """
        # H4: explicit refusal when full AutoCAD is detected. The PostMessage
        # trigger is LT-specific; on full AutoCAD it silently times out
        # despite the LISP being loaded.
        from autocad_mcp.config import _running_autocad_full_version

        try:
            full_version = _running_autocad_full_version()
        except Exception as ex:
            log.debug("file_ipc_full_version_probe_failed", error=str(ex))
            full_version = None
        if full_version:
            return CommandResult(
                ok=False,
                error=(
                    f"file_ipc backend isn't supported against full AutoCAD "
                    f"(detected {full_version}). The PostMessageW(WM_CHAR) "
                    "trigger this backend uses is LT-only — on full AutoCAD "
                    "the command line is rendered via DirectX and won't "
                    "receive the message. Use AUTOCAD_MCP_BACKEND=com instead "
                    "(or unset it; auto-mode prefers COM when full AutoCAD "
                    "is running)."
                ),
                category="not_supported",
            )

        self._hwnd = find_autocad_window()
        if not self._hwnd:
            return CommandResult(
                ok=False,
                error="AutoCAD LT window not found",
                category="backend_unavailable",
            )

        # Set up screenshot provider
        try:
            from autocad_mcp.screenshot import Win32ScreenshotProvider

            self._screenshot_provider = Win32ScreenshotProvider(self._hwnd)
        except Exception as ex:
            # Screenshot is optional — log and continue without one.
            log.debug("file_ipc_screenshot_init_failed", error=str(ex))

        # Find command-line child edit control for focus-free dispatch
        self._command_hwnd = self._find_command_line_hwnd()
        log.info("command_line_hwnd", hwnd=self._command_hwnd)

        # Ensure IPC directory exists
        self._ipc_dir.mkdir(parents=True, exist_ok=True)

        # Clean up stale IPC files
        self._cleanup_stale_files()

        # Ping the dispatcher to verify it's loaded
        result = await self._dispatch("ping", {})
        if not result.ok:
            lisp_path = str(LISP_DIR / "mcp_dispatch.lsp").replace("\\", "/")
            return CommandResult(
                ok=False,
                error=(
                    "AutoCAD LT detected but mcp_dispatch.lsp not loaded.\n"
                    f'In AutoCAD command line, type:\n  (load "{lisp_path}")\n'
                    "Or add lisp-code/ to trusted paths for auto-loading."
                ),
                category="backend_unavailable",
            )

        return CommandResult(ok=True, payload={"backend": "file_ipc", "hwnd": self._hwnd})

    async def status(self) -> CommandResult:
        info = {
            "backend": "file_ipc",
            "hwnd": self._hwnd,
            "ipc_dir": str(self._ipc_dir),
            "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
        }
        return CommandResult(ok=True, payload=info)

    # --- IPC dispatch ---

    async def _dispatch(self, command: str, params: dict) -> CommandResult:
        """Send a command via file IPC and wait for result."""
        async with self._lock:
            return await self._dispatch_unlocked(command, params)

    async def _dispatch_unlocked(self, command: str, params: dict) -> CommandResult:
        """Core IPC logic (must be called under _lock)."""
        request_id = uuid.uuid4().hex[:12]
        cmd_file = self._ipc_dir / f"autocad_mcp_cmd_{request_id}.json"
        result_file = self._ipc_dir / f"autocad_mcp_result_{request_id}.json"
        tmp_file = cmd_file.with_suffix(".tmp")

        try:
            # Strip None values — the simple LISP JSON parser can't handle null
            clean_params = {k: v for k, v in params.items() if v is not None}
            # Atomic write: write to .tmp, then rename
            payload = {
                "request_id": request_id,
                "command": command,
                "params": clean_params,
                "ts": time.time(),
            }
            tmp_file.write_text(json.dumps(payload), encoding="utf-8")
            tmp_file.rename(cmd_file)

            # Type the fixed dispatch trigger
            self._type_dispatch_trigger()

            # Poll for result
            deadline = time.time() + TIMEOUT
            while time.time() < deadline:
                if result_file.exists():
                    try:
                        # AutoCAD LISP writes files in Windows-1252 encoding;
                        # try UTF-8 first (covers ASCII), fall back to cp1252
                        try:
                            text = result_file.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            text = result_file.read_text(encoding="cp1252")
                        data = json.loads(text)
                        # Verify request_id matches
                        if data.get("request_id") == request_id:
                            return CommandResult(
                                ok=data.get("ok", False),
                                payload=data.get("payload"),
                                error=data.get("error"),
                            )
                    except (json.JSONDecodeError, OSError) as ex:
                        # File may be partially written — keep polling.
                        log.debug("file_ipc_result_parse_retry", request_id=request_id, error=str(ex))
                await asyncio.sleep(POLL_INTERVAL)

            return CommandResult(
                ok=False,
                error=f"Timeout waiting for result (request_id={request_id})",
                category="timeout",
            )

        finally:
            # Cleanup
            for f in (cmd_file, result_file, tmp_file):
                try:
                    f.unlink(missing_ok=True)
                except OSError as ex:
                    # File still locked or perm issue — _cleanup_stale_files on
                    # next init mops up. Log so we can spot the pattern.
                    log.debug("file_ipc_cleanup_unlink_failed", file=str(f), error=str(ex))

    def _find_command_line_hwnd(self) -> int | None:
        """Find AutoCAD's MDIClient child window for command routing."""
        if sys.platform != "win32" or not self._hwnd:
            return None
        try:
            import win32gui

            mdi_client: list[int] = []

            def cb(child_hwnd, _):
                if win32gui.GetClassName(child_hwnd) == "MDIClient":
                    mdi_client.append(child_hwnd)
                    return False  # stop enumeration
                return True

            win32gui.EnumChildWindows(self._hwnd, cb, None)
            return mdi_client[0] if mdi_client else None
        except Exception as ex:
            log.debug("file_ipc_command_line_lookup_failed", hwnd=self._hwnd, error=str(ex))
            return None

    def _type_dispatch_trigger(self):
        """Post '(c:mcp-dispatch)' + Enter via WM_CHAR to MDIClient — no focus steal.

        Sends ESC keystrokes first to cancel any stale pending command
        (e.g. from a previous timeout leaving AutoCAD in a command prompt).
        """
        try:
            import ctypes

            WM_CHAR = 0x0102
            WM_KEYDOWN = 0x0100
            WM_KEYUP = 0x0101
            VK_ESCAPE = 0x1B
            target = self._command_hwnd or self._hwnd
            post = ctypes.windll.user32.PostMessageW

            # Cancel any pending command (2x ESC for nested commands)
            for _ in range(2):
                post(target, WM_KEYDOWN, VK_ESCAPE, 0)
                post(target, WM_KEYUP, VK_ESCAPE, 0)
            time.sleep(0.05)

            for ch in "(c:mcp-dispatch)":
                post(target, WM_CHAR, ord(ch), 0)
            # Enter = carriage return
            post(target, WM_CHAR, 0x0D, 0)
            time.sleep(0.05)
        except Exception as e:
            log.error("dispatch_trigger_failed", error=str(e))

    def _cleanup_stale_files(self):
        """Remove stale IPC files from previous sessions. Best-effort."""
        try:
            now = time.time()
            for pattern in ("autocad_mcp_*.json", "autocad_mcp_*.tmp", "autocad_mcp_lisp_*.lsp"):
                for f in self._ipc_dir.glob(pattern):
                    if now - f.stat().st_mtime > STALE_THRESHOLD:
                        f.unlink(missing_ok=True)
        except OSError as ex:
            log.debug("file_ipc_cleanup_stale_failed", error=str(ex))

    # --- Drawing management ---

    async def drawing_info(self) -> CommandResult:
        return await self._dispatch("drawing-info", {})

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        return await self._dispatch("drawing-save", {"path": path})

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-save-as-dxf", {"path": path})

    async def drawing_create(self, name: str | None = None, force: bool = False) -> CommandResult:
        # `force=True` lets the LISP side discard unsaved changes in the
        # active drawing. Without it, the dispatcher refuses to clobber a
        # dirty drawing (DBMOD != 0).
        return await self._dispatch("drawing-create", {
            "name": name,
            "force": "1" if force else "0",
        })

    async def drawing_purge(self) -> CommandResult:
        return await self._dispatch("drawing-purge", {})

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-plot-pdf", {"path": path})

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        if names:
            # Strip $ prefix for AutoCAD compatibility (ezdxf uses $ACADVER, AutoCAD uses ACADVER)
            clean_names = [n.lstrip("$") for n in names]
            names_str = ";".join(clean_names)
        else:
            names_str = ""
        return await self._dispatch("drawing-get-variables", {"names_str": names_str})

    async def drawing_open(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-open", {"path": path})

    # --- Undo / Redo ---

    async def undo(self) -> CommandResult:
        return await self._dispatch("undo", {})

    async def redo(self) -> CommandResult:
        return await self._dispatch("redo", {})

    # --- Freehand LISP execution ---

    async def execute_lisp(self, code: str) -> CommandResult:
        """Execute arbitrary AutoLISP code via a temp .lsp file.

        The file is unlinked once the dispatcher returns — leaving the file
        on disk would create an attacker-controllable artefact when the LISP
        gate is intentionally opened (AUTOCAD_MCP_ENABLE_LISP_EXEC=1).
        """
        request_id = uuid.uuid4().hex[:12]
        code_file = self._ipc_dir / f"autocad_mcp_lisp_{request_id}.lsp"
        code_file.write_text(code, encoding="utf-8")
        try:
            return await self._dispatch("execute-lisp", {
                "code_file": str(code_file).replace("\\", "/")
            })
        finally:
            try:
                code_file.unlink(missing_ok=True)
            except OSError as ex:
                # AutoCAD may briefly hold the file open after loading it;
                # _cleanup_stale_files() on next init will mop up.
                log.debug("execute_lisp_unlink_failed", file=str(code_file), error=str(ex))

    # --- Entity operations ---

    async def create_line(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self._dispatch("create-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_circle(self, cx, cy, radius, layer=None) -> CommandResult:
        return await self._dispatch("create-circle", {"cx": cx, "cy": cy, "radius": radius, "layer": layer})

    async def create_polyline(self, points, closed=False, layer=None) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-polyline", {
            "points_str": pts_str, "closed": "1" if closed else "0", "layer": layer
        })

    async def create_rectangle(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self._dispatch("create-rectangle", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None) -> CommandResult:
        return await self._dispatch("create-arc", {"cx": cx, "cy": cy, "radius": radius, "start_angle": start_angle, "end_angle": end_angle, "layer": layer})

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None) -> CommandResult:
        return await self._dispatch("create-ellipse", {"cx": cx, "cy": cy, "major_x": major_x, "major_y": major_y, "ratio": ratio, "layer": layer})

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None) -> CommandResult:
        return await self._dispatch("create-mtext", {"x": x, "y": y, "width": width, "text": text, "height": height, "layer": layer})

    async def create_hatch(self, entity_id, pattern="ANSI31") -> CommandResult:
        return await self._dispatch("create-hatch", {"entity_id": entity_id, "pattern": pattern})

    async def entity_list(self, layer=None, limit: int = 1000) -> CommandResult:
        return await self._dispatch("entity-list", {"layer": layer, "limit": limit})

    async def entity_count(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-count", {"layer": layer})

    async def entity_get(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-get", {"entity_id": entity_id})

    async def entity_erase(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-erase", {"entity_id": entity_id})

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-copy", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-move", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        return await self._dispatch("entity-rotate", {"entity_id": entity_id, "cx": cx, "cy": cy, "angle": angle})

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        return await self._dispatch("entity-scale", {"entity_id": entity_id, "cx": cx, "cy": cy, "factor": factor})

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("entity-mirror", {"entity_id": entity_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        return await self._dispatch("entity-offset", {"entity_id": entity_id, "distance": distance})

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        return await self._dispatch("entity-array", {"entity_id": entity_id, "rows": rows, "cols": cols, "row_dist": row_dist, "col_dist": col_dist})

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        return await self._dispatch("entity-fillet", {"id1": entity_id1, "id2": entity_id2, "radius": radius})

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        return await self._dispatch("entity-chamfer", {"id1": entity_id1, "id2": entity_id2, "dist1": dist1, "dist2": dist2})

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        return await self._dispatch("layer-list", {})

    async def layer_create(self, name, color="white", linetype="CONTINUOUS") -> CommandResult:
        return await self._dispatch("layer-create", {"name": name, "color": color, "linetype": linetype})

    async def layer_set_current(self, name) -> CommandResult:
        return await self._dispatch("layer-set-current", {"name": name})

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        return await self._dispatch("layer-set-properties", {"name": name, "color": color, "linetype": linetype, "lineweight": lineweight})

    async def layer_freeze(self, name) -> CommandResult:
        return await self._dispatch("layer-freeze", {"name": name})

    async def layer_thaw(self, name) -> CommandResult:
        return await self._dispatch("layer-thaw", {"name": name})

    async def layer_lock(self, name) -> CommandResult:
        return await self._dispatch("layer-lock", {"name": name})

    async def layer_unlock(self, name) -> CommandResult:
        return await self._dispatch("layer-unlock", {"name": name})

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        return await self._dispatch("block-list", {})

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        return await self._dispatch("block-insert", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "block_id": block_id})

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("block-insert-with-attributes", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "attributes": attributes or {}})

    async def block_get_attributes(self, entity_id) -> CommandResult:
        return await self._dispatch("block-get-attributes", {"entity_id": entity_id})

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        return await self._dispatch("block-update-attribute", {"entity_id": entity_id, "tag": tag, "value": value})

    async def block_define(self, name, entities) -> CommandResult:
        return await self._dispatch("block-define", {"name": name, "entities": entities})

    # --- Annotation ---

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None) -> CommandResult:
        return await self._dispatch("create-text", {"x": x, "y": y, "text": text, "height": height, "rotation": rotation, "layer": layer})

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        return await self._dispatch("create-dimension-linear", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "dim_x": dim_x, "dim_y": dim_y})

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        return await self._dispatch("create-dimension-aligned", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "offset": offset})

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("create-dimension-angular", {"cx": cx, "cy": cy, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        return await self._dispatch("create-dimension-radius", {"cx": cx, "cy": cy, "radius": radius, "angle": angle})

    async def create_leader(self, points, text) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-leader", {"points_str": pts_str, "text": text})

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        return await self._dispatch("pid-setup-layers", {})

    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-insert-symbol", {"category": category, "symbol": symbol, "x": x, "y": y, "scale": scale, "rotation": rotation})

    async def pid_list_symbols(self, category) -> CommandResult:
        return await self._dispatch("pid-list-symbols", {"category": category})

    async def pid_draw_process_line(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-draw-process-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_connect_equipment(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-connect-equipment", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_add_flow_arrow(self, x, y, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-add-flow-arrow", {"x": x, "y": y, "rotation": rotation})

    async def pid_add_equipment_tag(self, x, y, tag, description="") -> CommandResult:
        return await self._dispatch("pid-add-equipment-tag", {"x": x, "y": y, "tag": tag, "description": description})

    async def pid_add_line_number(self, x, y, line_num, spec) -> CommandResult:
        return await self._dispatch("pid-add-line-number", {"x": x, "y": y, "line_num": line_num, "spec": spec})

    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-valve", {"x": x, "y": y, "valve_type": valve_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value="") -> CommandResult:
        return await self._dispatch("pid-insert-instrument", {"x": x, "y": y, "instrument_type": instrument_type, "rotation": rotation, "tag_id": tag_id, "range_value": range_value})

    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-pump", {"x": x, "y": y, "pump_type": pump_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-tank", {"x": x, "y": y, "tank_type": tank_type, "scale": scale, "attributes": attributes or {}})

    # --- View ---

    # --- Architectural primitives (AIA/NCS) ---

    async def arch_setup_layers(self) -> CommandResult:
        return await self._dispatch("arch-setup-layers", {})

    async def arch_wall(self, x1, y1, x2, y2, thickness=6.0, layer="A-WALL", justify="center") -> CommandResult:
        return await self._dispatch("arch-wall", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "thickness": thickness, "layer": layer, "justify": justify,
        })

    async def arch_wall_with_openings(
        self, x1, y1, x2, y2, thickness=6.0, openings=None,
        layer="A-WALL", door_layer="A-DOOR", window_layer="A-GLAZ", justify="center",
    ) -> CommandResult:
        # The LISP JSON parser can't decode nested arrays/objects, so each
        # opening is flattened into "pos|width|type|swing|open_angle|sill_thickness"
        # and openings are joined with ";". The LISP side fills per-type
        # defaults for empty fields.
        records: list[str] = []
        for o in openings or []:
            pos = float(o["position"])
            width = float(o["width"])
            kind = str(o.get("type", "door"))
            swing = str(o.get("swing", "left"))
            open_angle = o.get("open_angle", "")
            sill = o.get("sill_thickness", "")
            records.append(
                f"{pos}|{width}|{kind}|{swing}|{open_angle}|{sill}"
            )
        return await self._dispatch("arch-wall-with-openings", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "thickness": thickness,
            "openings_str": ";".join(records),
            "layer": layer,
            "door_layer": door_layer,
            "window_layer": window_layer,
            "justify": justify,
        })

    async def arch_door(self, x, y, width=36.0, angle=0.0, swing="left", open_angle=90.0, layer="A-DOOR", room_side=None) -> CommandResult:
        params: dict = {
            "x": x, "y": y, "width": width, "angle": angle,
            "swing": swing, "open_angle": open_angle, "layer": layer,
        }
        # LISP JSON parser can't decode nested arrays — flatten room_side to
        # two scalar params (x, y). The LISP handler treats either coordinate
        # being absent as "no room_side, use swing arg".
        if room_side is not None:
            params["room_side_x"] = float(room_side[0])
            params["room_side_y"] = float(room_side[1])
        return await self._dispatch("arch-door", params)

    async def arch_window(self, x1, y1, x2, y2, sill_thickness=6.0, layer="A-GLAZ") -> CommandResult:
        return await self._dispatch("arch-window", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "sill_thickness": sill_thickness, "layer": layer,
        })

    async def arch_room(
        self, boundary, name="", area_layer="A-AREA",
        text_layer="A-AREA-IDEN", text_height=6.0,
    ) -> CommandResult:
        # The LISP JSON parser can't handle nested arrays, so flatten the
        # boundary into a semicolon-delimited "x,y;x,y;..." string — same
        # convention used by create_polyline and create_leader.
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in boundary)
        return await self._dispatch("arch-room", {
            "points_str": pts_str, "name": name,
            "area_layer": area_layer, "text_layer": text_layer,
            "text_height": text_height,
        })

    async def arch_dimension_chain(
        self, points, offset=24.0, direction="horizontal", layer="A-ANNO-DIMS",
    ) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("arch-dimension-chain", {
            "points_str": pts_str, "offset": offset,
            "direction": direction, "layer": layer,
        })

    # --- New building primitives via arch-emit renderer ---
    # Python computes the geometry, serializes it as a layer-tagged spec,
    # and the LISP side (arch_tools.lsp::mcp-arch-emit-entities) emits each
    # entity on its tagged layer. Keeps the geometry math in one place.

    async def _emit_arch(self, entities_str: str, default_layer: str,
                          extra_payload: dict | None = None) -> CommandResult:
        result = await self._dispatch("arch-emit", {
            "entities": entities_str,
            "layer": default_layer,
        })
        if result.ok and extra_payload and isinstance(result.payload, dict):
            merged = {**result.payload, **extra_payload}
            return CommandResult(ok=True, payload=merged)
        return result

    async def arch_stair(
        self, x1, y1, x2, y2, width=36.0, num_treads=12, tread_depth=None,
        layer="A-FLOR-STRS", shape="straight", landing_depth=None,
    ) -> CommandResult:
        import math as _math
        from autocad_mcp.arch.geometry import stair_run, stair_u_shape
        from autocad_mcp.arch.lisp_render import serialize_stair, serialize_stair_u
        try:
            if shape == "straight":
                s = stair_run(x1, y1, x2, y2, width, num_treads, tread_depth=tread_depth)
                entities = serialize_stair(s, layer=layer)
                meta = {
                    "shape": "straight",
                    "length": s["length"],
                    "num_treads": s["num_treads"],
                    "tread_depth": s["tread_depth"],
                    "width": s["width"],
                }
            elif shape in ("u", "u_shape"):
                dx = x2 - x1
                dy = y2 - y1
                fl = _math.hypot(dx, dy)
                if fl == 0:
                    return CommandResult(
                        ok=False, error="U-shape stair: (x1,y1) and (x2,y2) coincide",
                        category="invalid_arg",
                    )
                angle = _math.degrees(_math.atan2(dy, dx))
                u = stair_u_shape(
                    x1, y1, flight_length=fl, width=width,
                    num_treads_per_flight=num_treads, landing_depth=landing_depth,
                    angle_deg=angle,
                )
                entities = serialize_stair_u(u, layer=layer)
                meta = {
                    "shape": "u",
                    "total_treads": u["total_treads"],
                    "width": width,
                }
            else:
                return CommandResult(
                    ok=False, error=f"shape must be 'straight' or 'u'; got {shape!r}",
                    category="invalid_arg",
                )
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        return await self._emit_arch(entities, layer, meta)

    async def arch_column(
        self, x, y, shape="square", size=12.0, size_y=None, angle=0.0, layer="A-COLS",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import column_corners
        from autocad_mcp.arch.lisp_render import serialize_column
        try:
            spec = column_corners(x, y, shape, size, size_y=size_y, angle_deg=angle)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        entities = serialize_column(spec, layer=layer)
        meta = {
            "shape": spec["shape"],
            "center": spec["center"],
        }
        if spec["shape"] == "rect":
            meta.update({"size_x": spec["size_x"], "size_y": spec["size_y"], "angle": spec["angle"]})
        else:
            meta["radius"] = spec["radius"]
        return await self._emit_arch(entities, layer, meta)

    async def arch_slab(self, boundary, edge_thickness=0.0, layer="A-FLOR") -> CommandResult:
        from autocad_mcp.arch.geometry import slab_outline
        from autocad_mcp.arch.lisp_render import serialize_slab
        try:
            pts = [(float(p[0]), float(p[1])) for p in boundary]
            spec = slab_outline(pts, edge_thickness=edge_thickness)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        entities = serialize_slab(spec, layer=layer)
        return await self._emit_arch(entities, layer, {
            "area": spec["area"],
            "centroid": spec["centroid"],
        })

    async def arch_beam(self, x1, y1, x2, y2, width=0.0, layer="A-WALL-HEAD") -> CommandResult:
        from autocad_mcp.arch.geometry import beam_endpoints
        from autocad_mcp.arch.lisp_render import serialize_beam
        try:
            spec = beam_endpoints(x1, y1, x2, y2, width=width)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        entities = serialize_beam(spec, layer=layer)
        return await self._emit_arch(entities, layer, {
            "length": spec["length"],
            "width": width,
        })

    async def arch_roof(
        self, boundary, style="gable", ridge_axis="x", overhang=12.0,
        eaves_layer="A-ROOF", ridge_layer="A-ROOF",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import roof_plan
        from autocad_mcp.arch.lisp_render import serialize_roof
        try:
            pts = [(float(p[0]), float(p[1])) for p in boundary]
            spec = roof_plan(pts, style=style, ridge_axis=ridge_axis, overhang=overhang)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        entities = serialize_roof(spec, eaves_layer=eaves_layer, ridge_layer=ridge_layer)
        return await self._emit_arch(entities, eaves_layer, {
            "style": spec["style"],
            "footprint": spec["footprint"],
        })

    async def arch_elevation(
        self, origin_x, origin_y, wall_length, height,
        openings=None, wall_layer="A-WALL", door_layer="A-DOOR", window_layer="A-GLAZ",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import elevation_view
        from autocad_mcp.arch.lisp_render import serialize_elevation
        try:
            spec = elevation_view(
                wall_length=wall_length, height=height, floor_y=0.0,
                openings=openings or [],
            )
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        entities = serialize_elevation(
            spec, origin_x, origin_y,
            wall_layer=wall_layer, door_layer=door_layer, window_layer=window_layer,
        )
        return await self._emit_arch(entities, wall_layer, {
            "wall_length": wall_length,
            "height": height,
            "opening_count": len(spec["openings"]),
        })

    async def arch_section(
        self, origin_x, origin_y, section_width, floor_y, ceiling_y,
        elements=None, wall_layer="A-WALL", slab_layer="A-FLOR",
        door_layer="A-DOOR", window_layer="A-GLAZ",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import section_view
        from autocad_mcp.arch.lisp_render import serialize_section
        try:
            spec = section_view(
                floor_y=floor_y, ceiling_y=ceiling_y,
                section_width=section_width, elements=elements or [],
            )
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        entities = serialize_section(
            spec, origin_x, origin_y,
            wall_layer=wall_layer, slab_layer=slab_layer,
            door_layer=door_layer, window_layer=window_layer,
        )
        return await self._emit_arch(entities, wall_layer, {
            "floor_y": floor_y,
            "ceiling_y": ceiling_y,
            "section_width": section_width,
        })

    async def arch_template(self, name, x, y, params=None) -> CommandResult:
        from autocad_mcp.arch.lisp_render import serialize_template
        try:
            entities, info = serialize_template(None, name, x, y, params)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="not_found")
        return await self._emit_arch(entities, "A-WALL", info)

    # --- Symbol library (procedural AEC blocks) ---
    #
    # Python is the source of truth for the library catalogue
    # (`autocad_mcp.arch.library.SYMBOL_LIBRARY`). For each `define`, the
    # spec list is serialized to a compact mini-language via
    # `serialize_spec_for_lisp` and sent in the IPC payload; the
    # `library-define` handler in `mcp_dispatch.lsp` parses it via
    # helpers in `lisp-code/library_tools.lsp` and absorbs the entities
    # into a `_.-BLOCK`. `library_insert` calls define + block-insert.

    async def library_list(self) -> CommandResult:
        # No AutoCAD round-trip — the catalogue is pure Python data.
        from autocad_mcp.arch.library import symbol_names
        names = symbol_names()
        return CommandResult(
            ok=True, payload={"symbols": names, "count": len(names)},
        )

    async def library_define(self, name: str) -> CommandResult:
        from autocad_mcp.arch.library import SYMBOL_LIBRARY, serialize_spec_for_lisp

        if name not in SYMBOL_LIBRARY:
            return CommandResult(
                ok=False, error=f"No library symbol named {name!r}",
                category="not_found",
            )
        entities_str = serialize_spec_for_lisp(SYMBOL_LIBRARY[name])
        return await self._dispatch("library-define", {
            "name": name,
            "entities_str": entities_str,
        })

    async def library_define_all(self) -> CommandResult:
        from autocad_mcp.arch.library import symbol_names

        defined: list[str] = []
        existed: list[str] = []
        errors: list[dict] = []
        for name in symbol_names():
            r = await self.library_define(name)
            if not r.ok:
                errors.append({"name": name, "error": r.error})
                continue
            if r.payload.get("existed"):
                existed.append(name)
            else:
                defined.append(name)
        return CommandResult(ok=True, payload={
            "defined": defined,
            "existed": existed,
            "errors": errors,
            "total": len(defined) + len(existed),
        })

    async def library_insert(
        self, name: str, x: float, y: float,
        scale: float = 1.0, rotation: float = 0.0,
    ) -> CommandResult:
        # Match COM's "ensure-then-insert" semantics: library_define is
        # idempotent (returns existed=true when the block is already in the
        # BLOCK table), so calling it on every insert is cheap.
        r = await self.library_define(name)
        if not r.ok:
            return r
        return await self.block_insert(name, x, y, scale, rotation)

    # --- View ---

    async def zoom_extents(self) -> CommandResult:
        return await self._dispatch("zoom-extents", {})

    async def zoom_window(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("zoom-window", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def get_screenshot(self) -> CommandResult:
        if self._screenshot_provider:
            data = self._screenshot_provider.capture()
            if data:
                return CommandResult(ok=True, payload=data)
        return CommandResult(
            ok=False, error="Screenshot capture failed",
            category="internal_error",
        )
