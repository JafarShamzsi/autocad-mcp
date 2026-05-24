"""Probe what `(getvar "DATE")` actually returns on this AutoCAD."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


async def main() -> int:
    from autocad_mcp.backends.com_backend import COMBackend

    com = COMBackend()
    r = await com.initialize()
    if not r.ok:
        print(f"COM init failed: {r.error}")
        return 1

    # Use the LISP event log to surface the value (princ doesn't come back
    # via SendCommand cleanly).
    code = (
        '(setq tmp (open "C:/Users/Axel/AppData/Local/Temp/acad_date_probe.txt" "w"))'
        '(write-line (rtos (getvar "DATE") 2 6) tmp)'
        '(write-line (rtos (getvar "TDCREATE") 2 6) tmp)'
        '(close tmp)'
    )
    r = await com.execute_lisp(code)
    print(f"execute_lisp ok={r.ok}")
    time.sleep(0.5)

    probe = Path(r"C:/Users/Axel/AppData/Local/Temp/acad_date_probe.txt")
    if probe.exists():
        for i, line in enumerate(probe.read_text().splitlines()):
            print(f"  line {i}: {line}")
        probe.unlink()
    else:
        print("probe file not written")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
