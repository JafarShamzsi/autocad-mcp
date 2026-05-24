"""COM-backed reader for AutoCAD 2024+ binary `.dst` sheet sets.

The pure-Python XML reader in `sheetset.py` can't decode the binary format
that AutoCAD 2024+ writes. AcSmSheetSetMgr decodes it, but only works
inside AutoCAD's process — the COM object created outside AutoCAD only
exposes `IUnknown` (verified empirically: `Marshal.QueryInterface` for
`IAcSmSheetSetMgr` returns `E_NOINTERFACE` from a standalone .NET
process).

This module bridges the gap by NETLOADing `bin/dotnet/SheetSetReader.dll`
into a running AutoCAD and invoking its `MCPREADDST` command. The reader
returns the parsed SheetSet using the same shape `sheetset.py` produces,
so callers don't branch on which path produced the result.

Prerequisites:
  - AutoCAD 2024+ running with at least one open document.
  - `bin/dotnet/SheetSetReader.dll` present (build via the .csproj at
    `dotnet/SheetSetReader/SheetSetReader.csproj`).
  - `pywin32` available (always present on Windows installs).

Concurrency: NETLOAD is idempotent per AutoCAD session, but doing it
once per process saves a redundant ARX load message in AutoCAD's
output window. We track it in a module-level set.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from autocad_mcp.project.sheetset import Sheet, SheetSet


# Track which DLLs we've NETLOADed in this Python process. AutoCAD itself
# ignores duplicate NETLOAD calls (well — it prints a benign warning), but
# avoiding the extra round-trip keeps logs clean.
_loaded_dlls: set[Path] = set()


def default_dll_path() -> Path:
    """Path to the canonical pre-built DLL location."""
    # src/autocad_mcp/project/sheetset_com_reader.py → repo root is 4 up.
    here = Path(__file__).resolve()
    return here.parent.parent.parent.parent / "bin" / "dotnet" / "SheetSetReader.dll"


def read_sheetset_via_com(
    dst_path: Path | str,
    *,
    dll_path: Path | None = None,
    timeout: float = 30.0,
) -> SheetSet:
    """Read a binary `.dst` by NETLOADing the reader and invoking MCPREADDST.

    Raises:
        FileNotFoundError: dst_path or dll_path doesn't exist.
        RuntimeError: AutoCAD isn't running, has no document, or the
            MCPREADDST command didn't produce output within `timeout`.
    """
    dst_path = Path(dst_path).expanduser().resolve()
    if not dst_path.exists():
        raise FileNotFoundError(f".dst not found: {dst_path}")
    if dll_path is None:
        dll_path = default_dll_path()
    if not dll_path.exists():
        raise FileNotFoundError(
            f"SheetSetReader.dll not found at {dll_path}. Build it with "
            f"`dotnet build -c Release dotnet/SheetSetReader/SheetSetReader.csproj`."
        )

    # Imports are local so the module is importable on non-Windows (e.g.
    # CI for type-checking) without pywin32.
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.GetActiveObject("AutoCAD.Application")
    except Exception as ex:
        raise RuntimeError(
            f"AutoCAD COM activation failed (is AutoCAD running?): {ex}"
        ) from ex

    if app.Documents.Count == 0:
        raise RuntimeError(
            "AutoCAD has no open document. SendCommand requires a "
            "document; open or create a drawing first."
        )
    doc = app.ActiveDocument

    # Use a per-call tempfile so concurrent indexing of multiple .dst
    # files doesn't collide on the output path.
    out_fd, out_str = tempfile.mkstemp(suffix=".json", prefix="acad_mcp_dst_")
    os.close(out_fd)
    out_path = Path(out_str)
    out_path.unlink(missing_ok=True)

    # NETLOAD is idempotent (AutoCAD silently ignores duplicate loads of
    # the same DLL path), so the per-process cache is a "have we already
    # done this in this session" optimization. If AutoCAD has been
    # restarted between calls, the cache is stale and the first MCPREADDST
    # will fail; we retry once with an unconditional NETLOAD.
    try:
        def _issue_netload() -> None:
            doc.SendCommand(f'(command "_NETLOAD" "{_lispify(dll_path)}") ')
            _loaded_dlls.add(dll_path)

        if dll_path not in _loaded_dlls:
            _issue_netload()

        cmd = (
            f'(command "MCPREADDST" "{_lispify(dst_path)}" '
            f'"{_lispify(out_path)}") '
        )
        doc.SendCommand(cmd)

        # Output file appears via atomic rename — exists as a whole or not
        # at all. Polling at 100ms covers fast cases without burning CPU.
        # Half the budget for first attempt so the retry has time.
        first_deadline = time.time() + (timeout / 2.0)
        while time.time() < first_deadline:
            if out_path.exists():
                break
            time.sleep(0.1)

        if not out_path.exists():
            # Stale-cache retry: re-issue NETLOAD then MCPREADDST. Common
            # when AutoCAD was restarted between two Python calls without
            # the Python process restarting.
            _issue_netload()
            doc.SendCommand(cmd)
            retry_deadline = time.time() + (timeout / 2.0)
            while time.time() < retry_deadline:
                if out_path.exists():
                    break
                time.sleep(0.1)

        if not out_path.exists():
            raise RuntimeError(
                f"MCPREADDST didn't produce output at {out_path} within "
                f"{timeout:.1f}s. Common causes: SheetSetReader.dll not "
                "NETLOADed, .dst path contains spaces or quotes that "
                "broke the LISP parse, or AutoCAD's command queue is "
                "blocked by a modal dialog."
            )

        data = json.loads(out_path.read_text(encoding="utf-8"))
        if not data.get("ok"):
            raise RuntimeError(
                f"MCPREADDST returned an error: {data.get('error')} "
                f"(category={data.get('category')})"
            )
        return _build_sheetset(dst_path, data)
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            # Best-effort cleanup. Tempdir entries get reaped by the OS.
            pass


def _lispify(p: Path) -> str:
    """Path → forward-slash string safe for the LISP `(command …)` parser.

    Backslashes inside a LISP string literal are an escape character, so a
    raw Windows path produces a parse error. Forward slashes are accepted
    by AutoCAD's command-line file-path arguments.
    """
    return str(p).replace("\\", "/")


def _build_sheetset(dst_path: Path, data: dict) -> SheetSet:
    sheets: list[Sheet] = []
    for s in data.get("sheets", []):
        sheets.append(Sheet(
            number=str(s.get("number", "") or ""),
            title=str(s.get("title", "") or ""),
            description=str(s.get("description", "") or ""),
            dwg_file=str(s.get("dwg_file", "") or ""),
            layout=str(s.get("layout", "") or ""),
        ))
    name = (data.get("name") or "").strip() or dst_path.stem
    return SheetSet(name=name, path=dst_path, sheets=sheets)
