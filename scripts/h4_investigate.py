"""H4 investigation: does PostMessageW(WM_CHAR) actually reach the
command line on full AutoCAD 2027? Drives the COM backend to load the
LISP dispatcher first, then drives the file_ipc backend through a ping
to see whether the trigger works.

Run from a host where full AutoCAD is open. Not part of the test suite —
this is an investigation script that reports observations, not asserts.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

# Force COM to load the LISP, then file_ipc to ping it.
os.environ.pop("AUTOCAD_MCP_BACKEND", None)


async def main() -> None:
    from autocad_mcp.backends.com_backend import COMBackend
    from autocad_mcp.backends.file_ipc import FileIPCBackend, find_autocad_window
    from autocad_mcp.config import LISP_DIR

    print("=== H4 investigation ===")
    print(f"LISP_DIR: {LISP_DIR}")

    # 1. Confirm a window is visible.
    hwnd = find_autocad_window()
    print(f"\n[1] find_autocad_window() -> {hwnd}")
    if not hwnd:
        print("    No AutoCAD window detected. Aborting.")
        return

    # 2. Use COM to load mcp_dispatch.lsp into the running AutoCAD.
    print("\n[2] Loading mcp_dispatch.lsp via COM ...")
    com = COMBackend()
    init = await com.initialize()
    if not init.ok:
        print(f"    COM init failed: {init.error}")
        return
    print(f"    COM attached: {init.payload}")

    # Ensure a document exists.
    info = await com.drawing_info()
    if not info.ok:
        print("    No active doc — creating one.")
        r = await com.drawing_create()
        print(f"    drawing_create -> ok={r.ok}, payload={r.payload}")

    # SECURELOAD=0 to load an unsigned file without prompting.
    lisp_path = (LISP_DIR / "mcp_dispatch.lsp").as_posix()
    loader = (
        '(setvar "SECURELOAD" 0)'
        f'(load "{lisp_path}")'
    )
    r = await com.execute_lisp(loader)
    print(f"    execute_lisp(load) -> ok={r.ok}, error={r.error}")

    # Give AutoCAD a moment to register c:mcp-dispatch.
    time.sleep(1.0)

    # 3. Now try file_ipc against the same AutoCAD.
    print("\n[3] file_ipc.ping via PostMessageW ...")
    fipc = FileIPCBackend()
    init = await fipc.initialize()
    print(f"    initialize() -> ok={init.ok}, error={init.error}")

    if init.ok:
        # Even on init success the dispatch trigger may not reach the
        # command line — the ping inside initialize() uses the same trigger.
        # If init succeeded, ping worked; otherwise H4 still applies.
        print("    PING WORKED — H4 is no longer reproducible on this build.")
    else:
        # Investigate the failure mode.
        print("    PING TIMED OUT — H4 reproduces on this AutoCAD.")
        # Check if c:mcp-dispatch is at least loaded:
        probe = await com.execute_lisp(
            "(princ (if c:mcp-dispatch \"DISPATCH-DEFINED\" \"DISPATCH-MISSING\"))"
        )
        print(f"    c:mcp-dispatch loaded? -> {probe.payload}")


if __name__ == "__main__":
    asyncio.run(main())
