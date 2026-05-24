"""Smoke test for the .NET ManagedReactor.

Requires:
  - Full AutoCAD running with at least one open document
  - bin/dotnet/MCPReactor.dll present (run dotnet build first)

What it does:
  1. Clear the event log so we have a clean baseline.
  2. Tell the server to start recording (auto-mode → picks .NET on COM).
  3. Drive AutoCAD: add a line, change a layer, run a command.
  4. Read the event log; assert we saw the right kinds.
  5. Detach the reactor.

Run with:
  .venv\\Scripts\\python.exe scripts\\smoke_dotnet_reactor.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make sure src/ is on the path even when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def main() -> int:
    from autocad_mcp.client import get_backend
    from autocad_mcp.server import events as events_tool

    fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool

    backend = await get_backend()
    print(f"Backend: {backend.name}")
    if backend.name != "com":
        print("This smoke test needs the COM backend (full AutoCAD running).")
        return 2

    print("\n--- Clearing event log ---")
    print(json.loads(await fn(operation="clear")))

    print("\n--- start_recording (auto) ---")
    res = json.loads(await fn(operation="start_recording"))
    print(json.dumps(res, indent=2))
    if not res.get("ok"):
        return 3
    impl = res["payload"]["reactor_impl"]
    print(f"Selected reactor: {impl}")

    # Give AutoCAD a moment to digest NETLOAD + command invocation.
    await asyncio.sleep(1.0)

    print("\n--- Drawing a line via the entity tool ---")
    from autocad_mcp.server import entity as entity_tool
    efn = entity_tool.fn if hasattr(entity_tool, "fn") else entity_tool
    r = await efn(operation="create_line", x1=0, y1=0, x2=100, y2=0, layer="0")
    print(json.loads(r))

    await asyncio.sleep(0.5)

    print("\n--- Reading recent events ---")
    res = json.loads(await fn(operation="recent", data={"limit": 50}))
    events_list = res["payload"]["events"]
    print(f"  events captured: {len(events_list)}")
    kinds = sorted({e.get("kind", "?") for e in events_list})
    print(f"  kinds: {kinds}")

    print("\n--- Last 10 events ---")
    for e in events_list[-10:]:
        kind = e.get("kind", "?")
        handle = e.get("handle", "")
        et = e.get("entity_type", "")
        cmd = e.get("command", "")
        extras = f" command={cmd}" if cmd else ""
        extras += f" handle={handle} type={et}" if handle else ""
        print(f"  [{kind}]{extras}")

    print("\n--- stop_recording ---")
    res = json.loads(await fn(operation="stop_recording"))
    print(json.dumps(res, indent=2))

    # Success criteria: at least session_started + the line we added.
    if "session_started" not in kinds:
        print("FAIL: no session_started event")
        return 4
    if "object_appended" not in kinds:
        print("FAIL: no object_appended event for the line we drew")
        return 5
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
