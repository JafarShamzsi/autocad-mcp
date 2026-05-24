"""Smoke test for pid_tools.lsp on live AutoCAD.

Mirrors the G5 reactor smoke test pattern: load both LISP files, drive
each `c:` function via COM `execute_lisp`, then check via COM that the
drawing actually got the entities we expect. Catches arg-count bugs in
the `(command ...)` sequences (`_.-LAYER _M name _C color name ""`,
`_TEXT _J _M ...`, etc.) that the static tests can't reach.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


async def main() -> int:
    from autocad_mcp.backends.com_backend import COMBackend
    from autocad_mcp.config import LISP_DIR

    com = COMBackend()
    init = await com.initialize()
    if not init.ok:
        print(f"FAIL: COM init: {init.error}")
        return 1

    print(f"COM attached: {init.payload['version']}")
    await com.drawing_create()

    # Load dispatch + pid_tools.
    dispatch_path = (LISP_DIR / "mcp_dispatch.lsp").as_posix()
    pid_path = (LISP_DIR / "pid_tools.lsp").as_posix()
    loader = (
        '(setvar "SECURELOAD" 0)'
        f'(load "{dispatch_path}")'
        f'(load "{pid_path}")'
    )
    r = await com.execute_lisp(loader)
    if not r.ok:
        print(f"FAIL: load LISP: {r.error}")
        return 1
    time.sleep(1.0)

    print("\n[1] setup-pid-layers")
    r = await com.execute_lisp("(c:setup-pid-layers)")
    time.sleep(0.8)
    if not r.ok:
        print(f"  FAIL: {r.error}")
        return 1

    # Check that the 7 PID layers exist.
    info = await com.drawing_info()
    layers = info.payload.get("layers", [])
    expected_layers = {
        "PID-EQUIPMENT", "PID-PROCESS-PIPING", "PID-UTILITY-PIPING",
        "PID-INSTRUMENTS", "PID-ELECTRICAL", "PID-ANNOTATION", "PID-VALVES",
    }
    missing = expected_layers - set(layers)
    if missing:
        print(f"  FAIL: missing PID layers: {missing}")
        return 1
    print(f"  OK: all 7 PID layers present")

    # Helper: run a c: function, time.sleep, snapshot entity count delta.
    async def run_and_count(label: str, code: str) -> int:
        before = (await com.entity_count()).payload["count"]
        r = await com.execute_lisp(code)
        time.sleep(0.6)
        after = (await com.entity_count()).payload["count"]
        delta = after - before
        if not r.ok:
            print(f"  FAIL ({label}): {r.error}")
            return -1
        if delta <= 0:
            print(f"  FAIL ({label}): no new entities added")
            return -1
        print(f"  OK ({label}): +{delta} entities")
        return delta

    print("\n[2] draw-process-line")
    if await run_and_count("draw-process-line",
                           "(c:draw-process-line 0 0 100 0)") < 0:
        return 1

    print("\n[3] connect-equipment")
    if await run_and_count("connect-equipment",
                           "(c:connect-equipment 0 50 100 80)") < 0:
        return 1

    print("\n[4] add-flow-arrow")
    if await run_and_count("add-flow-arrow",
                           "(c:add-flow-arrow 50 0 0.0)") < 0:
        return 1

    print("\n[5] add-equipment-tag")
    if await run_and_count("add-equipment-tag",
                           '(c:add-equipment-tag 10 10 "E-101" "Pump #1")') < 0:
        return 1

    print("\n[6] add-line-number")
    if await run_and_count("add-line-number",
                           '(c:add-line-number 50 20 "L-100" "CS-150")') < 0:
        return 1

    print("\n[7] insert-pid-block (placeholder, no CTO)")
    if await run_and_count("insert-pid-block",
                           '(c:insert-pid-block "EQUIPMENT" "TANK-VERTICAL-OPEN" 200 0 1.0 0.0)') < 0:
        return 1

    print("\n[8] insert-valve-on-line")
    if await run_and_count("insert-valve-on-line",
                           '(c:insert-valve-on-line 150 50 "GATE" 0.0)') < 0:
        return 1

    print("\n[9] insert-instrument")
    if await run_and_count("insert-instrument",
                           '(c:insert-instrument 200 50 "PI" 0.0)') < 0:
        return 1

    print("\n[10] insert-pump")
    if await run_and_count("insert-pump",
                           '(c:insert-pump 250 50 "CENTRIF" 0.0)') < 0:
        return 1

    print("\n[11] insert-tank")
    if await run_and_count("insert-tank",
                           '(c:insert-tank 300 50 "VERTICAL" 1.0)') < 0:
        return 1

    print("\nSUCCESS: all 11 pid_tools.lsp functions produce entities on AutoCAD 2027.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
