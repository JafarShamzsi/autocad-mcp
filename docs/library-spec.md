# Asset library — data model and operations

The `library` MCP tool exposes three tiers of block reuse. From smallest
to richest:

| Tier | What it is | Where it lives |
|---|---|---|
| **Procedural** | 22 CC0 blocks defined in Python code. Imperial inches. | `src/autocad_mcp/arch/library/symbols.py` |
| **Raw external indexer** | A walk-the-tree indexer over any directory of `.dwg` / `.dxf` files. Discovers every block in every file; no metadata. | `src/autocad_mcp/project/block_library.py` |
| **Structured catalog** | A git-friendly tree of asset directories, each with its own `block.dxf` + `meta.toml`. Searchable by name, tags, category, dimensions. | `src/autocad_mcp/library/catalog.py` |

This document describes the **structured catalog** — the new layer. The
other two tiers continue to work unchanged.

## Filesystem layout

```
<root>/
├── library.toml                              # catalog config (optional)
├── furniture/
│   ├── seating/
│   │   ├── sofa-84-modern/
│   │   │   ├── meta.toml
│   │   │   ├── block.dxf
│   │   │   ├── thumb.png        (optional)
│   │   │   └── notes.md          (optional)
│   │   └── chair-dining/
│   │       └── …
│   ├── beds/
│   ├── tables/
│   ├── storage/
│   └── desks/
├── fixtures/
│   ├── plumbing/
│   ├── kitchen/
│   ├── lighting/
│   └── electrical/
├── doors/                                    # by width × type
├── windows/                                  # by W×H × type
├── building-types/                           # whole-building templates
├── materials/                                # hatch + finish schedule rows
├── details/                                  # reusable CAD details
└── title-blocks/
```

The directory structure isn't load-bearing — `meta.toml` declares the
asset's `category` explicitly, which can be anything. The structure is
just a convention; the search index works against `category` strings
regardless of where the file sits on disk.

Default root: `<repo>/asset-library/`. Override per-call via
`data={"root": "C:/my-library"}` on any `catalog_*` op.

## `library.toml` (root)

Optional. Hand-edited. The catalog runs fine without it.

```toml
[library]
name = "autocad-mcp starter library"
version = "0.1.0"
description = "CC0 starter assets for floor-plan drafting"

[default_layers]
# Override an asset's `default_layer` for a given discipline. Empty by
# default — assets carry their own default_layer in meta.toml.
P = "P-FIXT"
A = "A-FLOR-APPL"
```

## `meta.toml` (per asset)

Required fields: `name`, `category`. Everything else is optional but
recommended.

```toml
id          = "furniture/seating/sofa-84-modern"
name        = "Sofa — 84 in. three-seat"
category    = "furniture/seating"
discipline  = "I"                              # AIA code
tags        = ["sofa", "couch", "seating", "living-room", "furniture"]
default_layer = "I-FURN"
license     = "CC0-1.0"
inserts_as  = "block"                          # block | group | xref
block_name  = "SOFA-84-MODERN"                 # name in block.dxf

[size]
width  = 84
depth  = 36
height = 32
units  = "inches"

[files]
block      = "block.dxf"                       # required; relative to asset dir
thumbnail  = "thumb.png"                       # optional
```

### Field reference

| Field | Required | What it does |
|---|---|---|
| `id` | yes (defaults to relative path) | Canonical id. Stable across renames; clients reference assets by this. |
| `name` | yes | Display name. Searchable. |
| `category` | yes | Hierarchical string like `furniture/seating`. Used in `catalog_list` aggregates and as a `catalog_search` filter (matches the category or any child). |
| `discipline` | no | AIA discipline code (`A`, `S`, `M`, `E`, `P`, `I`, `L`, …). |
| `tags` | no | List of lowercase keywords. Searchable. |
| `default_layer` | no | Layer the asset should land on by default. `catalog_insert` creates the layer if needed. |
| `license` | no | SPDX-style identifier. `CC0-1.0` for the starter set. |
| `inserts_as` | no | `block` (default), `group`, or `xref`. Only `block` is implemented today. |
| `block_name` | no | Block name inside `block.dxf`. Defaults to the uppercased asset directory name. |
| `[size]` | no | `width`, `depth`, `height`, `units`. Used for `max_width` search filter + downstream code (clearances, room-layout helpers). |
| `[files]` | no | Filename overrides. `block` defaults to `block.dxf`, `thumbnail` defaults to no thumbnail. |
| `notes.md` (sidecar) | no | Free-form Markdown. First 200 chars are indexed as `notes_excerpt`. |

### Block-name convention

`block_name` in `meta.toml` must match the block-table name inside the
`block.dxf` / `block.dwg`. By default we use the uppercased asset directory
name with underscores → dashes:

| Asset directory | Block name |
|---|---|
| `sofa-84-modern` | `SOFA-84-MODERN` |
| `bed-queen` | `BED-QUEEN` |
| `toilet-standard` | `TOILET-STANDARD` |

