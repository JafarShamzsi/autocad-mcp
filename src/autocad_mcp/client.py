"""Lazy backend singleton, _safe/_error/_json helpers, screenshot utility."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from typing import Any

import structlog
from mcp.types import ImageContent, TextContent

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.config import ONLY_TEXT_FEEDBACK, detect_backend

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy backend singleton
# ---------------------------------------------------------------------------

_backend: AutoCADBackend | None = None
_init_lock = asyncio.Lock()


async def _backend_is_alive(backend: AutoCADBackend) -> bool:
    """Probe whether the backend's external process is still reachable.

    On COM this asks the AutoCAD app for its version; if AutoCAD has been
    closed or crashed, the IDispatch call raises a recoverable pywintypes
    error. On other backends always True — ezdxf is in-process and
    file_ipc's liveness is rechecked on every dispatch anyway.
    """
    if backend.name != "com":
        return True
    # Try a lightweight status call. Anything that raises → dead.
    try:
        r = await backend.status()
        return r.ok
    except Exception:
        return False


async def get_backend() -> AutoCADBackend:
    """Return (and lazily initialize) the backend singleton.

    Uses an asyncio Lock to prevent concurrent initialization races
    when multiple MCP tool calls arrive simultaneously.
    """
    global _backend
    if _backend is not None:
        # Cheap liveness probe — if the backend reports dead, drop it.
        # Only the COM backend can lose its connection mid-session (AutoCAD
        # crash); file_ipc + ezdxf are stateless w.r.t. external processes
        # so the probe is a no-op for them.
        if await _backend_is_alive(_backend):
            return _backend
        log.warning("backend_disconnect_detected_recreating",
                    name=_backend.name)
        _backend = None

    async with _init_lock:
        # Double-check after acquiring lock (another task may have initialized)
        if _backend is not None:
            return _backend

        backend_name = detect_backend()

        if backend_name == "com":
            from autocad_mcp.backends.com_backend import COMBackend

            backend = COMBackend()
        elif backend_name == "file_ipc":
            from autocad_mcp.backends.file_ipc import FileIPCBackend

            backend = FileIPCBackend()
        else:
            from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

            backend = EzdxfBackend()

        result = await backend.initialize()
        if not result.ok:
            # In auto mode, fall back to ezdxf if COM/file_ipc init fails.
            from autocad_mcp.config import _current_backend_env

            if _current_backend_env() == "auto" and backend_name != "ezdxf":
                from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

                log.info(
                    "backend_init_failed_fallback_ezdxf",
                    requested=backend_name,
                    error=result.error,
                )
                backend = EzdxfBackend()
                result = await backend.initialize()

            if not result.ok:
                raise RuntimeError(f"Backend init failed: {result.error}")

        _backend = backend
        log.info("backend_initialized", backend=_backend.name)
        return _backend


# ---------------------------------------------------------------------------
# JSON serialization helper
# ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    """Serialize to compact JSON string."""
    return json.dumps(data, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Error formatting with actionable hints
# ---------------------------------------------------------------------------


# Hints keyed by category — preferred over substring matching.
_HINTS_BY_CATEGORY = {
    "not_supported": "Operation not supported on current backend. Check system(operation='status') for capabilities.",
    "not_found": "Target not found. Verify the entity ID / drawing path before retrying.",
    "invalid_arg": "Invalid argument. Check the operation's data schema in the tool docstring.",
    "backend_unavailable": "Backend isn't reachable. Start AutoCAD and open a drawing, then retry.",
    "timeout": "Command timed out. AutoCAD may be in a modal dialog. Press ESC in AutoCAD and retry.",
    "internal_error": "Unexpected error. Check AutoCAD is responsive and retry.",
}


def _error(e: Exception, context: str = "", category: str | None = None) -> str:
    """Format an exception with an actionable hint.

    If `category` is provided it takes priority over substring matching.
    The substring fallback is kept for error messages that flow through
    third-party libraries (pywin32, ezdxf) where we don't control the text.
    """
    msg = str(e)
    msg_lower = msg.lower()

    if category and category in _HINTS_BY_CATEGORY:
        hint = _HINTS_BY_CATEGORY[category]
    elif "window not found" in msg_lower or "no autocad" in msg_lower:
        hint = "AutoCAD LT is not running or no drawing is open. Start AutoCAD and open a .dwg file."
    elif any(s in msg_lower for s in ("rpc_e_disconnected", "the object invoked has disconnected",
                                      "the rpc server is unavailable", "0x800706be", "0x800401fd")):
        hint = (
            "AutoCAD's COM server disconnected (likely crashed or was closed). "
            "Restart AutoCAD, then call system(operation='init') to reattach."
        )
    elif "timeout" in msg_lower:
        hint = _HINTS_BY_CATEGORY["timeout"]
    elif "not supported" in msg_lower or "backend" in msg_lower:
        hint = _HINTS_BY_CATEGORY["not_supported"]
    elif "dispatcher" in msg_lower or "mcp_dispatch" in msg_lower:
        hint = "mcp_dispatch.lsp not loaded. In AutoCAD command line, type: (load \"mcp_dispatch.lsp\")"
    else:
        hint = _HINTS_BY_CATEGORY["internal_error"]

    payload = {"error": f"[{context}] {msg}" if context else msg, "hint": hint}
    if category:
        payload["category"] = category
    return _json(payload)


# ---------------------------------------------------------------------------
# _safe decorator for tool error handling
# ---------------------------------------------------------------------------


def _safe(tool_name: str):
    """Wrap an async tool handler with uniform error handling."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                op = kwargs.get("operation", "unknown")
                log.error("tool_error", tool=tool_name, operation=op, error=str(e))
                return _error(e, f"{tool_name}.{op}")

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------


