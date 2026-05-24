"""Project model: `.acadproj.toml` declares a multi-drawing project.

Public surface:
    ProjectConfig, DrawingRef, load_project — config layer (project/config.py)
    DXFIndexer, init_db                     — indexer (project/index.py)
    project_info, list_drawings, list_layers, find_entities  — queries (project/query.py)
"""

from autocad_mcp.project.config import DrawingRef, ProjectConfig, load_project
from autocad_mcp.project.dwg_reader import (
    DWGConverterMissing,
    available_dwg_converters,
    read_drawing,
)
from autocad_mcp.project.index import DXFIndexer, init_db
from autocad_mcp.project.query import (
    find_block_inconsistencies,
    find_broken_xrefs,
    find_entities,
    find_linetype_inconsistencies,
    find_sheets_for_drawing,
    find_style_inconsistencies,
    list_blocks,
    list_dimstyles,
    list_drawings,
    list_layers,
    list_linetypes,
    list_sheets,
    list_textstyles,
    list_xrefs,
    project_info,
)
from autocad_mcp.project.sheetset import (
    Sheet,
    SheetSet,
    SheetSetBinaryUnsupported,
    read_sheetset,
)
from autocad_mcp.project.standards import (
    DimstyleRule,
    LayerRule,
    LayerStandard,
    Standard,
    TextstyleRule,
    aia_standard,
    check_drawing,
    check_project,
    load_standard_from_dxf,
)

__all__ = [
    "ProjectConfig",
    "DrawingRef",
    "load_project",
    "DXFIndexer",
    "init_db",
    "read_drawing",
    "available_dwg_converters",
    "DWGConverterMissing",
    "project_info",
    "list_drawings",
    "list_layers",
    "find_entities",
    "find_block_inconsistencies",
    "find_broken_xrefs",
    "find_linetype_inconsistencies",
    "find_sheets_for_drawing",
    "find_style_inconsistencies",
    "list_blocks",
    "list_dimstyles",
    "list_linetypes",
    "list_sheets",
    "list_textstyles",
    "list_xrefs",
    "Sheet",
    "SheetSet",
    "SheetSetBinaryUnsupported",
    "read_sheetset",
    "LayerStandard",
    "Standard",
    "LayerRule",
    "DimstyleRule",
    "TextstyleRule",
    "aia_standard",
    "load_standard_from_dxf",
    "check_drawing",
    "check_project",
]
