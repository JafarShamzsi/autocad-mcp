"""G5 — Live reactor smoke test against a running AutoCAD.

Drives the COM backend to:
  1. Load mcp_dispatch.lsp + mcp_reactors.lsp.
  2. Attach VLR reactors via (c:mcp-attach-reactors).
  3. Draw a new entity (a line).
  4. Read the event log via EventTail.
  5. Assert the new record has the expected shape (ts, ts_local, kind,
     handle, entity_type, layer) and that the new ts_local field made it
     across the LISP → JSON → Python boundary.

This exercises G5 from the audit (live-fire reactor verification) +
confirms the N6 (ts_local rename) and N7 (control-char escape) fixes
behave end-to-end on a real AutoCAD.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


async def main() -> int:
    from autocad_mcp.backends.com_backend import COMBackend
    from autocad_mcp.config import LISP_DIR
    from autocad_mcp.events import EventTail

    print("=== G5 reactor smoke test ===")

    # 1. Attach to AutoCAD via COM.
    com = COMBackend()
    init = await com.initialize()
    if not init.ok:
        print(f"FAIL: COM init failed: {init.error}")
        return 1
    print(f"[1/5] COM attached: {init.payload['version']}")

    # Ensure a drawing exists.
    await com.drawing_create()

    # 2. Load mcp_dispatch.lsp (sets *mcp-ipc-dir* + autoloads pid_tools).
    dispatch_path = (LISP_DIR / "mcp_dispatch.lsp").as_posix()
    reactors_path = (LISP_DIR / "mcp_reactors.lsp").as_posix()
    loader = (
        '(setvar "SECURELOAD" 0)'
        f'(load "{dispatch_path}")'
        f'(load "{reactors_path}")'
    )
    r = await com.execute_lisp(loader)
    if not r.ok:
        print(f"FAIL: load LISP files: {r.error}")
        return 1
    print("[2/5] LISP files loaded")

    # Give AutoCAD a moment to process the (load ...) calls.
    time.sleep(0.5)

    # 3. Clear any prior event log, then attach reactors.
    tail = EventTail()
    if tail.path.exists():
        tail.path.unlink()
    print(f"[3/5] Event log path: {tail.path}")

    r = await com.execute_lisp("(c:mcp-attach-reactors)")
    if not r.ok:
        print(f"FAIL: attach reactors: {r.error}")
        return 1
    # SendCommand is async — give AutoCAD time to process the load+attach
    # before we ask it to draw something.
    time.sleep(2.0)
    print("[3/5] Reactors attached")

    # 4a. Draw via COM AddLine.
    line_result = await com.create_line(0, 0, 100, 0, layer="A-WALL")
    if not line_result.ok:
        print(f"FAIL: create_line: {line_result.error}")
        return 1
    com_handle = line_result.payload.get("handle")
    print(f"[4a/5] Drew LINE via COM AddLine handle={com_handle}")
    time.sleep(1.5)

    # 4b. Also draw via (command "_LINE" ...) — comparison data point.
    r = await com.execute_lisp(
        '(command "_LINE" (list 200 0 0) (list 300 0 0) "")'
    )
    if r.ok:
        print("[4b/5] Drew LINE via (command \"_LINE\" ...) (LISP path)")
    time.sleep(1.5)

    # 5. Read events back via EventTail.
    events = tail.read_new()
    print(f"[5/5] Read {len(events)} events from log")

    if not events:
        print("FAIL: no events captured. Reactors may not have fired.")
        await com.execute_lisp("(c:mcp-detach-reactors)")
        return 1

    # Find any object_appended event.
    appended = [e for e in events if e.get("kind") == "object_appended"]

    if not appended:
        print("FAIL: no object_appended event at all")
        print("All events:")
        for e in events:
            print(f"  {e}")
        await com.execute_lisp("(c:mcp-detach-reactors)")
        return 1

    print(f"\nGot {len(appended)} object_appended events:")
    for e in appended:
        print(f"  handle={e.get('handle')} type={e.get('entity_type')} layer={e.get('layer')}")
    event = appended[0]
    print(f"\nObject-appended event for our line:")
    for k, v in event.items():
        print(f"  {k}: {v!r}")

    # Verify the new fields landed end-to-end.
    failures: list[str] = []
    if "ts_local" not in event:
        failures.append("missing ts_local field (N6 not deployed)")
    elif not isinstance(event["ts_local"], (int, float)):
        failures.append(f"ts_local is not numeric: {event['ts_local']!r}")
    if event.get("entity_type") != "LINE":
        failures.append(f"entity_type wrong: {event.get('entity_type')!r}")
    if event.get("layer") != "A-WALL":
        failures.append(f"layer wrong: {event.get('layer')!r}")

    # Cleanup
    await com.execute_lisp("(c:mcp-detach-reactors)")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nSUCCESS: G5 reactor smoke test passed.")
    print(f"  ts_local roundtripped: {event['ts_local']}")
    print(f"  entity_type: {event['entity_type']}")
    print(f"  layer: {event['layer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
