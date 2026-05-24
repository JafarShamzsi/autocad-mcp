"""Live smoke test for the SheetSetReader .NET extension.

Drives a running AutoCAD via the COM backend:
1. Ensures a drawing is open (creates a fresh new file if the Start
   tab is active — NETLOAD needs a Document context).
2. NETLOADs `bin/dotnet/SheetSetReader.dll`.
3. Calls MCPREADDST against tests/fixtures/IRD_Addition.dst and a
   temp output path.
4. Reads the JSON, prints a one-line summary, exits 0 on success.

Prerequisite: AutoCAD 2027 running. The DLL must be built first
(`dotnet build -c Release dotnet/SheetSetReader/SheetSetReader.csproj`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DLL = REPO / "bin" / "dotnet" / "SheetSetReader.dll"
DST = REPO / "tests" / "fixtures" / "IRD_Addition.dst"


async def main() -> int:
    if not DLL.exists():
        print(f"DLL not found: {DLL}")
        print("Build it first: dotnet build -c Release dotnet/SheetSetReader/SheetSetReader.csproj")
        return 1
    if not DST.exists():
        print(f"Fixture missing: {DST}")
        return 1

    # Make 'autocad_mcp' importable from the repo's src/ layout.
    sys.path.insert(0, str(REPO / "src"))
    from autocad_mcp.backends.com_backend import COMBackend

    com = COMBackend()
    init = await com.initialize()
    if not init.ok:
        print(f"COM backend init failed: {init.error}")
        return 1

    # Ensure a drawing is open. Start tab can't host SendCommand.
    status = await com.status()
    print(f"Status before: {status.payload}")
    if not status.payload.get("has_document"):
        print("No drawing open — creating one for the smoke test.")
        cr = await com.drawing_create("acad.dwt")
        print(f"  drawing_create: ok={cr.ok} payload={cr.payload!r}")
        if not cr.ok:
            print(f"Could not create scratch drawing: {cr.error}")
            return 1
        status = await com.status()
        print(f"Status after create: {status.payload}")

    out_path = Path(os.environ.get("TEMP") or ".") / "mcp_sheetset_out.json"
    if out_path.exists():
        out_path.unlink()

    # NETLOAD — the dispatcher expects the DLL path with forward slashes.
    netload_path = str(DLL).replace("\\", "/")
    netload_cmd = f'(command "_NETLOAD" "{netload_path}") '
    r = await com.execute_lisp(netload_cmd)
    print(f"NETLOAD result: ok={r.ok} payload={r.payload!r}")
    if not r.ok:
        print(f"NETLOAD failed: {r.error}")
        return 1

    # Invoke MCPREADDST. Each prompted argument becomes its own (command) arg.
    dst_path = str(DST).replace("\\", "/")
    out_str = str(out_path).replace("\\", "/")
    read_cmd = f'(command "MCPREADDST" "{dst_path}" "{out_str}") '
    r2 = await com.execute_lisp(read_cmd)
    print(f"MCPREADDST result: ok={r2.ok} payload={r2.payload!r}")

    # Wait for the output file (atomic rename — should appear immediately
    # after the command returns, but allow a brief grace period).
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if out_path.exists():
            break
        time.sleep(0.1)

    if not out_path.exists():
        print(f"Output file never appeared at {out_path}")
        return 1

    data = json.loads(out_path.read_text(encoding="utf-8"))
    print(f"JSON head: {json.dumps(data, indent=2)[:500]}")
    if not data.get("ok"):
        print(f"Reader returned error: {data.get('error')}")
        return 1

    n = len(data.get("sheets", []))
    print(f"Sheets parsed: {n}; sheet set name: {data.get('name')!r}")
    return 0 if n > 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
