"""Real-call smoke test for `drawing.batch`.

Runs accoreconsole against a copy of a real .dwg from the seeded asset
library, executes a benign LISP script (count entities, print to stdout),
and verifies the per-file result shape.

Requires:
  - accoreconsole.exe (full AutoCAD installed)
  - bin/dotnet/SheetSetReader.dll NOT required
  - asset-library/.../block.dwg present (run `python -m autocad_mcp.library.seed`)
  - AUTOCAD_MCP_ENABLE_LISP_EXEC=1 in the env

Run with:
  $env:AUTOCAD_MCP_ENABLE_LISP_EXEC = "1"
  .venv\Scripts\python.exe scripts\smoke_batch.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def main() -> int:
    os.environ.setdefault("AUTOCAD_MCP_ENABLE_LISP_EXEC", "1")
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")

    repo = Path(__file__).resolve().parent.parent
    src_dwg = repo / "asset-library/furniture/seating/sofa-84-modern/block.dwg"
    if not src_dwg.exists():
        print(f"FAIL: seeded asset missing — run library.seed first")
        return 2

    # Copy to temp so the batch save doesn't churn git status.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        f1 = td / "a.dwg"
        f2 = td / "b.dwg"
        shutil.copy(src_dwg, f1)
        shutil.copy(src_dwg, f2)

        script = '(princ (strcat "\\nentities=" (itoa (sslength (ssget "_X")))))'

        from autocad_mcp.server import drawing as drawing_tool
        fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

        print(f"Running drawing.batch on 2 copies of {src_dwg.name}...")
        result = await fn(operation="batch", data={
            "paths": [str(f1), str(f2)],
            "script": script,
            "save": False,
            "timeout_per_file": 45,
        })
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)

        if not parsed.get("ok"):
            print(f"FAIL: {parsed.get('error') or parsed}")
            return 3

        p = parsed["payload"]
        print(f"\nfiles_total:  {p['files_total']}")
        print(f"files_ok:     {p['files_ok']}")
        print(f"files_failed: {p['files_failed']}")
        print(f"duration:     {p['total_duration_s']}s")
        for f in p["per_file"]:
            print(f"  {Path(f['path']).name}: ok={f['ok']} dt={f['duration_s']}s rc={f['returncode']}")
            if f.get("stdout_tail") and "entities=" in (f["stdout_tail"] or ""):
                # Find the line we printed.
                for line in f["stdout_tail"].splitlines():
                    if "entities=" in line:
                        print(f"    {line.strip()}")
                        break
        if p["files_failed"] > 0:
            return 4
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
