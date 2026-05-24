"""Per-drawing memory sidecar — `<drawing-stem>.claude.md`.

When Claude opens or indexes a drawing, it should know any project- or
drawing-specific context the architect carries in their head: "Type A
walls are 2×6 GWB both sides", "ceiling height is 9'-0\"", "egress
door is D03 — keep it 36 in. wide". Without this, every session starts
from zero.

The convention: a sibling file named `<drawing-stem>.claude.md` next to
each drawing. Plain Markdown. Loaded on demand, surfaced verbatim in
the response payload of any op that opens or inspects that drawing.

Examples:
    /project/
    ├── floor-plan-1.dwg
    ├── floor-plan-1.claude.md     ← sidecar for floor-plan-1
    ├── elevations.dwg
    └── (no sidecar for elevations — fine, sidecars are optional)

The sidecar is **not** the project-level config (`.acadproj.toml`) —
that's structured. The sidecar is free-form prose for the drawing.
"""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger()


# Cap the read size so a runaway sidecar doesn't dump 10 MB into the
# MCP response. The intended use is a few paragraphs.
_SIDECAR_MAX_BYTES = 64 * 1024


def sidecar_path(drawing_path: str | Path) -> Path:
    """Return the canonical sidecar path for a drawing."""
    p = Path(drawing_path).expanduser().resolve()
    return p.with_suffix(".claude.md")


def read_sidecar(drawing_path: str | Path) -> str | None:
    """Read `<drawing-stem>.claude.md` next to `drawing_path`.

    Returns the Markdown contents (truncated to 64 KB) or None when no
    sidecar exists. Surface failures as None — a missing or unreadable
    sidecar must never block a drawing-open op.
    """
    side = sidecar_path(drawing_path)
    if not side.exists():
        return None
    try:
        data = side.read_bytes()[:_SIDECAR_MAX_BYTES]
        return data.decode("utf-8", errors="replace")
    except Exception as ex:
        log.debug("sidecar_read_failed", path=str(side), error=str(ex))
        return None


def sidecar_summary(drawing_path: str | Path) -> dict | None:
    """Return a small dict describing the sidecar, or None when absent.

    Shape: `{path, size_bytes, excerpt}` where `excerpt` is the first
    1000 chars of Markdown. Use in op responses that wouldn't benefit
    from the full text (e.g. `project.info`).
    """
    side = sidecar_path(drawing_path)
    if not side.exists():
        return None
    try:
        size = side.stat().st_size
        text = side.read_bytes()[:1000].decode("utf-8", errors="replace")
        return {
            "path": str(side),
            "size_bytes": size,
            "excerpt": text,
            "truncated": size > 1000,
        }
    except Exception as ex:
        log.debug("sidecar_summary_failed", path=str(side), error=str(ex))
        return None
