"""Tests for the file_ipc arch.* dispatch shape — no live AutoCAD needed.

Monkeypatches `_dispatch` to capture the (command, params) the backend
would send to LISP, and asserts the spec strings are well-formed."""

from __future__ import annotations

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


@pytest.fixture
def captured_dispatch(monkeypatch):
    """Replace FileIPCBackend._dispatch with a capturer that returns ok."""
    captured: dict = {}

    async def fake_dispatch(self, command, params):  # noqa: ARG001
        captured["command"] = command
        captured["params"] = params
        return CommandResult(
            ok=True,
            payload={"handles": [], "count": 0},
        )

    monkeypatch.setattr(FileIPCBackend, "_dispatch", fake_dispatch)
    return captured


@pytest.mark.asyncio
class TestFileIpcArchEmit:
    async def test_stair_straight_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_stair(0, 0, 96, 0, width=36, num_treads=12)
        assert r.ok
        assert captured_dispatch["command"] == "arch-emit"
        params = captured_dispatch["params"]
        assert params["layer"] == "A-FLOR-STRS"
        assert params["entities"].startswith("@A-FLOR-STRS@P,1,4,")

    async def test_stair_u_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_stair(0, 0, 96, 0, width=36, num_treads=10,
                                shape="u", landing_depth=40)
        assert r.ok
        assert captured_dispatch["command"] == "arch-emit"
        # Two flights of treads + landing — many @A-FLOR-STRS@ tokens
        assert captured_dispatch["params"]["entities"].count("@A-FLOR-STRS@") >= 5

    async def test_stair_invalid_arg(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_stair(0, 0, 0, 0, width=36, num_treads=12)
        # Validation rejects before dispatching
        assert not r.ok
        assert r.category == "invalid_arg"
        assert "command" not in captured_dispatch  # no dispatch occurred

    async def test_column_round_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_column(0, 0, shape="round", size=12.0)
        assert r.ok
        assert captured_dispatch["params"]["entities"].startswith("@A-COLS@C,")

    async def test_column_square_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_column(0, 0, shape="square", size=12.0)
        assert r.ok
        assert captured_dispatch["params"]["entities"].startswith("@A-COLS@P,1,4,")

    async def test_slab_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_slab(
            [[0, 0], [100, 0], [100, 80], [0, 80]], edge_thickness=4,
        )
        assert r.ok
        s = captured_dispatch["params"]["entities"]
        assert s.count("@A-FLOR@P,") == 2  # outer + inner

    async def test_beam_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_beam(0, 0, 100, 0, width=4)
        assert r.ok
        s = captured_dispatch["params"]["entities"]
        assert s.count("@A-WALL-HEAD@L,") == 2

    async def test_roof_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_roof(
            [[0, 0], [120, 0], [120, 80], [0, 80]], style="hip", overhang=12,
        )
        assert r.ok
        s = captured_dispatch["params"]["entities"]
        assert "@A-ROOF@P," in s  # eaves polyline
        assert s.count("@A-ROOF@L,") == 5  # 1 ridge + 4 hips

    async def test_elevation_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_elevation(
            0, 0, 120, 96,
            openings=[{"position": 60, "width": 36, "height": 48,
                       "sill": 30, "type": "window"}],
        )
        assert r.ok
        s = captured_dispatch["params"]["entities"]
        assert "@A-WALL@" in s
        assert "@A-GLAZ@" in s

    async def test_section_dispatch(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_section(
            0, 0, 240, 0, 96,
            elements=[
                {"type": "wall", "x": 0, "thickness": 6},
                {"type": "floor_slab", "thickness": 6},
            ],
        )
        assert r.ok
        s = captured_dispatch["params"]["entities"]
        assert "@A-WALL@" in s
        assert "@A-FLOR@" in s

    async def test_template_dispatch_payload(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_template("residential_1br", 0, 0)
        assert r.ok
        s = captured_dispatch["params"]["entities"]
        # Multi-layer building — must contain several distinct layer tags
        assert "@A-WALL@" in s
        assert "@A-AREA@" in s

    async def test_template_unknown(self, captured_dispatch):
        b = FileIPCBackend()
        r = await b.arch_template("not_a_template", 0, 0)
        assert not r.ok
        assert r.category == "not_found"
        # No dispatch
        assert "command" not in captured_dispatch


class TestArchToolsLisp:
    """Static checks against the LISP source — verifies the file ships and
    declares the symbols the dispatcher expects."""

    def test_arch_tools_lsp_ships(self):
        from autocad_mcp.config import LISP_DIR
        path = LISP_DIR / "arch_tools.lsp"
        assert path.exists(), f"arch_tools.lsp missing from {LISP_DIR}"

    def test_emits_loaded_flag(self):
        from autocad_mcp.config import LISP_DIR
        src = (LISP_DIR / "arch_tools.lsp").read_text(encoding="utf-8")
        assert "*mcp-arch-tools-loaded*" in src, (
            "arch_tools.lsp must set *mcp-arch-tools-loaded* so the dispatcher "
            "can detect a missing load"
        )

    def test_defines_all_emit_helpers(self):
        from autocad_mcp.config import LISP_DIR
        src = (LISP_DIR / "arch_tools.lsp").read_text(encoding="utf-8")
        for fn in (
            "mcp-arch-extract-layer",
            "mcp-arch-emit-line",
            "mcp-arch-emit-circle",
            "mcp-arch-emit-arc",
            "mcp-arch-emit-polyline",
            "mcp-arch-emit-text",
            "mcp-arch-emit-entities",
            "mcp-cmd-arch-emit",
        ):
            assert f"(defun {fn}" in src, f"arch_tools.lsp missing {fn!r}"

    def test_dispatcher_references_handler(self):
        from autocad_mcp.config import LISP_DIR
        src = (LISP_DIR / "mcp_dispatch.lsp").read_text(encoding="utf-8")
        assert "arch-emit" in src
        assert "arch_tools.lsp" in src, (
            "mcp_dispatch.lsp must auto-load arch_tools.lsp via findfile"
        )
