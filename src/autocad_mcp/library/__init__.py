"""Structured asset catalog for autocad-mcp.

The procedural symbol library (`autocad_mcp.arch.library`) defines ~22
CC0 blocks in code. The *asset catalog* is the next tier up: a
git-friendly filesystem layout where each asset is its own directory
containing a `block.dxf`, a `meta.toml` with searchable metadata, and
optional `thumb.png` + `notes.md`. Claude searches the catalog by name,
tags, category, or size; resolves an asset to its DXF; then asks the
backend to import the block + insert it.

Filesystem layout (configurable per project — defaults to
`<repo>/asset-library/`):

    asset-library/
    ├── library.toml                # catalog config (default layers, version)
    ├── furniture/seating/sofa-84-modern/
    │   ├── meta.toml
    │   ├── block.dxf
    │   └── thumb.png   (optional)
    ├── fixtures/plumbing/toilet-standard/
    │   ├── meta.toml
    │   └── block.dxf
    └── ...

See `docs/library-spec.md` for the full schema.
"""

from autocad_mcp.library.catalog import (
    Asset,
    AssetCatalog,
    LibraryConfig,
    default_library_root,
)

__all__ = ["Asset", "AssetCatalog", "LibraryConfig", "default_library_root"]
