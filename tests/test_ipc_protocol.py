"""Tests for IPC protocol components — no AutoCAD needed.

Tests the file-based IPC protocol logic: command file structure,
result parsing, request_id generation, timeout behavior, stale cleanup,
LISP companion-file presence, and CommandResult serialization.
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autocad_mcp.backends.base import BackendCapabilities, CommandResult


# Repo root for file-presence checks.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LISP_DIR = _REPO_ROOT / "lisp-code"


class TestFileIPCFullAutoCADRefusal:
    """H4: file_ipc against full AutoCAD silently times out because the
    PostMessage trigger doesn't reach the DirectX-rendered command line.
    The backend now refuses upfront with a clear category=not_supported
    error rather than letting the user discover via timeout."""

    async def test_refuses_when_full_autocad_detected(self, monkeypatch):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        from autocad_mcp.config import _running_autocad_full_version

        # Force the probe to claim full AutoCAD is running, regardless of
        # what's actually installed on the test host.
        import autocad_mcp.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_running_autocad_full_version", lambda: "R26.0")
        # The file_ipc module imports this lazily inside `initialize`, so
        # patching cfg_mod is enough.

        b = FileIPCBackend()
        result = await b.initialize()
        assert result.ok is False
        assert result.category == "not_supported"
        assert "full AutoCAD" in result.error
        # The message must include the detected version + the COM hint.
        assert "R26.0" in result.error
        assert "AUTOCAD_MCP_BACKEND=com" in result.error

    async def test_proceeds_when_only_lt_running(self, monkeypatch):
        """If no full AutoCAD is detected, the backend continues with the
        normal init path. We can't drive the actual ping in unit-test land,
        but we can verify it gets past the refusal check."""
        import autocad_mcp.config as cfg_mod
        from autocad_mcp.backends import file_ipc as fipc_mod

        monkeypatch.setattr(cfg_mod, "_running_autocad_full_version", lambda: None)
        # Simulate no AutoCAD window so we predictably fail at the next step
        # (rather than hanging on a real ping).
        monkeypatch.setattr(fipc_mod, "find_autocad_window", lambda: None)

        b = fipc_mod.FileIPCBackend()
        result = await b.initialize()
        assert result.ok is False
        # The error should now be about LT window, not the full-AutoCAD refusal.
        assert "LT window not found" in result.error
        assert result.category == "backend_unavailable"


class TestLispCompanionFiles:
    """The dispatcher delegates P&ID operations to `c:` functions in
    `pid_tools.lsp`. Previously the file was referenced but not shipped,
    so every `pid_*` operation on file_ipc silently returned
    'pid_tools.lsp not loaded'. These tests catch that regression."""

    def test_pid_tools_file_exists(self):
        assert (_LISP_DIR / "pid_tools.lsp").exists(), (
            "pid_tools.lsp must ship — mcp_dispatch.lsp delegates to its "
            "c: functions and without this file every pid_* op fails."
        )

    def test_pid_tools_defines_every_referenced_command(self):
        """Each `(if c:foo ...)` site in the dispatcher's pid section
        requires a matching `(defun c:foo ...)` in pid_tools.lsp."""
        pid_text = (_LISP_DIR / "pid_tools.lsp").read_text(encoding="utf-8")
        for fn in (
            "c:setup-pid-layers",
            "c:insert-pid-block",
            "c:draw-process-line",
            "c:connect-equipment",
            "c:add-flow-arrow",
            "c:add-equipment-tag",
            "c:add-line-number",
            "c:insert-valve-on-line",
            "c:insert-instrument",
            "c:insert-pump",
            "c:insert-tank",
        ):
            assert f"(defun {fn}" in pid_text, (
                f"pid_tools.lsp is missing the `{fn}` definition that "
                "mcp_dispatch.lsp expects."
            )

    def test_dispatch_auto_loads_pid_tools(self):
        """mcp_dispatch.lsp should attempt to load pid_tools.lsp at startup
        so file_ipc users don't have to APPLOAD two files."""
        text = (_LISP_DIR / "mcp_dispatch.lsp").read_text(encoding="utf-8")
        assert "(findfile \"pid_tools.lsp\")" in text
        # And there must be a (load ...) of whatever findfile returned.
        assert "*mcp-pid-tools-path*" in text


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_ok_result(self):
        r = CommandResult(ok=True, payload={"entity": "LINE", "handle": "ABC"})
        d = r.to_dict()
        assert d["ok"] is True
        assert d["payload"]["entity"] == "LINE"
        assert "error" not in d

    def test_error_result(self):
        r = CommandResult(ok=False, error="Entity not found")
        d = r.to_dict()
        assert d["ok"] is False
        assert d["error"] == "Entity not found"
        assert "payload" not in d

    def test_default_values(self):
        r = CommandResult(ok=True)
        assert r.payload is None
        assert r.error is None
        d = r.to_dict()
        assert d["ok"] is True
        assert d["payload"] is None


