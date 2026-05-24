"""One-shot seeder: turn the 22 procedural CC0 symbols into asset-library/ entries.

Each procedural symbol becomes its own asset directory with a real
`block.dxf` (single block definition) plus a `meta.toml`. Run via:

    .venv/Scripts/python.exe -m autocad_mcp.library.seed

Or programmatically:

    from autocad_mcp.library.seed import seed_starter_assets
    seed_starter_assets(Path("asset-library"))

Safe to re-run: existing directories are overwritten in place. Designed
to be deterministic so a `git diff` after re-seeding only changes files
that genuinely changed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import ezdxf
import structlog

from autocad_mcp.arch.library import SYMBOL_LIBRARY
from autocad_mcp.arch.library.apply import apply_to_ezdxf_block

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Symbol → catalog metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedEntry:
    """One procedural symbol's catalog mapping."""

    block_name: str           # the symbol's name in SYMBOL_LIBRARY
    category: str             # e.g. "furniture/seating"
    asset_dir: str            # the last path component, e.g. "sofa-84-modern"
    display_name: str
    tags: tuple[str, ...]
    size: tuple[float, float, float] | None  # (width, depth, height) inches; None when N/A
    default_layer: str
    discipline: str           # AIA code


SEED_ENTRIES: tuple[SeedEntry, ...] = (
    # --- Annotation ---
    SeedEntry("ARCH-NORTH-ARROW", "annotation/orientation", "north-arrow",
              "North arrow", ("north", "compass", "orientation", "annotation"),
              None, "A-ANNO-SYMB", "A"),
    SeedEntry("ARCH-SCALE-BAR-10FT", "annotation/scale", "scale-bar-10ft",
              "Scale bar — 10 ft", ("scale", "bar", "annotation"),
              None, "A-ANNO-SYMB", "A"),
    SeedEntry("ARCH-STAIR-MARKER", "annotation/stairs", "stair-marker-up",
              "Stair direction marker (UP)", ("stair", "up", "annotation"),
              None, "A-FLOR-STRS", "A"),
    # --- Plumbing fixtures ---
    SeedEntry("PLMB-TOILET", "fixtures/plumbing", "toilet-standard",
              "Toilet — standard tank type",
              ("toilet", "wc", "fixture", "bathroom", "plumbing"),
              (20.0, 28.0, 30.0), "P-FIXT", "P"),
    SeedEntry("PLMB-SINK", "fixtures/plumbing", "lavatory-standard",
              "Lavatory — wall-hung",
              ("lav", "lavatory", "sink", "bathroom", "fixture"),
              (20.0, 18.0, 34.0), "P-FIXT", "P"),
    SeedEntry("PLMB-BATHTUB", "fixtures/plumbing", "bathtub-60in",
              "Bathtub — 60 in. alcove",
              ("tub", "bathtub", "alcove", "bathroom", "fixture"),
              (60.0, 30.0, 16.0), "P-FIXT", "P"),
    SeedEntry("PLMB-SHOWER", "fixtures/plumbing", "shower-36sq",
              "Shower — 36 in. square",
              ("shower", "stall", "bathroom", "fixture"),
              (36.0, 36.0, 80.0), "P-FIXT", "P"),
    SeedEntry("PLMB-KITCHEN-SINK", "fixtures/kitchen", "kitchen-sink-double",
              "Kitchen sink — double bowl",
              ("sink", "kitchen", "double", "fixture"),
              (33.0, 22.0, 9.0), "P-FIXT", "P"),
    # --- Kitchen appliances ---
    SeedEntry("APPL-RANGE", "fixtures/kitchen", "range-30in",
              "Range — 30 in. four-burner",
              ("range", "stove", "kitchen", "appliance"),
              (30.0, 25.0, 36.0), "A-FLOR-APPL", "A"),
    SeedEntry("APPL-REFRIGERATOR", "fixtures/kitchen", "refrigerator-36in",
              "Refrigerator — 36 in. side-by-side",
              ("fridge", "refrigerator", "kitchen", "appliance"),
              (36.0, 30.0, 70.0), "A-FLOR-APPL", "A"),
    SeedEntry("APPL-DISHWASHER", "fixtures/kitchen", "dishwasher-24in",
              "Dishwasher — 24 in. built-in",
              ("dishwasher", "kitchen", "appliance"),
              (24.0, 24.0, 34.0), "A-FLOR-APPL", "A"),
    # --- Furniture ---
    SeedEntry("FURN-BED-QUEEN", "furniture/beds", "bed-queen",
              "Bed — queen", ("bed", "queen", "bedroom", "furniture"),
              (60.0, 80.0, 24.0), "I-FURN", "I"),
    SeedEntry("FURN-BED-KING", "furniture/beds", "bed-king",
              "Bed — king", ("bed", "king", "bedroom", "furniture"),
              (76.0, 80.0, 24.0), "I-FURN", "I"),
    SeedEntry("FURN-DRESSER", "furniture/storage", "dresser-standard",
              "Dresser — 6-drawer",
              ("dresser", "storage", "bedroom", "furniture"),
              (60.0, 20.0, 36.0), "I-FURN", "I"),
    SeedEntry("FURN-NIGHTSTAND", "furniture/storage", "nightstand-standard",
              "Nightstand", ("nightstand", "bedside", "bedroom", "furniture"),
              (24.0, 18.0, 26.0), "I-FURN", "I"),
    SeedEntry("FURN-DESK", "furniture/desks", "desk-60in",
              "Desk — 60 in.", ("desk", "office", "furniture"),
              (60.0, 30.0, 30.0), "I-FURN", "I"),
    SeedEntry("FURN-CHAIR-OFFICE", "furniture/seating", "chair-office",
              "Office chair — task",
              ("chair", "office", "task", "seating", "furniture"),
              (24.0, 24.0, 36.0), "I-FURN", "I"),
    SeedEntry("FURN-CHAIR-DINING", "furniture/seating", "chair-dining",
              "Dining chair",
              ("chair", "dining", "seating", "furniture"),
              (18.0, 18.0, 36.0), "I-FURN", "I"),
    SeedEntry("FURN-TABLE-DINING", "furniture/tables", "table-dining-rectangle",
              "Dining table — rectangular 6-seat",
              ("table", "dining", "rectangle", "furniture"),
              (72.0, 36.0, 30.0), "I-FURN", "I"),
    SeedEntry("FURN-SOFA", "furniture/seating", "sofa-84-modern",
              "Sofa — 84 in. three-seat",
              ("sofa", "couch", "seating", "living-room", "furniture"),
              (84.0, 36.0, 32.0), "I-FURN", "I"),
    SeedEntry("FURN-FILE-CABINET", "furniture/storage", "file-cabinet-4drawer",
              "File cabinet — 4-drawer lateral",
              ("filing", "cabinet", "office", "storage", "furniture"),
              (36.0, 18.0, 54.0), "I-FURN", "I"),
    # --- Electrical ---
    SeedEntry("ELEC-OUTLET-DUPLEX", "fixtures/electrical", "outlet-duplex",
              "Outlet — duplex receptacle",
              ("outlet", "receptacle", "electrical", "duplex"),
              None, "E-POWR", "E"),
)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_meta(
    meta_path: Path, entry: SeedEntry, asset_id: str,
    *, primary_block: str = "block.dxf",
) -> None:
    """Write a TOML meta file. We hand-format rather than use a library so
    we can keep the file diff-friendly and human-edited.

    `primary_block` is the filename the meta's `[files].block` points at.
    Defaults to `block.dxf` (works everywhere); the seeder passes
    `block.dwg` when a converter is available so AutoCAD users open the
    native format. The DXF sibling stays on disk regardless — it's the
    byte-deterministic canonical copy.
    """
    lines: list[str] = []
    lines.append(f'id = "{asset_id}"')
    lines.append(f'name = "{entry.display_name}"')
    lines.append(f'category = "{entry.category}"')
    lines.append(f'discipline = "{entry.discipline}"')
    lines.append(f'tags = {_toml_string_list(entry.tags)}')
    lines.append(f'default_layer = "{entry.default_layer}"')
    lines.append('license = "CC0-1.0"')
    lines.append('inserts_as = "block"')
    lines.append('# Block name inside the file — uppercased asset dir name')
    lines.append(f'block_name = "{_block_name_for(asset_id)}"')
    lines.append('')
    if entry.size is not None:
        w, d, h = entry.size
        lines.append('[size]')
        lines.append(f'width = {w}')
        lines.append(f'depth = {d}')
        lines.append(f'height = {h}')
        lines.append('units = "inches"')
        lines.append('')
    lines.append('[files]')
    lines.append(f'block = "{primary_block}"')
    lines.append('')
    meta_path.write_text("\n".join(lines), encoding="utf-8")