`seed.py` follows this convention. If you author a `.dxf` / `.dwg` by hand
with a different internal name, set `block_name` explicitly in the meta file.

### `block.dwg` vs `block.dxf` — both ship

The starter library writes **both formats per asset** when a DWG converter
is available:

- **`block.dwg`** — the AutoCAD-native form. `meta.toml`'s `[files].block`
  points at this. What `catalog_insert` reads. What an architect would
  recognize.
- **`block.dxf`** — the byte-deterministic canonical copy. The seeder
  pins timestamps + GUIDs in the DXF (which is text) so re-seeding
  produces identical bytes; the DWG can't make the same promise
  (accoreconsole bakes binary metadata we can't post-process). Tests
  pin determinism against the DXF.

When no DWG converter is available, the seeder writes DXF only and
`[files].block` points at `block.dxf`. Catalog operations work either
way — `catalog_insert` reads whichever format `meta.toml` declares.

## SQLite cache

The catalog stores its index at `<root>/.acad-library.db`. Schema:
single `assets` table keyed by `id`, plus a `meta` table tracking the
schema version. Rebuilt from disk by `catalog_reindex`; no migrations —
the db is a derived cache.

The cache is **not** intended to be tracked in git. Add
`asset-library/.acad-library.db` to your `.gitignore` (the starter
`.gitignore` does this already).

## MCP operations

All operations live on the `library` MCP tool with the `catalog_*`
prefix. Each accepts an optional `root` param so the same Claude
session can work across multiple libraries.

| Operation | Data |
|---|---|
| `catalog_reindex` | `{root?}` — walk the tree, rebuild the cache. Returns counts + errors. |
| `catalog_list` | `{root?}` — category index + counts. |
| `catalog_stats` | `{root?}` — total assets, category count, library name + version, last-indexed timestamp. |
| `catalog_search` | `{query?, category?, discipline?, tag?, max_width?, limit?: 50, root?}` — keyword + filter search. `query` matches name + tags + notes excerpt; `max_width` returns only assets ≤ that width. |
| `catalog_get` | `{asset_id, root?}` — full asset record. |
| `catalog_insert` | `{asset_id, x, y, scale?, rotation?, layer?, root?}` — import the block + insert at (x,y). Uses the asset's `default_layer` unless `layer` overrides. |

## Search semantics

`catalog_search` uses a normalized `searchable_text` column on the
SQLite cache (lowercase concat of name + id + tags + notes excerpt) with
`LIKE %query%` matching. Filter args AND together.

- **Keyword fit:** good for "sofa", "queen bed", "36in". Works on partial
  matches: searching for "fri" hits "refrigerator".
- **Multi-word:** `query="modern sofa"` matches when **the whole phrase**
  appears in the searchable text. To match individual words separately,
  issue two searches or use the `tag` filter.
- **Semantic search:** deferred to a future optional install — once
  `pip install autocad-mcp[search]` ships, an FAISS-backed
  sentence-transformer index will sit alongside the LIKE search and
  rerank.

## Extensibility

Adding an asset is "create a directory + drop in two files":

```bash
mkdir -p asset-library/furniture/seating/armchair-leather/
$EDITOR  asset-library/furniture/seating/armchair-leather/meta.toml
cp my_chair.dxf  asset-library/furniture/seating/armchair-leather/block.dxf
# Then via MCP:
library(operation="catalog_reindex")
```

The `block.dxf` should contain exactly one block definition whose name
matches `block_name` (or the uppercased-dir-name convention). To
manufacture one from an existing complex DWG, open it in AutoCAD,
WBLOCK the geometry you want, and save with that block name as the file
stem.

## Procedural symbol bridge

The 22 procedural CC0 symbols (`PLMB-TOILET`, `FURN-SOFA`, etc.) are
also published as catalog assets so they're discoverable by the same
search interface. The seeder `python -m autocad_mcp.library.seed` is the
canonical way to (re-)generate this seed content — it produces a deterministic
set of files so re-running on the same procedural symbols produces zero
git diff.

## Tier-selection guide (for skills / agents)

When Claude needs an asset, the right tier depends on the request:

1. **Specific named procedural symbol** ("place a duplex outlet") →
   use `library.insert` with the procedural name. Cheapest.
2. **Catalog search by description** ("a 60-inch round dining table") →
   `library.catalog_search` then `catalog_insert` on the best hit. The
   common path; covers floor-plan-drafting.
3. **Wide hunt across vendor packs** ("any chair from the furniture
   library") → `library.search_external` against the raw indexer. Useful
   when you know the vendor pack but don't have `meta.toml` for it.
4. **Manual import from a specific DWG** ("import the title block from
   `firm-tblk.dwg`") → `block.list_from_file` + `block.import_from_file`.

The `architectural-drafting` skill recommends path (2) by default and
falls through to (1) only for procedural primitives.