# ---------------------------------------------------------------------------
# BackendCapabilities
# ---------------------------------------------------------------------------


class TestBackendCapabilities:
    def test_defaults_minimal(self):
        caps = BackendCapabilities()
        assert caps.can_create_entities is True
        assert caps.can_read_drawing is False
        assert caps.can_plot_pdf is False
        assert caps.can_undo is False

    def test_full_capabilities(self):
        caps = BackendCapabilities(
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
        for field_name, value in caps.__dict__.items():
            assert value is True, f"{field_name} should be True"


# ---------------------------------------------------------------------------
# IPC command file format
# ---------------------------------------------------------------------------


class TestIPCCommandFormat:
    """Test the JSON command file structure matches what LISP expects."""

    def test_command_file_structure(self):
        request_id = uuid.uuid4().hex[:12]
        payload = {
            "request_id": request_id,
            "command": "create-line",
            "params": {"x1": 0, "y1": 0, "x2": 100, "y2": 100, "layer": "0"},
            "ts": time.time(),
        }
        json_str = json.dumps(payload)
        parsed = json.loads(json_str)
        assert parsed["request_id"] == request_id
        assert parsed["command"] == "create-line"
        assert parsed["params"]["x1"] == 0
        assert isinstance(parsed["ts"], float)

    def test_result_file_structure(self):
        request_id = "abc123def456"
        result = {
            "request_id": request_id,
            "ok": True,
            "payload": {"entity_type": "LINE", "handle": "1A2"},
            "error": None,
            "ts": time.time(),
        }
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        assert parsed["ok"] is True
        assert parsed["payload"]["handle"] == "1A2"

    def test_error_result_file_structure(self):
        result = {
            "request_id": "abc123",
            "ok": False,
            "payload": None,
            "error": "Command 'nonexistent' not found in dispatch map",
            "ts": time.time(),
        }
        parsed = json.loads(json.dumps(result))
        assert parsed["ok"] is False
        assert "not found" in parsed["error"]


# ---------------------------------------------------------------------------
# Request ID uniqueness
# ---------------------------------------------------------------------------


class TestRequestID:
    def test_uniqueness(self):
        """Generate 1000 request IDs and verify no duplicates."""
        ids = {uuid.uuid4().hex[:12] for _ in range(1000)}
        assert len(ids) == 1000

    def test_format(self):
        rid = uuid.uuid4().hex[:12]
        assert len(rid) == 12
        assert rid.isalnum()


# ---------------------------------------------------------------------------
# Atomic write simulation
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_write_then_rename(self):
        """Verify atomic write pattern: write to .tmp, then rename."""
        with tempfile.TemporaryDirectory() as tmpdir:
            final_path = Path(tmpdir) / "autocad_mcp_cmd_test123.json"
            tmp_path = final_path.with_suffix(".tmp")

            payload = {"request_id": "test123", "command": "ping", "params": {}}
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")

            assert tmp_path.exists()
            assert not final_path.exists()

            tmp_path.rename(final_path)

            assert final_path.exists()
            assert not tmp_path.exists()

            data = json.loads(final_path.read_text(encoding="utf-8"))
            assert data["request_id"] == "test123"

    def test_concurrent_files_dont_collide(self):
        """Simulate two concurrent requests with different IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            id1 = uuid.uuid4().hex[:12]
            id2 = uuid.uuid4().hex[:12]

            f1 = Path(tmpdir) / f"autocad_mcp_cmd_{id1}.json"
            f2 = Path(tmpdir) / f"autocad_mcp_cmd_{id2}.json"

            f1.write_text(json.dumps({"request_id": id1, "command": "a"}))
            f2.write_text(json.dumps({"request_id": id2, "command": "b"}))

            d1 = json.loads(f1.read_text())
            d2 = json.loads(f2.read_text())

            assert d1["request_id"] != d2["request_id"]
            assert d1["command"] == "a"
            assert d2["command"] == "b"


# ---------------------------------------------------------------------------
# Stale file cleanup
# ---------------------------------------------------------------------------


class TestStaleCleanup:
    def test_cleanup_old_files(self):
        """Files older than threshold should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stale_threshold = 60.0

            # Create a "stale" file
            stale_file = Path(tmpdir) / "autocad_mcp_cmd_old123.json"
            stale_file.write_text('{"old": true}')
            # Backdate the file
            old_time = time.time() - stale_threshold - 10
            os.utime(stale_file, (old_time, old_time))

            # Create a "fresh" file
            fresh_file = Path(tmpdir) / "autocad_mcp_result_new456.json"
            fresh_file.write_text('{"new": true}')

            # Simulate cleanup
            now = time.time()
            for f in Path(tmpdir).glob("autocad_mcp_*.json"):
                if now - f.stat().st_mtime > stale_threshold:
                    f.unlink(missing_ok=True)

            assert not stale_file.exists()
            assert fresh_file.exists()

    def test_cleanup_tmp_files(self):
        """All .tmp files should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp1 = Path(tmpdir) / "autocad_mcp_cmd_abc.tmp"
            tmp2 = Path(tmpdir) / "autocad_mcp_result_def.tmp"
            tmp1.write_text("partial write")
            tmp2.write_text("partial write")

            for f in Path(tmpdir).glob("autocad_mcp_*.tmp"):
                f.unlink(missing_ok=True)

            assert not tmp1.exists()
            assert not tmp2.exists()


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_deadline_calculation(self):
        timeout = 10.0
        deadline = time.time() + timeout
        # Deadline should be ~10s in the future
        assert deadline > time.time()
        assert deadline - time.time() < timeout + 0.1

    def test_poll_interval_reasonable(self):
        """Verify poll interval gives ~100 checks in 10s timeout."""
        poll_interval = 0.1
        timeout = 10.0
        max_polls = timeout / poll_interval
        assert max_polls == 100


# ---------------------------------------------------------------------------
# Error hint mapping (from client.py)
# ---------------------------------------------------------------------------


class TestErrorHints:
    """Test the _error helper's hint mapping logic."""

    def _classify(self, msg: str) -> str:
        msg_lower = msg.lower()
        if "window not found" in msg_lower or "no autocad" in msg_lower:
            return "autocad_not_running"
        elif "timeout" in msg_lower:
            return "timeout"
        elif "not supported" in msg_lower or "backend" in msg_lower:
            return "unsupported"
        elif "dispatcher" in msg_lower or "mcp_dispatch" in msg_lower:
            return "dispatcher_not_loaded"
        else:
            return "unknown"

    def test_window_not_found(self):
        assert self._classify("AutoCAD window not found") == "autocad_not_running"

    def test_no_autocad(self):
        assert self._classify("No AutoCAD LT detected") == "autocad_not_running"

    def test_timeout(self):
        assert self._classify("Timeout waiting for result") == "timeout"

    def test_not_supported(self):
        assert self._classify("Not supported on this backend") == "unsupported"

    def test_dispatcher(self):
        assert self._classify("mcp_dispatch.lsp not loaded") == "dispatcher_not_loaded"

    def test_unknown(self):
        assert self._classify("Something unexpected happened") == "unknown"


# ---------------------------------------------------------------------------
# LISP command dispatch map coverage
# ---------------------------------------------------------------------------


class TestDispatchMapCoverage:
    """Verify all commands that FileIPCBackend sends are documented."""

    # These must match the command names in mcp_dispatch.lsp's command map
    EXPECTED_COMMANDS = [
        "ping",
        "execute-lisp",
        "undo",
        "redo",
        "drawing-info",
        "drawing-save",
        "drawing-save-as-dxf",
        "drawing-create",
        "drawing-purge",
        "drawing-open",
        "drawing-get-variables",
        "create-line",
        "create-circle",
        "create-polyline",
        "create-rectangle",
        "create-arc",
        "create-ellipse",
        "create-mtext",
        "create-hatch",
        "create-text",
        "entity-list",
        "entity-count",
        "entity-get",
        "entity-erase",
        "entity-copy",
        "entity-move",
        "entity-rotate",
        "entity-scale",
        "entity-mirror",
        "entity-offset",
        "entity-array",
        "entity-fillet",
        "entity-chamfer",
        "layer-list",
        "layer-create",
        "layer-set-current",
        "layer-set-properties",
        "layer-freeze",
        "layer-thaw",
        "layer-lock",
        "layer-unlock",
        "block-list",
        "block-insert",
        "block-insert-with-attributes",
        "block-get-attributes",
        "block-update-attribute",
        "block-define",
        "create-dimension-linear",
        "create-dimension-aligned",
        "create-dimension-angular",
        "create-dimension-radius",
        "create-leader",
        "zoom-extents",
        "zoom-window",
        "pid-setup-layers",
        "pid-insert-symbol",
        "pid-list-symbols",
        "pid-draw-process-line",
        "pid-connect-equipment",
        "pid-add-flow-arrow",
        "pid-add-equipment-tag",
        "pid-add-line-number",
        "pid-insert-valve",
        "pid-insert-instrument",
        "pid-insert-pump",
        "pid-insert-tank",
        "drawing-plot-pdf",
        "arch-setup-layers",
        "arch-wall",
        "arch-wall-with-openings",
        "arch-door",
        "arch-window",
        "arch-room",
        "arch-dimension-chain",
    ]

    def test_all_commands_use_hyphen_convention(self):
        for cmd in self.EXPECTED_COMMANDS:
            assert "_" not in cmd, f"Command '{cmd}' should use hyphens, not underscores"

    def test_no_duplicates(self):
        assert len(self.EXPECTED_COMMANDS) == len(set(self.EXPECTED_COMMANDS))

    def test_new_v31_commands_present(self):
        """Verify v3.1 additions are in the expected commands list."""
        for cmd in ("execute-lisp", "undo", "redo", "drawing-open"):
            assert cmd in self.EXPECTED_COMMANDS, f"Missing new command: {cmd}"

    def test_new_v32_arch_commands_present(self):
        """Verify v3.2 architectural commands are in the expected commands list."""
        for cmd in (
            "arch-setup-layers", "arch-wall", "arch-wall-with-openings",
            "arch-door", "arch-window", "arch-room", "arch-dimension-chain",
        ):
            assert cmd in self.EXPECTED_COMMANDS, f"Missing arch command: {cmd}"

    def test_lisp_file_declares_each_arch_command(self):
        """Every arch-* command in the expected list must have a matching
        ((= cmd-name "X") ...) clause in mcp_dispatch.lsp."""
        from pathlib import Path

        lisp = (
            Path(__file__).resolve().parent.parent
            / "lisp-code" / "mcp_dispatch.lsp"
        ).read_text(encoding="utf-8")
        for cmd in self.EXPECTED_COMMANDS:
            if cmd.startswith("arch-"):
                needle = f'(= cmd-name "{cmd}")'
                assert needle in lisp, f"Dispatch clause missing for {cmd!r}"


# ---------------------------------------------------------------------------
# Configurable IPC timeout
# ---------------------------------------------------------------------------


class TestConfigurableTimeout:
    def test_default_timeout(self):
        from autocad_mcp.config import IPC_TIMEOUT
        # Default should be 10.0 (unless overridden by env)
        assert isinstance(IPC_TIMEOUT, float)
        assert 1.0 <= IPC_TIMEOUT <= 300.0

    def test_timeout_used_by_file_ipc(self):
        from autocad_mcp.backends.file_ipc import TIMEOUT
        from autocad_mcp.config import IPC_TIMEOUT
        assert TIMEOUT == IPC_TIMEOUT


# ---------------------------------------------------------------------------
# Stale .lsp file cleanup
# ---------------------------------------------------------------------------


class TestStaleLispCleanup:
    def test_cleanup_stale_lisp_files(self):
        """Temp .lsp files older than threshold should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stale_threshold = 60.0

            stale_lsp = Path(tmpdir) / "autocad_mcp_lisp_old123.lsp"
            stale_lsp.write_text("(+ 1 2)")
            old_time = time.time() - stale_threshold - 10
            os.utime(stale_lsp, (old_time, old_time))

            fresh_lsp = Path(tmpdir) / "autocad_mcp_lisp_new456.lsp"
            fresh_lsp.write_text("(+ 3 4)")

            # Simulate the cleanup logic
            now = time.time()
            for pattern in ("autocad_mcp_*.json", "autocad_mcp_*.tmp", "autocad_mcp_lisp_*.lsp"):
                for f in Path(tmpdir).glob(pattern):
                    if now - f.stat().st_mtime > stale_threshold:
                        f.unlink(missing_ok=True)

            assert not stale_lsp.exists()
            assert fresh_lsp.exists()


# ---------------------------------------------------------------------------
# New backend methods — default implementations
# ---------------------------------------------------------------------------


class TestNewBackendDefaults:
    """New base class methods return 'not supported' by default."""

    @pytest.mark.asyncio
    async def test_execute_lisp_default(self):
        from autocad_mcp.backends.base import AutoCADBackend
        # Cannot instantiate ABC directly, use ezdxf backend instead
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.execute_lisp("(+ 1 2)")
        assert result.ok is False
        assert "Not supported" in result.error
        # The structured category survives wording changes; check it too.
        assert result.category == "not_supported"
        assert result.to_dict()["category"] == "not_supported"

    @pytest.mark.asyncio
    async def test_undo_default(self):
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.undo()
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_redo_default(self):
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.redo()
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_drawing_open_ezdxf(self):
        """ezdxf backend should support drawing_open for DXF files."""
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        # Save a DXF first, then open it
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
            dxf_path = f.name
        try:
            await backend.drawing_save(dxf_path)
            result = await backend.drawing_open(dxf_path)
            assert result.ok is True
            assert result.payload["path"] == dxf_path
        finally:
            Path(dxf_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Semicolon-encoded point passing (polyline / leader)
# ---------------------------------------------------------------------------


class TestSemicolonEncoding:
    def test_polyline_points_encoding(self):
        """Verify Python encodes polyline points as semicolon-delimited string."""
        points = [[0, 0], [10, 0], [10, 10], [0, 10]]
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        assert pts_str == "0,0;10,0;10,10;0,10"

    def test_leader_points_encoding(self):
        """Verify Python encodes leader points as semicolon-delimited string."""
        points = [[5, 5], [15, 15]]
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        assert pts_str == "5,5;15,15"

    def test_closed_boolean_encoding(self):
        """Closed flag encoded as string '1'/'0' for LISP compatibility."""
        assert ("1" if True else "0") == "1"
        assert ("1" if False else "0") == "0"


# ---------------------------------------------------------------------------
# Variable name $ prefix stripping
# ---------------------------------------------------------------------------


class TestVariableNameStripping:
    def test_strip_dollar_prefix(self):
        """File IPC strips $ prefix from ezdxf-style variable names."""
        names = ["$ACADVER", "DIMSCALE", "$CLAYER"]
        clean = [n.lstrip("$") for n in names]
        assert clean == ["ACADVER", "DIMSCALE", "CLAYER"]

    def test_semicolon_encoding(self):
        names = ["ACADVER", "DIMSCALE", "LTSCALE"]
        names_str = ";".join(names)
        assert names_str == "ACADVER;DIMSCALE;LTSCALE"

    def test_empty_names_list(self):
        """Empty names list results in empty string."""
        names = None
        names_str = "" if not names else ";".join(names)
        assert names_str == ""


# ---------------------------------------------------------------------------
# FileIPCBackend arch_* wiring
# ---------------------------------------------------------------------------


class TestFileIPCArchDispatch:
    """The FileIPCBackend's arch_* methods must dispatch hyphenated commands
    matching the LISP handlers. We mock `_dispatch` to capture what gets sent."""

    def _backend(self, monkeypatch):
        from autocad_mcp.backends.base import CommandResult
        from autocad_mcp.backends.file_ipc import FileIPCBackend

        b = FileIPCBackend()
        calls: list[tuple] = []

        async def fake_dispatch(cmd, params):
            calls.append((cmd, dict(params)))
            return CommandResult(ok=True, payload={"stub": True})

        monkeypatch.setattr(b, "_dispatch", fake_dispatch)
        return b, calls

    @pytest.mark.asyncio
    async def test_setup_layers_dispatches_correct_command(self, monkeypatch):
        b, calls = self._backend(monkeypatch)
        await b.arch_setup_layers()
        assert calls == [("arch-setup-layers", {})]

    @pytest.mark.asyncio
    async def test_wall_passes_params_through(self, monkeypatch):
        b, calls = self._backend(monkeypatch)
        await b.arch_wall(0, 0, 100, 50, thickness=6, layer="A-WALL", justify="left")
        cmd, params = calls[0]
        assert cmd == "arch-wall"
        assert params["x1"] == 0 and params["y2"] == 50
        assert params["thickness"] == 6
        assert params["layer"] == "A-WALL"
        assert params["justify"] == "left"

    @pytest.mark.asyncio
    async def test_door_passes_swing_through(self, monkeypatch):
        b, calls = self._backend(monkeypatch)
        await b.arch_door(5, 5, width=36, swing="right")
        cmd, params = calls[0]
        assert cmd == "arch-door"
        assert params["swing"] == "right"
        assert params["width"] == 36

    @pytest.mark.asyncio
    async def test_room_flattens_boundary_to_semicolon_string(self, monkeypatch):
        b, calls = self._backend(monkeypatch)
        await b.arch_room(
            [[0, 0], [10, 0], [10, 10], [0, 10]],
            name="OFFICE 101",
        )
        cmd, params = calls[0]
        assert cmd == "arch-room"
        # Same encoding as create_polyline / create_leader.
        assert params["points_str"] == "0,0;10,0;10,10;0,10"
        assert params["name"] == "OFFICE 101"

    @pytest.mark.asyncio
    async def test_dimension_chain_flattens_points(self, monkeypatch):
        b, calls = self._backend(monkeypatch)
        await b.arch_dimension_chain(
            [[0, 0], [40, 0], [80, 0]],
            offset=20, direction="horizontal",
        )
        cmd, params = calls[0]
        assert cmd == "arch-dimension-chain"
        assert params["points_str"] == "0,0;40,0;80,0"
        assert params["direction"] == "horizontal"
        assert params["offset"] == 20

    @pytest.mark.asyncio
    async def test_window_dispatches(self, monkeypatch):
        b, calls = self._backend(monkeypatch)
        await b.arch_window(0, 0, 30, 0, sill_thickness=6)
        cmd, params = calls[0]
        assert cmd == "arch-window"
        assert params["sill_thickness"] == 6
