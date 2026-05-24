---
name: asset-library-browse
description: Explore the autocad-mcp asset library — list categories, search by name/tag/dimension, inspect a single asset, and drop it into the active drawing. Trigger when the user wants to know what blocks are available ("show me all 36-inch doors", "what cottages do we have", "list available furniture") or wants to insert from the library by description rather than by exact block name.
---

# Browsing the asset library

The autocad-mcp asset library is a structured catalog of pre-drawn blocks
sitting on disk. Each asset is a directory under `asset-library/` with a
`meta.toml` (size, tags, default layer) plus a `block.dxf`. Use this skill
when the user wants to **explore** the catalogue or **search by description**
instead of asking for a specific block name.

## When to use this skill

Trigger on any of:

- "what's in the library", "show me the library", "browse assets"
- "find me a [thing]", "do we have a [thing]", "what [thing]s exist"
- "list [category]", "show all furniture", "all bathroom fixtures"
- "drop a [thing] here", "insert a [thing] at X, Y"
- The conversation mentions placing furniture / fixtures / doors / windows
  and the user doesn't already know the exact block name.

Do **not** use this skill for:

- Drafting walls / rooms / dimensions (use `architectural-drafting` instead).
- P&IDs (use the `pid` tool directly).
- Importing one-off blocks from a specific external DWG (use
  `block.list_from_file` + `block.import_from_file` from the `block` tool).

## Workflow

The four catalog operations form a discovery → insert pipeline:

```
catalog_list   ─▶ "what categories exist"     (overview)
catalog_search ─▶ "find me X"                 (narrow down)
catalog_get    ─▶ "tell me about <id>"        (confirm before insert)
catalog_insert ─▶ "drop it at (x, y)"         (commit to drawing)
```

A typical exchange:

1. User: *"Place a queen bed in the master bedroom centered at (120, 80)."*
2. Call **`library(operation="catalog_search", data={"query": "queen bed"})`**.
3. The first hit is `furniture/beds/bed-queen` with `default_layer = "I-FURN"`.
4. Call **`library(operation="catalog_insert", data={"asset_id": "furniture/beds/bed-queen", "x": 120, "y": 80})`**.
5. Report: *"Placed Sofa — 84 in. three-seat at (120, 80) on layer I-FURN."*

You almost never need `catalog_get` in this loop — `catalog_search` already
returns all the fields you need (size, default_layer, file path). Use
`catalog_get` only when the user explicitly asks about a specific asset
("tell me about the sofa").

## Search recipes

The `query` argument matches against name + tags + notes (case-insensitive
substring). Filter args AND together. Useful patterns:

| Goal | Args |
|---|---|
| All sofas | `{"query": "sofa"}` |
| All bathroom fixtures | `{"category": "fixtures/plumbing"}` |
| Plumbing assets ≤ 24″ wide | `{"discipline": "P", "max_width": 24}` |
| Mid-century furniture | `{"tag": "mid-century"}` |
| Anything in the seating subtree | `{"category": "furniture/seating"}` |
| Doors 36″ wide | `{"category": "doors", "query": "36"}` |
| All starter content | no args (returns up to 50 by default) |

## Inserting

`catalog_insert` is two-phase internally: it imports the asset's block
definition from `block.dxf` into the active drawing, then issues an
INSERT at `(x, y)` on the asset's `default_layer`. Both phases must
succeed for the call to return `ok=true`.

Defaults: `scale=1.0`, `rotation=0.0`, `layer = asset.default_layer`.
Override `layer` when the user explicitly says "put it on layer X".
Imperial inches throughout — for metric drawings, set `scale` to convert
(e.g. `scale=25.4` to render a 1″-unit block as 25.4 mm).

## When the catalog is empty

If `catalog_list` returns no categories the user hasn't seeded the
library yet. Tell them to run:

```bash
.venv\Scripts\python.exe -m autocad_mcp.library.seed
```

This writes 22 starter CC0 assets covering furniture, plumbing, kitchen,
electrical, and annotation. After that, `library(operation="catalog_reindex")`
picks them up.

## When to reindex

The cache (`<root>/.acad-library.db`) is **only** rebuilt by
`catalog_reindex`. Run it after:

- Dropping a new asset directory into the tree.
- Editing a `meta.toml` in an existing directory.
- Switching to a different library root.

A search returning unexpected results when the user just added an asset
is almost always a missed reindex.

## Coexistence with the other library tiers

The asset library is one of three reuse mechanisms. Don't confuse them
in your response:

- **Procedural symbols** — `library.list / define / insert`. The 22 built-in
  blocks (`PLMB-TOILET`, `FURN-SOFA`, …). No file paths involved.
- **Asset catalog** — `library.catalog_*` (this skill). On-disk tree with
  `meta.toml` + `block.dxf` per asset. Searchable.
- **Raw external indexer** — `library.scan_external / search_external`. Walks a
  directory of vendor DWGs and indexes every block inside, no metadata.

When the user asks "what's available" without specifying a tier:
- If the catalog is non-empty, start with `catalog_search`.
- Mention the procedural symbols only if the search returns nothing useful.

## Pitfalls

- **Don't invent `asset_id`s.** Always call `catalog_search` or
  `catalog_list` first; use the exact id from the response.
- **Don't reindex on every search.** The cache reflects the disk state
  between explicit `catalog_reindex` calls — running it on every search
  defeats the cache's purpose.
- **Imperial defaults.** If the user is working in metric, either set
  `$INSUNITS` on the drawing first or pass an explicit `scale` to the
  insert.
- **License field.** Catalog assets carry a `license` value (CC0-1.0 for
  the starter set). Surface it if the user asks about redistribution.
