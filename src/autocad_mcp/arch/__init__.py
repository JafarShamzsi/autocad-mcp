"""Architectural drafting module — AIA/NCS layer standard + composite primitives.

Backend-agnostic: operates on ezdxf documents directly so the same logic
can be reused by future COM/file_ipc backends.
"""

from autocad_mcp.arch.layers import AIA_LAYERS, AIALayer, setup_aia_layers
from autocad_mcp.arch.primitives import (
    create_beam,
    create_column,
    create_dimension_chain,
    create_door,
    create_elevation,
    create_roof_plan,
    create_room,
    create_section,
    create_slab,
    create_stair,
    create_wall,
    create_wall_with_openings,
    create_window,
)

__all__ = [
    "AIA_LAYERS",
    "AIALayer",
    "setup_aia_layers",
    "create_wall",
    "create_wall_with_openings",
    "create_door",
    "create_window",
    "create_room",
    "create_dimension_chain",
    "create_stair",
    "create_column",
    "create_slab",
    "create_beam",
    "create_roof_plan",
    "create_elevation",
    "create_section",
]
