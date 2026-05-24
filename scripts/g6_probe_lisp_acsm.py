"""Probe AcSmSheetSetMgr from inside AutoCAD's process via LISP.

Tries arxload of the Sheet Set ARX, then vlax-create-object. Multi-line
LISP via SendCommand breaks when newlines are interpreted as command
terminators — keeping all code on one logical line.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


async def main() -> int:
    from autocad_mcp.backends.com_backend import COMBackend

    com = COMBackend()
    await com.initialize()

    out = Path(r"C:\Users\Axel\AppData\Local\Temp\acad_acsm_probe.txt")
    try:
        if out.exists():
            out.unlink()
    except PermissionError:
        time.sleep(0.5)
        try:
            out.unlink()
        except PermissionError:
            pass

    # Single-expression LISP — multi-line via SendCommand is fragile.
    code = (
        '(progn'
        f' (setq out (open "{out.as_posix()}" "w"))'
        ' (write-line (strcat "arxload AcSmNav: " (vl-princ-to-string (vl-catch-all-apply (function arxload) (list "AcSmNav.arx")))) out)'
        ' (write-line (strcat "arxload AcSmComponents: " (vl-princ-to-string (vl-catch-all-apply (function arxload) (list "C:/Program Files/Autodesk/AutoCAD 2027/AcSmComponents.dll")))) out)'
        ' (setq mgr (vl-catch-all-apply (function vlax-create-object) (list "AcSmComponents.AcSmSheetSetMgr.26")))'
        ' (write-line (strcat "create-object: " (vl-princ-to-string mgr)) out)'
        ' (if (vl-catch-all-error-p mgr)'
        '   (progn (write-line "  caught error" out) (write-line (strcat "  msg: " (vl-catch-all-error-message mgr)) out))'
        '   (progn'
        '     (write-line (strcat "  type: " (vl-princ-to-string (type mgr))) out)'
        '     (setq db (vl-catch-all-apply (function vlax-invoke) (list mgr (quote OpenDatabase) "C:/Users/Axel/Developer/autocad-mcp-main/tests/fixtures/IRD_Addition.dst" :vlax-true)))'
        '     (write-line (strcat "OpenDatabase: " (vl-princ-to-string db)) out)'
        '     (if (vl-catch-all-error-p db)'
        '       (write-line (strcat "  err: " (vl-catch-all-error-message db)) out)'
        '       (progn'
        '         (setq ss (vlax-invoke db (quote GetSheetSet)))'
        '         (write-line (strcat "  name: " (vlax-invoke ss (quote GetName))) out)))'
        '     (vlax-release-object mgr)))'
        ' (close out)'
        ')'
    )
    r = await com.execute_lisp(code)
    print(f"execute_lisp ok={r.ok}")
    time.sleep(2.0)

    if out.exists():
        print("Probe output:")
        print(out.read_text())
    else:
        print("No probe output file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