def _toml_string_list(items: Iterable[str]) -> str:
    """TOML inline-array literal: ["a", "b", "c"].

    Each item is escaped per TOML basic-string rules so backslashes,
    quotes, and control characters don't produce malformed output. The
    starter SEED_ENTRIES use clean ASCII so this is mostly defense in
    depth, but the shared helper protects extensions that add new tags.
    """
    from autocad_mcp.library.vendor import _escape_toml_str
    return "[" + ", ".join(f'"{_escape_toml_str(i)}"' for i in items) + "]"


def _block_name_for(asset_id: str) -> str:
    """Block name uppercased + dashes — matches insert.asset_block_name()."""
    tail = asset_id.rsplit("/", 1)[-1]
    return tail.upper().replace("_", "-")


import re as _re


def _write_block_dxf(block_dxf_path: Path, source_block_name: str, spec_list) -> None:
    """Create a fresh DXF with one block whose name equals the asset's
    expected block name, containing the procedural spec entities.

    The saved DXF is post-processed so timestamps + GUIDs are replaced
    with fixed values. Without this, ezdxf regenerates
    `$TDCREATE` / `$TDUPDATE` / `$FINGERPRINTGUID` / `$VERSIONGUID` plus
    timestamp-bearing `DICTIONARYVAR` entries on every save, and the
    "re-seed produces zero git diff" property the spec promises would
    be false. Block content is identical — only metadata varies — so
    the post-process is safe.
    """
    doc = ezdxf.new("R2013")
    target_block_name = _block_name_for_dir(block_dxf_path.parent.name)
    blk = doc.blocks.new(name=target_block_name)
    apply_to_ezdxf_block(blk, spec_list)
    _pin_dxf_header_for_determinism(doc)
    doc.saveas(block_dxf_path)
    _normalize_dxf_metadata_in_place(block_dxf_path)


