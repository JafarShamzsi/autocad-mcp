"""Enumerate ALL child windows of the running AutoCAD main frame to find
the actual command-line edit/text control. Looks at depth>=1, not just
inside MDIClient."""

from __future__ import annotations

import sys

import win32gui

from autocad_mcp.backends.file_ipc import find_autocad_window


def _walk(hwnd: int, indent: int = 0, max_depth: int = 6) -> None:
    if indent > max_depth:
        return

    def cb(child, _):
        cls = win32gui.GetClassName(child)
        try:
            text = win32gui.GetWindowText(child)
        except Exception:
            text = "<unreadable>"
        visible = "V" if win32gui.IsWindowVisible(child) else " "
        prefix = "  " * indent
        # Highlight things that look like edit/text/command controls.
        flag = ""
        low = cls.lower()
        if any(k in low for k in ("edit", "text", "command", "input")):
            flag = "  <-- LOOK"
        print(f"{prefix}[{visible}] hwnd={child} class={cls!r} text={text[:40]!r}{flag}")
        _walk(child, indent + 1, max_depth)
        return True

    win32gui.EnumChildWindows(hwnd, cb, None)


def main() -> int:
    hwnd = find_autocad_window()
    if not hwnd:
        print("No AutoCAD window detected.")
        return 1
    print(f"AutoCAD main window: hwnd={hwnd}")
    print(f"Title: {win32gui.GetWindowText(hwnd)!r}")
    print(f"Class: {win32gui.GetClassName(hwnd)!r}")
    print("\nFull child tree:")
    _walk(hwnd, 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