def _format_result(
    result: CommandResult,
    include_screenshot: bool = False,
    screenshot_data: str | None = None,
) -> list[TextContent | ImageContent] | str:
    """Format a CommandResult for MCP response.

    Returns a list with TextContent + optional ImageContent if screenshot requested,
    or a plain JSON string if no screenshot.
    """
    text = _json(result.to_dict())

    if not include_screenshot or ONLY_TEXT_FEEDBACK or not screenshot_data:
        return text

    return [
        TextContent(type="text", text=text),
        ImageContent(
            type="image",
            data=screenshot_data,
            mimeType="image/png",
        ),
    ]


# ---------------------------------------------------------------------------
# Screenshot cache
# ---------------------------------------------------------------------------
#
# `view.get_screenshot` and `include_screenshot=True` on entity ops both
# call the backend's `get_screenshot()`. On COM that's a Win32 PrintWindow
# (~30-80 ms). Doing this on every tool call burns time, especially for
# read-only ops where the drawing hasn't changed. The cache stores the
# last screenshot + a "dirty" flag. Any entity-touching op sets dirty=True;
# the next include_screenshot=True call rebuilds the cache.
#
# Read-only ops (status, list, get, info, list_layers, ...) leave the
# cache alone — they re-serve whatever's there.

_screenshot_cache: dict = {"data": None, "dirty": True, "captured_at": 0.0}


def invalidate_screenshot_cache() -> None:
    """Mark the screenshot cache stale. Call after any entity-mutating op."""
    _screenshot_cache["dirty"] = True


def screenshot_cache_stats() -> dict:
    """Diagnostic — current cache state. Useful for tests / debugging."""
    return {
        "has_data": _screenshot_cache["data"] is not None,
        "dirty": _screenshot_cache["dirty"],
        "captured_at": _screenshot_cache["captured_at"],
    }


async def add_screenshot_if_available(
    result: CommandResult,
    include_screenshot: bool = False,
) -> list[TextContent | ImageContent] | str:
    """Conditionally append a screenshot to the result.

    Uses a process-local cache keyed by a dirty flag. The cache is bumped
    by `invalidate_screenshot_cache()` from any entity-mutating op (called
    via the `_dirties_screenshot` decorator on the relevant tools); when
    `include_screenshot=True` arrives while the cache is dirty, we
    re-capture; otherwise we re-serve.
    """
    if not include_screenshot or ONLY_TEXT_FEEDBACK:
        return _json(result.to_dict())

    backend = await get_backend()
    # Re-capture if cache is dirty or empty.
    cached = _screenshot_cache["data"]
    if cached is None or _screenshot_cache["dirty"]:
        screenshot_result = await backend.get_screenshot()
        if screenshot_result.ok and screenshot_result.payload:
            import time as _time
            _screenshot_cache["data"] = screenshot_result.payload
            _screenshot_cache["dirty"] = False
            _screenshot_cache["captured_at"] = _time.time()
            cached = screenshot_result.payload

    if cached:
        return _format_result(result, True, cached)
    return _json(result.to_dict())
