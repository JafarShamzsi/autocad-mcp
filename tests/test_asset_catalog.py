"""Tests for the structured asset catalog (`autocad_mcp.library.catalog`).

Covers:
  * `meta.toml` parsing — required + optional fields, defaults
  * `library.toml` config — present and absent cases
  * `AssetCatalog.reindex` — atomic rebuild, error capture
  * `AssetCatalog.search` — query, category, discipline, tag, max_width
  * `AssetCatalog.get` — found / not-found
  * `library.seed.seed_starter_assets` — produces a working catalog
  * `library.insert.insert_asset` — composes import + insert correctly
  * The MCP tool `library(operation="catalog_*")` dispatch surface
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.library.catalog import (
    Asset,
    AssetCatalog,
    LibraryConfig,
    load_library_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_block_dxf(path: Path, block_name: str) -> None:
    """Write a one-block DXF for an asset directory."""
    doc = ezdxf.new("R2013")
    blk = doc.blocks.new(name=block_name)
    blk.add_line((0, 0), (10, 0))
    blk.add_circle((5, 0), 1.0)
    doc.saveas(path)


def _make_asset(root: Path, rel_path: str, block_name: str, **extras) -> Path:
    """Create a complete asset directory: meta.toml + block.dxf.
    Returns the asset directory path."""
    asset_dir = root / rel_path
    asset_dir.mkdir(parents=True, exist_ok=True)
    _make_block_dxf(asset_dir / "block.dxf", block_name)

    name = extras.get("name", rel_path.rsplit("/", 1)[-1])
    category = extras.get("category", rel_path.rsplit("/", 1)[0])
    tags = extras.get("tags", [])
    discipline = extras.get("discipline")
    default_layer = extras.get("default_layer", "0")
    width = extras.get("width")
    depth = extras.get("depth")
    height = extras.get("height")
    license_ = extras.get("license", "CC0-1.0")

    lines = [
        f'name = "{name}"',
        f'category = "{category}"',
        f'tags = [' + ", ".join(f'"{t}"' for t in tags) + ']',
        f'default_layer = "{default_layer}"',
        f'license = "{license_}"',
        f'block_name = "{block_name}"',
    ]
    if discipline:
        lines.append(f'discipline = "{discipline}"')
    if any(v is not None for v in (width, depth, height)):
        lines.append("")
        lines.append("[size]")
        if width is not None:
            lines.append(f"width = {width}")
        if depth is not None:
            lines.append(f"depth = {depth}")
        if height is not None:
            lines.append(f"height = {height}")
        lines.append('units = "inches"')
    (asset_dir / "meta.toml").write_text("\n".join(lines), encoding="utf-8")
    return asset_dir


# ---------------------------------------------------------------------------
# library.toml config
# ---------------------------------------------------------------------------


def test_load_library_config_missing_returns_defaults(tmp_path):
    cfg = load_library_config(tmp_path)
    assert cfg.root == tmp_path
    assert cfg.name == "asset-library"
    assert cfg.version == "0.1.0"


def test_load_library_config_reads_toml(tmp_path):
    (tmp_path / "library.toml").write_text(
        '[library]\nname = "test-lib"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    cfg = load_library_config(tmp_path)
    assert cfg.name == "test-lib"
    assert cfg.version == "1.2.3"


# ---------------------------------------------------------------------------
# Catalog reindex
# ---------------------------------------------------------------------------


def test_reindex_indexes_three_assets(tmp_path):
    _make_asset(tmp_path, "furniture/seating/sofa-test", "SOFA-TEST",
                width=84, depth=36, tags=["sofa", "modern"], discipline="I")
    _make_asset(tmp_path, "fixtures/plumbing/toilet-test", "TOILET-TEST",
                width=20, depth=28, tags=["toilet", "bathroom"], discipline="P",
                default_layer="P-FIXT")
    _make_asset(tmp_path, "annotation/orientation/north-test", "NORTH-TEST",
                tags=["north", "annotation"], discipline="A")

    cat = AssetCatalog(tmp_path)
    result = cat.reindex()
    assert result["indexed"] == 3
    assert result["errors"] == []


def test_reindex_skips_asset_missing_block_dxf(tmp_path):
    # Create a meta.toml WITHOUT a block.dxf.
    bad = tmp_path / "broken/asset"
    bad.mkdir(parents=True)
    (bad / "meta.toml").write_text(
        'name = "broken"\ncategory = "broken"\n', encoding="utf-8",
    )
    cat = AssetCatalog(tmp_path)
    result = cat.reindex()
    assert result["indexed"] == 0
    assert len(result["errors"]) == 1
    assert "block.dxf" in result["errors"][0]["error"]


def test_reindex_skips_unreadable_meta(tmp_path):
    asset = tmp_path / "broken/junk-meta"
    asset.mkdir(parents=True)
    _make_block_dxf(asset / "block.dxf", "JUNK-META")
    (asset / "meta.toml").write_text("this is not valid TOML !!!", encoding="utf-8")
    cat = AssetCatalog(tmp_path)
    result = cat.reindex()
    # The walk skips the unparseable file silently; nothing indexed.
    assert result["indexed"] == 0


def test_reindex_is_atomic(tmp_path):
    """Calling reindex twice must end up with the same row count, not
    duplicates."""
    _make_asset(tmp_path, "x/a", "A", tags=["a"])
    _make_asset(tmp_path, "x/b", "B", tags=["b"])
    cat = AssetCatalog(tmp_path)
    r1 = cat.reindex()
    r2 = cat.reindex()
    assert r1["indexed"] == r2["indexed"] == 2
    assert cat.stats()["total_assets"] == 2


# ---------------------------------------------------------------------------
# Catalog search
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_catalog(tmp_path):
    _make_asset(tmp_path, "furniture/seating/sofa-modern", "SOFA-MODERN",
                width=84, depth=36, tags=["sofa", "modern", "mid-century"],
                discipline="I", default_layer="I-FURN")
    _make_asset(tmp_path, "furniture/seating/chair-dining", "CHAIR-DINING",
                width=18, depth=18, tags=["chair", "dining"],
                discipline="I", default_layer="I-FURN")
    _make_asset(tmp_path, "fixtures/plumbing/toilet-std", "TOILET-STD",
                width=20, depth=28, tags=["toilet", "bathroom"],
                discipline="P", default_layer="P-FIXT")
    _make_asset(tmp_path, "fixtures/plumbing/tub-60", "TUB-60",
                width=60, depth=30, tags=["tub", "bathtub"],
                discipline="P", default_layer="P-FIXT")
    _make_asset(tmp_path, "doors/single-36", "DOOR-36",
                width=36, tags=["door", "single"], discipline="A",
                default_layer="A-DOOR")
    cat = AssetCatalog(tmp_path)
    cat.reindex()
    return cat


def test_search_by_query(seeded_catalog):
    hits = seeded_catalog.search(query="sofa")
    assert len(hits) == 1
    assert hits[0].id == "furniture/seating/sofa-modern"


def test_search_by_tag(seeded_catalog):
    hits = seeded_catalog.search(tag="mid-century")
    assert len(hits) == 1
    assert hits[0].id == "furniture/seating/sofa-modern"


def test_search_by_category_exact(seeded_catalog):
    hits = seeded_catalog.search(category="fixtures/plumbing")
    ids = {h.id for h in hits}
    assert ids == {
        "fixtures/plumbing/toilet-std",
        "fixtures/plumbing/tub-60",
    }


def test_search_by_category_parent_matches_children(seeded_catalog):
    """`category="furniture"` should match both
    furniture/seating/* and any future furniture/<sub>/* hits."""
    hits = seeded_catalog.search(category="furniture")
    ids = {h.id for h in hits}
    assert "furniture/seating/sofa-modern" in ids
    assert "furniture/seating/chair-dining" in ids


def test_search_by_discipline(seeded_catalog):
    hits = seeded_catalog.search(discipline="P")
    ids = {h.id for h in hits}
    assert ids == {
        "fixtures/plumbing/toilet-std",
        "fixtures/plumbing/tub-60",
    }


def test_search_max_width_filter(seeded_catalog):
    hits = seeded_catalog.search(max_width=24)
    # Only items 24" wide or narrower. Toilet (20), chair (18) qualify;
    # sofa (84), tub (60), door (36) do not.
    ids = {h.id for h in hits}
    assert ids == {
        "fixtures/plumbing/toilet-std",
        "furniture/seating/chair-dining",
    }


def test_search_combines_filters(seeded_catalog):
    hits = seeded_catalog.search(
        category="furniture", max_width=20,
    )
    assert len(hits) == 1
    assert hits[0].id == "furniture/seating/chair-dining"


def test_search_returns_empty_on_miss(seeded_catalog):
    assert seeded_catalog.search(query="nonexistent-thing") == []


# ---------------------------------------------------------------------------
# Catalog get
# ---------------------------------------------------------------------------


def test_get_returns_full_asset(seeded_catalog):
    a = seeded_catalog.get("furniture/seating/sofa-modern")
    assert a is not None
    assert a.name == "sofa-modern"
    assert a.width == 84
    assert a.depth == 36
    assert "mid-century" in a.tags
    assert a.default_layer == "I-FURN"
    assert a.block_file is not None
    assert a.block_file.exists()


def test_get_returns_none_when_missing(seeded_catalog):
    assert seeded_catalog.get("nope/nope/nope") is None


# ---------------------------------------------------------------------------
# Stats + list_categories
# ---------------------------------------------------------------------------


def test_list_categories_returns_counts(seeded_catalog):
    cats = seeded_catalog.list_categories()
    by_name = {c["category"]: c["count"] for c in cats}
    assert by_name["furniture/seating"] == 2
    assert by_name["fixtures/plumbing"] == 2
    assert by_name["doors"] == 1


def test_stats_summarizes_catalog(seeded_catalog):
    s = seeded_catalog.stats()
    assert s["total_assets"] == 5
    assert s["category_count"] == 3
    assert s["last_indexed"] is not None


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


def test_seed_starter_assets_produces_22_assets(tmp_path):
    from autocad_mcp.library.seed import seed_starter_assets

    # dwg=False skips the slow accoreconsole conversion step for tests
    # that only care about the DXF + meta.toml side.
    result = seed_starter_assets(tmp_path, dwg=False)
    assert len(result["written"]) == 22
    assert result["skipped"] == []
    # library.toml shipped at the root.
    assert (tmp_path / "library.toml").exists()
    # Spot-check a few well-known assets.
    assert (tmp_path / "furniture/seating/sofa-84-modern/meta.toml").exists()
    assert (tmp_path / "furniture/seating/sofa-84-modern/block.dxf").exists()
    assert (tmp_path / "fixtures/plumbing/toilet-standard/meta.toml").exists()


def test_seed_is_idempotent(tmp_path):
    """Re-running the seeder must produce the same files (no diff)."""
    from autocad_mcp.library.seed import seed_starter_assets

    seed_starter_assets(tmp_path, dwg=False)
    snapshot_a = (
        tmp_path / "furniture/seating/sofa-84-modern/meta.toml"
    ).read_text(encoding="utf-8")
    seed_starter_assets(tmp_path, dwg=False)
    snapshot_b = (
        tmp_path / "furniture/seating/sofa-84-modern/meta.toml"
    ).read_text(encoding="utf-8")
    assert snapshot_a == snapshot_b


# ---------------------------------------------------------------------------
# Post-audit fixes
# ---------------------------------------------------------------------------


def test_tag_search_uses_word_boundary(tmp_path):
    """Audit GAP-2: searching `tag="mode"` must NOT match assets tagged
    "modern". Tags are atomic keywords; substring matches inside one tag
    are false positives."""
    _make_asset(
        tmp_path, "furniture/seating/modern-sofa", "MODERN-SOFA",
        tags=["sofa", "modern"],
    )
    _make_asset(
        tmp_path, "furniture/seating/plain-sofa", "PLAIN-SOFA",
        tags=["sofa", "mode"],   # synthetic — would have collided pre-fix
    )

    cat = AssetCatalog(tmp_path)
    cat.reindex()
    # `tag="mode"` only hits the asset literally tagged "mode".
    hits = cat.search(tag="mode")
    assert len(hits) == 1
    assert hits[0].id == "furniture/seating/plain-sofa"
    # `tag="modern"` only hits the modern one.
    hits = cat.search(tag="modern")
    assert len(hits) == 1
    assert hits[0].id == "furniture/seating/modern-sofa"


def test_toml_escape_handles_control_chars():
    """Audit BUG-2: TOML escape must handle newline/tab/quote/backslash.
    A vendor block name containing any of these used to corrupt the
    output meta.toml."""
    from autocad_mcp.library.vendor import _escape_toml_str

    # Plain text passes through.
    assert _escape_toml_str("simple") == "simple"
    # Quote + backslash get standard escapes.
    assert _escape_toml_str('a "b" c') == 'a \\"b\\" c'
    assert _escape_toml_str(r"C:\Users\Axel") == r"C:\\Users\\Axel"
    # Whitespace control chars.
    assert _escape_toml_str("a\tb") == "a\\tb"
    assert _escape_toml_str("a\nb") == "a\\nb"
    assert _escape_toml_str("a\rb") == "a\\rb"
    # Other sub-0x20 → \uXXXX.
    assert _escape_toml_str("a\x01b") == "a\\u0001b"


def test_toml_escape_keeps_meta_parseable_under_evil_inputs(tmp_path):
    """End-to-end: a vendor block name with quotes, backslashes, and
    newlines must produce parseable meta.toml that round-trips back
    through the catalog reader."""
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    from autocad_mcp.library.vendor import _write_meta

    asset_dir = tmp_path / "evil"
    asset_dir.mkdir()
    src_dxf = tmp_path / "src.dxf"
    src_dxf.touch()

    _write_meta(
        asset_dir / "meta.toml",
        asset_id="evil/asset",
        name='He said "hi"\nand left',  # quotes + newline
        category="furniture",
        discipline="I",
        tags=["weird\ttag", 'has "quote"'],
        default_layer="0",
        block_file=src_dxf,
        block_name=r'block with "quote" and \backslash',
        width=10, depth=10, entity_count=5,
        source_file=str(src_dxf),
    )

    # Must parse without raising.
    with (asset_dir / "meta.toml").open("rb") as fp:
        parsed = tomllib.load(fp)
    # Round-trip preserves the original characters.
    assert '"hi"' in parsed["name"]
    assert "\n" in parsed["name"]
    assert any("weird" in t for t in parsed["tags"])
    assert "\\" in parsed["block_name"]


def test_catalog_insert_missing_xy_returns_invalid_arg(tmp_path, monkeypatch):
    """Audit BUG-1: catalog_insert without x/y data must return a clean
    invalid_arg error, not raise KeyError up through the MCP layer."""
    import asyncio
    import json
    _make_asset(tmp_path, "x/a", "A-BLOCK", width=10)

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)

    from autocad_mcp.server import library as library_tool
    fn = library_tool.fn if hasattr(library_tool, "fn") else library_tool

    asyncio.run(fn(operation="catalog_reindex", data={"root": str(tmp_path)}))
    r = asyncio.run(fn(operation="catalog_insert", data={
        "root": str(tmp_path),
        "asset_id": "x/a",
        # Note: x and y intentionally absent.
    }))
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert parsed.get("category") == "invalid_arg"
    assert "x and" in parsed["error"].lower() or "required" in parsed["error"].lower()


def test_catalog_stats_returns_correct_aggregate(seeded_catalog):
    """Audit MAINT-2: stats() now uses a single aggregated query.
    Verify the values still match what list_categories computes."""
    s = seeded_catalog.stats()
    cats = seeded_catalog.list_categories()
    assert s["total_assets"] == sum(c["count"] for c in cats)
    assert s["category_count"] == len(cats)
    assert s["last_indexed"] is not None


def test_seed_dwg_disabled_keeps_meta_pointing_at_dxf(tmp_path):
    """When `dwg=False`, the seeder writes only block.dxf and the meta
    file's `[files].block` points at it. This is the no-converter
    fallback path."""
    from autocad_mcp.library.seed import seed_starter_assets

    result = seed_starter_assets(tmp_path, dwg=False)
    assert result["dwg_written"] == []
    meta = (tmp_path / "furniture/seating/sofa-84-modern/meta.toml").read_text(
        encoding="utf-8",
    )
    assert 'block = "block.dxf"' in meta
    # Block.dwg must NOT exist for any asset when dwg=False.
    dwg_files = list(tmp_path.rglob("block.dwg"))
    assert dwg_files == []


def test_seed_block_dxf_is_byte_deterministic(tmp_path):
    """Critical contract: re-running the seeder against unchanged
    procedural input must produce byte-identical block.dxf files.
    Without this property, every reseed shows up as a meaningless git
    diff and the library can never be cleanly checked in.

    ezdxf bakes timestamps + GUIDs into headers on every save, so the
    seeder post-processes each block.dxf to normalize those fields.
    This test guards that path against regressions.
    """
    import hashlib

    from autocad_mcp.library.seed import seed_starter_assets

    def _hash_tree(root: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(root.rglob("*.dxf")):
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
        return h.hexdigest()

    # dwg=False so the test exercises only DXF (which IS byte-deterministic).
    # The DWG path bakes binary timestamps we can't post-process; that
    # trade-off is documented in seed.py and the spec.
    seed_starter_assets(tmp_path, dwg=False)
    a = _hash_tree(tmp_path)
    seed_starter_assets(tmp_path, dwg=False)
    b = _hash_tree(tmp_path)
    seed_starter_assets(tmp_path, dwg=False)
    c = _hash_tree(tmp_path)
    assert a == b == c, (
        f"Re-seed not deterministic: {a[:12]} -> {b[:12]} -> {c[:12]}. "
        "ezdxf likely added a new timestamp / GUID field that "
        "`_normalize_dxf_metadata_in_place` doesn't catch yet."
    )


def test_seeded_assets_reindex_cleanly(tmp_path):
    from autocad_mcp.library.seed import seed_starter_assets

    seed_starter_assets(tmp_path, dwg=False)
    cat = AssetCatalog(tmp_path)
    result = cat.reindex()
    assert result["indexed"] == 22
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# Insert composition
# ---------------------------------------------------------------------------


async def test_insert_asset_imports_then_inserts(seeded_catalog):
    """The composed insert must call block_import_from_file then
    block_insert. We use the real ezdxf backend so the round-trip
    exercises both stages without mocks."""
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
    from autocad_mcp.library.insert import insert_asset

    backend = EzdxfBackend()
    await backend.initialize()

    asset = seeded_catalog.get("furniture/seating/sofa-modern")
    assert asset is not None

    result = await insert_asset(backend, asset, 50, 25)
    assert result.ok, result.error
    # Definition landed in the destination block table.
    assert "SOFA-MODERN" in backend._doc.blocks
    # Insert returned an INSERT handle and the asset's default_layer is
    # reflected in the response.
    assert result.payload["asset_id"] == "furniture/seating/sofa-modern"
    assert result.payload["layer"] == "I-FURN"
    assert result.payload["insert"]["entity_type"] == "INSERT"


async def test_insert_asset_layer_override(seeded_catalog):
    """Caller-supplied `layer` wins over the asset's default."""
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
    from autocad_mcp.library.insert import insert_asset

    backend = EzdxfBackend()
    await backend.initialize()

    asset = seeded_catalog.get("furniture/seating/sofa-modern")
    result = await insert_asset(backend, asset, 0, 0, layer="CUSTOM-LAYER")
    assert result.ok
    assert result.payload["layer"] == "CUSTOM-LAYER"


async def test_insert_asset_actually_lands_on_default_layer(seeded_catalog):
    """Audit GAP-1: the INSERT entity's `.layer` attribute must equal
    the asset's `default_layer`, not "0" / whatever was current.
    Pre-fix, the layer was created but not set current — the INSERT
    landed on whatever layer was active."""
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
    from autocad_mcp.library.insert import insert_asset

    backend = EzdxfBackend()
    await backend.initialize()

    asset = seeded_catalog.get("furniture/seating/sofa-modern")
    assert asset.default_layer == "I-FURN"

    result = await insert_asset(backend, asset, 0, 0)
    assert result.ok
    insert_handle = result.payload["insert"]["handle"]
    ent = backend._doc.entitydb.get(insert_handle)
    assert ent is not None
    # The actual entity's layer attribute must match the asset's default.
    assert ent.dxf.layer == "I-FURN"


async def test_insert_asset_missing_block_file_errors():
    """An Asset with no block_file should fail with category not_found."""
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
    from autocad_mcp.library.insert import insert_asset

    backend = EzdxfBackend()
    await backend.initialize()

    a = Asset(id="x/y/z", name="orphan", category="x/y", block_file=None)
    result = await insert_asset(backend, a, 0, 0)
    assert not result.ok
    assert result.category == "not_found"


# ---------------------------------------------------------------------------
# MCP tool integration
# ---------------------------------------------------------------------------


async def test_mcp_catalog_search_via_tool(tmp_path, monkeypatch):
    """End-to-end: build a small catalog, call the `library` MCP tool
    with `catalog_search`, verify the response shape and the right hits."""
    _make_asset(tmp_path, "furniture/seating/sofa-mcp", "SOFA-MCP",
                width=84, tags=["sofa"], discipline="I")
    _make_asset(tmp_path, "furniture/seating/chair-mcp", "CHAIR-MCP",
                width=18, tags=["chair"], discipline="I")

    # Force the backend to ezdxf so we don't try to attach to AutoCAD.
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    # Reset the lazy backend singleton.
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)

    from autocad_mcp.server import library as library_tool
    fn = library_tool.fn if hasattr(library_tool, "fn") else library_tool

    # Reindex against the test root, then search.
    r = json.loads(await fn(
        operation="catalog_reindex",
        data={"root": str(tmp_path)},
    ))
    assert r["ok"]
    assert r["payload"]["indexed"] == 2

    r = json.loads(await fn(
        operation="catalog_search",
        data={"root": str(tmp_path), "query": "sofa"},
    ))
    assert r["ok"]
    assert r["payload"]["count"] == 1
    assert r["payload"]["results"][0]["id"] == "furniture/seating/sofa-mcp"


async def test_mcp_catalog_get_via_tool(tmp_path, monkeypatch):
    _make_asset(tmp_path, "x/a", "A-BLOCK", width=10)

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)

    from autocad_mcp.server import library as library_tool
    fn = library_tool.fn if hasattr(library_tool, "fn") else library_tool

    await fn(operation="catalog_reindex", data={"root": str(tmp_path)})

    r = json.loads(await fn(
        operation="catalog_get",
        data={"root": str(tmp_path), "asset_id": "x/a"},
    ))
    assert r["ok"]
    assert r["payload"]["id"] == "x/a"

    # Missing asset returns a category=not_found error.
    r = json.loads(await fn(
        operation="catalog_get",
        data={"root": str(tmp_path), "asset_id": "nope"},
    ))
    assert "error" in r
    assert r["category"] == "not_found"


# ---------------------------------------------------------------------------
# Vendor importer
# ---------------------------------------------------------------------------


def _make_multi_block_dwg_like(path: Path, block_specs: list[tuple[str, int]]) -> None:
    """Author a DXF with `len(block_specs)` named blocks, each containing
    `entity_count` lines. Stand-in for "multi-block vendor DWG" that the
    importer would otherwise need a real accoreconsole call to read."""
    doc = ezdxf.new("R2013")
    for name, entity_count in block_specs:
        blk = doc.blocks.new(name=name)
        for i in range(entity_count):
            blk.add_line((i, 0), (i, 1))
    doc.saveas(path)


def test_vendor_infer_category_handles_common_filenames():
    from autocad_mcp.library.vendor import infer_category

    cases = [
        ("bedroom_furniture.dwg",          "furniture/bedroom"),
        ("dining_tables.dwg",              "furniture/tables"),
        ("chairs_and_armchairs.dwg",       "furniture/seating"),
        ("sofas.dwg",                      "furniture/seating"),
        ("doors_set.dwg",                  "doors"),
        ("modern-kitchen-elevation.dwg",   "fixtures/kitchen"),
        ("wood_panelling.dwg",             "materials/wood"),
        ("wardrobe_details.dwg",           "furniture/storage"),
        ("office_chair.dwg",               "furniture/seating"),  # 'chair' wins
        ("crib.dwg",                       "furniture/beds"),
        ("furniture-12.dwg",               "furniture"),           # generic fallback
    ]
    for filename, expected in cases:
        cat, _, _ = infer_category(filename)
        assert cat == expected, f"{filename!r}: expected {expected!r}, got {cat!r}"


def test_vendor_is_autonamed_catches_autocad_placeholders():
    from autocad_mcp.library.vendor import _is_autonamed

    # Real AutoCAD-generated names from the smoke-test sample.
    assert _is_autonamed("A$C00681915")
    assert _is_autonamed("A$C0068412a")
    assert _is_autonamed("A$12345")
    # Real-named blocks must not match.
    assert not _is_autonamed("Crib 2_front_dwgmodels.com")
    assert not _is_autonamed("chair a1")
    assert not _is_autonamed("FURN-SOFA")
    assert not _is_autonamed("Banqueta Barcelona")


def test_vendor_import_filters_autonamed_by_default(tmp_path):
    from autocad_mcp.library.vendor import import_vendor_library

    source = tmp_path / "vendor_src"
    source.mkdir()
    _make_multi_block_dwg_like(
        source / "chairs.dxf",  # .dxf so no converter dependency in tests
        block_specs=[
            ("chair a1",     5),    # real-named, 5 entities
            ("chair a2",     7),    # real-named, 7 entities
            ("A$C00681915",  4),    # AutoCAD placeholder — must be skipped
            ("tiny",         1),    # below min_entity_count
        ],
    )

    target = tmp_path / "library"
    result = import_vendor_library(
        source_dir=source,
        target_root=target,
        vendor_name="t",
        min_entity_count=3,
    )
    assert result.blocks_imported == 2   # chair a1, chair a2
    assert result.blocks_skipped == 2    # autoname + tiny

    ids = [a for a in result.asset_ids]
    assert all(a.startswith("vendor/t/") for a in ids)
    # Auto-named must NOT be present.
    assert not any("a-c00681915" in a for a in ids)


def test_vendor_import_include_autonamed_keeps_placeholders(tmp_path):
    from autocad_mcp.library.vendor import import_vendor_library

    source = tmp_path / "vendor_src"
    source.mkdir()
    _make_multi_block_dwg_like(
        source / "x.dxf",
        block_specs=[("real-block", 5), ("A$C00681915", 5)],
    )
    target = tmp_path / "library"
    result = import_vendor_library(
        source_dir=source, target_root=target,
        vendor_name="t", min_entity_count=3,
        include_autonamed=True,
    )
    assert result.blocks_imported == 2


def test_vendor_import_meta_points_at_source_dwg(tmp_path):
    """The asset's [files].block must be the absolute path to the source
    DWG — not a copy. That's the no-duplication contract."""
    from autocad_mcp.library.vendor import import_vendor_library

    source = tmp_path / "vendor_src"
    source.mkdir()
    src_dxf = source / "chairs.dxf"
    _make_multi_block_dwg_like(src_dxf, [("test-chair-1", 5)])

    target = tmp_path / "library"
    import_vendor_library(
        source_dir=source, target_root=target,
        vendor_name="t", min_entity_count=3,
    )

    # The new meta file should reference the real source DXF.
    meta_files = list(target.rglob("meta.toml"))
    assert len(meta_files) == 1
    meta = meta_files[0].read_text(encoding="utf-8")
    # Path uses forward slashes; check the resolved path's tail.
    assert "chairs.dxf" in meta
    assert "[vendor]" in meta
    assert "license = \"vendor\"" in meta


def test_vendor_import_then_catalog_insert_round_trip(tmp_path):
    """The full loop: vendor-import → reindex → catalog_search →
    catalog_insert. Confirms a vendor asset can land in an ezdxf doc
    via the catalog API exactly like a procedural asset would."""
    import asyncio
    from autocad_mcp.library.vendor import import_vendor_library
    from autocad_mcp.library.catalog import AssetCatalog
    from autocad_mcp.library.insert import insert_asset
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

    source = tmp_path / "vendor_src"
    source.mkdir()
    src_dxf = source / "chairs.dxf"
    _make_multi_block_dwg_like(
        src_dxf,
        [("vendor-chair-test", 5), ("other-block", 4)],
    )
    target = tmp_path / "library"
    import_vendor_library(
        source_dir=source, target_root=target,
        vendor_name="t", min_entity_count=3,
    )

    cat = AssetCatalog(target)
    cat.reindex()
    hits = cat.search(query="vendor-chair-test")
    assert len(hits) == 1
    asset = hits[0]
    assert asset.block_name == "vendor-chair-test"
    # The source DXF lives outside the asset dir — must be an absolute
    # path that still resolves.
    assert asset.block_file is not None
    assert asset.block_file.exists()
    assert asset.block_file.name == "chairs.dxf"

    async def _do_insert():
        backend = EzdxfBackend()
        await backend.initialize()
        return await insert_asset(backend, asset, 0, 0), backend

    result, backend = asyncio.run(_do_insert())
    assert result.ok, result.error
    # The imported block must land in the destination's block table —
    # note `other-block` should NOT be there because we asked for only
    # this one name via `block_import_from_file(names=[...])`.
    block_names = {b.name for b in backend._doc.blocks if not b.name.startswith("*")}
    assert "vendor-chair-test" in block_names
    assert "other-block" not in block_names


async def test_mcp_catalog_insert_via_tool(tmp_path, monkeypatch):
    """End-to-end: place an asset through the MCP tool against the
    real ezdxf backend."""
    _make_asset(tmp_path, "furniture/seating/insert-test", "INSERT-TEST",
                width=20, tags=["test"], default_layer="I-FURN")

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)

    from autocad_mcp.server import library as library_tool
    fn = library_tool.fn if hasattr(library_tool, "fn") else library_tool

    await fn(operation="catalog_reindex", data={"root": str(tmp_path)})

    r = json.loads(await fn(
        operation="catalog_insert",
        data={
            "root": str(tmp_path),
            "asset_id": "furniture/seating/insert-test",
            "x": 100, "y": 50,
        },
    ))
    assert r["ok"], r
    assert r["payload"]["layer"] == "I-FURN"
    assert r["payload"]["block_name"] == "INSERT-TEST"
