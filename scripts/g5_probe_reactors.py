"""Probe what `vlr-acdb-reactor` returns + whether reactors actually attach."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


async def main() -> int:
    from autocad_mcp.backends.com_backend import COMBackend
    from autocad_mcp.config import LISP_DIR

    com = COMBackend()
    r = await com.initialize()
    if not r.ok:
        return 1

    await com.drawing_create()

    code = (
        '(setvar "SECURELOAD" 0)'
        f'(load "{(LISP_DIR / "mcp_reactors.lsp").as_posix()}")'
        '(setq probe-file (open "C:/Users/Axel/AppData/Local/Temp/acad_reactor_probe.txt" "w"))'
        '(write-line "Before attach:" probe-file)'
        '(write-line (strcat "  *mcp-reactors* = " (vl-princ-to-string *mcp-reactors*)) probe-file)'
        '(c:mcp-attach-reactors)'
        '(write-line "After attach:" probe-file)'
        '(write-line (strcat "  *mcp-reactors* = " (vl-princ-to-string *mcp-reactors*)) probe-file)'
        '(write-line (strcat "  vlr-types: " (vl-princ-to-string (mapcar (function vlr-type) *mcp-reactors*))) probe-file)'
        '(write-line (strcat "  vlr-added-p: " (vl-princ-to-string (mapcar (function vlr-added-p) *mcp-reactors*))) probe-file)'
        '(write-line (strcat "  vlr-reactions: " (vl-princ-to-string (mapcar (function vlr-reactions) *mcp-reactors*))) probe-file)'
        '(write-line (strcat "  vlr-current-reaction-name: " (vl-princ-to-string (vlr-current-reaction-name))) probe-file)'
        '(close probe-file)'
    )
    r = await com.execute_lisp(code)
    print(f"execute_lisp ok={r.ok}")
    time.sleep(1.5)

    p = Path(r"C:/Users/Axel/AppData/Local/Temp/acad_reactor_probe.txt")
    if p.exists():
        print(p.read_text())
        p.unlink()
    else:
        print("Probe file not written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
