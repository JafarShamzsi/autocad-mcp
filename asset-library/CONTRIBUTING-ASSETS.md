# Adding assets to the library

This directory is the autocad-mcp asset catalog. Each asset is one
directory containing a `block.dxf` plus a `meta.toml`. Drop new assets
in following the patterns below, then run `library.catalog_reindex` to
update the search cache.

## Minimum viable asset (5 minutes)

1. **Pick a category and name.** Categories live under top-level
   directories: `furniture/`, `fixtures/`, `doors/`, `windows/`,
   `building-types/`, `materials/`, `details/`, `title-blocks/`.
   Asset directory names are kebab-case (`sofa-84-modern`, not
   `Sofa_84_Modern`).

2. **Author the `block.dxf`.** The DXF must contain **exactly one block
   definition** whose name matches the asset directory name uppercased
   with underscores → dashes:

   | Asset dir | Block name |
   |---|---|
   | `sofa-84-modern` | `SOFA-84-MODERN` |
   | `door-36-single` | `DOOR-36-SINGLE` |

   Easiest path: open AutoCAD, draw the geometry on layer "0", run
   `WBLOCK`, save with the right filename. Or build programmatically
   via ezdxf — see `src/autocad_mcp/library/seed.py` for the pattern.

3. **Write `meta.toml`.** Minimum:

   ```toml
   name = "Sofa — 84 in. three-seat"
   category = "furniture/seating"
   tags = ["sofa", "couch", "modern"]
   default_layer = "I-FURN"

   [size]
   width = 84
   depth = 36
   units = "inches"

   [files]
   block = "block.dxf"
   ```

4. **Reindex.** From an MCP client:

   ```
   library(operation="catalog_reindex")
   ```

   Or directly:

   ```bash
   .venv\Scripts\python.exe -c "from autocad_mcp.library.catalog import AssetCatalog, default_library_root; AssetCatalog(default_library_root()).reindex()"
   ```

## Full schema

See `docs/library-spec.md` for every field, including the optional
`block_name`, `thumbnail`, `notes.md` sidecar, and the `[default_layers]`
discipline override mechanism in `library.toml`.

## Importing a vendor pack (bulk)

If you have an existing directory of vendor DWGs (e.g. a `furniture and
details/` dump downloaded from a CAD-blocks site), use the **vendor
importer** to turn every block inside every DWG into a catalog asset
without copying the geometry:

```python
library(operation="vendor_import", data={
    "source": "C:/path/to/furniture and details",
    "vendor_name": "furniture-and-details",     # used in asset ids
    "min_entity_count": 3,                       # skip noise (default 3)
    "include_autonamed": false,                  # skip A$* placeholders (default)
})
```

What happens:

- Each `.dwg` is read once via `accoreconsole` / ODA File Converter.
- Every non-anonymous, non-XREF, non-auto-named block with ≥`min_entity_count`
  entities becomes a catalog asset.
- Asset id has the shape `vendor/<vendor_name>/<inferred-category>/<dwg-stem>/<block-sanitized>`.
- `meta.toml` points at the **original** DWG path (no geometry duplication).
- Category is inferred from the source filename (`bedroom_*.dwg` → `furniture/bedroom`,
  `chair*.dwg` → `furniture/seating`, etc.). Tags from filename + block name.
- License is set to `vendor` to signal "third-party — do not redistribute".
- Output lands under `asset-library/vendor/<vendor_name>/...` which the
  starter `.gitignore` excludes.

**Cost:** roughly 3 seconds per source DWG (accoreconsole shell-out).
A directory of 88 DWGs takes ~5 minutes.

**Reasonable expectations on a typical vendor pack:**

| Source size | Real-named blocks (kept) | Auto-named blocks (skipped) |
|---|---|---|
| 50 MB / 88 DWGs (our test set) | ~610 | ~810 (`A$*` placeholders) |

The auto-named blocks are AutoCAD-generated placeholders from
WBLOCK / XPLODE operations — they don't represent meaningful objects.
Pass `include_autonamed=true` if you specifically want them.

After import, `library(operation="catalog_search")` returns vendor
assets alongside the procedural starter set; `catalog_insert` lands
them in the active drawing on their inferred default layer.

## Licensing — read this before committing assets to git

The starter content shipped in this repo is **CC0-1.0** (public domain).
Mark each asset's license explicitly in `meta.toml`:

```toml
license = "CC0-1.0"
```

If you're contributing assets sourced from manufacturer CAD downloads
(Kohler, Toto, Moen, Steelcase, Knoll, etc.) — **don't** commit them to
this repo without reviewing the source's redistribution terms. Most
manufacturer libraries are licensed for "use in your projects" but
**not** "redistribute as part of a third-party library". Safe options:

1. **Keep manufacturer assets out of git.** Store them locally under
   `asset-library/vendor/...`, add the path to `.gitignore`, document
   where to download them.
2. **Bundle redistributable-licensed content only.** Public-domain
   (CC0), CC-BY (with attribution preserved), or content authored by
   you with explicit "license to redistribute" granted.
3. **Generate procedurally.** The `seed.py` pattern — write Python that
   emits clean CC0 geometry — sidesteps licensing entirely. Slower
   per-asset but legally bulletproof.

## Suggested categories (not enforced)

The category string in `meta.toml` is freeform. These are the
conventions used by the starter set; follow or deviate as needed.

| Top-level | Subcategories | Typical assets |
|---|---|---|
| `furniture` | `seating`, `beds`, `tables`, `storage`, `desks` | sofas, chairs, beds, dressers |
| `fixtures` | `plumbing`, `kitchen`, `electrical`, `lighting` | toilets, sinks, ranges, outlets |
| `doors` | `single-{w}`, `double-{w}`, `sliding-{w}`, `pocket-{w}` | 30″ single, 60″ double, … |
| `windows` | `single-hung-{wxh}`, `slider-{wxh}`, `casement-{wxh}` | 36″×48″ slider, … |
| `building-types` | `cottage-{sf}`, `adu-{sf}`, `townhouse-{stories}story` | whole-building templates |
| `materials` | `flooring`, `wallcoverings`, `roofing` | hatch patterns + finish schedule entries |
| `details` | `walls/2x4-gwb-batt`, `stairs`, `ada` | reusable wall sections, ADA clearances |
| `title-blocks` | `tblk-{wxh}` | sheet borders + title blocks |
| `annotation` | `orientation`, `scale`, `stairs` | north arrows, scale bars, markers |
