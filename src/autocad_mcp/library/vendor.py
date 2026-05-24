"""Vendor-library importer.

Turn an existing directory of vendor `.dwg` / `.dxf` files into asset
catalog entries without copying the geometry. Each block inside each
DWG becomes one asset directory containing only a `meta.toml`; the
`[files].block` field points at the **original** source file (absolute
path) and `block_name` selects the specific block inside.

Why no copy: vendor packs are often gigabytes. Pointing at the source
keeps the catalog small + git-tracking-safe (the path-reference is a
few lines of toml, the binary geometry stays put). The downside: if
the source moves, the catalog breaks — acceptable for single-machine
vendor packs.

Each vendor asset's id has the shape:
    vendor/<vendor_name>/<inferred-category>/<dwg-stem>/<block-sanitized>

Category is inferred from the source filename via `infer_category`.
Discipline + default_layer fall out from category via `discipline_for`.
Size comes from the block's bounding box.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


# Keyword → (category, discipline, default_layer). Probed in declaration
# order: the FIRST match wins. Specific keywords go first; generic ones last.
_CATEGORY_RULES: tuple[tuple[str, str, str, str], ...] = (
    # (keyword, category, discipline, default_layer)
    ("door fitting",   "details/hardware",      "A", "A-DETL"),
    ("door",           "doors",                 "A", "A-DOOR"),
    ("window",         "windows",               "A", "A-GLAZ"),
    ("curtain",        "window-treatments",     "I", "I-FURN-WIND"),
    ("kitchen",        "fixtures/kitchen",      "A", "A-FLOR-APPL"),
    ("bar ",           "fixtures/bar",          "I", "I-FURN"),
    ("bar_",           "fixtures/bar",          "I", "I-FURN"),
    ("bar-",           "fixtures/bar",          "I", "I-FURN"),
    ("toilet",         "fixtures/plumbing",     "P", "P-FIXT"),
    ("bathroom",       "fixtures/plumbing",     "P", "P-FIXT"),
    ("playseat",       "furniture/seating",     "I", "I-FURN"),
    ("crib",           "furniture/beds",        "I", "I-FURN"),
    # `bedroom` must come BEFORE `bed` so "bedroom_furniture.dwg" lands in
    # furniture/bedroom rather than furniture/beds.
    ("bedroom",        "furniture/bedroom",     "I", "I-FURN"),
    ("bed",            "furniture/beds",        "I", "I-FURN"),
    ("dining table",   "furniture/tables",      "I", "I-FURN"),
    ("dining",         "furniture/dining",      "I", "I-FURN"),
    ("dressing table", "furniture/bedroom",     "I", "I-FURN"),
    ("dresser",        "furniture/storage",     "I", "I-FURN"),
    ("table",          "furniture/tables",      "I", "I-FURN"),
    ("chair",          "furniture/seating",     "I", "I-FURN"),
    ("armchair",       "furniture/seating",     "I", "I-FURN"),
    ("sofa",           "furniture/seating",     "I", "I-FURN"),
    ("couch",          "furniture/seating",     "I", "I-FURN"),
    ("seat",           "furniture/seating",     "I", "I-FURN"),
    ("office",         "furniture/office",      "I", "I-FURN"),
    ("desk",           "furniture/office",      "I", "I-FURN"),
    ("working place",  "furniture/office",      "I", "I-FURN"),
    ("wardrobe",       "furniture/storage",     "I", "I-FURN"),
    ("locker",         "furniture/storage",     "I", "I-FURN"),
    ("bookcase",       "furniture/storage",     "I", "I-FURN"),
    ("cabinet",        "furniture/storage",     "I", "I-FURN"),
    ("mirror",         "decor/mirrors",         "I", "I-FURN"),
    ("clock",          "decor",                 "I", "I-FURN"),
    ("hotel",          "furniture/hospitality", "I", "I-FURN"),
    ("salon",          "furniture/hospitality", "I", "I-FURN"),
    ("medical",        "furniture/medical",     "I", "I-FURN"),
    ("patient",        "furniture/medical",     "I", "I-FURN"),
    ("conference",     "furniture/office",      "I", "I-FURN"),
    ("meeting",        "furniture/office",      "I", "I-FURN"),
    ("living room",    "furniture/living",      "I", "I-FURN"),
    ("living_room",    "furniture/living",      "I", "I-FURN"),
    ("living-room",    "furniture/living",      "I", "I-FURN"),
    ("childrens",      "furniture/childrens",   "I", "I-FURN"),
    ("children",       "furniture/childrens",   "I", "I-FURN"),
    ("wood panel",     "materials/wood",        "A", "A-DETL"),
    ("wood_panel",     "materials/wood",        "A", "A-DETL"),
    ("wood-panel",     "materials/wood",        "A", "A-DETL"),
    ("paneling",       "materials/wood",        "A", "A-DETL"),
    ("panelling",      "materials/wood",        "A", "A-DETL"),
    ("hotels",         "furniture/hospitality", "I", "I-FURN"),
)


def infer_category(filename: str) -> tuple[str, str, str]:
    """Return (category, discipline, default_layer) inferred from `filename`.

    The filename stem is normalized to lowercase with underscores and dashes
    converted to spaces, then probed against the rule table in order. Falls
    back to `("furniture", "I", "I-FURN")` when nothing matches — the most
    common case for the vendor packs we've seen.
    """
    stem = Path(filename).stem.lower()
    haystack = stem.replace("_", " ").replace("-", " ")
    for keyword, category, discipline, layer in _CATEGORY_RULES:
        if keyword in haystack:
            return category, discipline, layer
    return "furniture", "I", "I-FURN"


# ---------------------------------------------------------------------------
# Filesystem-friendly name sanitization
# ---------------------------------------------------------------------------


_SAFE_CHAR_RE = re.compile(r"[^a-z0-9]+")

# AutoCAD-internal "block reference" auto-names. These appear when the
# source DWG was made by exploding nested INSERTs or WBLOCKing a region:
# AutoCAD generates `A$C<hex>` placeholders that aren't meaningful
# blocks. Skip them by default to avoid polluting the catalog.
_AUTONAME_RES = (
    re.compile(r"^A\$[A-Z]?[0-9A-Fa-f]+$"),   # A$C00681915, A$12345
    re.compile(r"^\*[A-Z][0-9]+$"),            # *U123, *X45 (paranoid; usually
                                                # already filtered by `*`-prefix)
)


def _is_autonamed(block_name: str) -> bool:
    """True for block names that look like AutoCAD-generated placeholders."""
    return any(rx.match(block_name) for rx in _AUTONAME_RES)


def _sanitize_for_dir(name: str) -> str:
    """Lowercase, collapse non-alphanumerics to single dashes. Empty strings
    fall back to `block` so we always produce a usable directory name."""
    cleaned = _SAFE_CHAR_RE.sub("-", name.lower()).strip("-")
    return cleaned or "block"


# ---------------------------------------------------------------------------
# Import result types
# ---------------------------------------------------------------------------


@dataclass
class VendorImportResult:
    """Summary of one `import_vendor_library` run."""

    files_scanned: int = 0
    files_failed: list[dict] = field(default_factory=list)
    blocks_imported: int = 0
    blocks_skipped: int = 0
    asset_ids: list[str] = field(default_factory=list)
    target_root: str = ""


# ---------------------------------------------------------------------------
# Per-block geometry probe
# ---------------------------------------------------------------------------


def _block_size(blk) -> tuple[float | None, float | None]:
    """Bounding-box width + depth of a block's entities, in drawing units.

    Returns (None, None) on any failure — bbox math is fragile across
    ezdxf versions and entity types, and vendor DWGs sometimes contain
    funky entities we can't bbox.
    """
    try:
        from ezdxf.bbox import extents
        bbox = extents(blk, fast=True)
        if bbox is None or not bbox.has_data:
            return None, None
        e = bbox.extmax - bbox.extmin
        return float(e.x), float(e.y)
    except Exception as ex:
        log.debug("block_bbox_failed", error=str(ex))
        return None, None


# ---------------------------------------------------------------------------
# Walk + import
# ---------------------------------------------------------------------------


def _iter_drawings(root: Path, recursive: bool = True) -> Iterable[Path]:
    """Yield every `.dwg`/`.dxf` under `root`."""
    if not root.is_dir():
        return
    paths: Iterable[Path] = root.rglob("*") if recursive else root.iterdir()
    for p in paths:
        if p.is_file() and p.suffix.lower() in (".dwg", ".dxf"):
            yield p


def import_vendor_library(
    source_dir: str | Path,
    target_root: str | Path,
    *,
    vendor_name: str = "vendor",
    recursive: bool = True,
    min_entity_count: int = 3,
    progress: bool = True,
    include_autonamed: bool = False,
) -> VendorImportResult:
    """Walk `source_dir`, materialize every qualifying block as an asset.

    Each block becomes one asset directory under
        target_root / vendor / vendor_name / <category> / <dwg-stem> / <block-sanitized>
    containing only a meta.toml. The `block.dxf` reference in that meta
    points at the *original* source file, not a copy.

    `min_entity_count` filters out the tiny auto-generated blocks
    (dimension fragments, layer markers) that vendor packs accumulate.
    A value of 3 trims most noise while keeping real geometry.
    """
    source = Path(source_dir).expanduser().resolve()
    target = Path(target_root).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"vendor source dir not found: {source}")

    # Imports are local so `vendor` works on hosts without pywin32 etc.
    from autocad_mcp.project.dwg_reader import (
        DWGConverterMissing, available_dwg_converters, read_drawing,
    )

    # Probe the converter situation up front: if neither converter is
    # installed AT ALL, abort fast with a clear message. Mid-import
    # `DWGConverterMissing` exceptions then almost certainly mean the
    # converter rejected a specific file (typically a filename with
    # parens / spaces that the .scr-script flow misreads).
    converters = available_dwg_converters()
    if not any(converters.values()):
        raise DWGConverterMissing(
            f"vendor_import needs a DWG converter (accoreconsole or "
            f"ODA File Converter). Diagnostic: {converters}"
        )

    result = VendorImportResult(target_root=str(target))

    for dwg_path in _iter_drawings(source, recursive=recursive):
        if progress:
            log.info("vendor_import_scanning", file=str(dwg_path))
        try:
            doc = read_drawing(dwg_path)
        except Exception as ex:
            # A per-file converter failure (parens in name, corrupt
            # DWG, version mismatch) lands here. Record + continue —
            # we have 87 other files to try.
            result.files_failed.append({
                "file": str(dwg_path),
                "error": str(ex)[:300],
            })
            log.warning("vendor_import_file_failed",
                        file=str(dwg_path), error=str(ex))
            continue
        result.files_scanned += 1

        category, discipline, default_layer = infer_category(dwg_path.name)
        dwg_stem_dir = _sanitize_for_dir(dwg_path.stem)

        for blk in doc.blocks:
            if blk.name.startswith("*"):
                continue
            # Skip XREFs — DXF flag bit 4.
            try:
                flags = int(getattr(blk.block.dxf, "flags", 0) or 0)
            except Exception:
                flags = 0
            if flags & 4:
                continue

            entity_count = sum(1 for _ in blk)
            if entity_count < min_entity_count:
                result.blocks_skipped += 1
                continue

            block_name = blk.name
            # Skip AutoCAD-internal auto-names by default. The user can
            # set `include_autonamed=True` if they actually want the
            # placeholders (rare; usually they're WBLOCK / XPLODE
            # artifacts with no semantic meaning).
            if not include_autonamed and _is_autonamed(block_name):
                result.blocks_skipped += 1
                continue
            asset_dir_name = _sanitize_for_dir(block_name)
            asset_dir = (
                target / "vendor" / vendor_name / category
                / dwg_stem_dir / asset_dir_name
            )
            asset_dir.mkdir(parents=True, exist_ok=True)

            width, depth = _block_size(blk)
            asset_id = asset_dir.relative_to(target).as_posix()
            tags = _tags_from(dwg_path.stem, block_name, category)

            _write_meta(
                asset_dir / "meta.toml",
                asset_id=asset_id,
                name=block_name,
                category=category,
                discipline=discipline,
                tags=tags,
                default_layer=default_layer,
                block_file=dwg_path,
                block_name=block_name,
                width=width,
                depth=depth,
                entity_count=entity_count,
                source_file=str(dwg_path),
            )
            result.asset_ids.append(asset_id)
            result.blocks_imported += 1

    log.info(
        "vendor_import_complete",
        source=str(source),
        files=result.files_scanned,
        blocks_imported=result.blocks_imported,
        blocks_skipped=result.blocks_skipped,
        files_failed=len(result.files_failed),
    )
    return result


def _tags_from(file_stem: str, block_name: str, category: str) -> list[str]:
    """Generate searchable tags from filename + block name + category.

    Splits both strings on non-alphanumerics, lowercases, dedupes. Words
    shorter than 3 chars are dropped (they're noise like 'a', 'of'). The
    category's path components are added so `tag=furniture` works without
    having to specify `category=furniture`.
    """
    raw = (file_stem + " " + block_name + " " + category).lower()
    parts = [p for p in re.split(r"[^a-z0-9]+", raw) if len(p) >= 3]
    # Stable order, deduped.
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _write_meta(
    meta_path: Path,
    *,
    asset_id: str,
    name: str,
    category: str,
    discipline: str,
    tags: list[str],
    default_layer: str,
    block_file: Path,
    block_name: str,
    width: float | None,
    depth: float | None,
    entity_count: int,
    source_file: str,
) -> None:
    """Hand-format the meta.toml for a vendor asset."""
    lines: list[str] = []
    lines.append(f'id = "{asset_id}"')
    lines.append(f'name = "{_escape_toml_str(name)}"')
    lines.append(f'category = "{category}"')
    lines.append(f'discipline = "{discipline}"')
    lines.append(f'tags = {_toml_string_list(tags)}')
    lines.append(f'default_layer = "{default_layer}"')
    lines.append('license = "vendor"  # third-party content; do not redistribute')
    lines.append('inserts_as = "block"')
    lines.append(f'block_name = "{_escape_toml_str(block_name)}"')
    lines.append('')
    if width is not None and depth is not None:
        lines.append('[size]')
        lines.append(f'width = {width:.3f}')
        lines.append(f'depth = {depth:.3f}')
        lines.append('# units unknown — vendor DWGs vary; downstream code')
        lines.append('# should consult $INSUNITS on the source drawing.')
        lines.append('units = "unknown"')
        lines.append('')
    lines.append('[files]')
    # Absolute path — the source DWG lives outside the asset-library tree.
    # Use forward slashes so the toml is shell-/JSON-safe.
    posix = str(block_file).replace("\\", "/")
    lines.append(f'block = "{posix}"')
    lines.append('')
    lines.append('[vendor]')
    lines.append(f'source_file = "{source_file.replace(chr(92), "/")}"')
    lines.append(f'entity_count = {entity_count}')
    lines.append('')
    meta_path.write_text("\n".join(lines), encoding="utf-8")


def _toml_string_list(items: Iterable[str]) -> str:
    return "[" + ", ".join(f'"{_escape_toml_str(i)}"' for i in items) + "]"


def _escape_toml_str(s: str) -> str:
    """Escape a TOML basic string per the spec.

    TOML basic strings require escaping of `\\`, `"`, and the ASCII
    control characters U+0000..U+001F (with named escapes for `\\b`,
    `\\t`, `\\n`, `\\f`, `\\r` and `\\uXXXX` for the rest). Without these,
    a vendor block name containing a newline or tab would produce
    invalid TOML.
    """
    # Backslash first so we don't double-escape later replacements.
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\b", "\\b").replace("\t", "\\t").replace("\n", "\\n")
    s = s.replace("\f", "\\f").replace("\r", "\\r")
    # Any remaining sub-0x20 char → \uXXXX.
    out = []
    for ch in s:
        if ch < " " and ch not in ("\\",):  # `\\` already escaped above
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)