def _write_block_dwg(block_dwg_path: Path) -> bool:
    """Convert the sibling block.dxf to block.dwg via accoreconsole/ODA.

    Returns True when a DWG was written, False on any failure (no
    converter, accoreconsole timeout, prompt mismatch, license error...).
    Per-asset DWG failure is never fatal — the meta.toml falls back to
    pointing at block.dxf so the catalog stays usable.

    The DWG is NOT byte-deterministic — accoreconsole embeds its own
    timestamp in binary form that we can't post-process. The DXF sibling
    remains the deterministic canonical copy.
    """
    from autocad_mcp.project.dwg_reader import _convert_dxf_to_dwg

    dxf_path = block_dwg_path.with_suffix(".dxf")
    if not dxf_path.exists():
        return False
    try:
        _convert_dxf_to_dwg(dxf_path, block_dwg_path)
    except Exception as ex:
        # Any failure — DWGConverterMissing, subprocess.TimeoutExpired,
        # OSError on the .scr write, AutoCAD license errors — log and
        # carry on. The caller's meta.toml keeps pointing at block.dxf.
        log.debug("seed_write_block_dwg_failed",
                  path=str(block_dwg_path), error=str(ex))
        return False
    return block_dwg_path.exists()


# Asset-library epoch: 2026-01-01 00:00 UTC as a Julian Day Number.
# Arbitrary but fixed — what matters is that the value never changes.
_ASSET_EPOCH_JDN = 2461042.0
_NULL_GUID = "{00000000-0000-0000-0000-000000000000}"
_FROZEN_TIMESTAMP = "2026-01-01T00:00:00+00:00"

