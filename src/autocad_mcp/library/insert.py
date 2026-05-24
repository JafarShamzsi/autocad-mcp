"""Compose `catalog asset → import + insert` against any backend.

The asset catalog stores `meta.toml` + `block.dxf` for each asset. To
land an asset in the active drawing we need two steps:

1. Make sure the block definition exists in the active drawing's block
   table — call `backend.block_import_from_file` against the asset's
   `block.dxf`. This is idempotent on the existing definition.
2. Insert one instance at the requested point with the asset's default
   layer (or the caller's override).

The block name we ask AutoCAD to import is either the asset's explicit
`block_name` from `meta.toml`, or the stem of the asset id (last path
component, uppercased) by convention. That convention lines up with how
`seed.py` writes the starter assets — each block.dxf contains exactly
one definition whose name equals the asset's directory name.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.library.catalog import Asset

log = structlog.get_logger()


def asset_block_name(asset: Asset) -> str:
    """Block-table name the asset's `block.dxf` is expected to expose.

    Explicit `block_name` in `meta.toml` wins; otherwise we use the last
    path component of the asset id, uppercased and with `/` swapped for
    `-`. Matches the convention `seed.py` uses when authoring assets.
    """
    if asset.block_name:
        return asset.block_name
    tail = asset.id.rsplit("/", 1)[-1]
    return tail.upper().replace("_", "-")


async def insert_asset(
    backend,
    asset: Asset,
    x: float,
    y: float,
    *,
    scale: float = 1.0,
    rotation: float = 0.0,
    layer: str | None = None,
) -> CommandResult:
    """Materialize `asset` in the active drawing at (x, y).

    Returns a CommandResult whose payload combines the import-step result
    with the insert-step result so the caller can see both phases. Errors
    on either step surface with their original category.
    """
    if asset.block_file is None:
        return CommandResult(
            ok=False,
            error=f"Asset {asset.id} has no block.dxf",
            category="not_found",
        )

    block_name = asset_block_name(asset)

    # Step 1 — import the block definition. Skip-if-exists is the
    # backend's contract; calling it on every insert is cheap.
    import_result = await backend.block_import_from_file(
        str(asset.block_file), names=[block_name],
    )
    if not import_result.ok:
        return CommandResult(
            ok=False,
            error=f"Asset import failed: {import_result.error}",
            category=import_result.category or "internal_error",
        )

    # Step 2 — insert on the asset's default layer (or the override).
    # `block_insert` doesn't take a layer arg on any backend, so the INSERT
    # entity lands on whatever AutoCAD's current layer is. To honor the
    # asset's `default_layer`, we (a) create the layer if absent,
    # (b) set it current, (c) insert, (d) leave it as the current layer
    # since restoring requires a state-roundtrip that the catalog ops
    # don't need to bother with.
    target_layer = layer or asset.default_layer
    if target_layer:
        try:
            await backend.layer_create(target_layer)
        except Exception as ex:
            # Layer create is best-effort — already-existing is fine.
            log.debug(
                "asset_insert_layer_create_skipped",
                layer=target_layer, error=str(ex),
            )
        try:
            set_result = await backend.layer_set_current(target_layer)
            if not set_result.ok:
                log.debug(
                    "asset_insert_layer_set_current_failed",
                    layer=target_layer, error=set_result.error,
                )
        except Exception as ex:
            log.debug(
                "asset_insert_layer_set_current_exception",
                layer=target_layer, error=str(ex),
            )

    insert_result = await backend.block_insert(
        block_name, x, y, scale, rotation,
    )
    if not insert_result.ok:
        return CommandResult(
            ok=False,
            error=f"Asset insert failed: {insert_result.error}",
            category=insert_result.category or "internal_error",
        )

    payload = {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "block_name": block_name,
        "block_file": str(asset.block_file),
        "x": float(x),
        "y": float(y),
        "scale": float(scale),
        "rotation": float(rotation),
        "layer": target_layer,
        "import": import_result.payload,
        "insert": insert_result.payload,
    }
    return CommandResult(ok=True, payload=payload)
