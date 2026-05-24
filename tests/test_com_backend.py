"""Structural tests for the COM backend — no running AutoCAD required.

These exercise:
  * import / instantiation
  * ABC compliance (inherits AutoCADBackend)
  * capability flags
  * graceful failure when AutoCAD isn't running
  * the registry probe used by detect_backend() in auto mode

For tests that actually drive AutoCAD, see test_com_integration.py
(skipped by default; run with `pytest -m integration`).
"""

from __future__ import annotations

import os
import sys

import pytest

from autocad_mcp.backends.base import AutoCADBackend
from autocad_mcp.backends.com_backend import (
    COMBackend,
    _color_to_aci,
    _infer_category,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="COM backend is Windows-only",
)


# ---------------------------------------------------------------------------
# Class structure
# ---------------------------------------------------------------------------


class TestCOMBackendStructure:
    def test_imports_and_instantiates(self):
        b = COMBackend()
        assert isinstance(b, AutoCADBackend)

    def test_name(self):
        assert COMBackend().name == "com"

    def test_capabilities_full(self):
        caps = COMBackend().capabilities
        # Full AutoCAD via COM should advertise every capability.
        for flag in (
            "can_read_drawing",
            "can_modify_entities",
            "can_create_entities",
            "can_screenshot",
            "can_save",
            "can_plot_pdf",
            "can_zoom",
            "can_query_entities",
            "can_file_operations",
            "can_undo",
        ):
            assert getattr(caps, flag) is True, f"{flag} should be True for COM backend"

    def test_app_starts_unset(self):
        b = COMBackend()
        assert b._app is None
        assert b._screenshot_provider is None


# ---------------------------------------------------------------------------
# Graceful failure paths
# ---------------------------------------------------------------------------


class TestCOMBackendFailures:
    async def test_initialize_fails_when_no_autocad(self, monkeypatch):
        """If GetActiveObject can't find AutoCAD, initialize should return ok=False."""
        from autocad_mcp.backends import com_backend

        def _fail(progid):
            raise OSError("AutoCAD not running")

        # Patch every COM lookup to fail; initialize should give a clean error.
        monkeypatch.setattr(
            com_backend,
            "_attach_to_running_autocad",
            lambda: (_ for _ in ()).throw(RuntimeError("No running AutoCAD found")),
        )

        b = COMBackend()
        result = await b.initialize()
        assert result.ok is False
        assert "No running AutoCAD" in result.error

    async def test_status_before_init_errors(self):
        b = COMBackend()
        result = await b.status()
        assert result.ok is False
        assert "not initialized" in result.error

    async def test_drawing_info_without_init_errors(self):
        """Operations that need an active document should fail clearly."""
        b = COMBackend()
        result = await b.drawing_info()
        assert result.ok is False
        # Either "not initialized" (preferred) or any other clear error
        assert result.error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestColorMapping:
    @pytest.mark.parametrize("name,aci", [
        ("red", 1), ("yellow", 2), ("green", 3), ("cyan", 4),
        ("blue", 5), ("magenta", 6), ("white", 7), ("grey", 8), ("gray", 8),
    ])
    def test_known_colors(self, name, aci):
        assert _color_to_aci(name) == aci

    def test_int_passthrough(self):
        assert _color_to_aci(42) == 42

    def test_unknown_falls_back_to_white(self):
        assert _color_to_aci("chartreuse") == 7


class TestInferCategory:
    """The COM backend wraps inner-function `RuntimeError`s into
    CommandResults. _infer_category maps the wording back to the stable
    ErrorCategory enum so MCP clients can branch on `category` without
    doing their own substring matching."""

    @pytest.mark.parametrize("msg,expected", [
        ("Entity ABC not found", "not_found"),
        ("Block 'TITLE' not defined", "not_found"),
        ("Attribute 'EQUIP-NO' not found", "not_found"),
        ("No active document", "not_found"),
        ("Aligned dimension has zero length segment", "invalid_arg"),
        ("Fillet on COM backend supports LINE+LINE only (got X + Y)", "invalid_arg"),
        ("Neither line endpoint matches the fillet/chamfer corner", "invalid_arg"),
        ("Room boundary needs at least 3 points; got 1", "invalid_arg"),
        ("COM backend not initialized", "backend_unavailable"),
        ("totally unexpected wording from a future error site", "internal_error"),
    ])
    def test_category_inference(self, msg, expected):
        assert _infer_category(msg) == expected


# ---------------------------------------------------------------------------
# detect_backend integration
# ---------------------------------------------------------------------------


class TestDetectBackend:
    def test_explicit_com_returns_com(self, monkeypatch):
        from autocad_mcp.config import detect_backend

        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "com")
        # WIN32_AVAILABLE is read at import time, so on win32 systems this works.
        assert detect_backend() == "com"

    def test_explicit_ezdxf_returns_ezdxf(self, monkeypatch):
        from autocad_mcp.config import detect_backend

        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
        assert detect_backend() == "ezdxf"

    def test_auto_prefers_com_when_full_autocad_registered(self, monkeypatch):
        """In auto mode, having full AutoCAD in the registry should select COM."""
        import autocad_mcp.config as config_mod

        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        monkeypatch.setattr(config_mod, "_running_autocad_full_version", lambda: "R26.0")
        assert config_mod.detect_backend() == "com"

    def test_auto_falls_back_to_file_ipc_for_lt_only(self, monkeypatch):
        """If only LT is installed but its window is open, file_ipc wins."""
        import autocad_mcp.config as config_mod

        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        monkeypatch.setattr(config_mod, "_running_autocad_full_version", lambda: None)
        # Stub find_autocad_window to return a fake hwnd
        from autocad_mcp.backends import file_ipc as fipc_mod

        monkeypatch.setattr(fipc_mod, "find_autocad_window", lambda: 12345)
        assert config_mod.detect_backend() == "file_ipc"

    def test_auto_falls_back_to_ezdxf_when_nothing_running(self, monkeypatch):
        import autocad_mcp.config as config_mod

        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        monkeypatch.setattr(config_mod, "_running_autocad_full_version", lambda: None)
        from autocad_mcp.backends import file_ipc as fipc_mod

        monkeypatch.setattr(fipc_mod, "find_autocad_window", lambda: None)
        assert config_mod.detect_backend() == "ezdxf"


# ---------------------------------------------------------------------------
# Live registry probe (machine-specific; skip if no AutoCAD installed)
# ---------------------------------------------------------------------------


class TestRegistryProbe:
    def test_probe_returns_string_or_none(self):
        from autocad_mcp.config import _running_autocad_full_version

        result = _running_autocad_full_version()
        # Either None or a string like "R26.0"
        assert result is None or isinstance(result, str)
        if result is not None:
            assert result.startswith("R")
