"""Tests for the procedural symbol library."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.arch.library import (
    SYMBOL_LIBRARY,
    apply_to_ezdxf_block,
    symbol_names,
)
from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


# ---------------------------------------------------------------------------
# Library data
# ---------------------------------------------------------------------------


class TestLibraryData:
    def test_has_starter_symbols(self):
        names = symbol_names()
        # The 5 starter symbols must all be present.
        for required in (
            "ARCH-NORTH-ARROW",
            "ARCH-SCALE-BAR-10FT",
            "PLMB-TOILET",
            "PLMB-SINK",
            "ELEC-OUTLET-DUPLEX",
        ):
            assert required in names, f"Missing starter symbol: {required}"

    def test_names_use_discipline_prefix(self):
        """Names follow `<DISCIPLINE>-<KIND>` so they group cleanly by sort."""
        for name in symbol_names():
            assert "-" in name, f"Symbol name has no discipline separator: {name}"
            assert name.split("-")[0] in {"ARCH", "PLMB", "ELEC", "FURN", "STRUC", "MECH", "APPL"}

    def test_every_symbol_has_at_least_one_entity(self):
        for name, spec in SYMBOL_LIBRARY.items():
            assert len(spec) >= 1, f"Empty symbol spec: {name}"


# ---------------------------------------------------------------------------
# apply_to_ezdxf_block
# ---------------------------------------------------------------------------


class TestApplyToEzdxf:
    def test_applies_every_library_symbol_cleanly(self):
        doc = ezdxf.new("R2013", setup=True)
        for name in symbol_names():
            block = doc.blocks.new(name=name)
            apply_to_ezdxf_block(block, SYMBOL_LIBRARY[name])
            # Entity count in the block must match the spec count.
            assert sum(1 for _ in block) == len(SYMBOL_LIBRARY[name])

    def test_unknown_spec_type_raises(self):
        doc = ezdxf.new("R2013", setup=True)
        block = doc.blocks.new(name="UNKNOWN-TEST")
        with pytest.raises(ValueError, match="Unknown library entity spec"):
            apply_to_ezdxf_block(block, [{"type": "spline"}])

    def test_block_entities_on_layer_zero(self):
        """Block-internal entities should be on layer 0 so the insert
        controls coloring."""
        doc = ezdxf.new("R2013", setup=True)
        block = doc.blocks.new(name="LAYER-TEST")
        apply_to_ezdxf_block(block, SYMBOL_LIBRARY["PLMB-SINK"])
        for ent in block:
            assert ent.dxf.layer == "0"


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@pytest.fixture
async def backend():
    b = EzdxfBackend()
    result = await b.initialize()
    assert result.ok
    return b


class TestEzdxfLibraryBackend:
    async def test_library_list(self, backend):
        r = await backend.library_list()
        assert r.ok
        assert r.payload["count"] == len(symbol_names())
        assert "ARCH-NORTH-ARROW" in r.payload["symbols"]

    async def test_library_define(self, backend):
        r = await backend.library_define("PLMB-TOILET")
        assert r.ok
        assert r.payload["name"] == "PLMB-TOILET"
        assert r.payload["entity_count"] > 0
        # Block actually exists in the doc.
        assert "PLMB-TOILET" in backend._doc.blocks

    async def test_library_define_idempotent(self, backend):
        await backend.library_define("ELEC-OUTLET-DUPLEX")
        r = await backend.library_define("ELEC-OUTLET-DUPLEX")
        assert r.ok
        assert r.payload.get("existed") is True

    async def test_library_define_rejects_unknown(self, backend):
        r = await backend.library_define("NOT-A-REAL-SYMBOL")
        assert r.ok is False
        assert "NOT-A-REAL-SYMBOL" in r.error

    async def test_library_define_all(self, backend):
        r = await backend.library_define_all()
        assert r.ok
        assert r.payload["total"] == len(symbol_names())
        assert r.payload["errors"] == []

    async def test_library_insert_defines_on_demand(self, backend):
        """library_insert should auto-define the block, then insert it —
        callers shouldn't have to chain define + insert manually."""
        assert "ARCH-NORTH-ARROW" not in backend._doc.blocks
        r = await backend.library_insert("ARCH-NORTH-ARROW", 50, 50, scale=2.0, rotation=45.0)
        assert r.ok
        # Block now exists.
        assert "ARCH-NORTH-ARROW" in backend._doc.blocks
        # An INSERT entity is in modelspace.
        inserts = [e for e in backend._msp if e.dxftype() == "INSERT"]
        assert len(inserts) == 1
        assert inserts[0].dxf.name == "ARCH-NORTH-ARROW"


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestLibraryToolDispatch:
    async def test_list_via_tool(self, backend):
        from autocad_mcp.server import library

        fn = library.fn if hasattr(library, "fn") else library
        result = await fn(operation="list")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert "ARCH-NORTH-ARROW" in parsed["payload"]["symbols"]

    async def test_insert_via_tool(self, backend):
        from autocad_mcp.server import library

        fn = library.fn if hasattr(library, "fn") else library
        result = await fn(operation="insert", data={
            "name": "PLMB-SINK", "x": 100, "y": 100,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True

    async def test_unknown_operation_errors(self, backend):
        from autocad_mcp.server import library

        fn = library.fn if hasattr(library, "fn") else library
        result = await fn(operation="bogus")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed
        assert "Unknown library operation" in parsed["error"]

    async def test_missing_name_errors(self, backend):
        from autocad_mcp.server import library

        fn = library.fn if hasattr(library, "fn") else library
        result = await fn(operation="define")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed
        assert "name" in parsed["error"]
