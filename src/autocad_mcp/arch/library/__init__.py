"""Procedural symbol library: AEC blocks defined as data, not external files.

`SYMBOL_LIBRARY` maps a block name → list of entity specs (dicts). The
`apply_to_ezdxf_block` / `apply_to_com_block` helpers walk a spec and emit
the right primitive calls on the target block. New symbols are added by
appending to SYMBOL_LIBRARY; no asset files to ship.
"""

from autocad_mcp.arch.library.apply import (
    apply_to_com_block,
    apply_to_ezdxf_block,
    serialize_spec_for_lisp,
)
from autocad_mcp.arch.library.symbols import SYMBOL_LIBRARY, symbol_names

__all__ = [
    "SYMBOL_LIBRARY",
    "symbol_names",
    "apply_to_ezdxf_block",
    "apply_to_com_block",
    "serialize_spec_for_lisp",
]
