"""Probe AcSmSheetSetMgr — the COM API for reading binary .dst files.

If this works, we can ship a COM-backed sheet-set reader to complement
the pure-Python XML reader for the AutoCAD 2024+ binary format.
"""

from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "win32":
        print("Windows-only.")
        return 1

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()

    dst_path = r"C:\Users\Axel\Developer\autocad-mcp-main\tests\fixtures\IRD_Addition.dst"

    # AcSmSheetSetMgr is the entry point. The .26 suffix matches AutoCAD
    # R26 (2027); older AutoCADs use .23, .24, .25, etc. Try newest first.
    progids = (
        "AcSmComponents.AcSmSheetSetMgr.26",
        "AcSmComponents.AcSmSheetSetMgr.25",
        "AcSmComponents.AcSmSheetSetMgr.24",
        "AcSmComponents.AcSmSheetSetMgr.23",
        "AcSmComponents.AcSmSheetSetMgr",
    )
    mgr = None
    for pid in progids:
        try:
            mgr = win32com.client.Dispatch(pid)
            print(f"Got mgr via {pid!r}")
            break
        except Exception as ex:
            print(f"  {pid!r} failed: {ex}")

    if mgr is None:
        print("Could not get AcSmSheetSetMgr via any ProgID.")
        return 1

    # GetDatabase loads a .dst file.
    try:
        db = mgr.OpenDatabase(dst_path, True)  # readonly=True
        print(f"Opened db: {db}")
        ss = db.GetSheetSet()
        print(f"SheetSet: {ss}")
        print(f"  Name: {ss.GetName()}")
        # Enumerate sheets
        enum = ss.GetSheetEnumerator()
        i = 0
        while True:
            try:
                fetched = enum.Next(1)
                if not fetched:
                    break
                sheet = fetched[0]
                print(f"  Sheet[{i}]: number={sheet.GetNumber()!r} title={sheet.GetTitle()!r} dwg={sheet.GetFileName()!r}")
                i += 1
                if i > 5:
                    print("  ... (truncated)")
                    break
            except Exception as ex:
                print(f"  Sheet enumeration error: {ex}")
                break
        mgr.Close(db)
        return 0
    except Exception as ex:
        print(f"Failed to open db: {ex}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
