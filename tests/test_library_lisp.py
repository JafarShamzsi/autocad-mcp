"""Tests for the LISP-targeted serializer + file_ipc library_* dispatch."""

from __future__ import annotations

import pytest

from autocad_mcp.arch.library import SYMBOL_LIBRARY, symbol_names
from autocad_mcp.arch.library.apply import _fmt, serialize_spec_for_lisp
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


class TestFmt:
    def test_strips_trailing_zeros(self):
        assert _fmt(5.0) == "5"
        assert _fmt(5.500) == "5.5"

    def test_rounds_tiny_near_zero_to_zero(self):
        # cos(pi/2) emits ~6e-17 — must not produce scientific notation
        # in the output (atof reads it correctly but logs become noisy).
        import math
        assert _fmt(math.cos(math.pi / 2) * 7.0) == "0"

    def test_negative(self):
        assert _fmt(-3.14) == "-3.14"

    def test_no_decimal(self):
        assert _fmt(0.0) == "0"


class TestSerializer:
    def test_line(self):
        spec = [{"type": "line", "start": (0.0, 0.0), "end": (10.0, 5.0)}]
        assert serialize_spec_for_lisp(spec) == "L,0,0,10,5"

    def test_circle(self):
        spec = [{"type": "circle", "center": (1.0, 2.0), "radius": 3.5}]
        assert serialize_spec_for_lisp(spec) == "C,1,2,3.5"

    def test_arc(self):
        spec = [{
            "type": "arc", "center": (0.0, 0.0), "radius": 5.0,
            "start_angle": 0.0, "end_angle": 90.0,
        }]
        assert serialize_spec_for_lisp(spec) == "A,0,0,5,0,90"

    def test_polyline_closed(self):
        spec = [{
            "type": "polyline", "closed": True,
            "points": [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)],
        }]
        assert serialize_spec_for_lisp(spec) == "P,1,3,0,0,10,0,10,5"

    def test_polyline_open(self):
        spec = [{
            "type": "polyline", "closed": False,
            "points": [(0.0, 0.0), (10.0, 0.0)],
        }]
        assert serialize_spec_for_lisp(spec) == "P,0,2,0,0,10,0"

    def test_polyline_closed_default_false(self):
        spec = [{
            "type": "polyline",
            "points": [(0.0, 0.0), (10.0, 0.0)],
        }]
        assert serialize_spec_for_lisp(spec).startswith("P,0,2,")

    def test_text(self):
        spec = [{
            "type": "text", "insert": (1.0, 2.0), "text": "N", "height": 2.5,
        }]
        assert serialize_spec_for_lisp(spec) == "T,1,2,2.5,N"

    def test_text_default_height(self):
        spec = [{"type": "text", "insert": (0.0, 0.0), "text": "X"}]
        assert serialize_spec_for_lisp(spec) == "T,0,0,2.5,X"

    def test_text_rejects_comma(self):
        spec = [{"type": "text", "insert": (0.0, 0.0), "text": "a,b", "height": 1.0}]
        with pytest.raises(ValueError, match="reserved delimiter"):
            serialize_spec_for_lisp(spec)

    def test_text_rejects_semicolon(self):
        spec = [{"type": "text", "insert": (0.0, 0.0), "text": "a;b", "height": 1.0}]
        with pytest.raises(ValueError, match="reserved delimiter"):
            serialize_spec_for_lisp(spec)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown library entity spec type"):
            serialize_spec_for_lisp([{"type": "ellipse"}])

    def test_multiple_entities_joined_with_semicolon(self):
        spec = [
            {"type": "circle", "center": (0.0, 0.0), "radius": 5.0},
            {"type": "line", "start": (-5.0, 0.0), "end": (5.0, 0.0)},
        ]
        assert serialize_spec_for_lisp(spec) == "C,0,0,5;L,-5,0,5,0"

    def test_every_real_symbol_serializes(self):
        """Round-trip every shipping symbol — catches typed errors in symbols.py."""
        for name in symbol_names():
            s = serialize_spec_for_lisp(SYMBOL_LIBRARY[name])
            assert s, f"empty serialization for {name}"
            assert s[0] in "LCAPT", (
                f"first entity in {name} starts with unknown type: {s[:30]!r}"
            )
            # No scientific notation should sneak through _fmt.
            assert "e+" not in s and "e-" not in s, (
                f"{name} contains scientific notation: {s!r}"
            )


# ---------------------------------------------------------------------------
# FileIPCBackend.library_* dispatch
# ---------------------------------------------------------------------------


