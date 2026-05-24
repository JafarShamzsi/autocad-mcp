"""Backend detection and environment configuration."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger()


def lisp_exec_enabled() -> bool:
    """Return True iff the user has opted into `system.execute_lisp`.

    Centralizes the env-var read so the server tool, the events tool, and
    any future internal caller branch off the same predicate. The default
    is False — arbitrary AutoLISP execution must be explicitly enabled.
    """
    return os.environ.get(
        "AUTOCAD_MCP_ENABLE_LISP_EXEC", ""
    ).strip().lower() in ("1", "true", "yes")


def default_ipc_dir() -> Path:
    """Return the default IPC dir: <system tempdir>/autocad-mcp.

    The LISP side reads the same `AUTOCAD_MCP_IPC_DIR` env var (via
    `getenv`); when both sides default, they land on the same path. The
    directory is created on demand by callers — config code only computes
    the path.
    """
    return Path(tempfile.gettempdir()) / "autocad-mcp"


# Paths
LISP_DIR = Path(__file__).resolve().parent.parent.parent / "lisp-code"
IPC_DIR = Path(os.environ.get("AUTOCAD_MCP_IPC_DIR") or default_ipc_dir())

# Backend selection
BACKEND_DEFAULT = "auto"  # auto | file_ipc | ezdxf

# IPC timeout (seconds), clamped to [1, 300]
IPC_TIMEOUT = max(1.0, min(300.0, float(os.environ.get("AUTOCAD_MCP_IPC_TIMEOUT", "10.0"))))

# Screenshot
ONLY_TEXT_FEEDBACK = os.environ.get("AUTOCAD_MCP_ONLY_TEXT", "").lower() in ("1", "true", "yes")

# Win32 availability
WIN32_AVAILABLE = sys.platform == "win32"


def _current_backend_env() -> str:
    """Read backend selection from env with normalization."""
    return os.environ.get("AUTOCAD_MCP_BACKEND", BACKEND_DEFAULT).strip().lower()


def _is_wsl() -> bool:
    """Detect WSL Linux runtime."""
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def _write_debug_snapshot(backend_env: str):
    """Optionally write backend detection debug information.

    Set AUTOCAD_MCP_DEBUG_DETECT_FILE to enable.
    """
    debug_file = os.environ.get("AUTOCAD_MCP_DEBUG_DETECT_FILE", "").strip()
    if not debug_file:
        return

    try:
        debug_path = Path(debug_file)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(f"sys.platform={sys.platform}\n")
            f.write(f"WIN32_AVAILABLE={WIN32_AVAILABLE}\n")
            f.write(f"BACKEND_ENV={backend_env}\n")
            f.write(f"python={sys.executable}\n")
    except Exception as ex:
        # Best-effort only; never fail backend detection due debug writes.
        log.debug("debug_snapshot_write_failed", path=debug_file, error=str(ex))


def _running_autocad_full_version() -> str | None:
    """Return the registered version (e.g., 'R26.0') of full AutoCAD if present.

    Looks for `HKLM\\SOFTWARE\\Autodesk\\AutoCAD\\R*\\ACAD-A*` keys. AutoCAD LT
    registers under different ACAD-* product codes, so this is full-only.
    Returns None when only LT is installed or AutoCAD isn't installed at all.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Autodesk\AutoCAD") as root:
            i = 0
            while True:
                try:
                    release = winreg.EnumKey(root, i)
                except OSError:
                    return None
                i += 1
                # ACAD-A* = full AutoCAD; ACAD-D* / ACAD-E* = LT.
                try:
                    with winreg.OpenKey(root, release) as r_key:
                        j = 0
                        while True:
                            try:
                                product = winreg.EnumKey(r_key, j)
                            except OSError:
                                break
                            j += 1
                            if product.startswith("ACAD-A"):
                                return release
                except OSError:
                    continue
    except OSError:
        return None
    return None


def detect_backend() -> str:
    """Return the backend name: 'com' | 'file_ipc' | 'ezdxf'.

    Auto-mode priority: COM (full AutoCAD running) > file_ipc (LT window
    available) > ezdxf (headless fallback).

    Raises RuntimeError with actionable message if explicit backend fails.
    """
    backend_env = _current_backend_env()
    _write_debug_snapshot(backend_env)

    if backend_env == "ezdxf":
        return "ezdxf"

    # Explicit COM
    if backend_env == "com":
        if not WIN32_AVAILABLE:
            raise RuntimeError(
                "AUTOCAD_MCP_BACKEND=com requires Windows. "
                "Use AUTOCAD_MCP_BACKEND=ezdxf for headless mode."
            )
        # Probe the registry up front so the user sees a clear "AutoCAD
        # isn't installed" message instead of a confusing pywin32 COM
        # error during initialize(). We only check installed-ness here;
        # whether AutoCAD is *running* is verified during attach.
        try:
            installed = _running_autocad_full_version()
        except Exception as ex:
            log.debug("autocad_full_detect_failed_for_explicit_com", error=str(ex))
            installed = None
        if installed is None:
            log.warning(
                "explicit_com_no_full_autocad_registered",
                hint=(
                    "AUTOCAD_MCP_BACKEND=com is set but no full AutoCAD "
                    "install was found in HKLM\\SOFTWARE\\Autodesk\\AutoCAD. "
                    "Initialize() will fail unless full AutoCAD is running."
                ),
            )
        return "com"

    if backend_env in ("auto", "file_ipc"):
        if WIN32_AVAILABLE:
            # In auto mode, prefer COM (full AutoCAD) over LISP IPC (LT-only).
            if backend_env == "auto":
                try:
                    if _running_autocad_full_version():
                        log.info("autocad_full_detected_using_com")
                        return "com"
                except Exception as ex:
                    # Registry probe failed (locked key, permission, etc.) —
                    # treat as "no full AutoCAD" and fall through to file_ipc.
                    log.debug("autocad_full_detect_failed", error=str(ex))

            try:
                from autocad_mcp.backends.file_ipc import find_autocad_window

                hwnd = find_autocad_window()
                if hwnd:
                    log.info("autocad_window_found", hwnd=hwnd)
                    return "file_ipc"
                elif backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc but no AutoCAD window found. "
                        "Start AutoCAD LT and open a .dwg file."
                    )
            except ImportError:
                if backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc requires pywin32. "
                        "Install with: pip install pywin32"
                    )
                log.info("win32_deps_missing_fallback_ezdxf")
        elif backend_env == "file_ipc":
            raise RuntimeError(
                "AUTOCAD_MCP_BACKEND=file_ipc requires Windows. "
                "Use AUTOCAD_MCP_BACKEND=ezdxf for headless mode."
            )
        elif _is_wsl():
            log.info(
                "wsl_linux_python_fallback_ezdxf",
                platform=sys.platform,
                python=sys.executable,
                hint="Launch MCP with Windows python.exe for File IPC backend.",
            )

    log.info("using_ezdxf_backend")
    return "ezdxf"
