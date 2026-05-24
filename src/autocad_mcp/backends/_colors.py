"""Shared color-name → AutoCAD ACI mapping.

The COM and ezdxf backends used to ship near-identical copies of this table.
Centralising it here means adding a new colour name (e.g. "orange": 30)
applies to every backend automatically and prevents the kind of silent
divergence the audit flagged.
"""

from __future__ import annotations


# AutoCAD Color Index (ACI) lookup. Lowercased keys; consumers must lowercase
# the caller's input before checking. Index 7 (white-on-dark / black-on-light)
# is the safe default for unknown names.
_COLOR_MAP: dict[str, int] = {
    "red": 1,
    "yellow": 2,
    "green": 3,
    "cyan": 4,
    "blue": 5,
    "magenta": 6,
    "white": 7,
    "black": 7,
    "grey": 8,
    "gray": 8,
}


def color_to_aci(color: str | int | None) -> int:
    """Resolve a colour name or ACI int to an ACI int.

    Returns 7 (white/black) for `None` or unrecognised string inputs — same
    behaviour both backends previously had.
    """
    if color is None:
        return 7
    if isinstance(color, int):
        return color
    return _COLOR_MAP.get(str(color).strip().lower(), 7)
