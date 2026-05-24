"""Probe whether vlr-acdb-reactor fires for entmake (direct database add)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


async def main() -> int:
    from autocad_mcp.backends.com_backend import COMBackend
    from autocad_mcp.config import LISP_DIR
    from autocad_mcp.events import EventTail

    com = COMBackend()
    await com.initialize()
    await com.drawing_create()

    # Load reactors module, attach reactors, then use entmake to add a LINE.
    tail = EventTail()
    if tail.path.exists():
        tail.path.unlink()

    code = (
        '(setvar "SECURELOAD" 0)'
        f'(load "{(LISP_DIR / "mcp_reactors.lsp").as_posix()}")'
        '(c:mcp-attach-reactors)'
        # Pause then add via entmake (direct database).
        '(setq ent (entmakex (list (cons 0 "LINE") (cons 10 (list 0 0 0)) (cons 11 (list 100 0 0)))))'
        '(if ent (princ "\\nentmake returned: ") (princ "\\nentmake failed"))'
        '(princ ent)'
    )
    r = await com.execute_lisp(code)
    print(f"execute_lisp ok={r.ok}")
    time.sleep(2.0)

    events = tail.read_new()
    print(f"\nGot {len(events)} events:")
    for e in events:
        print(f"  {e}")
    await com.execute_lisp("(c:mcp-detach-reactors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