class TestFileIpcLibrary:
    @pytest.mark.asyncio
    async def test_library_list_does_not_dispatch(self, monkeypatch):
        backend = FileIPCBackend()
        called = False

        async def fail_dispatch(self, command, params):  # noqa: ARG001
            nonlocal called
            called = True
            raise AssertionError("should not dispatch")

        monkeypatch.setattr(FileIPCBackend, "_dispatch", fail_dispatch)
        r = await backend.library_list()
        assert r.ok
        assert not called
        assert set(r.payload["symbols"]) == set(symbol_names())
        assert r.payload["count"] == len(symbol_names())

    @pytest.mark.asyncio
    async def test_library_define_unknown_symbol(self):
        backend = FileIPCBackend()
        r = await backend.library_define("NOT-A-REAL-SYMBOL")
        assert not r.ok
        assert r.category == "not_found"

    @pytest.mark.asyncio
    async def test_library_define_dispatch_shape(self, monkeypatch):
        backend = FileIPCBackend()
        captured: dict = {}

        async def fake_dispatch(self, command, params):  # noqa: ARG001
            captured["command"] = command
            captured["params"] = params
            return CommandResult(
                ok=True,
                payload={
                    "name": params["name"],
                    "existed": False,
                    "entity_count": 3,
                },
            )

        monkeypatch.setattr(FileIPCBackend, "_dispatch", fake_dispatch)
        r = await backend.library_define("PLMB-TOILET")
        assert r.ok
        assert captured["command"] == "library-define"
        assert captured["params"]["name"] == "PLMB-TOILET"
        assert "entities_str" in captured["params"]
        s = captured["params"]["entities_str"]
        # Toilet has 2 polylines; first entity is the tank rectangle (P,1,4,...).
        assert s.startswith("P,1,4,")
        assert ";" in s  # multiple entities joined

    @pytest.mark.asyncio
    async def test_library_define_all_iterates(self, monkeypatch):
        backend = FileIPCBackend()
        calls: list[str] = []

        async def fake_dispatch(self, command, params):  # noqa: ARG001
            calls.append(params["name"])
            return CommandResult(
                ok=True,
                payload={"name": params["name"], "existed": False, "entity_count": 1},
            )

        monkeypatch.setattr(FileIPCBackend, "_dispatch", fake_dispatch)
        r = await backend.library_define_all()
        assert r.ok
        assert set(calls) == set(symbol_names())
        assert set(r.payload["defined"]) == set(symbol_names())
        assert r.payload["existed"] == []
        assert r.payload["errors"] == []
        assert r.payload["total"] == len(symbol_names())

    @pytest.mark.asyncio
    async def test_library_define_all_aggregates_errors(self, monkeypatch):
        backend = FileIPCBackend()

        async def fake_dispatch(self, command, params):  # noqa: ARG001
            if params["name"] == "PLMB-TOILET":
                return CommandResult(
                    ok=False, error="boom", category="internal_error",
                )
            return CommandResult(
                ok=True,
                payload={"name": params["name"], "existed": False, "entity_count": 1},
            )

        monkeypatch.setattr(FileIPCBackend, "_dispatch", fake_dispatch)
        r = await backend.library_define_all()
        assert r.ok  # aggregate is still ok even with per-symbol failures
        names_with_errors = {e["name"] for e in r.payload["errors"]}
        assert names_with_errors == {"PLMB-TOILET"}
        assert "PLMB-TOILET" not in r.payload["defined"]

    @pytest.mark.asyncio
    async def test_library_insert_calls_define_then_block_insert(self, monkeypatch):
        backend = FileIPCBackend()
        calls: list[tuple[str, dict]] = []

        async def fake_dispatch(self, command, params):  # noqa: ARG001
            calls.append((command, params))
            if command == "library-define":
                return CommandResult(
                    ok=True,
                    payload={"name": params["name"], "existed": False, "entity_count": 3},
                )
            return CommandResult(
                ok=True, payload={"entity_type": "INSERT", "handle": "abc"},
            )

        monkeypatch.setattr(FileIPCBackend, "_dispatch", fake_dispatch)
        r = await backend.library_insert(
            "PLMB-SINK", 10.0, 20.0, scale=2.0, rotation=45.0,
        )
        assert r.ok
        assert [c[0] for c in calls] == ["library-define", "block-insert"]
        insert_params = calls[1][1]
        assert insert_params["name"] == "PLMB-SINK"
        assert insert_params["x"] == 10.0
        assert insert_params["y"] == 20.0
        assert insert_params["scale"] == 2.0
        assert insert_params["rotation"] == 45.0

    @pytest.mark.asyncio
    async def test_library_insert_propagates_define_failure(self, monkeypatch):
        backend = FileIPCBackend()

        async def fake_dispatch(self, command, params):  # noqa: ARG001
            return CommandResult(
                ok=False, error="library_tools.lsp not loaded",
                category="not_supported",
            )

        monkeypatch.setattr(FileIPCBackend, "_dispatch", fake_dispatch)
        r = await backend.library_insert("PLMB-SINK", 0.0, 0.0)
        assert not r.ok
        assert "not loaded" in r.error


# ---------------------------------------------------------------------------
# LISP file presence + grep-level structure
# ---------------------------------------------------------------------------


class TestLibraryToolsLisp:
    """Static checks against the LISP source — verifies the file ships and
    declares the symbols the dispatcher expects."""

    def test_library_tools_lsp_ships(self):
        from autocad_mcp.config import LISP_DIR
        path = LISP_DIR / "library_tools.lsp"
        assert path.exists(), f"library_tools.lsp missing from {LISP_DIR}"

    def test_emits_loaded_flag(self):
        from autocad_mcp.config import LISP_DIR
        src = (LISP_DIR / "library_tools.lsp").read_text(encoding="utf-8")
        assert "*mcp-library-loaded*" in src, (
            "library_tools.lsp must set *mcp-library-loaded* so the "
            "dispatcher can detect a missing load"
        )

    def test_defines_all_emit_helpers(self):
        from autocad_mcp.config import LISP_DIR
        src = (LISP_DIR / "library_tools.lsp").read_text(encoding="utf-8")
        for fn in (
            "mcp-lib-emit-line",
            "mcp-lib-emit-circle",
            "mcp-lib-emit-arc",
            "mcp-lib-emit-polyline",
            "mcp-lib-emit-text",
            "mcp-lib-emit-entities",
        ):
            assert f"(defun {fn}" in src, f"library_tools.lsp missing {fn!r}"

    def test_dispatcher_references_handler(self):
        from autocad_mcp.config import LISP_DIR
        src = (LISP_DIR / "mcp_dispatch.lsp").read_text(encoding="utf-8")
        assert "(mcp-cmd-library-define" in src, (
            "mcp_dispatch.lsp must dispatch library-define to a handler"
        )
        assert "library-define" in src
        assert "library_tools.lsp" in src, (
            "mcp_dispatch.lsp must auto-load library_tools.lsp via findfile"
        )
