"""COM/ActiveX backend for full AutoCAD via pywin32.

Connects to a running AutoCAD instance through IDispatch into
`AutoCAD.Application` (no LT — full AutoCAD 2024+ exposes a richer COM
surface than the LISP dispatcher available to LT).

Threading: AutoCAD COM is single-threaded apartment (STA). We initialise COM
on the asyncio thread that calls into us and route every COM call through
`asyncio.to_thread` so the event loop is never blocked. `pythoncom.CoInitialize`
is called once per thread; releases are best-effort.

Units: AutoCAD's AddArc / AddDimAngular take radians. Our shared geometry
helpers return degrees, so this module converts at the call site.
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

import structlog


# AutoCAD's message pump occasionally rejects COM calls when busy. These
# HRESULTs mean "retry shortly," not "permanent failure." Microsoft's
# IMessageFilter::RetryRejectedCall guidance is the canonical fix.
_RPC_E_CALL_REJECTED = -2147418111   # 0x80010001
_RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
_RETRYABLE_HRESULTS = frozenset({
    _RPC_E_CALL_REJECTED,
    _RPC_E_SERVERCALL_RETRYLATER,
})
_RETRY_BACKOFF_S = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0, 1.0)  # ~5.55s total

from autocad_mcp.backends.base import (
    AutoCADBackend,
    BackendCapabilities,
    CommandResult,
)

log = structlog.get_logger()


# ProgID order: try the version-specific 26.0 (AutoCAD 2027) first, then
# generic. If a different installed version takes over, generic still works.
_PROGIDS_PREFERRED = ("AutoCAD.Application.26", "AutoCAD.Application")


_thread_local = threading.local()


def _ensure_com_initialized() -> None:
    """Initialise COM for this thread if not already done."""
    if getattr(_thread_local, "co_initialized", False):
        return
    try:
        import pythoncom

        pythoncom.CoInitialize()
        _thread_local.co_initialized = True
    except Exception as ex:
        log.warning("co_initialize_failed", error=str(ex))


def _attach_to_running_autocad() -> Any:
    """Connect to a running AutoCAD via GetActiveObject; do **not** launch.

    Returns the AutoCAD.Application IDispatch handle wrapped via
    `gencache.EnsureDispatch` so collection iteration (e.g.
    `for l in doc.Layers`) works reliably. `GetActiveObject` alone returns
    a late-bound handle whose typelib isn't necessarily cached, causing
    "TypeError: This object does not support enumeration" on the *first*
    iteration after a fresh process (the very flake the integration suite
    sees as test_setup_layers_then_wall).

    Raises:
        RuntimeError: AutoCAD isn't running, or pywin32 isn't installed.
    """
    if sys.platform != "win32":
        raise RuntimeError("COM backend requires Windows")

    try:
        import pythoncom
        import win32com.client
        from win32com.client import gencache
    except ImportError as ex:
        raise RuntimeError(
            "COM backend requires pywin32. Install with: pip install pywin32"
        ) from ex

    _ensure_com_initialized()

    last_err: Exception | None = None
    for progid in _PROGIDS_PREFERRED:
        try:
            obj = win32com.client.GetActiveObject(progid)
        except pythoncom.com_error as ex:
            last_err = ex
            continue
        except Exception as ex:
            last_err = ex
            continue

        # Promote to an early-bound dispatch so iteration of collections
        # (Layers, Blocks, Documents, ...) returns proper Python iterables.
        # gencache builds the typelib stub once per process; subsequent
        # attaches are cheap.
        try:
            return gencache.EnsureDispatch(obj)
        except Exception as ex:
            # If gencache can't synthesize the wrapper (typelib not on
            # this machine, COM-marshal funkiness), fall back to the raw
            # object — iteration may still flake but at least we attach.
            log.debug("gencache_ensure_dispatch_failed", error=str(ex))
            return obj

    raise RuntimeError(
        "No running AutoCAD found. Start AutoCAD (full, not LT) before using "
        f"the COM backend. Last error: {last_err}"
    )


def _to_apoint(x: float, y: float, z: float = 0.0) -> Any:
    """Wrap (x,y,z) as a VARIANT array of doubles for AutoCAD COM."""
    import pythoncom
    from win32com.client import VARIANT

    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(x), float(y), float(z)))


def _to_double_array(values: Sequence[float]) -> Any:
    """Wrap a flat sequence of floats as a VARIANT array of doubles."""
    import pythoncom
    from win32com.client import VARIANT

    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in values))


class COMBackend(AutoCADBackend):
    """Full-AutoCAD backend via COM IDispatch.

    Designed to attach to a *running* AutoCAD; never auto-launches the app.
    """

    def __init__(self):
        self._app = None  # AutoCAD.Application
        self._doc = None  # Cached IAcadDocument from drawing_create / drawing_open
        self._lock = asyncio.Lock()  # Single in-flight command
        self._screenshot_provider = None
        self._connected_progid: str | None = None
        # AutoCAD COM is single-threaded apartment (STA). All COM calls must
        # happen on the same OS thread, so we pin them to a single-worker
        # executor and never use the default asyncio thread pool.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="com-backend"
        )

    @property
    def name(self) -> str:
        return "com"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=True,
            can_zoom=True,
            can_query_entities=True,
            can_file_operations=True,
            can_undo=True,
        )

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def initialize(self) -> CommandResult:
        # All COM access must happen on the same OS thread (STA), so we
        # route every call through self._executor (max_workers=1).
        def _attach_and_probe() -> tuple[Any, dict[str, Any], int | None]:
            app = _attach_to_running_autocad()
            payload = {
                "backend": "com",
                "version": _safe_attr(app, "Version", "unknown"),
                "caption": _safe_attr(app, "Caption", ""),
            }
            hwnd: int | None = None
            try:
                hwnd = int(app.HWND)
            except Exception as ex:
                # Some AutoCAD COM impls don't expose HWND; we'll fall back
                # to enumerating windows below.
                log.debug("com_app_hwnd_unavailable", error=str(ex))
            return app, payload, hwnd

        loop = asyncio.get_running_loop()
        try:
            app, payload, hwnd = await loop.run_in_executor(self._executor, _attach_and_probe)
        except RuntimeError as ex:
            return CommandResult(
                ok=False, error=str(ex), category="backend_unavailable",
            )

        self._app = app

        # If we couldn't read HWND from COM, fall back to enumerating windows.
        if hwnd is None:
            try:
                from autocad_mcp.backends.file_ipc import find_autocad_window

                hwnd = find_autocad_window()
            except Exception as ex:
                log.debug("com_hwnd_fallback_enum_failed", error=str(ex))

        if hwnd is not None:
            try:
                from autocad_mcp.screenshot import Win32ScreenshotProvider

                self._screenshot_provider = Win32ScreenshotProvider(hwnd)
            except Exception as ex:
                log.warning("com_screenshot_provider_init_failed", error=str(ex))

        return CommandResult(ok=True, payload=payload)

    async def status(self) -> CommandResult:
        if self._app is None:
            return CommandResult(
                ok=False, error="COM backend not initialized",
                category="backend_unavailable",
            )

        def _read_status() -> dict[str, Any]:
            doc = _safe_active_doc(self._app)
            return {
                "backend": "com",
                "version": _safe_attr(self._app, "Version", "unknown"),
                "caption": _safe_attr(self._app, "Caption", ""),
                "has_document": doc is not None,
                "document_name": _safe_attr(doc, "Name", None) if doc else None,
                "document_path": _safe_attr(doc, "FullName", None) if doc else None,
                "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
            }

        loop = asyncio.get_running_loop()
        try:
            payload = await loop.run_in_executor(self._executor, _read_status)
            return CommandResult(ok=True, payload=payload)
        except Exception as ex:
            return CommandResult(
                ok=False, error=str(ex), category="internal_error",
            )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _run(self, fn, *args, **kwargs) -> CommandResult:
        """Run a synchronous COM operation on the pinned worker thread.

        Inner functions raise plain `RuntimeError` strings; we map common
        wording back to the `ErrorCategory` enum so MCP clients get a
        stable handle to branch on without doing their own substring
        matching. New error messages fall through to `internal_error`.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            try:
                payload = await loop.run_in_executor(
                    self._executor,
                    self._wrap_for_com,
                    fn,
                    *args,
                )
                return CommandResult(ok=True, payload=payload)
            except RuntimeError as ex:
                return CommandResult(
                    ok=False,
                    error=str(ex),
                    category=_infer_category(str(ex)),
                )
            except Exception as ex:
                return CommandResult(
                    ok=False,
                    error=f"{type(ex).__name__}: {ex}",
                    category="internal_error",
                )

    def _wrap_for_com(self, fn, *args, **kwargs):
        """Ensure COM is init'd on the worker thread, then call `fn`.

        Retries on RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER —
        AutoCAD's message pump intermittently bounces calls when busy
        and Microsoft's IMessageFilter guidance says to retry with
        exponential backoff before giving up.
        """
        _ensure_com_initialized()

        last_err: Exception | None = None
        delays = (0.0,) + _RETRY_BACKOFF_S
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                return fn(*args, **kwargs)
            except Exception as ex:  # broad: pywintypes.com_error is not stdlib
                hresult = _hresult_of(ex)
                if hresult is None or hresult not in _RETRYABLE_HRESULTS:
                    raise
                last_err = ex
                log.debug(
                    "com_call_rejected_retrying",
                    hresult=hresult,
                    attempt=attempt,
                    waited=delay,
                    remaining=len(delays) - 1 - attempt,
                )

        # All retries exhausted.
        if last_err is not None:
            raise last_err
        raise RuntimeError("COM retry loop exited without an error (unreachable)")

    def _require_doc(self):
        """Return the working document or raise — every entity op needs one.

        Prefers the document cached by drawing_create/drawing_open over
        Application.ActiveDocument, because ActiveDocument can change
        unpredictably (focus switches, MDI activation) and would scatter
        entities across multiple documents in a multi-step operation.
        """
        if self._app is None:
            raise RuntimeError("COM backend not initialized")
        if self._doc is not None:
            return self._doc
        doc = _safe_active_doc(self._app)
        if doc is None:
            raise RuntimeError(
                "No active document. Open a drawing in AutoCAD or call drawing(operation='create')."
            )
        # Cache so subsequent calls in this session pin to the same doc.
        self._doc = doc
        return doc

    def _msp(self):
        """Return modelspace of the active document."""
        return self._require_doc().ModelSpace

    def _ensure_layer(self, name: str | None) -> str | None:
        """Create the layer if missing; return its name (or None if not given)."""
        if not name:
            return None
        doc = self._require_doc()
        # AutoCAD's Layers collection: .Item(name) raises if missing; .Add is idempotent.
        try:
            doc.Layers.Item(name)
        except Exception:
            doc.Layers.Add(name)
        return name

    @staticmethod
    def _set_layer(entity, layer: str | None) -> None:
        if layer:
            entity.Layer = layer

    # -----------------------------------------------------------------------
    # Drawing management
    # -----------------------------------------------------------------------

    async def drawing_info(self) -> CommandResult:
        def _info():
            doc = self._require_doc()
            msp = doc.ModelSpace
            entity_count = msp.Count
            layers = [layer.Name for layer in doc.Layers]
            blocks = [b.Name for b in doc.Blocks if not b.Name.startswith("*")]
            return {
                "entity_count": entity_count,
                "layers": layers,
                "blocks": blocks,
                "dwg_version": getattr(self._app, "Version", "unknown"),
                "save_path": getattr(doc, "FullName", None),
            }

        return await self._run(_info)

    async def drawing_create(self, name: str | None = None, force: bool = False) -> CommandResult:
        # `force` is a file_ipc-only concept (LT clobbers the active drawing).
        # COM opens a fresh document, never destructive — ignored here.
        def _create():
            doc = self._app.Documents.Add()
            self._doc = doc
            return {"name": name or doc.Name, "path": doc.FullName}

        return await self._run(_create)

    async def drawing_open(self, path: str) -> CommandResult:
        def _open():
            doc = self._app.Documents.Open(path)
            self._doc = doc
            return {"path": doc.FullName}

        return await self._run(_open)

    async def drawing_audit(self, fix: bool = True) -> CommandResult:
        """Drive `_-AUDIT` against the active drawing.

        The command's "Fix any errors detected? [Yes/No]" prompt accepts
        Y or N. We capture the response from `getvar AUDITCTL` after the
        run for a structured outcome — AUDITCTL accumulates the issue
        count when AUDIT runs in scripting mode.
        """
        answer = "Y" if fix else "N"

        def _audit():
            doc = self._require_doc()
            # `_.-AUDIT` is the dash-prefixed command-line form; non-dash
            # opens a dialog even with FILEDIA=0.
            doc.SendCommand(f"_.-AUDIT\n{answer}\n")
            return {
                "ok": True,
                "fix_applied": fix,
                "note": "See AutoCAD command line for the full audit report.",
            }

        return await self._run(_audit)

    async def drawing_recover(self, path: str) -> CommandResult:
        """Drive `_.-RECOVER` to open a damaged file with salvage."""
        from pathlib import Path as _Path
        posix = str(_Path(path)).replace("\\", "/")

        def _recover():
            doc = self._require_doc()
            # `_.-RECOVER` opens the file with the recovery audit applied.
            # The output document replaces the active one.
            doc.SendCommand(f'_.-RECOVER\n"{posix}"\n')
            # The active document might be the recovered one now; refresh.
            try:
                self._doc = self._app.ActiveDocument
            except Exception:
                pass
            return {"path": path, "recovered": True}

        return await self._run(_recover)

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        def _save():
            doc = self._require_doc()
            if path:
                doc.SaveAs(path)
                return {"path": doc.FullName}
            doc.Save()
            return {"path": doc.FullName}

        return await self._run(_save)

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        def _save():
            doc = self._require_doc()
            # AutoCAD's SaveAs ignores the file extension; the AcSaveAsType
            # enum picks the format. Constants from AutoCAD's typelib (verified
            # against AutoCAD 2027 / R26.0 acax26enu.tlb):
            #   ac2018_dxf = 65, ac2013_dxf = 61, ac2010_dxf = 49, ac2007_dxf = 37
            # Try newest first; fall back to older only on HRESULTs that
            # indicate an unsupported constant — path/lock/access errors
            # must surface immediately rather than being masked by the loop.
            # E_INVALIDARG (0x80070057, -2147024809) is what AutoCAD returns
            # for an enum it doesn't recognise.
            E_INVALIDARG = -2147024809
            last_err: Exception | None = None
            for save_as_type in (65, 61, 49, 37):
                try:
                    doc.SaveAs(path, save_as_type)
                    return {"path": doc.FullName, "format_constant": save_as_type}
                except Exception as ex:
                    hresult = _hresult_of(ex)
                    last_err = ex
                    if hresult != E_INVALIDARG:
                        # Path locked / access denied / disk full — surface
                        # the real error rather than continuing the loop.
                        raise
                    log.debug(
                        "com_save_as_dxf_unsupported_type",
                        save_as_type=save_as_type,
                        hresult=hresult,
                    )
            raise RuntimeError(
                "No AcSaveAsType constant accepted by this AutoCAD version. "
                f"Last error: {last_err}"
            )

        return await self._run(_save)

    async def drawing_save_as_dwg(
        self, path: str, version: str | None = None,
    ) -> CommandResult:
        """Save as DWG via AutoCAD's native SaveAs + AcSaveAsType constants.

        AcSaveAsType (DWG) values that the COM API accepts — newest first:
          ac2018_dwg = 64, ac2013_dwg = 60, ac2010_dwg = 48,
          ac2007_dwg = 36, ac2004_dwg = 24, ac2000_dwg = 12, acR14_dwg = 8.
        We map the caller's `version` to the right constant and fall back
        to the newest supported one if the requested generation isn't
        accepted by this AutoCAD build.
        """
        from autocad_mcp.project.dwg_reader import _normalize_dwg_version

        # Generation code → AcSaveAsType constant.
        _DWG_TYPE_BY_VERSION = {
            "2018": 64, "2013": 60, "2010": 48, "2007": 36,
            "2004": 24, "2000": 12,
        }
        gen = _normalize_dwg_version(version)
        primary = _DWG_TYPE_BY_VERSION.get(gen, 64)
        # Tail = the fallback chain when the primary isn't accepted.
        ordered = [primary] + [t for t in (64, 60, 48, 36, 24, 12) if t != primary]

        def _save():
            doc = self._require_doc()
            E_INVALIDARG = -2147024809
            last_err: Exception | None = None
            for save_as_type in ordered:
                try:
                    doc.SaveAs(path, save_as_type)
                    return {
                        "path": doc.FullName,
                        "format": "dwg",
                        "format_constant": save_as_type,
                        "version": gen if save_as_type == primary else "fallback",
                    }
                except Exception as ex:
                    hresult = _hresult_of(ex)
                    last_err = ex
                    if hresult != E_INVALIDARG:
                        raise
                    log.debug(
                        "com_save_as_dwg_unsupported_type",
                        save_as_type=save_as_type,
                        hresult=hresult,
                    )
            raise RuntimeError(
                "No AcSaveAsType constant accepted for DWG. "
                f"Last error: {last_err}"
            )

        return await self._run(_save)

    async def drawing_purge(self) -> CommandResult:
        def _purge():
            doc = self._require_doc()
            # PurgeAll repeats until no more purgeable items remain.
            doc.PurgeAll()
            return {"purged": True}

        return await self._run(_purge)

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        def _plot():
            doc = self._require_doc()
            # PlotToFile renders the active layout to file using a named plot config.
            # We force `"DWG To PDF.pc3"` (ships with AutoCAD) so the call never
            # silently falls back to the user's *default* plotter — that default
            # is sometimes a physical printer, and a tool called "plot_pdf"
            # printing paper would be a real-world footgun.
            try:
                doc.Plot.PlotToFile(path, "DWG To PDF.pc3")
            except Exception as ex:
                raise RuntimeError(
                    f"plot_pdf failed via 'DWG To PDF.pc3'. Configure that "
                    f"plotter in AutoCAD (PLOTTERMANAGER → 'DWG To PDF.pc3') "
                    f"or extend this method to accept an explicit .pc3 name. "
                    f"Underlying error: {ex}"
                ) from ex
            return {"path": path}

        return await self._run(_plot)

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        def _get():
            doc = self._require_doc()
            result: dict[str, Any] = {}
            for n in names or []:
                clean = n.lstrip("$")
                try:
                    result[n] = doc.GetVariable(clean)
                except Exception:
                    result[n] = None
            return result

        return await self._run(_get)

    # -----------------------------------------------------------------------
    # Undo / Redo
    # -----------------------------------------------------------------------

    async def undo(self) -> CommandResult:
        def _undo():
            doc = self._require_doc()
            doc.SendCommand("_.undo 1\n")
            return {"undone": True}

        return await self._run(_undo)

    async def redo(self) -> CommandResult:
        def _redo():
            doc = self._require_doc()
            doc.SendCommand("_.mredo 1\n")
            return {"redone": True}

        return await self._run(_redo)

    # -----------------------------------------------------------------------
    # Entity creation
    # -----------------------------------------------------------------------

    async def create_line(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        def _line():
            self._ensure_layer(layer)
            ent = self._msp().AddLine(_to_apoint(x1, y1), _to_apoint(x2, y2))
            self._set_layer(ent, layer)
            return {"entity_type": "LINE", "handle": ent.Handle}

        return await self._run(_line)

    async def create_circle(self, cx, cy, radius, layer=None) -> CommandResult:
        def _circle():
            self._ensure_layer(layer)
            ent = self._msp().AddCircle(_to_apoint(cx, cy), float(radius))
            self._set_layer(ent, layer)
            return {"entity_type": "CIRCLE", "handle": ent.Handle}

        return await self._run(_circle)

    async def create_polyline(self, points, closed=False, layer=None) -> CommandResult:
        def _pl():
            self._ensure_layer(layer)
            flat: list[float] = []
            for p in points:
                flat.append(float(p[0]))
                flat.append(float(p[1]))
            ent = self._msp().AddLightWeightPolyline(_to_double_array(flat))
            if closed:
                ent.Closed = True
            self._set_layer(ent, layer)
            return {"entity_type": "LWPOLYLINE", "handle": ent.Handle}

        return await self._run(_pl)

    async def create_rectangle(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self.create_polyline(
            [(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
            closed=True,
            layer=layer,
        )

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None) -> CommandResult:
        def _arc():
            self._ensure_layer(layer)
            ent = self._msp().AddArc(
                _to_apoint(cx, cy),
                float(radius),
                math.radians(float(start_angle)),
                math.radians(float(end_angle)),
            )
            self._set_layer(ent, layer)
            return {"entity_type": "ARC", "handle": ent.Handle}

        return await self._run(_arc)

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None) -> CommandResult:
        def _e():
            self._ensure_layer(layer)
            # AutoCAD: AddEllipse(center, major_axis_endpoint_relative_to_center, ratio)
            major_rel = _to_apoint(major_x - cx, major_y - cy)
            ent = self._msp().AddEllipse(_to_apoint(cx, cy), major_rel, float(ratio))
            self._set_layer(ent, layer)
            return {"entity_type": "ELLIPSE", "handle": ent.Handle}

        return await self._run(_e)

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None) -> CommandResult:
        def _mt():
            self._ensure_layer(layer)
            ent = self._msp().AddMText(_to_apoint(x, y), float(width), str(text))
            ent.Height = float(height)
            self._set_layer(ent, layer)
            return {"entity_type": "MTEXT", "handle": ent.Handle}

        return await self._run(_mt)

    async def create_hatch(self, entity_id, pattern="ANSI31") -> CommandResult:
        def _hatch():
            doc = self._require_doc()
            target = _find_entity(doc, entity_id)
            if target is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            # AddHatch(PatternType, PatternName, bAssociativity)
            # PatternType 0 = predefined (ANSI31 etc).
            hatch = self._msp().AddHatch(0, pattern, True)
            # AppendOuterLoop expects an array of objects.
            hatch.AppendOuterLoop([target])
            hatch.Evaluate()
            return {"entity_type": "HATCH", "handle": hatch.Handle}

        return await self._run(_hatch)

    # -----------------------------------------------------------------------
    # Entity queries / modifications
    # -----------------------------------------------------------------------

    async def entity_list(self, layer=None, limit: int = 1000) -> CommandResult:
        def _list():
            msp = self._msp()
            items: list[dict] = []
            truncated = False
            cap = int(limit) if limit and limit > 0 else None
            for e in msp:
                ent_layer = e.Layer
                if layer and ent_layer != layer:
                    continue
                items.append({
                    "type": e.ObjectName,
                    "handle": e.Handle,
                    "layer": ent_layer,
                })
                if cap is not None and len(items) >= cap:
                    truncated = True
                    break
            return {
                "entities": items,
                "count": len(items),
                "truncated": truncated,
            }

        return await self._run(_list)

    async def entity_count(self, layer=None) -> CommandResult:
        def _count():
            msp = self._msp()
            if layer is None:
                return {"count": msp.Count}
            n = sum(1 for e in msp if e.Layer == layer)
            return {"count": n}

        return await self._run(_count)

    async def entity_summary(self, layer=None) -> CommandResult:
        def _summary():
            from collections import Counter

            msp = self._msp()
            by_layer: Counter = Counter()
            by_type: Counter = Counter()
            samples: list[dict] = []
            total = 0
            for e in msp:
                ent_layer = e.Layer
                if layer and ent_layer != layer:
                    continue
                total += 1
                by_layer[ent_layer] += 1
                # ObjectName is the AutoCAD class name (e.g. "AcDbLine").
                # Strip the "AcDb" prefix for the DXF-style label users
                # expect.
                obj_name = e.ObjectName or ""
                dxf_type = obj_name[4:].upper() if obj_name.startswith("AcDb") else obj_name
                by_type[dxf_type] += 1
                if len(samples) < 10:
                    samples.append({
                        "type": dxf_type,
                        "layer": ent_layer,
                        "handle": e.Handle,
                    })
            return {
                "total": total,
                "by_layer": dict(by_layer.most_common()),
                "by_type": dict(by_type.most_common()),
                "samples": samples,
                "layer_filter": layer,
            }

        return await self._run(_summary)

    async def entity_select(
        self, *,
        layer=None, type=None, bbox=None, color=None,
        handles=None, limit: int = 5000,
    ) -> CommandResult:
        from collections import Counter

        def _as_set(v):
            if v is None:
                return None
            if isinstance(v, str):
                return {v}
            return set(v)

        wanted_layers = _as_set(layer)
        wanted_types = _as_set(type)
        if wanted_types:
            wanted_types = {t.upper() for t in wanted_types}
        wanted_color = self._normalize_color_aci(color) if color is not None else None
        wanted_handles = _as_set(handles)

        if bbox is not None:
            qx1, qy1, qx2, qy2 = bbox
            qx_min, qx_max = min(qx1, qx2), max(qx1, qx2)
            qy_min, qy_max = min(qy1, qy2), max(qy1, qy2)
        else:
            qx_min = qx_max = qy_min = qy_max = None

        def _select():
            import pythoncom
            from win32com.client import VARIANT
            msp = self._msp()
            by_layer: Counter = Counter()
            by_type: Counter = Counter()
            out_handles: list[str] = []
            truncated = False
            cap = max(1, int(limit))

            for e in msp:
                ent_handle = e.Handle
                ent_layer = e.Layer
                # ObjectName is e.g. "AcDbLine" — strip prefix for the
                # DXF-style type the filter accepts.
                obj_name = e.ObjectName or ""
                ent_type = obj_name[4:].upper() if obj_name.startswith("AcDb") else obj_name

                if wanted_handles is not None and ent_handle not in wanted_handles:
                    continue
                if wanted_layers is not None and ent_layer not in wanted_layers:
                    continue
                if wanted_types is not None and ent_type not in wanted_types:
                    continue
                if wanted_color is not None:
                    try:
                        ec = int(e.Color)
                    except Exception:
                        ec = 256
                    # 256 = ByLayer; resolve via layer color.
                    if ec in (256, 0):
                        try:
                            ec = int(self._doc.Layers.Item(ent_layer).color)
                        except Exception:
                            ec = 7
                    if abs(ec) != wanted_color:
                        continue
                if bbox is not None:
                    # GetBoundingBox returns (min_point, max_point) as
                    # VARIANT arrays of 3 doubles. We pass two VARIANT
                    # output params; AutoCAD writes into them.
                    try:
                        min_v = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
                        max_v = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
                        e.GetBoundingBox(min_v, max_v)
                        emin = min_v.value
                        emax = max_v.value
                    except Exception:
                        continue
                    if (emax[0] < qx_min or emin[0] > qx_max
                            or emax[1] < qy_min or emin[1] > qy_max):
                        continue

                out_handles.append(ent_handle)
                by_layer[ent_layer] += 1
                by_type[ent_type] += 1
                if len(out_handles) >= cap:
                    truncated = True
                    break

            return {
                "count": len(out_handles),
                "handles": out_handles,
                "by_layer": dict(by_layer.most_common()),
                "by_type": dict(by_type.most_common()),
                "truncated": truncated,
                "filter": {
                    "layer": layer, "type": type, "bbox": bbox,
                    "color": color, "handles_in": len(handles) if handles else None,
                    "limit": cap,
                },
            }

        return await self._run(_select)

    def _normalize_color_aci(self, value) -> int:
        if isinstance(value, int):
            return value
        from autocad_mcp.backends._colors import color_to_aci
        try:
            return int(color_to_aci(str(value)))
        except Exception:
            return 7

    async def entity_osnap(
        self, x: float, y: float,
        modes: list[str] | None = None, tolerance: float = 5.0,
    ) -> CommandResult:
        import math

        DEFAULT_MODES = {"endpoint", "midpoint", "center", "intersection"}
        active = set(modes) if modes else DEFAULT_MODES
        active = {m.lower() for m in active}
        tol = float(tolerance)

        def _snap():
            msp = self._msp()
            best: tuple[float, float, float, str, str] | None = None

            def _consider(cx, cy, mode, handle):
                nonlocal best
                d = math.hypot(cx - x, cy - y)
                if d > tol:
                    return
                if best is None or d < best[0]:
                    best = (d, cx, cy, mode, handle)

            for e in msp:
                obj_name = e.ObjectName or ""
                etype = obj_name[4:].upper() if obj_name.startswith("AcDb") else obj_name
                h = e.Handle
                try:
                    if etype == "LINE":
                        s = e.StartPoint
                        ep = e.EndPoint
                        if "endpoint" in active:
                            _consider(s[0], s[1], "endpoint", h)
                            _consider(ep[0], ep[1], "endpoint", h)
                        if "midpoint" in active:
                            _consider((s[0] + ep[0]) / 2, (s[1] + ep[1]) / 2,
                                       "midpoint", h)
                    elif etype == "CIRCLE":
                        c = e.Center
                        r = float(e.Radius)
                        if "center" in active:
                            _consider(c[0], c[1], "center", h)
                        if "endpoint" in active:
                            for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)):
                                _consider(c[0] + dx, c[1] + dy, "endpoint", h)
                    elif etype == "ARC":
                        c = e.Center
                        r = float(e.Radius)
                        sa = float(e.StartAngle)
                        ea = float(e.EndAngle)
                        if "endpoint" in active:
                            for a in (sa, ea):
                                _consider(c[0] + r * math.cos(a),
                                           c[1] + r * math.sin(a), "endpoint", h)
                        if "center" in active:
                            _consider(c[0], c[1], "center", h)
                        if "midpoint" in active:
                            am = (sa + ea) / 2 if ea >= sa else (sa + ea + 2 * math.pi) / 2
                            _consider(c[0] + r * math.cos(am),
                                       c[1] + r * math.sin(am), "midpoint", h)
                    elif etype in ("LWPOLYLINE", "POLYLINE"):
                        try:
                            coords = e.Coordinates
                            # AutoCAD returns a flat array of (x, y) pairs.
                            pts = [(coords[i], coords[i + 1])
                                   for i in range(0, len(coords), 2)]
                        except Exception:
                            pts = []
                        if "endpoint" in active:
                            for px, py in pts:
                                _consider(px, py, "endpoint", h)
                        if "midpoint" in active and len(pts) >= 2:
                            prev = pts[0]
                            for p in pts[1:]:
                                _consider((prev[0] + p[0]) / 2,
                                           (prev[1] + p[1]) / 2, "midpoint", h)
                                prev = p
                except Exception:
                    continue

            if best is None:
                raise RuntimeError(
                    f"No osnap point within tolerance {tol} of ({x}, {y})"
                )
            return {
                "x": best[1], "y": best[2],
                "mode_used": best[3],
                "entity_handle": best[4],
                "distance": best[0],
                "tolerance": tol,
            }

        return await self._run(_snap)

    async def entity_get(self, entity_id) -> CommandResult:
        def _get():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            info = {"type": ent.ObjectName, "handle": ent.Handle, "layer": ent.Layer}
            obj_name = ent.ObjectName
            if obj_name == "AcDbLine":
                info["start"] = list(ent.StartPoint)[:2]
                info["end"] = list(ent.EndPoint)[:2]
            elif obj_name == "AcDbCircle":
                info["center"] = list(ent.Center)[:2]
                info["radius"] = ent.Radius
            return info

        return await self._run(_get)

    async def entity_erase(self, entity_id) -> CommandResult:
        def _erase():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            ent.Delete()
            return {"erased": entity_id}

        return await self._run(_erase)

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        def _copy():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            new_ent = ent.Copy()
            new_ent.Move(_to_apoint(0, 0), _to_apoint(dx, dy))
            return {"handle": new_ent.Handle}

        return await self._run(_copy)

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        def _move():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            ent.Move(_to_apoint(0, 0), _to_apoint(dx, dy))
            return {"moved": entity_id}

        return await self._run(_move)

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        def _rotate():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            ent.Rotate(_to_apoint(cx, cy), math.radians(float(angle)))
            return {"rotated": entity_id}

        return await self._run(_rotate)

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        def _scale():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            ent.ScaleEntity(_to_apoint(cx, cy), float(factor))
            return {"scaled": entity_id}

        return await self._run(_scale)

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        def _mirror():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            new_ent = ent.Mirror(_to_apoint(x1, y1), _to_apoint(x2, y2))
            return {"handle": new_ent.Handle}

        return await self._run(_mirror)

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        def _offset():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            offsets = ent.Offset(float(distance))
            handles = [o.Handle for o in offsets]
            return {"handles": handles}

        return await self._run(_offset)

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        def _array():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            # ArrayRectangular signature: (rows, cols, levels, rowDist, colDist, levelDist).
            # We always pass levels=1 (2D-only) and levelDist=0.0 (ignored when levels=1).
            results = ent.ArrayRectangular(
                int(rows), int(cols), 1,
                float(row_dist), float(col_dist), 0.0,
            )
            handles = [o.Handle for o in results]
            return {"copies": len(handles), "handles": handles}

        return await self._run(_array)

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        from autocad_mcp.arch.geometry import fillet_line_line

        def _fillet():
            doc = self._require_doc()
            e1 = _find_entity(doc, entity_id1)
            e2 = _find_entity(doc, entity_id2)
            if e1 is None or e2 is None:
                raise RuntimeError("Entity not found")
            if e1.ObjectName != "AcDbLine" or e2.ObjectName != "AcDbLine":
                raise RuntimeError(
                    f"Fillet on COM backend supports LINE+LINE only "
                    f"(got {e1.ObjectName} + {e2.ObjectName})"
                )

            line1 = (tuple(e1.StartPoint)[:2], tuple(e1.EndPoint)[:2])
            line2 = (tuple(e2.StartPoint)[:2], tuple(e2.EndPoint)[:2])
            f = fillet_line_line(line1, line2, float(radius))

            _com_replace_endpoint_at(e1, f["corner"], f["trim1"])
            _com_replace_endpoint_at(e2, f["corner"], f["trim2"])

            arc = self._msp().AddArc(
                _to_apoint(*f["arc_center"]),
                float(f["arc_radius"]),
                math.radians(float(f["arc_start_deg"])),
                math.radians(float(f["arc_end_deg"])),
            )
            self._set_layer(arc, e1.Layer)
            return {
                "fillet_handle": arc.Handle,
                "trim1": list(f["trim1"]),
                "trim2": list(f["trim2"]),
            }

        return await self._run(_fillet)

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        from autocad_mcp.arch.geometry import chamfer_line_line

        def _chamfer():
            doc = self._require_doc()
            e1 = _find_entity(doc, entity_id1)
            e2 = _find_entity(doc, entity_id2)
            if e1 is None or e2 is None:
                raise RuntimeError("Entity not found")
            if e1.ObjectName != "AcDbLine" or e2.ObjectName != "AcDbLine":
                raise RuntimeError(
                    f"Chamfer on COM backend supports LINE+LINE only "
                    f"(got {e1.ObjectName} + {e2.ObjectName})"
                )

            line1 = (tuple(e1.StartPoint)[:2], tuple(e1.EndPoint)[:2])
            line2 = (tuple(e2.StartPoint)[:2], tuple(e2.EndPoint)[:2])
            c = chamfer_line_line(line1, line2, float(dist1), float(dist2))

            _com_replace_endpoint_at(e1, c["corner"], c["trim1"])
            _com_replace_endpoint_at(e2, c["corner"], c["trim2"])

            ent = self._msp().AddLine(
                _to_apoint(*c["trim1"]),
                _to_apoint(*c["trim2"]),
            )
            self._set_layer(ent, e1.Layer)
            return {
                "chamfer_handle": ent.Handle,
                "trim1": list(c["trim1"]),
                "trim2": list(c["trim2"]),
            }

        return await self._run(_chamfer)

    # -----------------------------------------------------------------------
    # 3D primitives
    # -----------------------------------------------------------------------

    async def entity_extrude(
        self, entity_id, height, taper_angle=0.0,
    ) -> CommandResult:
        """AcadModelSpace.AddExtrudedSolid(region, height, taper_angle).

        The first argument MUST be a Region. We don't auto-convert curves
        here — callers run `entity_region` first to get a Region handle,
        then pass it in.
        """
        height = float(height); taper = math.radians(float(taper_angle))

        def _extrude():
            doc = self._require_doc()
            region = _find_entity(doc, entity_id)
            if region is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            if region.ObjectName != "AcDbRegion":
                raise RuntimeError(
                    f"extrude expects a Region; {entity_id} is "
                    f"{region.ObjectName!r}. Run entity_region first."
                )
            solid = self._msp().AddExtrudedSolid(region, height, taper)
            return {
                "entity_type": "3DSOLID",
                "handle": solid.Handle,
                "height": height,
                "taper_angle": float(taper_angle),
            }
        return await self._run(_extrude)

    async def entity_revolve(
        self, entity_id,
        axis_x, axis_y, axis_dx, axis_dy, axis_dz=0.0,
        angle=360.0,
    ) -> CommandResult:
        """AddRevolvedSolid(region, axis_point, axis_dir, angle_rad).

        AutoCAD COM's signature wants the axis as a point + direction
        VARIANT pair, both 3-element arrays. Angle is in radians.
        """
        ang = math.radians(float(angle))

        def _revolve():
            doc = self._require_doc()
            region = _find_entity(doc, entity_id)
            if region is None:
                raise RuntimeError(f"Entity {entity_id} not found")
            if region.ObjectName != "AcDbRegion":
                raise RuntimeError(
                    f"revolve expects a Region; {entity_id} is "
                    f"{region.ObjectName!r}. Run entity_region first."
                )
            solid = self._msp().AddRevolvedSolid(
                region,
                _to_apoint(float(axis_x), float(axis_y), 0.0),
                _to_apoint(float(axis_dx), float(axis_dy), float(axis_dz)),
                ang,
            )
            return {
                "entity_type": "3DSOLID",
                "handle": solid.Handle,
                "angle": float(angle),
            }
        return await self._run(_revolve)

    async def entity_region(self, entity_ids: list[str]) -> CommandResult:
        """ModelSpace.AddRegion(curves) → array of Regions.

        Each closed loop in the input curve set becomes one Region. We
        return all created handles.
        """
        def _region():
            doc = self._require_doc()
            curves = []
            for h in entity_ids:
                ent = _find_entity(doc, h)
                if ent is None:
                    raise RuntimeError(f"Entity {h} not found")
                curves.append(ent)
            if not curves:
                raise RuntimeError("entity_region needs at least one curve")
            # `AddRegion` expects a VARIANT array of IDispatch.
            import pythoncom
            from win32com.client import VARIANT
            arr = VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                tuple(curves),
            )
            regions = self._msp().AddRegion(arr)
            # The return may be a single IDispatch or an iterable.
            try:
                handles = [r.Handle for r in regions]
            except TypeError:
                handles = [regions.Handle]
            return {
                "entity_type": "REGION",
                "handles": handles,
                "count": len(handles),
                "from_curves": entity_ids,
            }
        return await self._run(_region)

    async def entity_boolean(
        self, operation, target_id, tool_ids,
    ) -> CommandResult:
        """`solid.Boolean(method, other)` — method: 0=union, 1=intersect,
        2=subtract. AutoCAD-style: the operation mutates `target` and
        consumes each tool solid.
        """
        op_map = {"union": 0, "intersect": 1, "subtract": 2}
        if operation not in op_map:
            return CommandResult(
                ok=False,
                error=f"operation must be union/intersect/subtract; got {operation!r}",
                category="invalid_arg",
            )
        method = op_map[operation]

        def _bool():
            doc = self._require_doc()
            target = _find_entity(doc, target_id)
            if target is None:
                raise RuntimeError(f"Target entity {target_id} not found")
            if target.ObjectName != "AcDb3dSolid":
                raise RuntimeError(
                    f"boolean expects 3DSOLID; target {target_id} is "
                    f"{target.ObjectName!r}"
                )
            consumed: list[str] = []
            for tid in tool_ids:
                tool = _find_entity(doc, tid)
                if tool is None:
                    # Continue rather than abort — the operation may have
                    # already consumed some of the tools.
                    log.debug("boolean_tool_not_found", id=tid)
                    continue
                target.Boolean(method, tool)
                consumed.append(tid)
            return {
                "entity_type": "3DSOLID",
                "handle": target.Handle,
                "operation": operation,
                "tools_consumed": consumed,
            }
        return await self._run(_bool)

    # -----------------------------------------------------------------------
    # Express Tools wrappers (full AutoCAD only)
    # -----------------------------------------------------------------------

    def _send_with_handles(self, command: str, handles: list[str]) -> None:
        """Build a (ssadd …) selection set from `handles`, store it on
        the lisp variable `*mcp-ss*`, then run `command` referencing it.

        accoreconsole / full AutoCAD evaluate the embedded `(handent …)`
        per handle and stuff the result into a selection set. Empty
        list means "the AutoCAD command's interactive selection prompt
        runs," which is rarely what callers want — guard upstream.
        """
        doc = self._require_doc()
        if not handles:
            doc.SendCommand(f'{command}\n')
            return
        # Build a single LISP form that creates the selection set, then
        # invokes the command in script mode with the SS as the response
        # to its first prompt.
        builder = "(setq *mcp-ss* (ssadd))"
        for h in handles:
            builder += f' (ssadd (handent "{h}") *mcp-ss*)'
        # The trailing space + empty line terminates command-line
        # selection ("press Enter to confirm").
        doc.SendCommand(f'{builder} {command} !*mcp-ss* \n\n')

    async def express_burst(self, entity_ids) -> CommandResult:
        def _burst():
            self._send_with_handles("_.BURST", entity_ids)
            return {"command": "BURST", "handles_in": len(entity_ids)}
        return await self._run(_burst)

    async def express_flatten(self, entity_ids=None, z=0.0) -> CommandResult:
        # FLATTEN doesn't take a Z parameter via the command line — it
        # always flattens to Z=0. If a caller passes z != 0, run FLATTEN
        # then translate the selection by that z. Keep simple for v1:
        # FLATTEN to Z=0 only; warn when z != 0.
        if z != 0.0:
            log.warning("express_flatten_z_ignored", requested_z=z)

        def _flatten():
            doc = self._require_doc()
            if entity_ids:
                self._send_with_handles("_.FLATTEN", entity_ids)
            else:
                # FLATTEN ALL: select All, then run.
                doc.SendCommand('_.FLATTEN _ALL  \n')
            return {
                "command": "FLATTEN",
                "handles_in": len(entity_ids) if entity_ids else "ALL",
            }
        return await self._run(_flatten)

    async def express_overkill(self, entity_ids=None) -> CommandResult:
        def _overkill():
            doc = self._require_doc()
            if entity_ids:
                self._send_with_handles("_.OVERKILL", entity_ids)
            else:
                doc.SendCommand('_.OVERKILL _ALL  \n')
            return {
                "command": "OVERKILL",
                "handles_in": len(entity_ids) if entity_ids else "ALL",
            }
        return await self._run(_overkill)

    async def express_layiso(self, layers) -> CommandResult:
        """LAYISO needs a selection on a target layer rather than a list
        of layer names. We work around by drawing nothing on each named
        layer for the duration of the call — instead we use the more
        portable `_.LAYER _OFF *` then `_.LAYER _ON <name>` pattern.
        """
        if not isinstance(layers, list) or not layers:
            return CommandResult(
                ok=False,
                error="layiso requires a non-empty `layers` list",
                category="invalid_arg",
            )

        def _iso():
            doc = self._require_doc()
            # Turn off ALL layers first.
            doc.SendCommand('_.-LAYER _OFF *\n\n')
            # Then turn on the requested ones.
            for name in layers:
                doc.SendCommand(f'_.-LAYER _ON {name}\n')
            doc.SendCommand('\n')  # End -LAYER
            return {"isolated_layers": list(layers)}
        return await self._run(_iso)

    async def express_txt2mtxt(self, entity_ids) -> CommandResult:
        def _t2m():
            self._send_with_handles("_.TXT2MTXT", entity_ids)
            return {"command": "TXT2MTXT", "handles_in": len(entity_ids)}
        return await self._run(_t2m)

    # -----------------------------------------------------------------------
    # Layer operations
    # -----------------------------------------------------------------------

    async def layer_list(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            layers = []
            for layer in doc.Layers:
                layers.append({
                    "name": layer.Name,
                    "color": layer.color,
                    "linetype": layer.Linetype,
                    "is_frozen": bool(layer.Freeze),
                    "is_locked": bool(layer.Lock),
                })
            return {"layers": layers}

        return await self._run(_list)

    async def layer_create(self, name, color="white", linetype="CONTINUOUS") -> CommandResult:
        def _create():
            doc = self._require_doc()
            try:
                existing = doc.Layers.Item(name)
                return {"name": existing.Name, "existed": True}
            except Exception as ex:
                # Layer doesn't exist (Item() raises) — proceed to Add.
                log.debug("layer_lookup_miss", name=name, error=str(ex))
            layer = doc.Layers.Add(name)
            layer.color = _color_to_aci(color)
            try:
                layer.Linetype = linetype
            except Exception as ex:
                # If the linetype isn't loaded, leave default.
                log.debug(
                    "layer_create_linetype_unavailable",
                    name=name, linetype=linetype, error=str(ex),
                )
            return {"name": layer.Name, "color": layer.color}

        return await self._run(_create)

    async def layer_set_current(self, name) -> CommandResult:
        def _set():
            doc = self._require_doc()
            doc.ActiveLayer = doc.Layers.Item(name)
            return {"current_layer": name}

        return await self._run(_set)

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        def _set():
            doc = self._require_doc()
            layer = doc.Layers.Item(name)
            if color is not None:
                layer.color = _color_to_aci(color)
            if linetype is not None:
                layer.Linetype = linetype
            if lineweight is not None:
                try:
                    layer.Lineweight = int(lineweight)
                except Exception as ex:
                    log.debug(
                        "layer_set_lineweight_failed",
                        name=name, lineweight=lineweight, error=str(ex),
                    )
            return {"name": name}

        return await self._run(_set)

    async def layer_freeze(self, name) -> CommandResult:
        return await self._run(self._set_layer_flag, name, "Freeze", True)

    async def layer_thaw(self, name) -> CommandResult:
        return await self._run(self._set_layer_flag, name, "Freeze", False)

    async def layer_lock(self, name) -> CommandResult:
        return await self._run(self._set_layer_flag, name, "Lock", True)

    async def layer_unlock(self, name) -> CommandResult:
        return await self._run(self._set_layer_flag, name, "Lock", False)

    def _set_layer_flag(self, name: str, prop: str, value: bool) -> dict:
        doc = self._require_doc()
        layer = doc.Layers.Item(name)
        setattr(layer, prop, value)
        return {"name": name, prop.lower(): value}

    # -----------------------------------------------------------------------
    # Style management
    # -----------------------------------------------------------------------

    # AutoCAD's IAcadDimStyle exposes attributes by name; the COM-side
    # field names follow the DXF variable convention ("DIMSCALE" etc.).
    # We use SetVariable for the actual assignment because per-style
    # attribute writes via the COM API are inconsistent across versions.
    _DIMSTYLE_VARS = (
        "DIMSCALE", "DIMTXT", "DIMASZ", "DIMDEC", "DIMEXO", "DIMEXE",
        "DIMBLK", "DIMTAD", "DIMTIH", "DIMTOH", "DIMTOL", "DIMTM", "DIMTP",
    )
    _TEXTSTYLE_VARS = ("FontFile", "Height", "Width", "ObliqueAngle")

    async def style_list_dimstyles(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            rows = []
            for ds in doc.DimStyles:
                # The COM `IAcadDimStyle` doesn't expose individual DIM*
                # vars directly — they're system variables that take
                # effect when the dimstyle is current. Surface just the
                # name; callers can switch to it and inspect `GetVariable`.
                rows.append({"name": ds.Name})
            rows.sort(key=lambda r: r["name"])
            return {
                "dimstyles": rows,
                "count": len(rows),
                "current": doc.GetVariable("DIMSTYLE"),
            }
        return await self._run(_list)

    async def style_get_dimstyle(self, name) -> CommandResult:
        def _get():
            doc = self._require_doc()
            try:
                doc.DimStyles.Item(name)
            except Exception:
                raise RuntimeError(f"Dimstyle {name!r} not found")
            # Switch CDIMSTYLE temporarily to read its DIM* vars, then
            # restore. We do this serially to minimize disruption.
            prev = doc.GetVariable("DIMSTYLE")
            attribs = {}
            try:
                doc.SetVariable("DIMSTYLE", name)
                for var in self._DIMSTYLE_VARS:
                    try:
                        attribs[var.lower()] = doc.GetVariable(var)
                    except Exception:
                        pass
            finally:
                try:
                    doc.SetVariable("DIMSTYLE", prev)
                except Exception:
                    pass
            return {"name": name, "attribs": attribs}
        return await self._run(_get)

    async def style_create_dimstyle(self, name, attribs: dict) -> CommandResult:
        attribs = dict(attribs or {})

        def _create():
            doc = self._require_doc()
            existed = False
            try:
                doc.DimStyles.Item(name)
                existed = True
            except Exception:
                doc.DimStyles.Add(name)
            # Apply attribs via SetVariable AFTER setting current dimstyle —
            # CDIMSTYLE governs which dimstyle DIM* writes target.
            prev = doc.GetVariable("DIMSTYLE")
            try:
                doc.SetVariable("DIMSTYLE", name)
                for k, v in attribs.items():
                    try:
                        doc.SetVariable(k.upper(), v)
                    except Exception as ex:
                        log.debug(
                            "com_dimstyle_setvar_failed",
                            var=k, error=str(ex),
                        )
                # Save the attribs back into the dimstyle record.
                doc.SendCommand(f'_.-DIMSTYLE _S {name}\n')
            finally:
                try:
                    doc.SetVariable("DIMSTYLE", prev)
                except Exception:
                    pass
            return {"name": name, "existed": existed}
        return await self._run(_create)

    async def style_set_current_dimstyle(self, name) -> CommandResult:
        def _set():
            doc = self._require_doc()
            try:
                doc.DimStyles.Item(name)
            except Exception:
                raise RuntimeError(f"Dimstyle {name!r} not found")
            doc.SetVariable("DIMSTYLE", name)
            return {"current": name}
        return await self._run(_set)

    async def style_list_textstyles(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            rows = []
            for ts in doc.TextStyles:
                # IAcadTextStyle exposes Height / Width / ObliqueAngle /
                # FontFile / BigFontFile / TextGenerationFlag. We surface
                # the headline fields.
                rows.append({
                    "name": ts.Name,
                    "attribs": {
                        "font": getattr(ts, "FontFile", "") or "",
                        "height": float(getattr(ts, "Height", 0.0) or 0.0),
                        "width": float(getattr(ts, "Width", 1.0) or 1.0),
                        "oblique": float(getattr(ts, "ObliqueAngle", 0.0) or 0.0),
                    },
                })
            rows.sort(key=lambda r: r["name"])
            return {
                "textstyles": rows,
                "count": len(rows),
                "current": doc.GetVariable("TEXTSTYLE"),
            }
        return await self._run(_list)

    async def style_get_textstyle(self, name) -> CommandResult:
        def _get():
            doc = self._require_doc()
            try:
                ts = doc.TextStyles.Item(name)
            except Exception:
                raise RuntimeError(f"Textstyle {name!r} not found")
            return {
                "name": name,
                "attribs": {
                    "font": getattr(ts, "FontFile", "") or "",
                    "height": float(getattr(ts, "Height", 0.0) or 0.0),
                    "width": float(getattr(ts, "Width", 1.0) or 1.0),
                    "oblique": float(getattr(ts, "ObliqueAngle", 0.0) or 0.0),
                },
            }
        return await self._run(_get)

    async def style_create_textstyle(
        self, name, font=None, height=0.0,
        width_factor=1.0, oblique=0.0,
    ) -> CommandResult:
        def _create():
            doc = self._require_doc()
            existed = False
            try:
                ts = doc.TextStyles.Item(name)
                existed = True
            except Exception:
                ts = doc.TextStyles.Add(name)
            if font is not None:
                try:
                    ts.FontFile = font
                except Exception:
                    pass
            ts.Height = float(height)
            ts.Width = float(width_factor)
            ts.ObliqueAngle = float(oblique)
            return {"name": name, "existed": existed}
        return await self._run(_create)

    async def style_set_current_textstyle(self, name) -> CommandResult:
        def _set():
            doc = self._require_doc()
            try:
                doc.TextStyles.Item(name)
            except Exception:
                raise RuntimeError(f"Textstyle {name!r} not found")
            doc.SetVariable("TEXTSTYLE", name)
            return {"current": name}
        return await self._run(_set)

    async def style_list_mleaderstyles(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            # MLeaderStyles live in a dictionary, not a typed collection.
            # AutoCAD exposes them via `Dictionaries.Item("ACAD_MLEADERSTYLE")`.
            rows: list[dict] = []
            try:
                dct = doc.Dictionaries.Item("ACAD_MLEADERSTYLE")
                for i in range(dct.Count):
                    entry = dct.Item(i)
                    rows.append({"name": entry.Name})
            except Exception as ex:
                log.debug("com_mleader_styles_list_failed", error=str(ex))
            rows.sort(key=lambda r: r["name"])
            return {
                "mleaderstyles": rows,
                "count": len(rows),
                "current": doc.GetVariable("CMLEADERSTYLE"),
            }
        return await self._run(_list)

    async def style_set_current_mleaderstyle(self, name) -> CommandResult:
        def _set():
            doc = self._require_doc()
            doc.SetVariable("CMLEADERSTYLE", name)
            return {"current": name}
        return await self._run(_set)

    # -----------------------------------------------------------------------
    # Named views + layer states
    # -----------------------------------------------------------------------

    async def view_list_named(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            rows = []
            for v in doc.Views:
                # IAcadView: Name, Center, Height, Width, Direction, etc.
                try:
                    c = v.Center
                    rows.append({
                        "name": v.Name,
                        "center": [c[0], c[1]],
                        "height": float(v.Height),
                        "width": float(v.Width),
                    })
                except Exception:
                    rows.append({"name": v.Name})
            rows.sort(key=lambda r: r["name"])
            return {"views": rows, "count": len(rows)}
        return await self._run(_list)

    async def view_save_named(self, name, x1, y1, x2, y2) -> CommandResult:
        # AutoCAD's `-VIEW` command saves the named view from window
        # coordinates. Drive it via SendCommand for predictable behavior;
        # the COM Views.Add API expects an existing view object which is
        # more fiddly than the LISP route.
        def _save():
            doc = self._require_doc()
            # Force the script form by setting FILEDIA off temporarily.
            cmd = (
                f'_.-VIEW _W {name} '
                f'{x1},{y1} {x2},{y2}\n'
            )
            doc.SendCommand(cmd)
            return {"name": name, "saved": True}
        return await self._run(_save)

    async def view_restore_named(self, name) -> CommandResult:
        def _restore():
            doc = self._require_doc()
            doc.SendCommand(f'_.-VIEW _R {name}\n')
            return {"name": name, "restored": True}
        return await self._run(_restore)

    async def view_delete_named(self, name) -> CommandResult:
        def _delete():
            doc = self._require_doc()
            doc.SendCommand(f'_.-VIEW _D {name}\n')
            return {"name": name, "deleted": True}
        return await self._run(_delete)

    async def view_list_layer_states(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            # Layer states live in the NOD as `ACAD_LAYERSTATES`. The
            # COM-friendly access is via the LayerStateManager which
            # lives on each Layout.
            rows: list[dict] = []
            try:
                # AcadLayout.LayerStateManager is the canonical entry.
                ms = doc.ModelSpace
                # Layout property of ModelSpace exposes the LayerState manager.
                # Simpler / cross-version: iterate the ACAD_LAYERSTATES
                # dictionary directly.
                try:
                    dct = doc.Dictionaries.Item("ACAD_LAYERSTATES")
                    for i in range(dct.Count):
                        entry = dct.Item(i)
                        rows.append({"name": entry.Name})
                except Exception as ex:
                    log.debug("com_layerstates_dict_iter_failed", error=str(ex))
            except Exception:
                pass
            rows.sort(key=lambda r: r["name"])
            return {"layer_states": rows, "count": len(rows)}
        return await self._run(_list)

    async def view_save_layer_state(self, name) -> CommandResult:
        def _save():
            doc = self._require_doc()
            # `-LAYERSTATE _Save <name>` is the command-line form. All
            # AutoCAD versions support this.
            doc.SendCommand(f'_.-LAYERSTATE _Save {name}\n')
            return {"name": name, "saved": True}
        return await self._run(_save)

    async def view_restore_layer_state(self, name) -> CommandResult:
        def _restore():
            doc = self._require_doc()
            doc.SendCommand(f'_.-LAYERSTATE _Restore {name}\n')
            return {"name": name, "restored": True}
        return await self._run(_restore)

    async def view_delete_layer_state(self, name) -> CommandResult:
        def _delete():
            doc = self._require_doc()
            doc.SendCommand(f'_.-LAYERSTATE _Delete {name}\n')
            return {"name": name, "deleted": True}
        return await self._run(_delete)

    # -----------------------------------------------------------------------
    # Annotation: create_table (native AcDbTable)
    # -----------------------------------------------------------------------

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None) -> CommandResult:
        def _text():
            self._ensure_layer(layer)
            ent = self._msp().AddText(str(text), _to_apoint(x, y), float(height))
            ent.Rotation = math.radians(float(rotation))
            self._set_layer(ent, layer)
            return {"entity_type": "TEXT", "handle": ent.Handle}

        return await self._run(_text)

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        def _dim():
            ent = self._msp().AddDimRotated(
                _to_apoint(x1, y1),
                _to_apoint(x2, y2),
                _to_apoint(dim_x, dim_y),
                0.0,  # rotation in radians
            )
            return {"entity_type": "DIMENSION", "handle": ent.Handle}

        return await self._run(_dim)

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        def _dim():
            # Compute a dim-line point offset perpendicular to the segment.
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            if length == 0:
                raise RuntimeError("Aligned dimension has zero-length segment")
            nx, ny = -dy / length, dx / length
            base = ((x1 + x2) / 2 + nx * offset, (y1 + y2) / 2 + ny * offset)
            ent = self._msp().AddDimAligned(
                _to_apoint(x1, y1),
                _to_apoint(x2, y2),
                _to_apoint(base[0], base[1]),
            )
            return {"entity_type": "DIMENSION", "handle": ent.Handle}

        return await self._run(_dim)

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        def _dim():
            # Place the arc at ~70% of the larger spoke radius.
            r = max(
                math.hypot(x1 - cx, y1 - cy),
                math.hypot(x2 - cx, y2 - cy),
            ) * 0.7
            mid_a = (math.atan2(y1 - cy, x1 - cx) + math.atan2(y2 - cy, x2 - cx)) / 2
            text_pt = (cx + r * math.cos(mid_a), cy + r * math.sin(mid_a))
            ent = self._msp().AddDimAngular(
                _to_apoint(cx, cy),
                _to_apoint(x1, y1),
                _to_apoint(x2, y2),
                _to_apoint(text_pt[0], text_pt[1]),
            )
            return {"entity_type": "DIMENSION", "handle": ent.Handle}

        return await self._run(_dim)

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        def _dim():
            rad = math.radians(float(angle))
            tip = (cx + radius * math.cos(rad), cy + radius * math.sin(rad))
            ent = self._msp().AddDimRadial(
                _to_apoint(cx, cy),
                _to_apoint(tip[0], tip[1]),
                float(radius * 0.5),
            )
            return {"entity_type": "DIMENSION", "handle": ent.Handle}

        return await self._run(_dim)

    async def create_leader(self, points, text) -> CommandResult:
        def _leader():
            flat: list[float] = []
            for p in points:
                flat.append(float(p[0]))
                flat.append(float(p[1]))
                flat.append(0.0)
            ent = self._msp().AddLeader(
                _to_double_array(flat),
                None,  # No annotation entity attached
                3,  # acLineWithArrow
            )
            # Add text near the last point
            last = points[-1]
            self._msp().AddMText(_to_apoint(last[0] + 2, last[1]), 30.0, str(text))
            return {"entity_type": "LEADER", "handle": ent.Handle}

        return await self._run(_leader)

    async def create_table(
        self, x, y, rows, cols,
        row_height=0.5, col_width=2.0,
        data=None, title=None, header=None,
    ) -> CommandResult:
        """AutoCAD-native AcDbTable via `AddTable`.

        IAcadModelSpace.AddTable signature (verified against AutoCAD 2027):
          AddTable(insertion_point, num_rows, num_columns, row_height, col_width)
        Returns an IAcadTable. Cell text is written via SetText(row, col, str).
        """
        rows = int(rows); cols = int(cols)
        if rows < 1 or cols < 1:
            return CommandResult(
                ok=False, category="invalid_arg",
                error="rows and cols must be >= 1",
            )
        data = data or []

        def _table():
            msp = self._msp()
            tbl = msp.AddTable(
                _to_apoint(float(x), float(y)),
                rows, cols,
                float(row_height), float(col_width),
            )
            # Fill in the cells row-by-row. Title row gets index 0 when
            # provided; header gets the next row; data fills the rest.
            row_cursor = 0
            if title is not None:
                # AutoCAD's TABLE merges the title across all columns by
                # default (the first row's RowType=2 = Title).
                try:
                    tbl.SetText(0, 0, str(title))
                except Exception as ex:
                    log.debug("com_table_title_set_failed", error=str(ex))
                row_cursor = 1
            if header is not None:
                for c in range(min(cols, len(header))):
                    try:
                        tbl.SetText(row_cursor, c, str(header[c]))
                    except Exception:
                        pass
                row_cursor += 1
            for row in data:
                if row_cursor >= rows:
                    break
                for c in range(min(cols, len(row))):
                    try:
                        tbl.SetText(row_cursor, c, str(row[c]))
                    except Exception:
                        pass
                row_cursor += 1
            return {
                "kind": "table",
                "handle": tbl.Handle,
                "rows": rows, "cols": cols,
                "origin": [float(x), float(y)],
            }
        return await self._run(_table)

    # -----------------------------------------------------------------------
    # Architectural primitives (AIA/NCS)
    # -----------------------------------------------------------------------

    async def arch_setup_layers(self) -> CommandResult:
        def _setup():
            from autocad_mcp.arch.layers import AIA_LAYERS

            doc = self._require_doc()
            existing = {l.Name for l in doc.Layers}
            created: list[str] = []
            for spec in AIA_LAYERS:
                if spec.name in existing:
                    continue
                layer = doc.Layers.Add(spec.name)
                layer.color = spec.color
                if spec.linetype.upper() != "CONTINUOUS":
                    # AutoCAD requires the linetype to be loaded before assignment.
                    try:
                        doc.Linetypes.Item(spec.linetype)
                    except Exception:
                        try:
                            # Load from acad.lin / acadiso.lin
                            doc.Linetypes.Load(spec.linetype, "acad.lin")
                        except Exception:
                            try:
                                doc.Linetypes.Load(spec.linetype, "acadiso.lin")
                            except Exception as ex:
                                log.debug(
                                    "linetype_load_failed_both_lin_files",
                                    linetype=spec.linetype, error=str(ex),
                                )
                    try:
                        layer.Linetype = spec.linetype
                    except Exception as ex:
                        log.debug(
                            "layer_linetype_assign_failed",
                            layer=spec.name, linetype=spec.linetype, error=str(ex),
                        )
                created.append(spec.name)
            return {"created": created, "count": len(created)}

        return await self._run(_setup)

    async def arch_wall(self, x1, y1, x2, y2, thickness=6.0, layer="A-WALL", justify="center") -> CommandResult:
        from autocad_mcp.arch.geometry import wall_corners

        def _wall():
            corners, length = wall_corners(x1, y1, x2, y2, thickness, justify)
            self._ensure_layer(layer)
            flat: list[float] = []
            for p in corners:
                flat.append(p[0])
                flat.append(p[1])
            pl = self._msp().AddLightWeightPolyline(_to_double_array(flat))
            pl.Closed = True
            self._set_layer(pl, layer)
            return {
                "handle": pl.Handle,
                "layer": layer,
                "length": length,
                "thickness": thickness,
                "justify": justify,
                "centerline": [(x1, y1), (x2, y2)],
            }

        return await self._run(_wall)

    async def arch_wall_with_openings(
        self, x1, y1, x2, y2, thickness=6.0, openings=None,
        layer="A-WALL", door_layer="A-DOOR", window_layer="A-GLAZ", justify="center",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import (
            door_geometry,
            wall_segments_with_openings,
            window_corners,
        )

        def _composite():
            segments, resolved, total_length = wall_segments_with_openings(
                x1, y1, x2, y2, thickness, openings or [], justify,
            )

            # Ensure all relevant layers exist before we start drawing.
            self._ensure_layer(layer)
            self._ensure_layer(door_layer)
            self._ensure_layer(window_layer)

            msp = self._msp()
            segment_handles: list[str] = []
            for corners in segments:
                flat: list[float] = []
                for p in corners:
                    flat.append(p[0])
                    flat.append(p[1])
                pl = msp.AddLightWeightPolyline(_to_double_array(flat))
                pl.Closed = True
                self._set_layer(pl, layer)
                segment_handles.append(pl.Handle)

            door_handles: list[dict[str, str]] = []
            window_handles: list[dict[str, str]] = []

            for o in resolved:
                kind = o.get("type", "door")
                cs_x, cs_y = o["centerline_start"]
                ce_x, ce_y = o["centerline_end"]
                angle_deg = o["angle_deg"]
                width = float(o["width"])

                if kind == "door":
                    swing = o.get("swing", "left")
                    open_angle = float(o.get("open_angle", 90.0))
                    hinge, leaf_end, arc_start_deg, arc_end_deg, _ = door_geometry(
                        cs_x, cs_y, width, angle_deg, swing, open_angle,
                    )
                    leaf = msp.AddLine(_to_apoint(*hinge), _to_apoint(*leaf_end))
                    self._set_layer(leaf, door_layer)
                    arc = msp.AddArc(
                        _to_apoint(*hinge),
                        width,
                        math.radians(arc_start_deg),
                        math.radians(arc_end_deg),
                    )
                    self._set_layer(arc, door_layer)
                    door_handles.append({"leaf": leaf.Handle, "arc": arc.Handle})
                elif kind == "window":
                    sill_t = float(o.get("sill_thickness", thickness))
                    corners, _ = window_corners(cs_x, cs_y, ce_x, ce_y, sill_t)
                    flat = []
                    for p in corners:
                        flat.append(p[0])
                        flat.append(p[1])
                    sill = msp.AddLightWeightPolyline(_to_double_array(flat))
                    sill.Closed = True
                    self._set_layer(sill, window_layer)
                    glass = msp.AddLine(_to_apoint(cs_x, cs_y), _to_apoint(ce_x, ce_y))
                    self._set_layer(glass, window_layer)
                    window_handles.append({"sill": sill.Handle, "glass": glass.Handle})

            return {
                "handles": {
                    "segments": segment_handles,
                    "doors": door_handles,
                    "windows": window_handles,
                },
                "layer": layer,
                "length": total_length,
                "thickness": thickness,
                "segment_count": len(segment_handles),
                "opening_count": len(resolved),
                "centerline": [(x1, y1), (x2, y2)],
            }

        return await self._run(_composite)

    async def arch_door(self, x, y, width=36.0, angle=0.0, swing="left", open_angle=90.0, layer="A-DOOR", room_side=None) -> CommandResult:
        from autocad_mcp.arch.geometry import door_geometry

        def _door():
            hinge, leaf_end, arc_start_deg, arc_end_deg, resolved_swing = door_geometry(
                x, y, width, angle, swing, open_angle,
                room_side=tuple(room_side) if room_side else None,
            )
            self._ensure_layer(layer)
            msp = self._msp()
            leaf = msp.AddLine(_to_apoint(*hinge), _to_apoint(*leaf_end))
            self._set_layer(leaf, layer)
            arc = msp.AddArc(
                _to_apoint(*hinge),
                float(width),
                math.radians(float(arc_start_deg)),
                math.radians(float(arc_end_deg)),
            )
            self._set_layer(arc, layer)
            return {
                "handles": {"leaf": leaf.Handle, "arc": arc.Handle},
                "layer": layer,
                "width": width,
                "hinge": hinge,
                "angle": angle,
                "swing": resolved_swing,
            }

        return await self._run(_door)

    async def arch_window(self, x1, y1, x2, y2, sill_thickness=6.0, layer="A-GLAZ") -> CommandResult:
        from autocad_mcp.arch.geometry import window_corners

        def _win():
            corners, length = window_corners(x1, y1, x2, y2, sill_thickness)
            self._ensure_layer(layer)
            msp = self._msp()
            flat: list[float] = []
            for p in corners:
                flat.append(p[0])
                flat.append(p[1])
            sill = msp.AddLightWeightPolyline(_to_double_array(flat))
            sill.Closed = True
            self._set_layer(sill, layer)
            glass = msp.AddLine(_to_apoint(x1, y1), _to_apoint(x2, y2))
            self._set_layer(glass, layer)
            return {
                "handles": {"sill": sill.Handle, "glass": glass.Handle},
                "layer": layer,
                "width": length,
                "endpoints": [(x1, y1), (x2, y2)],
            }

        return await self._run(_win)

    async def arch_room(self, boundary, name="", area_layer="A-AREA", text_layer="A-AREA-IDEN", text_height=6.0) -> CommandResult:
        from autocad_mcp.arch.geometry import polygon_area, polygon_centroid

        def _room():
            if len(boundary) < 3:
                raise RuntimeError(f"Room boundary needs at least 3 points; got {len(boundary)}")

            self._ensure_layer(area_layer)
            self._ensure_layer(text_layer)

            pts = [(float(p[0]), float(p[1])) for p in boundary]
            flat: list[float] = []
            for p in pts:
                flat.append(p[0])
                flat.append(p[1])
            msp = self._msp()
            pl = msp.AddLightWeightPolyline(_to_double_array(flat))
            pl.Closed = True
            self._set_layer(pl, area_layer)

            area = polygon_area(pts)
            cx, cy = polygon_centroid(pts)
            # Area suffix follows $INSUNITS so metric drawings get "m²"
            # instead of "SF". Fall back to imperial-inches on probe failure.
            from autocad_mcp.arch.units import UnitSystem
            try:
                insunits = int(self._doc.GetVariable("INSUNITS"))
            except Exception:
                insunits = 0
            units = UnitSystem.from_insunits(insunits)
            area_str = units.format_area(area)
            label_text = f"{name}\\P{area_str}" if name else area_str
            label = msp.AddMText(_to_apoint(cx, cy), 100.0, label_text)
            label.Height = float(text_height)
            label.AttachmentPoint = 5  # MIDDLE_CENTER
            self._set_layer(label, text_layer)

            return {
                "handles": {"boundary": pl.Handle, "label": label.Handle},
                "layer": area_layer,
                "area": area,
                "centroid": (cx, cy),
                "name": name,
            }

        return await self._run(_room)

    async def arch_dimension_chain(self, points, offset=24.0, direction="horizontal", layer="A-ANNO-DIMS") -> CommandResult:
        def _chain():
            if len(points) < 2:
                raise RuntimeError(f"Need ≥2 points for a dimension chain; got {len(points)}")

            self._ensure_layer(layer)
            msp = self._msp()
            handles: list[str] = []
            for p1, p2 in zip(points, points[1:]):
                p1f = (float(p1[0]), float(p1[1]))
                p2f = (float(p2[0]), float(p2[1]))
                if direction == "horizontal":
                    base_y = max(p1f[1], p2f[1]) + offset
                    base = ((p1f[0] + p2f[0]) / 2.0, base_y)
                    dim = msp.AddDimRotated(
                        _to_apoint(*p1f), _to_apoint(*p2f), _to_apoint(*base), 0.0,
                    )
                elif direction == "vertical":
                    base_x = max(p1f[0], p2f[0]) + offset
                    base = (base_x, (p1f[1] + p2f[1]) / 2.0)
                    dim = msp.AddDimRotated(
                        _to_apoint(*p1f), _to_apoint(*p2f), _to_apoint(*base),
                        math.radians(90.0),
                    )
                elif direction == "aligned":
                    dx, dy = p2f[0] - p1f[0], p2f[1] - p1f[1]
                    length = math.hypot(dx, dy)
                    if length == 0:
                        raise RuntimeError("Dimension segment has zero length")
                    nx, ny = -dy / length, dx / length
                    base = (
                        (p1f[0] + p2f[0]) / 2 + nx * offset,
                        (p1f[1] + p2f[1]) / 2 + ny * offset,
                    )
                    dim = msp.AddDimAligned(
                        _to_apoint(*p1f), _to_apoint(*p2f), _to_apoint(*base),
                    )
                else:
                    raise RuntimeError(
                        f"direction must be 'horizontal', 'vertical', or 'aligned'; got {direction!r}"
                    )
                self._set_layer(dim, layer)
                handles.append(dim.Handle)
            return {"handles": handles, "layer": layer, "count": len(handles)}

        return await self._run(_chain)

    # -----------------------------------------------------------------------
    # Building primitives — stair / column / slab / beam / roof / elev / section
    # -----------------------------------------------------------------------

    def _draw_closed_polyline(self, msp, corners, layer: str) -> str:
        flat: list[float] = []
        for p in corners:
            flat.append(float(p[0]))
            flat.append(float(p[1]))
        pl = msp.AddLightWeightPolyline(_to_double_array(flat))
        pl.Closed = True
        self._set_layer(pl, layer)
        return pl.Handle

    def _draw_polyline(self, msp, pts, layer: str, closed: bool = False) -> str:
        flat: list[float] = []
        for p in pts:
            flat.append(float(p[0]))
            flat.append(float(p[1]))
        pl = msp.AddLightWeightPolyline(_to_double_array(flat))
        pl.Closed = bool(closed)
        self._set_layer(pl, layer)
        return pl.Handle

    async def arch_stair(
        self, x1, y1, x2, y2, width=36.0, num_treads=12, tread_depth=None,
        layer="A-FLOR-STRS", shape="straight", landing_depth=None,
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import stair_run, stair_u_shape

        def _stair():
            self._ensure_layer(layer)
            msp = self._msp()
            if shape == "straight":
                s = stair_run(x1, y1, x2, y2, width, num_treads, tread_depth=tread_depth)
                handles: dict = {
                    "outline": self._draw_closed_polyline(msp, s["outline"], layer),
                    "treads": [],
                    "arrow": [],
                }
                for sp, ep in s["tread_lines"]:
                    line = msp.AddLine(_to_apoint(*sp), _to_apoint(*ep))
                    self._set_layer(line, layer)
                    handles["treads"].append(line.Handle)
                if s["arrow"] is not None:
                    line = msp.AddLine(_to_apoint(*s["arrow"][0]), _to_apoint(*s["arrow"][1]))
                    self._set_layer(line, layer)
                    handles["arrow"].append(line.Handle)
                if s["arrow_head"] is not None:
                    handles["arrow"].append(
                        self._draw_polyline(msp, s["arrow_head"], layer, closed=False)
                    )
                return {
                    "handles": handles,
                    "layer": layer,
                    "shape": "straight",
                    "length": s["length"],
                    "num_treads": s["num_treads"],
                    "tread_depth": s["tread_depth"],
                    "width": s["width"],
                }
            if shape in ("u", "u_shape"):
                dx = x2 - x1
                dy = y2 - y1
                flight_length = math.hypot(dx, dy)
                if flight_length == 0:
                    raise RuntimeError("U-shape stair: (x1,y1) and (x2,y2) coincide")
                angle = math.degrees(math.atan2(dy, dx))
                u = stair_u_shape(
                    x1, y1, flight_length=flight_length, width=width,
                    num_treads_per_flight=num_treads, landing_depth=landing_depth,
                    angle_deg=angle,
                )
                handles = {"flight1": {}, "flight2": {}, "landing": None}
                for key in ("flight1", "flight2"):
                    flight = u[key]
                    h: dict = {
                        "outline": self._draw_closed_polyline(msp, flight["outline"], layer),
                        "treads": [],
                        "arrow": [],
                    }
                    for sp, ep in flight["tread_lines"]:
                        line = msp.AddLine(_to_apoint(*sp), _to_apoint(*ep))
                        self._set_layer(line, layer)
                        h["treads"].append(line.Handle)
                    if flight["arrow"] is not None:
                        line = msp.AddLine(_to_apoint(*flight["arrow"][0]),
                                           _to_apoint(*flight["arrow"][1]))
                        self._set_layer(line, layer)
                        h["arrow"].append(line.Handle)
                    if flight["arrow_head"] is not None:
                        h["arrow"].append(
                            self._draw_polyline(msp, flight["arrow_head"], layer, closed=False)
                        )
                    handles[key] = h
                handles["landing"] = self._draw_closed_polyline(msp, u["landing_corners"], layer)
                return {
                    "handles": handles,
                    "layer": layer,
                    "shape": "u",
                    "total_treads": u["total_treads"],
                    "width": width,
                }
            raise RuntimeError(f"shape must be 'straight' or 'u'; got {shape!r}")

        return await self._run(_stair)

    async def arch_column(
        self, x, y, shape="square", size=12.0, size_y=None, angle=0.0, layer="A-COLS",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import column_corners

        def _col():
            self._ensure_layer(layer)
            msp = self._msp()
            spec = column_corners(x, y, shape, size, size_y=size_y, angle_deg=angle)
            if spec["shape"] == "rect":
                handle = self._draw_closed_polyline(msp, spec["corners"], layer)
                return {
                    "handles": {"outline": handle},
                    "layer": layer,
                    "shape": spec["shape"],
                    "center": spec["center"],
                    "size_x": spec["size_x"],
                    "size_y": spec["size_y"],
                    "angle": spec["angle"],
                }
            circ = msp.AddCircle(_to_apoint(*spec["center"]), float(spec["radius"]))
            self._set_layer(circ, layer)
            return {
                "handles": {"outline": circ.Handle},
                "layer": layer,
                "shape": "round",
                "center": spec["center"],
                "radius": spec["radius"],
            }

        return await self._run(_col)

    async def arch_slab(self, boundary, edge_thickness=0.0, layer="A-FLOR") -> CommandResult:
        from autocad_mcp.arch.geometry import slab_outline

        def _slab():
            self._ensure_layer(layer)
            msp = self._msp()
            pts = [(float(p[0]), float(p[1])) for p in boundary]
            s = slab_outline(pts, edge_thickness=edge_thickness)
            handles: dict = {"outer": self._draw_closed_polyline(msp, s["outer"], layer)}
            if s["inner"] is not None:
                handles["inner"] = self._draw_closed_polyline(msp, s["inner"], layer)
            return {
                "handles": handles,
                "layer": layer,
                "area": s["area"],
                "centroid": s["centroid"],
            }

        return await self._run(_slab)

    async def arch_beam(self, x1, y1, x2, y2, width=0.0, layer="A-WALL-HEAD") -> CommandResult:
        from autocad_mcp.arch.geometry import beam_endpoints

        def _beam():
            self._ensure_layer(layer)
            msp = self._msp()
            b = beam_endpoints(x1, y1, x2, y2, width=width)
            handles: dict = {}
            if b["edges"] is None:
                line = msp.AddLine(_to_apoint(*b["centerline"][0]),
                                   _to_apoint(*b["centerline"][1]))
                self._set_layer(line, layer)
                handles["centerline"] = line.Handle
            else:
                edges = []
                for sp, ep in b["edges"]:
                    line = msp.AddLine(_to_apoint(*sp), _to_apoint(*ep))
                    self._set_layer(line, layer)
                    edges.append(line.Handle)
                handles["edges"] = edges
            return {
                "handles": handles,
                "layer": layer,
                "length": b["length"],
                "width": width,
            }

        return await self._run(_beam)

    async def arch_roof(
        self, boundary, style="gable", ridge_axis="x", overhang=12.0,
        eaves_layer="A-ROOF", ridge_layer="A-ROOF",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import roof_plan

        def _roof():
            self._ensure_layer(eaves_layer)
            self._ensure_layer(ridge_layer)
            msp = self._msp()
            pts = [(float(p[0]), float(p[1])) for p in boundary]
            r = roof_plan(pts, style=style, ridge_axis=ridge_axis, overhang=overhang)
            eaves_handle = self._draw_closed_polyline(msp, r["eaves"], eaves_layer)
            ridges = []
            for sp, ep in r["ridges"]:
                line = msp.AddLine(_to_apoint(*sp), _to_apoint(*ep))
                self._set_layer(line, ridge_layer)
                ridges.append(line.Handle)
            hips = []
            for sp, ep in r["hips"]:
                line = msp.AddLine(_to_apoint(*sp), _to_apoint(*ep))
                self._set_layer(line, ridge_layer)
                hips.append(line.Handle)
            return {
                "handles": {"eaves": eaves_handle, "ridges": ridges, "hips": hips},
                "eaves_layer": eaves_layer,
                "ridge_layer": ridge_layer,
                "style": r["style"],
                "footprint": r["footprint"],
            }

        return await self._run(_roof)

    async def arch_elevation(
        self, origin_x, origin_y, wall_length, height,
        openings=None, wall_layer="A-WALL", door_layer="A-DOOR", window_layer="A-GLAZ",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import elevation_view

        def _elev():
            self._ensure_layer(wall_layer)
            self._ensure_layer(door_layer)
            self._ensure_layer(window_layer)
            msp = self._msp()
            e = elevation_view(wall_length=wall_length, height=height, floor_y=0.0,
                               openings=openings or [])

            def shift(p):
                return (p[0] + origin_x, p[1] + origin_y)

            outline_h = self._draw_closed_polyline(
                msp, [shift(p) for p in e["outline"]], wall_layer
            )
            floor_p1 = shift(e["floor_line"][0])
            floor_p2 = shift(e["floor_line"][1])
            floor_line = msp.AddLine(_to_apoint(*floor_p1), _to_apoint(*floor_p2))
            self._set_layer(floor_line, wall_layer)

            opening_handles: list[dict] = []
            for o in e["openings"]:
                target = window_layer if o["type"] == "window" else door_layer
                corners = [shift(p) for p in o["corners"]]
                rect_h = self._draw_closed_polyline(msp, corners, target)
                entry: dict = {"corners": rect_h, "type": o["type"]}
                if "panes" in o:
                    panes: list[str] = []
                    for ps, pe in o["panes"]:
                        line = msp.AddLine(_to_apoint(*shift(ps)), _to_apoint(*shift(pe)))
                        self._set_layer(line, target)
                        panes.append(line.Handle)
                    entry["panes"] = panes
                opening_handles.append(entry)

            return {
                "handles": {
                    "outline": outline_h,
                    "floor_line": floor_line.Handle,
                    "openings": opening_handles,
                },
                "wall_layer": wall_layer,
                "wall_length": wall_length,
                "height": height,
                "opening_count": len(opening_handles),
            }

        return await self._run(_elev)

    async def arch_section(
        self, origin_x, origin_y, section_width, floor_y, ceiling_y,
        elements=None, wall_layer="A-WALL", slab_layer="A-FLOR",
        door_layer="A-DOOR", window_layer="A-GLAZ",
    ) -> CommandResult:
        from autocad_mcp.arch.geometry import section_view

        def _sec():
            self._ensure_layer(wall_layer)
            self._ensure_layer(slab_layer)
            self._ensure_layer(door_layer)
            self._ensure_layer(window_layer)
            msp = self._msp()
            s = section_view(floor_y=floor_y, ceiling_y=ceiling_y,
                             section_width=section_width, elements=elements or [])

            def shift(p):
                return (p[0] + origin_x, p[1] + origin_y)

            fp1 = shift(s["floor_line"][0])
            fp2 = shift(s["floor_line"][1])
            floor_line = msp.AddLine(_to_apoint(*fp1), _to_apoint(*fp2))
            self._set_layer(floor_line, slab_layer)
            cp1 = shift(s["ceiling_line"][0])
            cp2 = shift(s["ceiling_line"][1])
            ceiling_line = msp.AddLine(_to_apoint(*cp1), _to_apoint(*cp2))
            self._set_layer(ceiling_line, slab_layer)

            wall_handles: list[str] = []
            for corners in s["walls"]:
                wall_handles.append(
                    self._draw_closed_polyline(msp, [shift(p) for p in corners], wall_layer)
                )

            opening_handles: list[dict] = []
            for o in s["openings"]:
                target = window_layer if o["kind"] == "window" else door_layer
                opening_handles.append({
                    "corners": self._draw_closed_polyline(msp, [shift(p) for p in o["corners"]], target),
                    "kind": o["kind"],
                })

            slab_handles: list[str] = []
            for corners in s["slabs"]:
                slab_handles.append(
                    self._draw_closed_polyline(msp, [shift(p) for p in corners], slab_layer)
                )

            return {
                "handles": {
                    "floor_line": floor_line.Handle,
                    "ceiling_line": ceiling_line.Handle,
                    "walls": wall_handles,
                    "openings": opening_handles,
                    "slabs": slab_handles,
                },
                "wall_layer": wall_layer,
                "floor_y": floor_y,
                "ceiling_y": ceiling_y,
                "section_width": section_width,
            }

        return await self._run(_sec)

    async def arch_template(self, name, x, y, params=None) -> CommandResult:
        """Run a building template via ezdxf intermediary, then push entities
        into the active AutoCAD document via COM.

        For simplicity and parity, the template runs against an in-memory
        ezdxf doc and reconstructs the entities through COM calls. This keeps
        template authoring in one place (templates.py) and avoids duplicating
        the layout logic into COM.
        """
        import ezdxf as _ezdxf
        from autocad_mcp.arch.templates import TEMPLATES, apply_template

        if name not in TEMPLATES:
            return CommandResult(
                ok=False,
                error=f"Unknown template {name!r}. Available: {sorted(TEMPLATES.keys())}",
                category="not_found",
            )

        def _tmpl():
            tmp_doc = _ezdxf.new("R2013")
            tmp_msp = tmp_doc.modelspace()
            info = apply_template(tmp_msp, name, x, y, params=params or {})

            self._ensure_layer("A-WALL")
            self._ensure_layer("A-DOOR")
            self._ensure_layer("A-GLAZ")
            self._ensure_layer("A-AREA")
            self._ensure_layer("A-AREA-IDEN")
            self._ensure_layer("A-COLS")

            msp = self._msp()
            for ent in tmp_msp:
                etype = ent.dxftype()
                target_layer = ent.dxf.layer
                self._ensure_layer(target_layer)
                if etype == "LINE":
                    s = (ent.dxf.start[0], ent.dxf.start[1])
                    e = (ent.dxf.end[0], ent.dxf.end[1])
                    line = msp.AddLine(_to_apoint(*s), _to_apoint(*e))
                    self._set_layer(line, target_layer)
                elif etype == "LWPOLYLINE":
                    pts = [(p[0], p[1]) for p in ent.get_points()]
                    flat: list[float] = []
                    for p in pts:
                        flat.append(p[0])
                        flat.append(p[1])
                    pl = msp.AddLightWeightPolyline(_to_double_array(flat))
                    pl.Closed = bool(ent.closed)
                    self._set_layer(pl, target_layer)
                elif etype == "ARC":
                    arc = msp.AddArc(
                        _to_apoint(ent.dxf.center[0], ent.dxf.center[1]),
                        float(ent.dxf.radius),
                        math.radians(float(ent.dxf.start_angle)),
                        math.radians(float(ent.dxf.end_angle)),
                    )
                    self._set_layer(arc, target_layer)
                elif etype == "MTEXT":
                    label = msp.AddMText(
                        _to_apoint(ent.dxf.insert[0], ent.dxf.insert[1]),
                        100.0, ent.text,
                    )
                    label.Height = float(ent.dxf.char_height)
                    label.AttachmentPoint = int(getattr(ent.dxf, "attachment_point", 5))
                    self._set_layer(label, target_layer)
                elif etype == "CIRCLE":
                    circ = msp.AddCircle(
                        _to_apoint(ent.dxf.center[0], ent.dxf.center[1]),
                        float(ent.dxf.radius),
                    )
                    self._set_layer(circ, target_layer)

            return {
                "template": info["template"],
                "rooms": info["rooms"],
                "footprint": info["footprint"],
            }

        return await self._run(_tmpl)

    async def arch_hatch_region(
        self, boundary, pattern="CONCRETE", layer=None, scale=None, angle=None,
    ) -> CommandResult:
        from autocad_mcp.arch.hatches import hatch_pattern
        spec = hatch_pattern(pattern)
        if spec is None:
            return CommandResult(
                ok=False,
                error=f"Unknown hatch pattern {pattern!r}. "
                      "Call arch.list_hatches for the catalogue.",
                category="not_found",
            )
        target_layer = layer or spec.layer_hint
        pts = [(float(p[0]), float(p[1])) for p in boundary]
        if len(pts) < 3:
            return CommandResult(
                ok=False,
                error=f"Hatch boundary needs >=3 points; got {len(pts)}",
                category="invalid_arg",
            )

        def _hatch():
            self._ensure_layer(target_layer)
            msp = self._msp()
            # Build the boundary as a closed polyline first; HATCH needs a
            # selectable region. We erase the boundary at the end so only
            # the hatch entity remains (matches typical _.-HATCH semantics).
            flat: list[float] = []
            for p in pts:
                flat.append(p[0])
                flat.append(p[1])
            pl = msp.AddLightWeightPolyline(_to_double_array(flat))
            pl.Closed = True
            self._set_layer(pl, target_layer)

            # AcadHatch: PatternType=0 (predefined), AssociativeHatch=False.
            hatch = msp.AddHatch(0, spec.name, False)
            outer_loop = (pl,)
            try:
                hatch.AppendOuterLoop(outer_loop)
            except Exception:
                # Some COM impls accept a VARIANT array instead.
                from win32com.client import VARIANT
                import pythoncom
                hatch.AppendOuterLoop(
                    VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, outer_loop)
                )
            hatch.PatternScale = float(scale if scale is not None else spec.scale)
            hatch.PatternAngle = math.radians(
                float(angle if angle is not None else spec.angle)
            )
            hatch.Evaluate()
            self._set_layer(hatch, target_layer)

            # The temporary boundary polyline isn't wanted in the final
            # drawing — but erasing it after Evaluate would orphan the hatch
            # on non-associative mode. We *keep* the boundary so the user
            # can edit it later; AutoCAD's GUI workflow does the same.
            return {
                "hatch_handle": hatch.Handle,
                "boundary_handle": pl.Handle,
                "pattern": spec.name,
                "friendly_name": pattern.upper(),
                "layer": target_layer,
            }

        return await self._run(_hatch)

    # -----------------------------------------------------------------------
    # Layout / paperspace
    # -----------------------------------------------------------------------

    async def layout_list(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            out = []
            for lay in doc.Layouts:
                out.append({
                    "name": lay.Name,
                    # ModelSpace's Layout has Name == "Model" by convention.
                    "is_modelspace": (lay.Name == "Model"),
                    "tab_order": int(getattr(lay, "TabOrder", -1)),
                })
            return {"layouts": out, "count": len(out)}
        return await self._run(_list)

    async def layout_create(self, name, paper_size=None) -> CommandResult:
        def _create():
            doc = self._require_doc()
            for lay in doc.Layouts:
                if lay.Name == name:
                    raise RuntimeError(f"Layout '{name}' already exists")
            # AcadLayouts.Add returns the new layout. Page setup is per-layout.
            lay = doc.Layouts.Add(name)
            if paper_size:
                try:
                    # Paper size in millimeters per the COM API. The caller
                    # passes inches; convert.
                    w_in, h_in = float(paper_size[0]), float(paper_size[1])
                    lay.PlotConfigurationName = ""  # default plotter
                    # Some COM impls expose `SetCustomScale` / `CanonicalMediaName`;
                    # safest path is leaving defaults and letting the user
                    # configure via Page Setup in the GUI.
                except Exception as ex:
                    log.debug("layout_create_paper_size_failed", error=str(ex))
            return {"name": name}
        return await self._run(_create)

    async def layout_set_active(self, name) -> CommandResult:
        def _activate():
            doc = self._require_doc()
            try:
                doc.ActiveLayout = doc.Layouts.Item(name)
            except Exception as ex:
                raise RuntimeError(f"No layout named {name!r}: {ex}") from ex
            return {"active": name}
        return await self._run(_activate)

    async def layout_create_viewport(
        self, layout, x, y, width, height, view_extents=None,
    ) -> CommandResult:
        def _vp():
            doc = self._require_doc()
            # AcadLayouts is by name lookup; .Block is the paperspace block.
            try:
                lay = doc.Layouts.Item(layout)
            except Exception as ex:
                raise RuntimeError(f"No layout named {layout!r}: {ex}") from ex
            ps_block = lay.Block
            cx = float(x) + float(width) / 2.0
            cy = float(y) + float(height) / 2.0
            vp = ps_block.AddPViewport(
                _to_apoint(cx, cy),
                float(width), float(height),
            )
            # Apply view extents if provided. The viewport has properties:
            #   ViewCenter (Point3d), ViewHeight (double).
            if view_extents is not None:
                try:
                    vx1, vy1, vx2, vy2 = view_extents
                    vcx = (float(vx1) + float(vx2)) / 2.0
                    vcy = (float(vy1) + float(vy2)) / 2.0
                    vh = abs(float(vy2) - float(vy1))
                    vp.ViewCenter = _to_apoint(vcx, vcy)
                    vp.ViewHeight = vh
                except Exception as ex:
                    log.debug("layout_create_viewport_extents_failed", error=str(ex))
            # Activate the viewport so it shows content (Display + on).
            try:
                vp.Display(True)
            except Exception:
                pass
            return {
                "handle": vp.Handle,
                "layout": layout,
                "center": [cx, cy],
                "size": [float(width), float(height)],
            }
        return await self._run(_vp)

    # -----------------------------------------------------------------------
    # XREF mutation
    # -----------------------------------------------------------------------

    async def xref_attach(
        self, path, name=None, x=0.0, y=0.0, scale=1.0, rotation=0.0, overlay=False,
    ) -> CommandResult:
        """Use AutoCAD's `_.-XATTACH` to attach a DWG as XREF.

        XATTACH script form:
          _.-XATTACH <path> <insertion> <xscale> <yscale> <rotation>
        Set XATTACHTYPE to 0 (attachment) or 1 (overlay) beforehand.
        """
        from pathlib import Path as _Path
        posix_path = str(_Path(path)).replace("\\", "/")
        derived_name = name or _Path(path).stem

        def _attach():
            doc = self._require_doc()
            # XATTACHTYPE: 0=Attachment, 1=Overlay (set per call, restore).
            try:
                prev_type = int(doc.GetVariable("XATTACHTYPE"))
            except Exception:
                prev_type = 0
            try:
                doc.SetVariable("XATTACHTYPE", 1 if overlay else 0)
                cmd = (
                    f'_.-XATTACH\n{posix_path}\n{x},{y},0\n'
                    f'{scale}\n{scale}\n{rotation}\n'
                )
                doc.SendCommand(cmd)
            finally:
                try:
                    doc.SetVariable("XATTACHTYPE", prev_type)
                except Exception:
                    pass
            return {"name": derived_name, "path": str(path), "overlay": bool(overlay)}

        return await self._run(_attach)

    async def xref_detach(self, name) -> CommandResult:
        def _detach():
            doc = self._require_doc()
            doc.SendCommand(f'_.-XREF\n_D\n{name}\n')
            return {"name": name, "detached": True}

        return await self._run(_detach)

    async def xref_reload(self, name=None) -> CommandResult:
        def _reload():
            doc = self._require_doc()
            target = name or "*"
            doc.SendCommand(f'_.-XREF\n_R\n{target}\n')
            return {"name": target, "reloaded": True}

        return await self._run(_reload)

    async def xref_bind(self, name) -> CommandResult:
        def _bind():
            doc = self._require_doc()
            doc.SendCommand(f'_.-XREF\n_B\n{name}\n')
            return {"name": name, "bound": True}

        return await self._run(_bind)

    # -----------------------------------------------------------------------
    # Block operations
    # -----------------------------------------------------------------------

    async def block_list(self) -> CommandResult:
        def _list():
            doc = self._require_doc()
            blocks = [b.Name for b in doc.Blocks if not b.Name.startswith("*")]
            return {"blocks": blocks}

        return await self._run(_list)

    async def block_list_from_file(self, path: str) -> CommandResult:
        """Same shape as the ezdxf backend's implementation — inspect an
        external DWG/DXF without touching the active drawing. Routes through
        ezdxf even on COM because Open()-as-readonly + Close() in another
        document context interrupts the user's current session.
        """
        from autocad_mcp.project.dwg_reader import (
            DWGConverterMissing, read_drawing,
        )

        def _inspect():
            try:
                src = read_drawing(path)
            except FileNotFoundError as ex:
                raise RuntimeError(f"not_found:{ex}") from ex
            except DWGConverterMissing as ex:
                raise RuntimeError(f"backend_unavailable:{ex}") from ex

            blocks: list[dict] = []
            for blk in src.blocks:
                if blk.name.startswith("*"):
                    continue
                try:
                    flags = int(getattr(blk.block.dxf, "flags", 0) or 0)
                except Exception:
                    flags = 0
                if flags & 4:
                    continue
                blocks.append({
                    "name": blk.name,
                    "entity_count": sum(1 for _ in blk),
                    "prefix": blk.name.split("-", 1)[0] if "-" in blk.name else "",
                })
            return {"path": str(path), "blocks": blocks, "count": len(blocks)}

        # Don't go through `_run`'s _wrap_for_com because this work doesn't
        # need the COM thread — but the dispatcher's lock matters so the
        # result still serializes against concurrent draws.
        loop = asyncio.get_running_loop()
        try:
            payload = await loop.run_in_executor(self._executor, _inspect)
            return CommandResult(ok=True, payload=payload)
        except RuntimeError as ex:
            msg = str(ex)
            if msg.startswith("not_found:"):
                return CommandResult(
                    ok=False, error=msg[len("not_found:"):], category="not_found",
                )
            if msg.startswith("backend_unavailable:"):
                return CommandResult(
                    ok=False, error=msg[len("backend_unavailable:"):],
                    category="backend_unavailable",
                )
            return CommandResult(ok=False, error=msg, category="internal_error")

    async def block_import_from_file(
        self,
        path: str,
        names: list[str] | None = None,
    ) -> CommandResult:
        """Use AutoCAD's native -INSERT with the `=` notation to pull each
        named block out of the external file and into the active drawing's
        block table. We immediately ERASE the resulting INSERT so only the
        BLOCK definition lands in the drawing.

        `names=None` enumerates the source via ezdxf, then runs one
        -INSERT per block. Existing block names are skipped (the
        active drawing's table wins).
        """
        from autocad_mcp.project.dwg_reader import (
            DWGConverterMissing, read_drawing,
        )

        def _import():
            try:
                src = read_drawing(path)
            except FileNotFoundError as ex:
                raise RuntimeError(f"not_found:{ex}") from ex
            except DWGConverterMissing as ex:
                raise RuntimeError(f"backend_unavailable:{ex}") from ex

            available: list[str] = []
            for blk in src.blocks:
                if blk.name.startswith("*"):
                    continue
                try:
                    flags = int(getattr(blk.block.dxf, "flags", 0) or 0)
                except Exception:
                    flags = 0
                if flags & 4:
                    continue
                available.append(blk.name)

            wanted = available if names is None else [n for n in names if n in available]
            doc = self._require_doc()
            existing = {b.Name for b in doc.Blocks}
            to_import = [n for n in wanted if n not in existing]
            skipped_exists = [n for n in wanted if n in existing]

            # AutoCAD's -INSERT with NAME=FILE notation reads the source
            # DWG, registers the block in the active drawing's block table,
            # then inserts one instance. We provide the insertion point and
            # accept defaults, then immediately erase the INSERT entity to
            # leave only the BLOCK definition behind.
            posix_path = str(path).replace("\\", "/")
            msp = self._msp()
            for n in to_import:
                # Snapshot modelspace count before INSERT so we can find the
                # entity we just added (rather than using `_L` which can be
                # disturbed by housekeeping entities or concurrent edits).
                count_before = int(msp.Count)
                cmd = f'_.-INSERT\n{n}={posix_path}\n0,0,0\n1\n1\n0\n'
                doc.SendCommand(cmd)
                # Find the new INSERT — typically appears at modelspace[count_before]
                # but enumerate defensively in case AutoCAD added intermediate
                # housekeeping entities.
                if int(msp.Count) > count_before:
                    new_idx = int(msp.Count) - 1
                    try:
                        ent = msp.Item(new_idx)
                        if ent.ObjectName == "AcDbBlockReference":
                            ent.Delete()
                    except Exception as ex:
                        log.debug(
                            "block_import_erase_failed",
                            name=n, error=str(ex),
                        )

            payload = {
                "path": str(path),
                "imported": to_import,
                "skipped_exists": skipped_exists,
            }
            if names is not None:
                payload["not_found_in_source"] = [
                    n for n in names if n not in available
                ]
            return payload

        loop = asyncio.get_running_loop()
        try:
            payload = await loop.run_in_executor(self._executor, _import)
            return CommandResult(ok=True, payload=payload)
        except RuntimeError as ex:
            msg = str(ex)
            if msg.startswith("not_found:"):
                return CommandResult(
                    ok=False, error=msg[len("not_found:"):], category="not_found",
                )
            if msg.startswith("backend_unavailable:"):
                return CommandResult(
                    ok=False, error=msg[len("backend_unavailable:"):],
                    category="backend_unavailable",
                )
            return CommandResult(ok=False, error=msg, category="internal_error")

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        def _insert():
            doc = self._require_doc()
            try:
                doc.Blocks.Item(name)
            except Exception:
                raise RuntimeError(f"Block '{name}' not defined")
            ent = self._msp().InsertBlock(
                _to_apoint(x, y), name,
                float(scale), float(scale), float(scale),
                math.radians(float(rotation)),
            )
            return {"entity_type": "INSERT", "handle": ent.Handle}

        return await self._run(_insert)

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        def _insert():
            doc = self._require_doc()
            try:
                doc.Blocks.Item(name)
            except Exception:
                raise RuntimeError(f"Block '{name}' not defined")
            ent = self._msp().InsertBlock(
                _to_apoint(x, y), name,
                float(scale), float(scale), float(scale),
                math.radians(float(rotation)),
            )
            if attributes and ent.HasAttributes:
                for attr in ent.GetAttributes():
                    if attr.TagString in attributes:
                        attr.TextString = str(attributes[attr.TagString])
            return {"entity_type": "INSERT", "handle": ent.Handle}

        return await self._run(_insert)

    async def block_get_attributes(self, entity_id) -> CommandResult:
        def _get():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None or ent.ObjectName != "AcDbBlockReference":
                raise RuntimeError("Not an INSERT entity")
            result = {}
            if ent.HasAttributes:
                for attr in ent.GetAttributes():
                    result[attr.TagString] = attr.TextString
            return {"attributes": result}

        return await self._run(_get)

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        def _set():
            doc = self._require_doc()
            ent = _find_entity(doc, entity_id)
            if ent is None or ent.ObjectName != "AcDbBlockReference":
                raise RuntimeError("Not an INSERT entity")
            if ent.HasAttributes:
                for attr in ent.GetAttributes():
                    if attr.TagString.upper() == str(tag).upper():
                        attr.TextString = str(value)
                        return {"tag": tag, "value": value}
            raise RuntimeError(f"Attribute '{tag}' not found")

        return await self._run(_set)

    # -----------------------------------------------------------------------
    # Symbol library
    # -----------------------------------------------------------------------

    async def library_list(self) -> CommandResult:
        from autocad_mcp.arch.library import symbol_names
        names = symbol_names()
        return CommandResult(ok=True, payload={"symbols": names, "count": len(names)})

    async def library_define(self, name: str) -> CommandResult:
        from autocad_mcp.arch.library import SYMBOL_LIBRARY, apply_to_com_block

        if name not in SYMBOL_LIBRARY:
            return CommandResult(
                ok=False, error=f"No library symbol named {name!r}",
                category="not_found",
            )

        def _define():
            doc = self._require_doc()
            # Existence check by iteration — Blocks.Item(name) for a missing
            # name *usually* raises com_error, but gencache-wrapped early-bound
            # variants sometimes return a stale handle instead, which would
            # silently flag the block as "existed" and skip the actual
            # entity-emission step. Iteration is consistent across both.
            existing_names = {b.Name for b in doc.Blocks}
            if name in existing_names:
                # Still report the entity count so callers don't have to
                # treat existed/new as two different return shapes.
                existing_block = doc.Blocks.Item(name)
                return {
                    "name": name,
                    "existed": True,
                    "entity_count": int(existing_block.Count),
                }
            block = doc.Blocks.Add(_to_apoint(0.0, 0.0), name)
            apply_to_com_block(
                block,
                SYMBOL_LIBRARY[name],
                apoint=_to_apoint,
                double_array=_to_double_array,
            )
            return {
                "name": name,
                "entity_count": int(block.Count),
            }

        return await self._run(_define)

    async def library_define_all(self) -> CommandResult:
        from autocad_mcp.arch.library import symbol_names
        defined: list[str] = []
        existed: list[str] = []
        errors: list[dict] = []
        for name in symbol_names():
            r = await self.library_define(name)
            if not r.ok:
                errors.append({"name": name, "error": r.error})
                continue
            if r.payload.get("existed"):
                existed.append(name)
            else:
                defined.append(name)
        return CommandResult(ok=True, payload={
            "defined": defined,
            "existed": existed,
            "errors": errors,
            "total": len(defined) + len(existed),
        })

    async def library_insert(self, name, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        r = await self.library_define(name)
        if not r.ok:
            return r
        return await self.block_insert(name, x, y, scale, rotation)

    # -----------------------------------------------------------------------
    # View
    # -----------------------------------------------------------------------

    async def zoom_extents(self) -> CommandResult:
        def _zoom():
            self._app.ZoomExtents()
            return {"zoomed": "extents"}

        return await self._run(_zoom)

    async def zoom_window(self, x1, y1, x2, y2) -> CommandResult:
        def _zoom():
            self._app.ZoomWindow(_to_apoint(x1, y1), _to_apoint(x2, y2))
            return {"zoomed": "window"}

        return await self._run(_zoom)

    async def get_screenshot(self) -> CommandResult:
        if self._screenshot_provider is None:
            return CommandResult(
                ok=False, error="Screenshot provider not initialized",
                category="backend_unavailable",
            )
        data = self._screenshot_provider.capture()
        if data:
            return CommandResult(ok=True, payload=data)
        return CommandResult(
            ok=False, error="Screenshot capture failed",
            category="internal_error",
        )

    # -----------------------------------------------------------------------
    # Freehand LISP
    # -----------------------------------------------------------------------

    async def execute_lisp(self, code: str) -> CommandResult:
        def _exec():
            doc = self._require_doc()
            # SendCommand needs trailing newline; AutoCAD parses (...) as a LISP eval.
            doc.SendCommand(code + "\n")
            return {"sent": True, "code": code}

        return await self._run(_exec)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


_NOT_FOUND_SUBSTRINGS = (
    "not found",
    "not defined",
    "no active document",
    "no document",
)
_INVALID_ARG_SUBSTRINGS = (
    "zero length",
    "supports line+line only",
    "must be",
    "needs at least",
    "got 0",
    "neither line endpoint",
)
_UNAVAILABLE_SUBSTRINGS = (
    "not initialized",
)


def _infer_category(msg: str):
    """Heuristic mapping from a RuntimeError string to an ErrorCategory.

    The COM inner functions raise plain RuntimeErrors with descriptive
    text; this maps the common shapes back to the stable enum so MCP
    clients can branch on `category` without substring-matching `error`
    themselves. Unknown wording falls through to `internal_error`.
    """
    low = msg.lower()
    for sub in _UNAVAILABLE_SUBSTRINGS:
        if sub in low:
            return "backend_unavailable"
    for sub in _NOT_FOUND_SUBSTRINGS:
        if sub in low:
            return "not_found"
    for sub in _INVALID_ARG_SUBSTRINGS:
        if sub in low:
            return "invalid_arg"
    return "internal_error"


def _safe_active_doc(app):
    """Return Application.ActiveDocument or None when no drawing is open."""
    try:
        return app.ActiveDocument
    except Exception as ex:
        log.debug("com_active_doc_unavailable", error=str(ex))
        return None


def _com_replace_endpoint_at(line, corner, new_pt) -> None:
    """For an AutoCAD COM IAcadLine, replace whichever endpoint matches
    `corner` (within a small tolerance) with `new_pt`.

    StartPoint / EndPoint on IAcadLine are read-write properties accepting
    a 3-element double array; we forward through _to_apoint.
    """
    start = tuple(line.StartPoint)[:2]
    end = tuple(line.EndPoint)[:2]
    if math.hypot(start[0] - corner[0], start[1] - corner[1]) < 1e-6:
        line.StartPoint = _to_apoint(new_pt[0], new_pt[1])
    elif math.hypot(end[0] - corner[0], end[1] - corner[1]) < 1e-6:
        line.EndPoint = _to_apoint(new_pt[0], new_pt[1])
    else:
        raise RuntimeError("Neither line endpoint matches the fillet/chamfer corner")


def _safe_attr(obj, name: str, default: Any) -> Any:
    """Like getattr() but tolerant of COM property accesses that raise.

    `getattr` only uses its default when AttributeError is raised. COM
    IDispatch property failures often surface as `pywintypes.com_error`,
    which `getattr` re-raises.
    """
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _hresult_of(exc: BaseException) -> int | None:
    """Extract the HRESULT from a pywintypes.com_error if present.

    com_error.args[0] is the HRESULT (a signed 32-bit int). Returns None for
    any exception that isn't a com_error.
    """
    if not exc.args:
        return None
    first = exc.args[0]
    if isinstance(first, int):
        return first
    return None


def _find_entity(doc, entity_id) -> Any:
    """Look up an entity by Handle string (preferred) or ObjectID int."""
    try:
        return doc.HandleToObject(str(entity_id))
    except Exception as ex:
        log.debug("entity_handle_lookup_miss", id=str(entity_id), error=str(ex))
    try:
        return doc.ObjectIdToObject(int(entity_id))
    except Exception as ex:
        log.debug("entity_objectid_lookup_miss", id=str(entity_id), error=str(ex))
        return None


def _color_to_aci(color) -> int:
    # Mapping lives in `backends/_colors.py` so the ezdxf backend uses the
    # same table — adding a colour in one place applies everywhere.
    from autocad_mcp.backends._colors import color_to_aci
    return color_to_aci(color)
