"""Smoke test for library_tools.lsp on live AutoCAD.

Same shape as smoke_pid_tools.py: load the LISP files, drive
`mcp-cmd-library-define` against every shipping symbol, then read the
BLOCK table back via COM to confirm each block has the expected entity
count. Catches `_.-BLOCK` / `_.PLINE` / `_.TEXT` command-sequence bugs
the unit tests can't reach.

Requires AutoCAD running. The COM backend's `execute_lisp` is the
authoritative dispatcher path on this machine (file_ipc's PostMessage
trigger is unreliable against full AutoCAD 2027 — see audit-2026-05-12
H4). LT users can run an equivalent in-AutoCAD manually:

    Command: APPLOAD
    Pick: mcp_dispatch.lsp (which auto-loads library_tools.lsp)
    Command: (mcp-cmd-library-define '(("name" . "PLMB-TOILET") ...))
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


async def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    from autocad_mcp.arch.library import SYMBOL_LIBRARY, serialize_spec_for_lisp
    from autocad_mcp.backends.com_backend import COMBackend
    from autocad_mcp.config import LISP_DIR

    com = COMBackend()
    init = await com.initialize()
    if not init.ok:
        print(f"FAIL: COM init: {init.error}")
        return 1
    print(f"COM attached: {init.payload['version']}")

    status = await com.status()
    if not status.payload.get("has_document"):
        cr = await com.drawing_create()
        if not cr.ok:
            print(f"FAIL: drawing_create: {cr.error}")
            return 1
        print(f"Created scratch drawing: {(await com.status()).payload['document_name']}")

    # Load dispatch + library_tools. (mcp_dispatch.lsp auto-loads
    # library_tools.lsp if it's on the support path — but explicit-load
    # is more reliable when running ad-hoc.)
    dispatch_path = (LISP_DIR / "mcp_dispatch.lsp").as_posix()
    library_path = (LISP_DIR / "library_tools.lsp").as_posix()
    loader = (
        '(setvar "SECURELOAD" 0)'
        f'(load "{dispatch_path}")'
        f'(load "{library_path}")'
    )
    r = await com.execute_lisp(loader)
    if not r.ok:
        print(f"FAIL: load LISP: {r.error}")
        return 1
    time.sleep(1.0)

    # Confirm the library renderer is live in the LISP environment.
    r = await com.execute_lisp(
        '(if *mcp-library-loaded* (princ "library-loaded-ok") (princ "library-NOT-loaded"))'
    )
    if not r.ok:
        print(f"FAIL: loaded-flag probe: {r.error}")
        return 1
    time.sleep(0.3)

    # `execute_lisp` runs (c:mcp-dispatch)'s commands but doesn't return
    # the LISP cons-cell value — we need a separate COM probe to verify
    # the blocks actually landed in the BLOCK table.
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app = win32com.client.GetActiveObject("AutoCAD.Application")
    doc = app.ActiveDocument

    failures: list[str] = []
    for name, spec in SYMBOL_LIBRARY.items():
        expected_count = len(spec)
        entities_str = serialize_spec_for_lisp(spec)
        # Re-encode the params JSON so it's a valid LISP string literal:
        # escape `\` and `"`. The LISP-side parser (`mcp-json-get-string`)
        # handles JSON escape codes once we get there.
        params_json = json.dumps({"name": name, "entities_str": entities_str})
        params_lisp = params_json.replace("\\", "\\\\").replace('"', '\\"')
        cmd = f'(mcp-cmd-library-define "{params_lisp}")'
        print(f"\n[ {name} ] expected entities: {expected_count}")
        r = await com.execute_lisp(cmd)
        if not r.ok:
            print(f"  FAIL: dispatch error: {r.error}")
            failures.append(name)
            continue
        time.sleep(0.4)

        # COM probe: does the block now exist, and does its entity count
        # match the spec? (The block contains 1 entity per spec entry —
        # the renderer emits one primitive call per entity.)
        try:
            blk = doc.Blocks.Item(name)
        except Exception:
            blk = None
        if blk is None:
            print(f"  FAIL: block {name!r} not in BLOCK table")
            failures.append(name)
            continue

        actual = int(blk.Count)
        if actual != expected_count:
            print(f"  FAIL: entity count mismatch — expected {expected_count}, got {actual}")
            failures.append(name)
            continue
        print(f"  ok: block defined with {actual} entities")

    print(f"\nSummary: {len(SYMBOL_LIBRARY) - len(failures)}/{len(SYMBOL_LIBRARY)} symbols passed")
    if failures:
        print(f"Failures: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