# GUID pattern (curly-braced UUID). Matches both $FINGERPRINTGUID /
# $VERSIONGUID header values and any other GUID-shaped strings ezdxf
# embeds. Conservative on purpose — false positives in a procedurally-
# generated block.dxf would be vanishingly rare.
_GUID_RE = _re.compile(
    r"\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}",
    _re.IGNORECASE,
)
# Matches the timestamp ezdxf bakes into DICTIONARYVAR records of the form
# "<version> @ <iso8601>". Replaces the whole "version @ timestamp" with
# a frozen pair so version bumps don't reshuffle bytes either.
_EZDXF_VERSION_TIMESTAMP_RE = _re.compile(
    r"\d+\.\d+\.\d+(?:\.\d+)?\s*@\s*[0-9T:\-+.]+"
)
# DXF header timestamp pairs. The format is two-line: a `9` group code with
# the variable name, then a `40` group code with the value. ezdxf
# overwrites $TDUPDATE / $TDCREATE at save time regardless of our pre-save
# pin, so we restore them via post-process. The frozen value is the
# stringified _ASSET_EPOCH_JDN.
_HEADER_TIMESTAMP_RE = _re.compile(
    r"(\$TD(?:CREATE|UPDATE|INDWG|USRTIMER)\s*\r?\n\s*40\s*\r?\n)"
    r"(\d+\.?\d*)",
)


def _pin_dxf_header_for_determinism(doc) -> None:
    """Best-effort: pin time- and GUID-shaped header vars at build time.
    The post-process `_normalize_dxf_metadata_in_place` is the real
    guarantee — this is just to keep the in-memory document honest."""
    header = doc.header
    for var, value in (
        ("$TDCREATE", _ASSET_EPOCH_JDN),
        ("$TDUPDATE", _ASSET_EPOCH_JDN),
        ("$TDINDWG", 0.0),
        ("$TDUSRTIMER", 0.0),
        ("$FINGERPRINTGUID", _NULL_GUID),
        ("$VERSIONGUID", _NULL_GUID),
    ):
        try:
            header[var] = value
        except Exception:
            pass


