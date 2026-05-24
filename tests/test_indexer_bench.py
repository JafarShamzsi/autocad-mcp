"""Indexer benchmark — synthesize a 50-drawing project and time it.

Establishes a wall-clock baseline so a regression that doubles index
time (e.g., adding a per-entity I/O round-trip) shows up as a CI
failure. Threshold is intentionally lax — we care about order-of-
magnitude regressions, not microbenchmark precision.

Run alone:
    pytest tests/test_indexer_bench.py -v -s
"""

from __future__ import annotations

import time
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.arch import create_wall, setup_aia_layers
from autocad_mcp.project import DXFIndexer, load_project


# Tuning knobs. The defaults match the original audit's F4 ("synthetic
# 50-drawing project") and keep wall-clock under a few seconds even on
# modest hardware.
_NUM_DRAWINGS = 50
_ENTITIES_PER_DRAWING = 40

# Wall-clock threshold (seconds). On a 2024-era laptop the real number
# is ~0.5s; we set the failure bar at 10x that — anything beyond is a
# real regression. CI machines vary, hence the generous headroom.
_INDEX_THRESHOLD_SECONDS = 5.0


def _build_synthetic_project(root: Path) -> None:
    """Create N drawings + an .acadproj.toml under root."""
    setup = ezdxf.new("R2013", setup=True)
    setup_aia_layers(setup)  # seed the AIA layer set into the template doc

    for i in range(_NUM_DRAWINGS):
        doc = ezdxf.new("R2013", setup=True)
        setup_aia_layers(doc)
        msp = doc.modelspace()
        # ENTITIES_PER_DRAWING walls per file — exercises the entity loop.
        for j in range(_ENTITIES_PER_DRAWING):
            create_wall(msp, j * 10, 0, j * 10 + 8, 0, thickness=6)
        doc.saveas(str(root / f"plan-{i:02d}.dxf"))

    lines = ['[project]', 'name = "BenchProject"', '']
    for i in range(_NUM_DRAWINGS):
        lines += [
            "[[drawings]]",
            f'name = "plan-{i:02d}"',
            f'file = "plan-{i:02d}.dxf"',
            "",
        ]
    (root / ".acadproj.toml").write_text("\n".join(lines))


class TestIndexerBench:
    def test_fifty_drawings_under_threshold(self, tmp_path):
        """Index 50 synthetic drawings × 40 entities each in under 5s."""
        _build_synthetic_project(tmp_path)

        cfg = load_project(tmp_path)
        idx = DXFIndexer(tmp_path / ".acadproj.db")
        try:
            t0 = time.perf_counter()
            result = idx.index_project(cfg)
            elapsed = time.perf_counter() - t0
        finally:
            idx.close()

        # Sanity-check the indexer actually processed everything.
        assert result.indexed == _NUM_DRAWINGS, (
            f"Expected {_NUM_DRAWINGS} indexed; got {result.indexed} "
            f"(errors: {result.errors!r})"
        )
        assert result.skipped == 0

        # Wall-clock — the actual baseline value (printed below) is the
        # regression signal. The threshold catches order-of-magnitude
        # regressions, not microbenchmarks.
        print(
            f"\nIndexer baseline: {_NUM_DRAWINGS} drawings × "
            f"{_ENTITIES_PER_DRAWING} entities indexed in {elapsed:.3f}s"
        )
        assert elapsed < _INDEX_THRESHOLD_SECONDS, (
            f"Indexer regression: {elapsed:.3f}s for {_NUM_DRAWINGS}"
            f" drawings (threshold {_INDEX_THRESHOLD_SECONDS}s)"
        )