def _normalize_dxf_metadata_in_place(path: Path) -> None:
    """Rewrite GUIDs and ezdxf-version timestamps in `path` to fixed values.

    DXF files are line-oriented ASCII; replacing whole-line tokens by regex
    preserves structure while erasing the wobble. We do this on the bytes
    after `saveas` because ezdxf's `DICTIONARYVAR` writer ignores the
    pre-save header pin for those embedded timestamps.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1252")
    new = _GUID_RE.sub(_NULL_GUID, text)
    new = _EZDXF_VERSION_TIMESTAMP_RE.sub(
        f"0.0.0 @ {_FROZEN_TIMESTAMP}", new,
    )
    # Header timestamp pairs need a callback so we keep the label/group-code
    # prefix and only replace the numeric value.
    new = _HEADER_TIMESTAMP_RE.sub(
        lambda m: f"{m.group(1)}{_ASSET_EPOCH_JDN}",
        new,
    )
    if new != text:
        path.write_text(new, encoding="utf-8")


def _block_name_for_dir(asset_dir_name: str) -> str:
    return asset_dir_name.upper().replace("_", "-")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def seed_starter_assets(root: Path, *, dwg: bool | None = None) -> dict:
    """Populate `root` (asset-library/) with one directory per starter symbol.

    Idempotent: re-running overwrites existing meta.toml + block.dxf with the
    same content, so a clean re-seed produces zero git diff if the procedural
    symbols haven't changed.

    Args:
        root: target directory (typically `<repo>/asset-library/`).
        dwg: when True, also write `block.dwg` for each asset and point the
             meta.toml's `[files].block` at it. When None (auto, the default),
             check whether a DWG converter is available and turn DWG output
             on if so; otherwise skip silently. When False, only `block.dxf`
             is written.

    The DXF is always written. It's byte-deterministic (the post-process pass
    pins timestamps + GUIDs) so re-seeding produces zero git diff. The DWG
    is NOT byte-deterministic — accoreconsole bakes a save timestamp into
    binary metadata that can't be regex'd out — but the block geometry is
    identical to the DXF.
    """
    from autocad_mcp.project.dwg_reader import available_dwg_converters

    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    _write_library_toml(root)

    if dwg is None:
        dwg = any(available_dwg_converters().values())

    written: list[str] = []
    dwg_written: list[str] = []
    skipped: list[dict] = []
    for entry in SEED_ENTRIES:
        if entry.block_name not in SYMBOL_LIBRARY:
            skipped.append({"block": entry.block_name, "reason": "not in SYMBOL_LIBRARY"})
            continue
        asset_id = f"{entry.category}/{entry.asset_dir}"
        asset_dir = root / entry.category / entry.asset_dir
        asset_dir.mkdir(parents=True, exist_ok=True)

        # DXF first — the canonical, deterministic, fast-to-read copy.
        _write_block_dxf(
            asset_dir / "block.dxf",
            entry.block_name,
            SYMBOL_LIBRARY[entry.block_name],
        )

        # DWG second — the AutoCAD-native form. Only when a converter is
        # available and the caller wants it. Failure to write doesn't fail
        # the asset; the meta.toml just keeps pointing at the DXF.
        primary_block = "block.dxf"
        if dwg:
            wrote_dwg = _write_block_dwg(asset_dir / "block.dwg")
            if wrote_dwg:
                primary_block = "block.dwg"
                dwg_written.append(asset_id)

        _write_meta(
            asset_dir / "meta.toml", entry, asset_id,
            primary_block=primary_block,
        )
        written.append(asset_id)

    return {
        "root": str(root),
        "written": written,
        "dwg_written": dwg_written,
        "skipped": skipped,
    }


def _write_library_toml(root: Path) -> None:
    """Write `library.toml` with the default config. Atomic-replace so the
    file always reflects the current seeder version."""
    content = """# Asset library catalog config — see docs/library-spec.md.
[library]
name = "autocad-mcp starter library"
version = "0.1.0"
description = "CC0 starter assets for floor-plan drafting"

[default_layers]
# Overrides for the meta.toml default_layer when the asset's discipline
# matches one of these keys. Empty by default — assets carry their own
# default_layer in meta.toml.
"""
    (root / "library.toml").write_text(content, encoding="utf-8")


def main() -> int:
    """CLI entry: write starter library to `<repo>/asset-library/` then
    rebuild the catalog cache. Prints a summary."""
    from autocad_mcp.library.catalog import AssetCatalog, default_library_root

    root = default_library_root()
    result = seed_starter_assets(root)
    print(f"Seeded {len(result['written'])} assets into {result['root']}")
    dwg = result.get("dwg_written", [])
    if dwg:
        print(f"  block.dwg written for {len(dwg)} assets")
    else:
        print("  no DWG written (no converter available, or dwg=False)")
    if result["skipped"]:
        print(f"Skipped: {result['skipped']}")
    cat = AssetCatalog(root)
    reindex = cat.reindex()
    print(f"Catalog reindexed: {reindex['indexed']} assets, "
          f"{len(reindex['errors'])} errors")
    cat.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
