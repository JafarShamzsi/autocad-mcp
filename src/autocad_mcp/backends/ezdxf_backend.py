"""Headless DXF backend using ezdxf — no AutoCAD needed."""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from typing import Any

import ezdxf
import structlog

from autocad_mcp.backends._colors import color_to_aci
from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult, not_supported
from autocad_mcp.screenshot import MatplotlibScreenshotProvider

log = structlog.get_logger()


def _new_doc() -> "ezdxf.document.Drawing":
    """Build a fresh ezdxf doc with the unit defaults this backend expects.

    `ezdxf.new("R2013")` sets `$INSUNITS=6` (meters), which would flip area
    labels to "m²" with a /1e6 conversion factor. The historical default
    treated drawings as feet (area was raw value with "SF" suffix, no
    conversion), so we set `$INSUNITS=2` (feet) to preserve that contract.
    Callers who want metric set it explicitly via `drawing.create` data or
    `header['$INSUNITS']=...`.
    """
    doc = ezdxf.new("R2013")
    try:
        doc.header["$INSUNITS"] = 2
    except Exception:
        pass
    return doc


def _replace_endpoint_at(line, corner, new_pt) -> None:
    """For an ezdxf LINE entity, replace whichever endpoint matches `corner`
    (within a small tolerance) with `new_pt`."""
    start = tuple(line.dxf.start)[:2]
    end = tuple(line.dxf.end)[:2]
    if math.hypot(start[0] - corner[0], start[1] - corner[1]) < 1e-6:
        line.dxf.start = (new_pt[0], new_pt[1], 0.0)
    elif math.hypot(end[0] - corner[0], end[1] - corner[1]) < 1e-6:
        line.dxf.end = (new_pt[0], new_pt[1], 0.0)
    else:
        raise ValueError("Neither endpoint matches the fillet/chamfer corner")


class EzdxfBackend(AutoCADBackend):
    """Pure-Python DXF generation via ezdxf."""

    def __init__(self):
        # Eager init: build the empty document at construction time so
        # downstream methods can never see `self._doc is None` even if
        # `initialize()` wasn't awaited. `initialize` overwrites these
        # references with a fresh doc if it's called later.
        self._doc: ezdxf.document.Drawing | None = _new_doc()
        self._msp = self._doc.modelspace()
        self._save_path: str | None = None
        self._screenshot = MatplotlibScreenshotProvider(self._doc)
        self._entity_counter = 0

    @property
    def name(self) -> str:
        return "ezdxf"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=False,
            can_zoom=False,  # No viewport in headless
            can_query_entities=True,
            can_file_operations=True,
            can_undo=False,
        )

    async def initialize(self) -> CommandResult:
        # `__init__` builds an empty doc so the backend is usable before
        # initialize() is awaited (N12). Re-init here only when the previous
        # state has been mutated — `_save_path`/`_entity_counter` are the
        # cheapest "dirty" signal short of inspecting modelspace.
        if (
            self._doc is None
            or self._save_path is not None
            or self._entity_counter > 0
            or len(self._msp) > 0
        ):
            self._doc = _new_doc()
            self._msp = self._doc.modelspace()
            self._screenshot.doc = self._doc
            self._save_path = None
            self._entity_counter = 0
        return CommandResult(ok=True, payload={"backend": "ezdxf", "version": ezdxf.__version__})

    async def status(self) -> CommandResult:
        entity_count = len(self._msp) if self._msp else 0
        return CommandResult(ok=True, payload={
            "backend": "ezdxf",
            "version": ezdxf.__version__,
            "has_document": self._doc is not None,
            "entity_count": entity_count,
            "save_path": self._save_path,
            "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
        })

    def _next_id(self) -> str:
        self._entity_counter += 1
        return f"ezdxf_{self._entity_counter}"

    def _ensure_layer(self, layer: str | None):
        if layer and layer not in self._doc.layers:
            self._doc.layers.add(layer)

    # --- Drawing management ---

    async def drawing_info(self) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open", category="not_found")
        layers = [l.dxf.name for l in self._doc.layers]
        entity_count = len(self._msp)
        blocks = [b.name for b in self._doc.blocks if not b.name.startswith("*")]
        return CommandResult(ok=True, payload={
            "entity_count": entity_count,
            "layers": layers,
            "blocks": blocks,
            "dxf_version": self._doc.dxfversion,
            "save_path": self._save_path,
        })

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open", category="not_found")
        save_path = path or self._save_path
        if not save_path:
            return CommandResult(ok=False, error="No save path specified", category="invalid_arg")
        # When the caller passes a path with no extension AND a DWG converter
        # is available, default to DWG (the AutoCAD-native format) instead
        # of DXF. Users who specifically want DXF call `save_as_dxf` or pass
        # a `.dxf` suffix explicitly.
        if not Path(save_path).suffix:
            from autocad_mcp.project.dwg_reader import available_dwg_converters
            if any(available_dwg_converters().values()):
                save_path = str(Path(save_path).with_suffix(".dwg"))
            else:
                save_path = str(Path(save_path).with_suffix(".dxf"))
        if str(save_path).lower().endswith(".dwg"):
            return await self._save_dwg(save_path, version=None)
        await asyncio.to_thread(self._doc.saveas, save_path)
        self._save_path = save_path
        return CommandResult(ok=True, payload={"path": save_path, "format": "dxf"})

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        return await self.drawing_save(path)

    async def drawing_save_as_dwg(
        self, path: str, version: str | None = None,
    ) -> CommandResult:
        """Save as DWG, optionally pinning a specific format version.

        `version` accepts the DWG format generation ("2018"/"2013"/.../"2000")
        or an AutoCAD year ("2024" → "2018"). Defaults to "2018" which is
        broadly readable by every AutoCAD release since 2018.
        """
        if not self._doc:
            return CommandResult(ok=False, error="No document open", category="not_found")
        # Force the .dwg suffix so callers who pass a bare stem still get DWG.
        out = Path(path).expanduser().resolve()
        if out.suffix.lower() != ".dwg":
            out = out.with_suffix(".dwg")
        return await self._save_dwg(str(out), version=version)

    async def _save_dwg(
        self, path: str, *, version: str | None,
    ) -> CommandResult:
        """Shared DWG-save body used by `drawing_save` and `drawing_save_as_dwg`."""
        from autocad_mcp.project.dwg_reader import DWGConverterMissing, write_dwg

        def _do_write() -> Path:
            return write_dwg(self._doc, path, version=version)

        try:
            out = await asyncio.to_thread(_do_write)
            self._save_path = str(out)
            return CommandResult(ok=True, payload={
                "path": str(out),
                "format": "dwg",
                "version": version or "2018",
            })
        except DWGConverterMissing as ex:
            return CommandResult(ok=False, error=str(ex), category="backend_unavailable")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def drawing_create(self, name: str | None = None, force: bool = False) -> CommandResult:
        # `force` is a file_ipc-only flag; ezdxf always builds a new in-memory
        # doc so there's nothing to overwrite. Accepted-and-ignored to keep
        # signatures uniform across backends.
        self._doc = _new_doc()
        self._msp = self._doc.modelspace()
        self._screenshot.doc = self._doc
        self._entity_counter = 0
        self._save_path = f"{name}.dxf" if name else None
        return CommandResult(ok=True, payload={"name": name or "untitled"})

    async def drawing_purge(self) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open", category="not_found")
        # ezdxf has no PurgeAll equivalent — a real purge would have to walk
        # blocks/layers/dimstyles/linetypes/textstyles and drop unused entries
        # by hand. Until that's implemented, surface honestly rather than
        # claiming success on a no-op (callers may schedule a save based on
        # the response and end up with a still-dirty file).
        return not_supported(
            "purge not implemented for ezdxf — drop unused entries by hand "
            "or save + reload through AutoCAD's PURGE command."
        )

    async def drawing_open(self, path: str) -> CommandResult:
        # Route .dwg through `read_drawing` so the converter (accoreconsole /
        # ODA) is invoked. `readfile` for DXF and the converter shell-out both
        # block, so dispatch to a thread.
        try:
            from autocad_mcp.project.dwg_reader import read_drawing
            from autocad_mcp.project.sidecar import sidecar_summary
            self._doc = await asyncio.to_thread(read_drawing, path)
            self._msp = self._doc.modelspace()
            self._screenshot.doc = self._doc
            self._save_path = path
            payload = {"path": path}
            # Surface the per-drawing `.claude.md` sidecar so the caller
            # has the architect's context without an explicit re-fetch.
            side = sidecar_summary(path)
            if side is not None:
                payload["sidecar"] = side
            return CommandResult(ok=True, payload=payload)
        except FileNotFoundError as ex:
            return CommandResult(ok=False, error=str(ex), category="not_found")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open", category="not_found")
        result = {}
        header = self._doc.header
        for name in (names or []):
            try:
                result[name] = str(header[name])
            except (KeyError, ezdxf.DXFKeyError):
                result[name] = None
        return CommandResult(ok=True, payload=result)

    # --- Entity operations ---

    async def create_line(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "LINE", "handle": e.dxf.handle})

    async def create_circle(self, cx, cy, radius, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_circle((cx, cy), radius, dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "CIRCLE", "handle": e.dxf.handle})

    async def create_polyline(self, points, closed=False, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        pts = [(p[0], p[1]) for p in points]
        e = self._msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "LWPOLYLINE", "handle": e.dxf.handle})

    async def create_rectangle(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return await self.create_polyline(pts, closed=True, layer=layer)

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_arc((cx, cy), radius, start_angle, end_angle, dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "ARC", "handle": e.dxf.handle})

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_ellipse(
            (cx, cy), major_axis=(major_x - cx, major_y - cy, 0), ratio=ratio,
            dxfattribs={"layer": layer or "0"},
        )
        return CommandResult(ok=True, payload={"entity_type": "ELLIPSE", "handle": e.dxf.handle})

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_mtext(text, dxfattribs={
            "insert": (x, y),
            "char_height": height,
            "width": width,
            "layer": layer or "0",
        })
        return CommandResult(ok=True, payload={"entity_type": "MTEXT", "handle": e.dxf.handle})

    async def entity_list(self, layer=None, limit: int = 1000) -> CommandResult:
        entities: list[dict] = []
        truncated = False
        cap = int(limit) if limit and limit > 0 else None
        for e in self._msp:
            if layer and e.dxf.get("layer", "0") != layer:
                continue
            entities.append({
                "type": e.dxftype(),
                "handle": e.dxf.handle,
                "layer": e.dxf.get("layer", "0"),
            })
            if cap is not None and len(entities) >= cap:
                truncated = True
                break
        return CommandResult(
            ok=True,
            payload={
                "entities": entities,
                "count": len(entities),
                "truncated": truncated,
            },
        )

    async def entity_count(self, layer=None) -> CommandResult:
        if layer:
            count = sum(1 for e in self._msp if e.dxf.get("layer", "0") == layer)
        else:
            count = len(self._msp)
        return CommandResult(ok=True, payload={"count": count})

    async def entity_summary(self, layer=None) -> CommandResult:
        from collections import Counter

        by_layer: Counter = Counter()
        by_type: Counter = Counter()
        samples: list[dict] = []
        total = 0
        for e in self._msp:
            ent_layer = e.dxf.get("layer", "0")
            if layer and ent_layer != layer:
                continue
            total += 1
            by_layer[ent_layer] += 1
            by_type[e.dxftype()] += 1
            if len(samples) < 10:
                samples.append({
                    "type": e.dxftype(),
                    "layer": ent_layer,
                    "handle": e.dxf.handle,
                })
        return CommandResult(ok=True, payload={
            "total": total,
            "by_layer": dict(by_layer.most_common()),
            "by_type": dict(by_type.most_common()),
            "samples": samples,
            "layer_filter": layer,
        })

    async def entity_select(
        self, *,
        layer=None, type=None, bbox=None, color=None,
        handles=None, limit: int = 5000,
    ) -> CommandResult:
        from collections import Counter

        # Normalize string-or-list params into lowercase-set tests so the
        # filter loop is a single dict-lookup per entity.
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

        # Query bbox: tuple/list of 4 numbers. Caller passes
        # (x_min, y_min, x_max, y_max). We allow loose ordering by
        # min/max-ing so a swapped corner doesn't return empty.
        if bbox is not None:
            qx1, qy1, qx2, qy2 = bbox
            qx_min, qx_max = min(qx1, qx2), max(qx1, qx2)
            qy_min, qy_max = min(qy1, qy2), max(qy1, qy2)
        else:
            qx_min = qx_max = qy_min = qy_max = None

        by_layer: Counter = Counter()
        by_type: Counter = Counter()
        out_handles: list[str] = []
        truncated = False

        # ezdxf bbox helper — cheap for line/circle/poly, slow for splines.
        from ezdxf.bbox import extents

        for e in self._msp:
            ent_layer = e.dxf.get("layer", "0")
            ent_type = e.dxftype()
            ent_handle = e.dxf.handle

            if wanted_handles is not None and ent_handle not in wanted_handles:
                continue
            if wanted_layers is not None and ent_layer not in wanted_layers:
                continue
            if wanted_types is not None and ent_type not in wanted_types:
                continue
            if wanted_color is not None:
                # Entity color (-1 = ByLayer, -2 = ByBlock). Fall back to
                # the layer's color when ByLayer so filtering by color
                # matches what the user sees on screen.
                ec = int(e.dxf.get("color", 256))  # 256 = ByLayer in DXF
                if ec in (256, 0):
                    try:
                        ec = int(self._doc.layers.get(ent_layer).dxf.color)
                    except Exception:
                        ec = 7
                if abs(ec) != wanted_color:
                    continue
            if bbox is not None:
                try:
                    b = extents([e], fast=True)
                except Exception:
                    b = None
                if b is None or not b.has_data:
                    # Entity has no geometric extent we can test against —
                    # bbox filter excludes it.
                    continue
                emin, emax = b.extmin, b.extmax
                # Crossing semantics: any overlap counts.
                if (emax.x < qx_min or emin.x > qx_max
                        or emax.y < qy_min or emin.y > qy_max):
                    continue

            out_handles.append(ent_handle)
            by_layer[ent_layer] += 1
            by_type[ent_type] += 1
            if len(out_handles) >= max(1, int(limit)):
                truncated = True
                break

        return CommandResult(ok=True, payload={
            "count": len(out_handles),
            "handles": out_handles,
            "by_layer": dict(by_layer.most_common()),
            "by_type": dict(by_type.most_common()),
            "truncated": truncated,
            "filter": {
                "layer": layer, "type": type, "bbox": bbox,
                "color": color, "handles_in": len(handles) if handles else None,
                "limit": int(limit),
            },
        })

    def _normalize_color_aci(self, value) -> int:
        """Accept either an ACI int or a named color, return the int."""
        if isinstance(value, int):
            return value
        from autocad_mcp.backends._colors import color_to_aci
        try:
            return int(color_to_aci(str(value)))
        except Exception:
            # Unknown name → 7 (white/black depending on viewport) is the
            # safe fallback; selection just won't match.
            return 7

    async def entity_osnap(
        self, x: float, y: float,
        modes: list[str] | None = None, tolerance: float = 5.0,
    ) -> CommandResult:
        import math

        # Default-on modes match AutoCAD's "common" osnap set. `nearest`
        # is opt-in because it always matches something on every curve
        # and would drown the other modes.
        DEFAULT_MODES = {"endpoint", "midpoint", "center", "intersection"}
        active = set(modes) if modes else DEFAULT_MODES
        # Normalize.
        active = {m.lower() for m in active}

        # Best hit so far: (distance, x, y, mode, handle).
        best: tuple[float, float, float, str, str] | None = None
        tol = float(tolerance)

        def _consider(candidate_x, candidate_y, mode, handle):
            nonlocal best
            d = math.hypot(candidate_x - x, candidate_y - y)
            if d > tol:
                return
            if best is None or d < best[0]:
                best = (d, candidate_x, candidate_y, mode, handle)

        for e in self._msp:
            etype = e.dxftype()
            h = e.dxf.handle
            try:
                if etype == "LINE":
                    s = e.dxf.start
                    ep = e.dxf.end
                    if "endpoint" in active:
                        _consider(s.x, s.y, "endpoint", h)
                        _consider(ep.x, ep.y, "endpoint", h)
                    if "midpoint" in active:
                        _consider((s.x + ep.x) / 2, (s.y + ep.y) / 2, "midpoint", h)
                elif etype == "CIRCLE":
                    c = e.dxf.center
                    r = float(e.dxf.radius)
                    if "center" in active:
                        _consider(c.x, c.y, "center", h)
                    # Quadrant endpoints (most-useful "endpoint-of-circle")
                    if "endpoint" in active:
                        for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)):
                            _consider(c.x + dx, c.y + dy, "endpoint", h)
                elif etype == "ARC":
                    c = e.dxf.center
                    r = float(e.dxf.radius)
                    sa = math.radians(float(e.dxf.start_angle))
                    ea = math.radians(float(e.dxf.end_angle))
                    if "endpoint" in active:
                        for a in (sa, ea):
                            _consider(c.x + r * math.cos(a),
                                       c.y + r * math.sin(a), "endpoint", h)
                    if "center" in active:
                        _consider(c.x, c.y, "center", h)
                    if "midpoint" in active:
                        # Midpoint of the arc along its sweep
                        am = (sa + ea) / 2 if ea >= sa else (sa + ea + 2 * math.pi) / 2
                        _consider(c.x + r * math.cos(am),
                                   c.y + r * math.sin(am), "midpoint", h)
                elif etype == "LWPOLYLINE":
                    pts = list(e.get_points("xy"))
                    if "endpoint" in active:
                        for px, py in pts:
                            _consider(px, py, "endpoint", h)
                    if "midpoint" in active and len(pts) >= 2:
                        # Midpoint of each segment.
                        prev = pts[0]
                        for px, py in pts[1:]:
                            _consider((prev[0] + px) / 2, (prev[1] + py) / 2,
                                       "midpoint", h)
                            prev = (px, py)
                # Other entity types (HATCH, MTEXT, INSERT, etc.) skipped
                # for now — adding their osnaps is straightforward but the
                # four basic types cover the common drafting cases.
            except Exception:
                # An entity with a malformed dxf attribute set shouldn't
                # break the whole snap query.
                continue

        if best is None:
            return CommandResult(
                ok=False,
                error=f"No osnap point within tolerance {tol} of ({x}, {y})",
                category="not_found",
            )
        return CommandResult(ok=True, payload={
            "x": best[1], "y": best[2],
            "mode_used": best[3],
            "entity_handle": best[4],
            "distance": best[0],
            "tolerance": tol,
        })

    async def entity_get(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            info = {"type": e.dxftype(), "handle": e.dxf.handle, "layer": e.dxf.get("layer", "0")}
            # Add type-specific info
            if e.dxftype() == "LINE":
                info["start"] = list(e.dxf.start)[:2]
                info["end"] = list(e.dxf.end)[:2]
            elif e.dxftype() == "CIRCLE":
                info["center"] = list(e.dxf.center)[:2]
                info["radius"] = e.dxf.radius
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_erase(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                # Try "last" keyword
                if entity_id == "last" and len(self._msp) > 0:
                    entities = list(self._msp)
                    e = entities[-1]
                else:
                    return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            self._msp.delete_entity(e)
            return CommandResult(ok=True, payload={"erased": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            copy = e.copy()
            self._msp.add_entity(copy)
            copy.translate(dx, dy, 0)
            return CommandResult(ok=True, payload={"handle": copy.dxf.handle})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            e.translate(dx, dy, 0)
            return CommandResult(ok=True, payload={"moved": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            from ezdxf.math import Matrix44
            m = Matrix44.z_rotate(math.radians(angle))
            # Translate to origin, rotate, translate back
            e.translate(-cx, -cy, 0)
            e.transform(m)
            e.translate(cx, cy, 0)
            return CommandResult(ok=True, payload={"rotated": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            from ezdxf.math import Matrix44
            m = Matrix44.scale(factor, factor, factor)
            e.translate(-cx, -cy, 0)
            e.transform(m)
            e.translate(cx, cy, 0)
            return CommandResult(ok=True, payload={"scaled": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            copy = e.copy()
            self._msp.add_entity(copy)
            # Mirror across line (x1,y1)-(x2,y2) using reflection matrix
            dx, dy = x2 - x1, y2 - y1
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                return CommandResult(ok=False, error="Mirror line has zero length", category="invalid_arg")
            from ezdxf.math import Matrix44
            # Reflect: translate to origin, reflect, translate back
            # Reflection matrix across line through origin with direction (dx, dy):
            #   [[cos2a, sin2a], [sin2a, -cos2a]] where a = atan2(dy, dx)
            a = math.atan2(dy, dx)
            cos2a = math.cos(2 * a)
            sin2a = math.sin(2 * a)
            m = Matrix44([
                cos2a, sin2a, 0, 0,
                sin2a, -cos2a, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ])
            copy.translate(-x1, -y1, 0)
            copy.transform(m)
            copy.translate(x1, y1, 0)
            return CommandResult(ok=True, payload={"handle": copy.dxf.handle})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        # ezdxf has no native offset; do the LINE special case (perpendicular
        # translate). Curves (arcs, polylines with bulges) need real parallel-
        # offset math we don't ship yet — punt with a precise error.
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            if e.dxftype() != "LINE":
                return CommandResult(
                    ok=False,
                    error=(
                        "Offset on ezdxf backend supports LINE only "
                        f"(got {e.dxftype()}). Use the COM backend for arcs/polylines."
                    ),
                )
            sx, sy = tuple(e.dxf.start)[:2]
            ex, ey = tuple(e.dxf.end)[:2]
            dx, dy = ex - sx, ey - sy
            length = math.hypot(dx, dy)
            if length == 0:
                return CommandResult(ok=False, error="Cannot offset a zero-length line", category="invalid_arg")
            # Perpendicular unit vector (CCW = positive distance = "left").
            nx, ny = -dy / length, dx / length
            d = float(distance)
            new_start = (sx + nx * d, sy + ny * d)
            new_end = (ex + nx * d, ey + ny * d)
            copy = self._msp.add_line(
                new_start, new_end,
                dxfattribs={"layer": e.dxf.layer},
            )
            return CommandResult(ok=True, payload={
                "handle": copy.dxf.handle,
                "start": list(new_start),
                "end": list(new_end),
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            handles = []
            for r in range(rows):
                for c in range(cols):
                    if r == 0 and c == 0:
                        continue  # Skip original position
                    copy = e.copy()
                    self._msp.add_entity(copy)
                    copy.translate(c * col_dist, r * row_dist, 0)
                    handles.append(copy.dxf.handle)
            return CommandResult(ok=True, payload={"copies": len(handles), "handles": handles})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        from autocad_mcp.arch.geometry import fillet_line_line

        try:
            e1 = self._doc.entitydb.get(entity_id1)
            e2 = self._doc.entitydb.get(entity_id2)
            if e1 is None or e2 is None:
                return CommandResult(ok=False, error="Entity not found", category="not_found")
            if e1.dxftype() != "LINE" or e2.dxftype() != "LINE":
                return CommandResult(
                    ok=False,
                    error="Fillet on ezdxf backend supports LINE+LINE only "
                          f"(got {e1.dxftype()} + {e2.dxftype()})",
                )

            line1 = (tuple(e1.dxf.start)[:2], tuple(e1.dxf.end)[:2])
            line2 = (tuple(e2.dxf.start)[:2], tuple(e2.dxf.end)[:2])
            f = fillet_line_line(line1, line2, float(radius))

            # Trim each line back to its tangent point. The endpoint replaced
            # is whichever was the shared corner.
            _replace_endpoint_at(e1, f["corner"], f["trim1"])
            _replace_endpoint_at(e2, f["corner"], f["trim2"])

            arc = self._msp.add_arc(
                center=f["arc_center"],
                radius=f["arc_radius"],
                start_angle=f["arc_start_deg"],
                end_angle=f["arc_end_deg"],
                dxfattribs={"layer": e1.dxf.layer},
            )
            return CommandResult(ok=True, payload={
                "fillet_handle": arc.dxf.handle,
                "trim1": list(f["trim1"]),
                "trim2": list(f["trim2"]),
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        from autocad_mcp.arch.geometry import chamfer_line_line

        try:
            e1 = self._doc.entitydb.get(entity_id1)
            e2 = self._doc.entitydb.get(entity_id2)
            if e1 is None or e2 is None:
                return CommandResult(ok=False, error="Entity not found", category="not_found")
            if e1.dxftype() != "LINE" or e2.dxftype() != "LINE":
                return CommandResult(
                    ok=False,
                    error="Chamfer on ezdxf backend supports LINE+LINE only "
                          f"(got {e1.dxftype()} + {e2.dxftype()})",
                )

            line1 = (tuple(e1.dxf.start)[:2], tuple(e1.dxf.end)[:2])
            line2 = (tuple(e2.dxf.start)[:2], tuple(e2.dxf.end)[:2])
            c = chamfer_line_line(line1, line2, float(dist1), float(dist2))

            _replace_endpoint_at(e1, c["corner"], c["trim1"])
            _replace_endpoint_at(e2, c["corner"], c["trim2"])

            cham = self._msp.add_line(
                c["trim1"], c["trim2"],
                dxfattribs={"layer": e1.dxf.layer},
            )
            return CommandResult(ok=True, payload={
                "chamfer_handle": cham.dxf.handle,
                "trim1": list(c["trim1"]),
                "trim2": list(c["trim2"]),
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def create_hatch(self, entity_id, pattern="ANSI31") -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found", category="not_found")
            hatch = self._msp.add_hatch()
            hatch.set_pattern_fill(pattern, scale=1.0)
            # Try to use the entity as a boundary path
            hatch.paths.add_polyline_path(
                [(p[0], p[1]) for p in e.get_points(format="xy")],
                is_closed=True,
            )
            return CommandResult(ok=True, payload={"entity_type": "HATCH", "handle": hatch.dxf.handle})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    # --- 3D primitives ---
    #
    # ezdxf can't construct ACIS solids the way AutoCAD's COM does. We
    # build polyface MESH entities for `extrude` — visually identical
    # when viewed in any DWG / DXF viewer, but not a true 3DSOLID.
    # `revolve`, `region`, `boolean` are deferred — they need real
    # solid-modeling math that doesn't pay off as a fake on headless.

    def _polyline_vertices(self, ent) -> list[tuple[float, float]] | None:
        """Pull the 2D vertex list out of a closed curve, or None if the
        entity isn't a curve we can extrude (CIRCLE / ELLIPSE / etc. need
        sampling first — skip for v1)."""
        t = ent.dxftype()
        if t == "LWPOLYLINE":
            return [(p[0], p[1]) for p in ent.get_points("xy")]
        if t == "POLYLINE" and ent.is_2d_polyline:
            return [(v.dxf.location[0], v.dxf.location[1])
                    for v in ent.vertices]
        return None

    async def entity_extrude(
        self, entity_id, height, taper_angle=0.0,
    ) -> CommandResult:
        try:
            ent = self._doc.entitydb.get(entity_id)
            if ent is None:
                return CommandResult(
                    ok=False, error=f"Entity {entity_id} not found",
                    category="not_found",
                )
            verts = self._polyline_vertices(ent)
            if verts is None:
                return CommandResult(
                    ok=False, category="not_supported",
                    error=(
                        f"ezdxf extrude only supports LWPOLYLINE/POLYLINE "
                        f"on this backend; entity {entity_id} is "
                        f"{ent.dxftype()!r}. Use the COM backend for a "
                        "real 3DSOLID from arbitrary regions."
                    ),
                )
            if taper_angle != 0:
                # Drafted extrusion would require trig + per-vertex
                # offset math. Honest fallback: ignore + warn.
                log.warning("ezdxf_extrude_taper_ignored", taper=taper_angle)

            h = float(height)
            # Build a polyface MESH: bottom + top caps + side faces.
            mesh = self._msp.add_polyface(
                dxfattribs={"layer": ent.dxf.get("layer", "0")},
            )
            n = len(verts)
            # Bottom cap (z=0) and top cap (z=h) as polygon faces.
            bottom = [(x, y, 0.0) for (x, y) in verts]
            top = [(x, y, h) for (x, y) in verts]
            # Add the bottom face (one polygon).
            mesh.append_face(bottom)
            # Add the top face.
            mesh.append_face(top)
            # Add each side quad.
            for i in range(n):
                j = (i + 1) % n
                quad = [
                    (verts[i][0], verts[i][1], 0.0),
                    (verts[j][0], verts[j][1], 0.0),
                    (verts[j][0], verts[j][1], h),
                    (verts[i][0], verts[i][1], h),
                ]
                mesh.append_face(quad)

            return CommandResult(ok=True, payload={
                "entity_type": "POLYFACE",
                "handle": mesh.dxf.handle,
                "height": h,
                "vertex_count": n,
                "note": (
                    "ezdxf builds a polyface MESH (not a real 3DSOLID). "
                    "Use the COM backend for editable solids + boolean ops."
                ),
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def entity_revolve(
        self, entity_id,
        axis_x, axis_y, axis_dx, axis_dy, axis_dz=0.0,
        angle=360.0,
    ) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error=(
                "Revolve produces an ACIS solid which ezdxf can't construct. "
                "Use the COM backend for entity_revolve."
            ),
        )

    async def entity_region(self, entity_ids) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error=(
                "Region creation requires AutoCAD's solid-modeling kernel "
                "(ACIS). Use the COM backend for entity_region."
            ),
        )

    async def entity_boolean(self, operation, target_id, tool_ids) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error=(
                "Boolean ops on 3D solids require ACIS. Use the COM backend "
                "for entity_boolean."
            ),
        )

    # Express Tools are AutoCAD-only — there's no headless equivalent.

    async def express_burst(self, entity_ids) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Express Tools BURST is full-AutoCAD-only. Use the COM backend.",
        )

    async def express_flatten(self, entity_ids=None, z=0.0) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Express Tools FLATTEN is full-AutoCAD-only. Use the COM backend.",
        )

    async def express_overkill(self, entity_ids=None) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Express Tools OVERKILL is full-AutoCAD-only. Use the COM backend.",
        )

    async def express_layiso(self, layers) -> CommandResult:
        # LAYISO can be emulated on ezdxf by toggling layer.is_off,
        # which IS supported. Implement the emulation here since it's
        # genuinely useful headless.
        try:
            if not layers:
                return CommandResult(
                    ok=False, category="invalid_arg",
                    error="layiso needs a non-empty `layers` list",
                )
            keep = {l for l in layers}
            for layer in self._doc.layers:
                if layer.dxf.name not in keep:
                    try:
                        layer.off()
                    except Exception:
                        pass
            return CommandResult(ok=True, payload={
                "isolated_layers": list(keep),
                "note": "ezdxf emulation — other layers turned off via .off()",
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def express_txt2mtxt(self, entity_ids) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Express Tools TXT2MTXT is full-AutoCAD-only. Use the COM backend.",
        )

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        layers = []
        for l in self._doc.layers:
            layers.append({
                "name": l.dxf.name,
                "color": l.dxf.get("color", 7),
                "linetype": l.dxf.get("linetype", "Continuous"),
                "is_frozen": l.is_frozen(),
                "is_locked": l.is_locked(),
            })
        return CommandResult(ok=True, payload={"layers": layers})

    async def layer_create(self, name, color="white", linetype="CONTINUOUS") -> CommandResult:
        if name in self._doc.layers:
            return CommandResult(ok=True, payload={"name": name, "existed": True})
        color_int = self._color_to_int(color)
        self._doc.layers.add(name, color=color_int, linetype=linetype)
        return CommandResult(ok=True, payload={"name": name, "color": color_int})

    async def layer_set_current(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist", category="not_found")
        self._doc.header["$CLAYER"] = name
        return CommandResult(ok=True, payload={"current_layer": name})

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist", category="not_found")
        layer = self._doc.layers.get(name)
        if color is not None:
            layer.color = self._color_to_int(color)
        if linetype is not None:
            layer.dxf.linetype = linetype
        return CommandResult(ok=True, payload={"name": name})

    async def layer_freeze(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist", category="not_found")
        self._doc.layers.get(name).freeze()
        return CommandResult(ok=True, payload={"name": name, "frozen": True})

    async def layer_thaw(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist", category="not_found")
        self._doc.layers.get(name).thaw()
        return CommandResult(ok=True, payload={"name": name, "frozen": False})

    async def layer_lock(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist", category="not_found")
        self._doc.layers.get(name).lock()
        return CommandResult(ok=True, payload={"name": name, "locked": True})

    async def layer_unlock(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist", category="not_found")
        self._doc.layers.get(name).unlock()
        return CommandResult(ok=True, payload={"name": name, "locked": False})

    # --- Style management ---

    # DXF attribs we surface in `list_*` calls. Full dictionaries are
    # available via `get_*` — these are the headline "what does this
    # style look like" fields.
    _DIMSTYLE_KEY_ATTRIBS = (
        "dimscale", "dimtxt", "dimasz", "dimdec",
        "dimexo", "dimexe", "dimblk", "dimtad",
    )
    _TEXTSTYLE_KEY_ATTRIBS = ("font", "height", "width", "oblique")

    async def style_list_dimstyles(self) -> CommandResult:
        rows = []
        for ds in self._doc.dimstyles:
            attribs = {
                k: ds.dxf.get(k) for k in self._DIMSTYLE_KEY_ATTRIBS
                if ds.dxf.is_supported(k)
            }
            rows.append({"name": ds.dxf.name, "attribs": attribs})
        # Stable order — name alphabetical.
        rows.sort(key=lambda r: r["name"])
        return CommandResult(ok=True, payload={
            "dimstyles": rows,
            "count": len(rows),
            "current": self._doc.header.get("$DIMSTYLE", "Standard"),
        })

    async def style_get_dimstyle(self, name) -> CommandResult:
        if name not in self._doc.dimstyles:
            return CommandResult(
                ok=False, error=f"Dimstyle {name!r} not found",
                category="not_found",
            )
        ds = self._doc.dimstyles.get(name)
        attribs = dict(ds.dxf.all_existing_dxf_attribs())
        # Drop the housekeeping fields callers don't care about.
        for k in ("handle", "owner", "flags"):
            attribs.pop(k, None)
        return CommandResult(ok=True, payload={
            "name": name,
            "attribs": attribs,
        })

    async def style_create_dimstyle(self, name, attribs: dict) -> CommandResult:
        # Drop the name out of attribs so callers can re-use a dict.
        clean = {k: v for k, v in (attribs or {}).items() if k != "name"}
        try:
            if name in self._doc.dimstyles:
                # Overwrite — apply the attribs to the existing dimstyle
                # rather than deleting + recreating (which would lose
                # any unspecified attribs).
                ds = self._doc.dimstyles.get(name)
                for k, v in clean.items():
                    try:
                        ds.dxf.set(k, v)
                    except Exception as ex:
                        log.debug(
                            "dimstyle_attrib_set_failed",
                            dimstyle=name, attrib=k, error=str(ex),
                        )
                return CommandResult(ok=True, payload={
                    "name": name, "existed": True,
                })
            self._doc.dimstyles.new(name, dxfattribs=clean)
            return CommandResult(ok=True, payload={
                "name": name, "existed": False,
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def style_set_current_dimstyle(self, name) -> CommandResult:
        if name not in self._doc.dimstyles:
            return CommandResult(
                ok=False, error=f"Dimstyle {name!r} not found",
                category="not_found",
            )
        self._doc.header["$DIMSTYLE"] = name
        return CommandResult(ok=True, payload={"current": name})

    async def style_list_textstyles(self) -> CommandResult:
        rows = []
        for ts in self._doc.styles:
            attribs = {
                k: ts.dxf.get(k) for k in self._TEXTSTYLE_KEY_ATTRIBS
                if ts.dxf.is_supported(k)
            }
            rows.append({"name": ts.dxf.name, "attribs": attribs})
        rows.sort(key=lambda r: r["name"])
        return CommandResult(ok=True, payload={
            "textstyles": rows,
            "count": len(rows),
            "current": self._doc.header.get("$TEXTSTYLE", "Standard"),
        })

    async def style_get_textstyle(self, name) -> CommandResult:
        if name not in self._doc.styles:
            return CommandResult(
                ok=False, error=f"Textstyle {name!r} not found",
                category="not_found",
            )
        ts = self._doc.styles.get(name)
        attribs = dict(ts.dxf.all_existing_dxf_attribs())
        for k in ("handle", "owner", "flags"):
            attribs.pop(k, None)
        return CommandResult(ok=True, payload={
            "name": name, "attribs": attribs,
        })

    async def style_create_textstyle(
        self, name, font=None, height=0.0,
        width_factor=1.0, oblique=0.0,
    ) -> CommandResult:
        attribs = {}
        if font is not None:
            attribs["font"] = font
        attribs["height"] = float(height)
        attribs["width"] = float(width_factor)
        attribs["oblique"] = float(oblique)
        try:
            if name in self._doc.styles:
                ts = self._doc.styles.get(name)
                for k, v in attribs.items():
                    try:
                        ts.dxf.set(k, v)
                    except Exception:
                        pass
                return CommandResult(ok=True, payload={
                    "name": name, "existed": True,
                })
            self._doc.styles.new(name, dxfattribs=attribs)
            return CommandResult(ok=True, payload={
                "name": name, "existed": False,
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def style_set_current_textstyle(self, name) -> CommandResult:
        if name not in self._doc.styles:
            return CommandResult(
                ok=False, error=f"Textstyle {name!r} not found",
                category="not_found",
            )
        self._doc.header["$TEXTSTYLE"] = name
        return CommandResult(ok=True, payload={"current": name})

    async def style_list_mleaderstyles(self) -> CommandResult:
        # ezdxf's MLeaderStyleCollection iterates as `(name, style)` tuples.
        # Pull the names out of element 0. `has_entry(name)` is the
        # membership check (no `__contains__` provided).
        try:
            names = sorted(item[0] for item in self._doc.mleader_styles)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")
        rows = [{"name": n} for n in names]
        return CommandResult(ok=True, payload={
            "mleaderstyles": rows,
            "count": len(rows),
            "current": self._doc.header.get("$CMLEADERSTYLE", "Standard"),
        })

    async def style_set_current_mleaderstyle(self, name) -> CommandResult:
        if not self._doc.mleader_styles.has_entry(name):
            return CommandResult(
                ok=False, error=f"MLeaderStyle {name!r} not found",
                category="not_found",
            )
        self._doc.header["$CMLEADERSTYLE"] = name
        return CommandResult(ok=True, payload={"current": name})

    # --- Named views ---

    async def view_list_named(self) -> CommandResult:
        rows = []
        for v in self._doc.views:
            rows.append({
                "name": v.dxf.name,
                "center": list(v.dxf.center)[:2] if v.dxf.is_supported("center") else None,
                "height": float(v.dxf.get("height", 0)),
                "width": float(v.dxf.get("width", 0)),
            })
        rows.sort(key=lambda r: r["name"])
        return CommandResult(ok=True, payload={
            "views": rows, "count": len(rows),
        })

    async def view_save_named(self, name, x1, y1, x2, y2) -> CommandResult:
        cx = (float(x1) + float(x2)) / 2.0
        cy = (float(y1) + float(y2)) / 2.0
        h = abs(float(y2) - float(y1))
        w = abs(float(x2) - float(x1))
        try:
            if name in self._doc.views:
                # Delete + recreate so attribs are clean.
                self._doc.views.remove(name)
            self._doc.views.new(name, dxfattribs={
                "center": (cx, cy),
                "height": max(h, 1.0),
                "width": max(w, 1.0),
            })
            return CommandResult(ok=True, payload={
                "name": name, "center": [cx, cy],
                "height": h, "width": w,
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def view_restore_named(self, name) -> CommandResult:
        # On headless ezdxf there's no viewport to actually zoom — we
        # surface the view's stored extents so the caller can act on
        # them (e.g. plot the named view).
        if name not in self._doc.views:
            return CommandResult(
                ok=False, error=f"View {name!r} not found",
                category="not_found",
            )
        v = self._doc.views.get(name)
        c = v.dxf.center
        return CommandResult(ok=True, payload={
            "name": name,
            "center": [c.x, c.y] if hasattr(c, "x") else list(c)[:2],
            "height": float(v.dxf.get("height", 0)),
            "width": float(v.dxf.get("width", 0)),
            "note": "ezdxf is headless; the call returns the stored extents.",
        })

    async def view_delete_named(self, name) -> CommandResult:
        if name not in self._doc.views:
            return CommandResult(
                ok=False, error=f"View {name!r} not found",
                category="not_found",
            )
        self._doc.views.remove(name)
        return CommandResult(ok=True, payload={"name": name, "deleted": True})

    # Layer states aren't exposed in ezdxf's high-level API — they live
    # in the NOD `ACAD_LAYERSTATES` dict. Surface a clear not_supported
    # rather than building a fragile NOD walker; COM is the right path.

    async def view_list_layer_states(self) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error=("Layer states are not exposed by ezdxf's high-level API. "
                   "Use the COM backend, or AutoCAD's LAYERSTATE command."),
        )

    async def view_save_layer_state(self, name) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Layer states are COM-backend only on this server.",
        )

    async def view_restore_layer_state(self, name) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Layer states are COM-backend only on this server.",
        )

    async def view_delete_layer_state(self, name) -> CommandResult:
        return CommandResult(
            ok=False, category="not_supported",
            error="Layer states are COM-backend only on this server.",
        )

    # --- Annotation: create_table (grid-style on ezdxf) ---

    async def create_table(
        self, x, y, rows, cols,
        row_height=0.5, col_width=2.0,
        data=None, title=None, header=None,
    ) -> CommandResult:
        """ezdxf has no high-level AcDbTable factory. Render a grid of
        LINE + TEXT entities — visually identical for plotting, but not
        a real AutoCAD TABLE entity. Use the COM backend for that.
        """
        try:
            rows = int(rows); cols = int(cols)
            if rows < 1 or cols < 1:
                return CommandResult(
                    ok=False, category="invalid_arg",
                    error="rows and cols must be >= 1",
                )
            rh = float(row_height); cw = float(col_width)
            origin_x = float(x); origin_y = float(y)
            handles: dict[str, list[str]] = {"lines": [], "texts": []}

            # Account for title + header rows being part of `rows` total.
            # `data` fills the rest.
            data_rows_start = 0
            if title is not None:
                data_rows_start += 1
            if header is not None:
                data_rows_start += 1
            data = data or []

            # Vertical lines (cols + 1)
            top_y = origin_y
            bot_y = origin_y - rows * rh
            for c in range(cols + 1):
                lx = origin_x + c * cw
                line = self._msp.add_line((lx, top_y), (lx, bot_y))
                handles["lines"].append(line.dxf.handle)

            # Horizontal lines (rows + 1)
            for r in range(rows + 1):
                ly = origin_y - r * rh
                line = self._msp.add_line(
                    (origin_x, ly), (origin_x + cols * cw, ly),
                )
                handles["lines"].append(line.dxf.handle)

            # Title row — spans all columns. Drawn slightly bigger.
            if title is not None:
                tx = origin_x + (cols * cw) / 2.0
                ty = origin_y - rh * 0.5
                t = self._msp.add_text(
                    str(title),
                    dxfattribs={
                        "insert": (tx, ty),
                        "height": rh * 0.6,
                        "halign": 1, "valign": 2,  # center / middle
                        "align_point": (tx, ty),
                    },
                )
                handles["texts"].append(t.dxf.handle)

            # Header row.
            if header is not None:
                hy_top = top_y - (rh if title is not None else 0)
                for c in range(cols):
                    if c >= len(header):
                        break
                    cx = origin_x + c * cw + cw / 2.0
                    cy = hy_top - rh * 0.5
                    t = self._msp.add_text(
                        str(header[c]),
                        dxfattribs={
                            "insert": (cx, cy),
                            "height": rh * 0.45,
                            "halign": 1, "valign": 2,
                            "align_point": (cx, cy),
                        },
                    )
                    handles["texts"].append(t.dxf.handle)

            # Data rows.
            for r_offset, row in enumerate(data):
                r_index = data_rows_start + r_offset
                if r_index >= rows:
                    break
                for c_offset, cell in enumerate(row):
                    if c_offset >= cols:
                        break
                    cx = origin_x + c_offset * cw + cw / 2.0
                    cy = origin_y - r_index * rh - rh * 0.5
                    t = self._msp.add_text(
                        str(cell),
                        dxfattribs={
                            "insert": (cx, cy),
                            "height": rh * 0.4,
                            "halign": 1, "valign": 2,
                            "align_point": (cx, cy),
                        },
                    )
                    handles["texts"].append(t.dxf.handle)

            return CommandResult(ok=True, payload={
                "kind": "grid-table",
                "handles": handles,
                "rows": rows, "cols": cols,
                "origin": [origin_x, origin_y],
                "note": (
                    "ezdxf has no AcDbTable factory; rendered as LINE+TEXT. "
                    "Use COM for a real TABLE entity."
                ),
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        blocks = [b.name for b in self._doc.blocks if not b.name.startswith("*")]
        return CommandResult(ok=True, payload={"blocks": blocks})

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        if name not in self._doc.blocks:
            return CommandResult(ok=False, error=f"Block '{name}' not defined", category="not_found")
        # Honor $CLAYER — `layer_set_current` updates that header, and
        # AutoCAD-native INSERT uses the current layer. ezdxf's
        # add_blockref defaults to layer "0" unless we pass it explicitly,
        # so we read $CLAYER here to match AutoCAD's behavior. Callers
        # who want a specific layer call `layer_set_current` first; the
        # `library.insert_asset` flow does this for the asset's default_layer.
        attribs: dict[str, Any] = {
            "xscale": scale, "yscale": scale, "zscale": scale,
            "rotation": rotation,
        }
        try:
            clayer = self._doc.header.get("$CLAYER", "0")
            if clayer and clayer != "0" and clayer in self._doc.layers:
                attribs["layer"] = clayer
        except Exception as ex:
            log.debug("block_insert_clayer_read_failed", error=str(ex))
        e = self._msp.add_blockref(name, (x, y), dxfattribs=attribs)
        if block_id:
            try:
                e.add_attrib("ID", block_id)
            except Exception as ex:
                # The block may have no ATTDEF for ID; that's not fatal.
                log.debug("block_insert_id_attrib_failed", name=name, error=str(ex))
        return CommandResult(ok=True, payload={"entity_type": "INSERT", "handle": e.dxf.handle})

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        if name not in self._doc.blocks:
            return CommandResult(ok=False, error=f"Block '{name}' not defined", category="not_found")
        block = self._doc.blocks[name]
        e = self._msp.add_blockref(name, (x, y), dxfattribs={
            "xscale": scale, "yscale": scale, "zscale": scale,
            "rotation": rotation,
        })
        if attributes:
            # Try add_auto_attribs first (uses ATTDEF templates)
            try:
                e.add_auto_attribs(attributes)
            except Exception as auto_ex:
                # Fallback: add manual attribs when the block has no ATTDEFs.
                log.debug(
                    "block_insert_auto_attribs_fallback",
                    name=name, error=str(auto_ex),
                )
                for tag, value in attributes.items():
                    try:
                        e.add_attrib(tag, value, (x, y))
                    except Exception as manual_ex:
                        log.debug(
                            "block_insert_manual_attrib_failed",
                            name=name, tag=tag, error=str(manual_ex),
                        )
        return CommandResult(ok=True, payload={"entity_type": "INSERT", "handle": e.dxf.handle})

    async def block_get_attributes(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None or e.dxftype() != "INSERT":
                return CommandResult(ok=False, error="Not an INSERT entity", category="invalid_arg")
            attribs = {}
            for attrib in e.attribs:
                attribs[attrib.dxf.tag] = attrib.dxf.text
            return CommandResult(ok=True, payload={"attributes": attribs})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None or e.dxftype() != "INSERT":
                return CommandResult(ok=False, error="Not an INSERT entity", category="invalid_arg")
            for attrib in e.attribs:
                if attrib.dxf.tag.upper() == tag.upper():
                    attrib.dxf.text = value
                    return CommandResult(ok=True, payload={"tag": tag, "value": value})
            return CommandResult(ok=False, error=f"Attribute '{tag}' not found", category="not_found")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def block_define(self, name, entities) -> CommandResult:
        block = self._doc.blocks.new(name=name)
        for ent_def in entities:
            etype = ent_def.get("type", "LINE")
            if etype == "LINE":
                block.add_line(
                    (ent_def.get("x1", 0), ent_def.get("y1", 0)),
                    (ent_def.get("x2", 0), ent_def.get("y2", 0)),
                )
            elif etype == "CIRCLE":
                block.add_circle(
                    (ent_def.get("cx", 0), ent_def.get("cy", 0)),
                    ent_def.get("radius", 1),
                )
            elif etype == "ATTDEF":
                block.add_attdef(
                    ent_def.get("tag", "TAG"),
                    (ent_def.get("x", 0), ent_def.get("y", 0)),
                    dxfattribs={"height": ent_def.get("height", 2.5)},
                )
        return CommandResult(ok=True, payload={"block": name, "entity_count": len(entities)})

    # --- Block import from external files (asset reuse) ---

    async def block_list_from_file(self, path: str) -> CommandResult:
        """Walk every non-anonymous block defined in `path` and report
        its name, entity count, and a guess at the discipline (from
        block-name prefix).

        Supports `.dxf` natively and `.dwg` via the existing accoreconsole
        / ODA converter (same path as `drawing_open`).
        """
        from autocad_mcp.project.dwg_reader import (
            DWGConverterMissing, read_drawing,
        )
        try:
            src = await asyncio.to_thread(read_drawing, path)
        except FileNotFoundError as ex:
            return CommandResult(ok=False, error=str(ex), category="not_found")
        except DWGConverterMissing as ex:
            return CommandResult(
                ok=False, error=str(ex), category="backend_unavailable",
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

        blocks: list[dict] = []
        for blk in src.blocks:
            # Skip anonymous blocks (`*Model_Space`, `*Paper_Space`,
            # dimension-blocks `*D...`, hatch-blocks `*X...` etc.).
            if blk.name.startswith("*"):
                continue
            # XREFs declare themselves via the EXTERNAL_REFERENCE flag; treat
            # those as XREFs not standalone blocks (importing them headlessly
            # would silently produce a broken reference).
            try:
                flags = int(getattr(blk.block.dxf, "flags", 0) or 0)
            except Exception:
                flags = 0
            if flags & 4:
                continue
            entity_count = sum(1 for _ in blk)
            # Heuristic: the prefix before the first dash is often the
            # discipline / library tag (PLMB-, FURN-, APPL-, A-, etc.).
            prefix = blk.name.split("-", 1)[0] if "-" in blk.name else ""
            blocks.append({
                "name": blk.name,
                "entity_count": entity_count,
                "prefix": prefix,
            })
        return CommandResult(ok=True, payload={
            "path": str(path),
            "blocks": blocks,
            "count": len(blocks),
        })

    async def block_import_from_file(
        self,
        path: str,
        names: list[str] | None = None,
    ) -> CommandResult:
        """Copy block definitions from `path` into the active drawing.

        Uses ezdxf's `Importer` which handles entity-by-entity copy plus the
        required handle remapping. Skips block names that already exist
        in the active drawing (caller can purge then re-import if a
        refresh is wanted).
        """
        from ezdxf.addons import Importer
        from autocad_mcp.project.dwg_reader import (
            DWGConverterMissing, read_drawing,
        )

        try:
            src = await asyncio.to_thread(read_drawing, path)
        except FileNotFoundError as ex:
            return CommandResult(ok=False, error=str(ex), category="not_found")
        except DWGConverterMissing as ex:
            return CommandResult(
                ok=False, error=str(ex), category="backend_unavailable",
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

        # Resolve the requested name list. None = every non-anonymous,
        # non-xref block in the source.
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

        if names is None:
            wanted = available
        else:
            wanted = [n for n in names if n in available]
            missing = [n for n in names if n not in available]
        # Block names already in the destination — skip rather than
        # overwrite, so the caller controls definition replacement.
        existing = {b.name for b in self._doc.blocks}
        to_import = [n for n in wanted if n not in existing]
        skipped_exists = [n for n in wanted if n in existing]

        if to_import:
            try:
                # `rename=False` means the importer keeps source block names;
                # we've already filtered out names that exist in the
                # destination so there's no collision to resolve.
                imp = Importer(src, self._doc)
                imp.import_blocks(to_import, rename=False)
                imp.finalize()
            except Exception as ex:
                return CommandResult(
                    ok=False, error=str(ex), category="internal_error",
                )

        payload = {
            "path": str(path),
            "imported": to_import,
            "skipped_exists": skipped_exists,
        }
        if names is not None:
            payload["not_found_in_source"] = [n for n in names if n not in available]
        return CommandResult(ok=True, payload=payload)

    # --- Annotation ---

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_text(text, dxfattribs={
            "insert": (x, y),
            "height": height,
            "rotation": rotation,
            "layer": layer or "0",
        })
        return CommandResult(ok=True, payload={"entity_type": "TEXT", "handle": e.dxf.handle})

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        try:
            dim = self._msp.add_linear_dim(
                base=(dim_x, dim_y),
                p1=(x1, y1),
                p2=(x2, y2),
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        try:
            dim = self._msp.add_aligned_dim(
                p1=(x1, y1),
                p2=(x2, y2),
                distance=offset,
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        try:
            # Calculate angle arc midpoint for dimension location
            a1 = math.atan2(y1 - cy, x1 - cx)
            a2 = math.atan2(y2 - cy, x2 - cx)
            amid = (a1 + a2) / 2
            r = max(math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy)) * 0.7
            dim = self._msp.add_angular_dim_cra(
                center=(cx, cy),
                radius=r,
                start_angle=math.degrees(a1),
                end_angle=math.degrees(a2),
                distance=r * 1.2,
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        try:
            rad = math.radians(angle)
            px = cx + radius * math.cos(rad)
            py = cy + radius * math.sin(rad)
            dim = self._msp.add_radius_dim(
                center=(cx, cy),
                mpoint=(px, py),
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def create_leader(self, points, text) -> CommandResult:
        try:
            pts = [(p[0], p[1]) for p in points]
            leader = self._msp.add_leader(pts)
            # Add text at the last point
            last = pts[-1]
            self._msp.add_mtext(text, dxfattribs={
                "insert": (last[0] + 2, last[1]),
                "char_height": 2.5,
                "width": 30,
            })
            return CommandResult(ok=True, payload={"entity_type": "LEADER"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        pid_layers = [
            ("PID-EQUIPMENT", 6, "CONTINUOUS"),
            ("PID-PROCESS-PIPING", 4, "CONTINUOUS"),
            ("PID-UTILITY-PIPING", 3, "CONTINUOUS"),
            ("PID-INSTRUMENTS", 5, "CONTINUOUS"),
            ("PID-ELECTRICAL", 1, "CONTINUOUS"),
            ("PID-ANNOTATION", 7, "CONTINUOUS"),
            ("PID-VALVES", 2, "CONTINUOUS"),
        ]
        for name, color, lt in pid_layers:
            if name not in self._doc.layers:
                self._doc.layers.add(name, color=color, linetype=lt)
        return CommandResult(ok=True, payload={"layers_created": len(pid_layers)})

    async def pid_list_symbols(self, category) -> CommandResult:
        """List CTO symbols from disk or built-in catalog."""
        from autocad_mcp.pid.cto_library import CTO_ROOT, list_symbols
        symbols = list_symbols(category)
        return CommandResult(ok=True, payload={"category": category, "symbols": symbols, "count": len(symbols)})

    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        """Insert a CTO symbol as a simple block placeholder."""
        self._ensure_layer("PID-EQUIPMENT")
        # In headless mode, create a placeholder rectangle with the symbol name
        half = 5 * scale
        pts = [(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)]
        e = self._msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "PID-EQUIPMENT"})
        self._msp.add_text(symbol, dxfattribs={
            "insert": (x, y), "height": 1.5 * scale, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"symbol": symbol, "handle": e.dxf.handle})

    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None) -> CommandResult:
        """Insert a valve symbol (simplified for headless)."""
        self._ensure_layer("PID-VALVES")
        # Simplified diamond shape for valve
        size = 3.0
        pts = [(x - size, y), (x, y + size), (x + size, y), (x, y - size)]
        e = self._msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "PID-VALVES"})
        self._msp.add_text(valve_type, dxfattribs={
            "insert": (x, y - size - 2), "height": 1.5, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"valve_type": valve_type, "handle": e.dxf.handle})

    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value="") -> CommandResult:
        """Insert an instrument symbol (simplified for headless)."""
        self._ensure_layer("PID-INSTRUMENTS")
        # Circle with crosshair for instrument
        e = self._msp.add_circle((x, y), 4, dxfattribs={"layer": "PID-INSTRUMENTS"})
        self._msp.add_line((x - 4, y), (x + 4, y), dxfattribs={"layer": "PID-INSTRUMENTS"})
        label = tag_id if tag_id else instrument_type
        self._msp.add_text(label, dxfattribs={
            "insert": (x, y - 6), "height": 1.5, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"instrument_type": instrument_type, "handle": e.dxf.handle})

    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None) -> CommandResult:
        """Insert a pump symbol (simplified for headless)."""
        self._ensure_layer("PID-EQUIPMENT")
        # Circle with triangle for pump
        e = self._msp.add_circle((x, y), 6, dxfattribs={"layer": "PID-EQUIPMENT"})
        rad = math.radians(rotation)
        tip_x = x + 8 * math.cos(rad)
        tip_y = y + 8 * math.sin(rad)
        self._msp.add_lwpolyline(
            [(x + 6 * math.cos(rad + 0.5), y + 6 * math.sin(rad + 0.5)),
             (tip_x, tip_y),
             (x + 6 * math.cos(rad - 0.5), y + 6 * math.sin(rad - 0.5))],
            close=True,
            dxfattribs={"layer": "PID-EQUIPMENT"},
        )
        self._msp.add_text(pump_type, dxfattribs={
            "insert": (x, y - 8), "height": 1.5, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"pump_type": pump_type, "handle": e.dxf.handle})

    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None) -> CommandResult:
        """Insert a tank symbol (simplified for headless)."""
        self._ensure_layer("PID-EQUIPMENT")
        w = 10 * scale
        h = 15 * scale
        pts = [(x - w, y), (x + w, y), (x + w, y + h), (x - w, y + h)]
        e = self._msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "PID-EQUIPMENT"})
        self._msp.add_text(tank_type, dxfattribs={
            "insert": (x, y + h + 2), "height": 2.0 * scale, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"tank_type": tank_type, "handle": e.dxf.handle})

    async def pid_draw_process_line(self, x1, y1, x2, y2) -> CommandResult:
        self._ensure_layer("PID-PROCESS-PIPING")
        e = self._msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "PID-PROCESS-PIPING"})
        return CommandResult(ok=True, payload={"entity_type": "LINE", "handle": e.dxf.handle})

    async def pid_connect_equipment(self, x1, y1, x2, y2) -> CommandResult:
        """Connect two points with orthogonal routing."""
        self._ensure_layer("PID-PROCESS-PIPING")
        mid_x = (x1 + x2) / 2
        pts = [(x1, y1), (mid_x, y1), (mid_x, y2), (x2, y2)]
        e = self._msp.add_lwpolyline(pts, dxfattribs={"layer": "PID-PROCESS-PIPING"})
        return CommandResult(ok=True, payload={"entity_type": "LWPOLYLINE", "handle": e.dxf.handle})

    async def pid_add_flow_arrow(self, x, y, rotation=0.0) -> CommandResult:
        self._ensure_layer("PID-ANNOTATION")
        # Simple triangle arrow
        rad = math.radians(rotation)
        size = 2.0
        p1 = (x + size * math.cos(rad), y + size * math.sin(rad))
        p2 = (x + size * 0.5 * math.cos(rad + 2.4), y + size * 0.5 * math.sin(rad + 2.4))
        p3 = (x + size * 0.5 * math.cos(rad - 2.4), y + size * 0.5 * math.sin(rad - 2.4))
        e = self._msp.add_lwpolyline([p1, p2, p3], close=True, dxfattribs={"layer": "PID-ANNOTATION"})
        return CommandResult(ok=True, payload={"entity_type": "LWPOLYLINE", "handle": e.dxf.handle})

    async def pid_add_equipment_tag(self, x, y, tag, description="") -> CommandResult:
        self._ensure_layer("PID-ANNOTATION")
        e = self._msp.add_text(tag, dxfattribs={
            "insert": (x, y), "height": 2.5, "layer": "PID-ANNOTATION",
        })
        result = {"entity_type": "TEXT", "handle": e.dxf.handle, "tag": tag}
        if description:
            e2 = self._msp.add_text(description, dxfattribs={
                "insert": (x, y - 3.5), "height": 1.8, "layer": "PID-ANNOTATION",
            })
            result["description_handle"] = e2.dxf.handle
        return CommandResult(ok=True, payload=result)

    async def pid_add_line_number(self, x, y, line_num, spec) -> CommandResult:
        self._ensure_layer("PID-ANNOTATION")
        text = f"{line_num}-{spec}"
        e = self._msp.add_text(text, dxfattribs={
            "insert": (x, y), "height": 2.0, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"entity_type": "TEXT", "handle": e.dxf.handle})

    # --- Architectural primitives (AIA/NCS) ---

    async def arch_setup_layers(self) -> CommandResult:
        from autocad_mcp.arch import setup_aia_layers
        try:
            created = setup_aia_layers(self._doc)
            return CommandResult(ok=True, payload={"created": created, "count": len(created)})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_wall(self, x1, y1, x2, y2, thickness=6.0, layer="A-WALL", justify="center") -> CommandResult:
        from autocad_mcp.arch import create_wall
        try:
            info = create_wall(self._msp, x1, y1, x2, y2, thickness=thickness, layer=layer, justify=justify)
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_wall_with_openings(
        self, x1, y1, x2, y2, thickness=6.0, openings=None,
        layer="A-WALL", door_layer="A-DOOR", window_layer="A-GLAZ", justify="center",
    ) -> CommandResult:
        from autocad_mcp.arch import create_wall_with_openings
        try:
            info = create_wall_with_openings(
                self._msp, x1, y1, x2, y2,
                thickness=thickness, openings=openings or [],
                layer=layer, door_layer=door_layer, window_layer=window_layer,
                justify=justify,
            )
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_door(self, x, y, width=36.0, angle=0.0, swing="left", open_angle=90.0, layer="A-DOOR", room_side=None) -> CommandResult:
        from autocad_mcp.arch import create_door
        try:
            info = create_door(
                self._msp, x, y, width=width, angle=angle,
                swing=swing, open_angle=open_angle, layer=layer,
                room_side=tuple(room_side) if room_side else None,
            )
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_window(self, x1, y1, x2, y2, sill_thickness=6.0, layer="A-GLAZ") -> CommandResult:
        from autocad_mcp.arch import create_window
        try:
            info = create_window(self._msp, x1, y1, x2, y2, sill_thickness=sill_thickness, layer=layer)
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_room(self, boundary, name="", area_layer="A-AREA", text_layer="A-AREA-IDEN", text_height=6.0) -> CommandResult:
        from autocad_mcp.arch import create_room
        try:
            pts = [(p[0], p[1]) for p in boundary]
            info = create_room(self._msp, pts, name=name, area_layer=area_layer, text_layer=text_layer, text_height=text_height)
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_dimension_chain(self, points, offset=24.0, direction="horizontal", layer="A-ANNO-DIMS") -> CommandResult:
        from autocad_mcp.arch import create_dimension_chain
        try:
            pts = [(p[0], p[1]) for p in points]
            info = create_dimension_chain(self._msp, pts, offset=offset, direction=direction, layer=layer)
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_stair(
        self, x1, y1, x2, y2, width=36.0, num_treads=12, tread_depth=None,
        layer="A-FLOR-STRS", shape="straight", landing_depth=None,
    ) -> CommandResult:
        from autocad_mcp.arch import create_stair
        try:
            info = create_stair(
                self._msp, x1, y1, x2, y2,
                width=width, num_treads=num_treads, tread_depth=tread_depth,
                layer=layer, shape=shape, landing_depth=landing_depth,
            )
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_column(
        self, x, y, shape="square", size=12.0, size_y=None, angle=0.0, layer="A-COLS",
    ) -> CommandResult:
        from autocad_mcp.arch import create_column
        try:
            info = create_column(
                self._msp, x, y, shape=shape, size=size, size_y=size_y,
                angle=angle, layer=layer,
            )
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_slab(self, boundary, edge_thickness=0.0, layer="A-FLOR") -> CommandResult:
        from autocad_mcp.arch import create_slab
        try:
            pts = [(p[0], p[1]) for p in boundary]
            info = create_slab(self._msp, pts, edge_thickness=edge_thickness, layer=layer)
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_beam(self, x1, y1, x2, y2, width=0.0, layer="A-WALL-HEAD") -> CommandResult:
        from autocad_mcp.arch import create_beam
        try:
            info = create_beam(self._msp, x1, y1, x2, y2, width=width, layer=layer)
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_roof(
        self, boundary, style="gable", ridge_axis="x", overhang=12.0,
        eaves_layer="A-ROOF", ridge_layer="A-ROOF",
    ) -> CommandResult:
        from autocad_mcp.arch import create_roof_plan
        try:
            pts = [(p[0], p[1]) for p in boundary]
            info = create_roof_plan(
                self._msp, pts, style=style, ridge_axis=ridge_axis,
                overhang=overhang, eaves_layer=eaves_layer, ridge_layer=ridge_layer,
            )
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_elevation(
        self, origin_x, origin_y, wall_length, height,
        openings=None, wall_layer="A-WALL", door_layer="A-DOOR", window_layer="A-GLAZ",
    ) -> CommandResult:
        from autocad_mcp.arch import create_elevation
        try:
            info = create_elevation(
                self._msp, origin_x, origin_y, wall_length, height,
                openings=openings, wall_layer=wall_layer,
                door_layer=door_layer, window_layer=window_layer,
            )
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_section(
        self, origin_x, origin_y, section_width, floor_y, ceiling_y,
        elements=None, wall_layer="A-WALL", slab_layer="A-FLOR",
        door_layer="A-DOOR", window_layer="A-GLAZ",
    ) -> CommandResult:
        from autocad_mcp.arch import create_section
        try:
            info = create_section(
                self._msp, origin_x, origin_y, section_width,
                floor_y, ceiling_y, elements=elements,
                wall_layer=wall_layer, slab_layer=slab_layer,
                door_layer=door_layer, window_layer=window_layer,
            )
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def arch_template(self, name, x, y, params=None) -> CommandResult:
        from autocad_mcp.arch.templates import apply_template, TEMPLATES
        if name not in TEMPLATES:
            return CommandResult(
                ok=False,
                error=f"Unknown template {name!r}. Available: {sorted(TEMPLATES.keys())}",
                category="not_found",
            )
        try:
            info = apply_template(self._msp, name, x, y, params=params or {})
            return CommandResult(ok=True, payload=info)
        except ValueError as ex:
            return CommandResult(ok=False, error=str(ex), category="invalid_arg")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

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
        self._ensure_layer(target_layer)
        try:
            pts = [(float(p[0]), float(p[1])) for p in boundary]
            if len(pts) < 3:
                return CommandResult(
                    ok=False,
                    error=f"Hatch boundary needs ≥3 points; got {len(pts)}",
                    category="invalid_arg",
                )
            hatch = self._msp.add_hatch(
                color=7,
                dxfattribs={"layer": target_layer},
            )
            # SOLID is a special case in ezdxf — needs `solid_fill=True`.
            if spec.name.upper() == "SOLID":
                hatch.set_solid_fill(color=7)
            else:
                hatch.set_pattern_fill(
                    spec.name,
                    scale=float(scale if scale is not None else spec.scale),
                    angle=float(angle if angle is not None else spec.angle),
                )
            hatch.paths.add_polyline_path(pts, is_closed=True)
            return CommandResult(ok=True, payload={
                "handle": hatch.dxf.handle,
                "pattern": spec.name,
                "friendly_name": pattern.upper(),
                "layer": target_layer,
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    # --- XREF mutation ---

    async def xref_attach(
        self, path, name=None, x=0.0, y=0.0, scale=1.0, rotation=0.0, overlay=False,
    ) -> CommandResult:
        """Attach a DWG as an XREF in the active ezdxf document.

        Implemented via ezdxf's `doc.add_xref_def` + INSERT entity. The
        resulting drawing references `path` — viewers must resolve it
        on read.

        DXF group code 70 (block flags) — only the bits that matter here:
          4 = external reference (XREF)
          8 = external reference overlay
        Attachment sets bit 4 alone; overlay sets bits 4 and 8.
        """
        from pathlib import Path as _Path
        try:
            derived_name = name or _Path(path).stem
            # Skip if name already taken (don't redefine silently).
            if derived_name in self._doc.blocks:
                return CommandResult(
                    ok=False,
                    error=f"Block / XREF named {derived_name!r} already exists",
                    category="invalid_arg",
                )
            xref_def = self._doc.add_xref_def(
                filename=str(path),
                name=derived_name,
                flags=12 if overlay else 4,  # 4=XREF, 4|8=XREF overlay
            )
            insert = self._msp.add_blockref(
                derived_name,
                insert=(float(x), float(y)),
                dxfattribs={
                    "xscale": float(scale),
                    "yscale": float(scale),
                    "zscale": float(scale),
                    "rotation": float(rotation),
                },
            )
            return CommandResult(ok=True, payload={
                "name": derived_name,
                "path": str(path),
                "overlay": bool(overlay),
                "handle": insert.dxf.handle,
            })
        except AttributeError:
            return CommandResult(
                ok=False,
                error=(
                    "ezdxf in this environment does not expose `add_xref_def`. "
                    "Use the COM backend for XREF attach against a running AutoCAD."
                ),
                category="not_supported",
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def xref_detach(self, name) -> CommandResult:
        try:
            if name not in self._doc.blocks:
                return CommandResult(
                    ok=False, error=f"No XREF named {name!r}", category="not_found",
                )
            # Guard against deleting a regular block — `xref_detach` must
            # only operate on actual XREFs. DXF flag bit 4 = EXTERNAL_REFERENCE.
            blk = self._doc.blocks.get(name)
            try:
                flags = int(getattr(blk.block.dxf, "flags", 0) or 0)
            except Exception:
                flags = 0
            if not (flags & 4):
                return CommandResult(
                    ok=False,
                    error=(
                        f"{name!r} is a regular block, not an XREF. "
                        "Use `purge` or `block.erase` to remove it; "
                        "xref_detach is reserved for external references."
                    ),
                    category="invalid_arg",
                )
            self._doc.blocks.delete_block(name, safe=False)
            return CommandResult(ok=True, payload={"name": name, "detached": True})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def xref_reload(self, name=None) -> CommandResult:
        # Headless: nothing to reload (file is read at attach time).
        return CommandResult(
            ok=False,
            error=(
                "xref reload is a no-op on the ezdxf backend — XREF contents "
                "are resolved when the drawing is opened in AutoCAD. Use the "
                "COM backend to reload against a running session."
            ),
            category="not_supported",
        )

    async def xref_bind(self, name) -> CommandResult:
        return CommandResult(
            ok=False,
            error=(
                "xref bind is a COM-backend operation — it relies on AutoCAD's "
                "BIND command to merge symbol tables. Open the drawing in AutoCAD "
                "or use the COM backend."
            ),
            category="not_supported",
        )

    # --- Layout / paperspace ---

    async def layout_list(self) -> CommandResult:
        try:
            layouts = []
            for lay in self._doc.layouts:
                layouts.append({
                    "name": lay.name,
                    "is_modelspace": bool(lay.is_modelspace),
                    "entity_count": sum(1 for _ in lay),
                })
            return CommandResult(ok=True, payload={"layouts": layouts, "count": len(layouts)})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def layout_create(self, name, paper_size=None) -> CommandResult:
        try:
            if name in [l.name for l in self._doc.layouts]:
                return CommandResult(
                    ok=False, error=f"Layout {name!r} already exists",
                    category="invalid_arg",
                )
            lay = self._doc.layouts.new(name)
            if paper_size:
                # ezdxf stores paper size on the plot config; best-effort here.
                try:
                    w, h = float(paper_size[0]), float(paper_size[1])
                    lay.page_setup(size=(w, h))
                except Exception:
                    pass
            return CommandResult(ok=True, payload={"name": name})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def layout_set_active(self, name) -> CommandResult:
        # ezdxf doesn't track an "active" layout — that's a UI concept in
        # AutoCAD. Surface the limitation cleanly.
        try:
            existing = [l.name for l in self._doc.layouts]
            if name not in existing:
                return CommandResult(
                    ok=False, error=f"No layout named {name!r}",
                    category="not_found",
                )
            self._msp = self._doc.layouts.get(name)
            return CommandResult(ok=True, payload={"active": name})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    async def layout_create_viewport(
        self, layout, x, y, width, height, view_extents=None,
    ) -> CommandResult:
        try:
            existing = [l.name for l in self._doc.layouts]
            if layout not in existing:
                return CommandResult(
                    ok=False, error=f"No layout named {layout!r}",
                    category="not_found",
                )
            lay = self._doc.layouts.get(layout)
            if lay.is_modelspace:
                return CommandResult(
                    ok=False,
                    error="Viewports live in paperspace; cannot add to modelspace.",
                    category="invalid_arg",
                )
            # ezdxf paperspace.add_viewport: center, size, view_center, view_height
            cx = float(x) + float(width) / 2.0
            cy = float(y) + float(height) / 2.0
            if view_extents is not None:
                vx1, vy1, vx2, vy2 = view_extents
                vcx = (float(vx1) + float(vx2)) / 2.0
                vcy = (float(vy1) + float(vy2)) / 2.0
                vh = abs(float(vy2) - float(vy1))
            else:
                # Fall back to a sensible default — viewport same size as the
                # paperspace rectangle, view height matching modelspace extents.
                vcx, vcy, vh = 0.0, 0.0, max(height, width)
            vp = lay.add_viewport(
                center=(cx, cy),
                size=(float(width), float(height)),
                view_center_point=(vcx, vcy),
                view_height=vh,
            )
            return CommandResult(ok=True, payload={
                "handle": vp.dxf.handle,
                "layout": layout,
                "center": [cx, cy],
                "size": [float(width), float(height)],
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

    # --- Symbol library ---

    async def library_list(self) -> CommandResult:
        from autocad_mcp.arch.library import symbol_names
        names = symbol_names()
        return CommandResult(ok=True, payload={"symbols": names, "count": len(names)})

    async def library_define(self, name: str) -> CommandResult:
        from autocad_mcp.arch.library import SYMBOL_LIBRARY, apply_to_ezdxf_block
        if name not in SYMBOL_LIBRARY:
            return CommandResult(ok=False, error=f"No library symbol named {name!r}", category="not_found")
        if name in self._doc.blocks:
            return CommandResult(ok=True, payload={"name": name, "existed": True})
        try:
            block = self._doc.blocks.new(name=name)
            apply_to_ezdxf_block(block, SYMBOL_LIBRARY[name])
            return CommandResult(ok=True, payload={
                "name": name,
                "entity_count": sum(1 for _ in block),
            })
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex), category="internal_error")

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
        # Define-on-demand so callers don't have to chain define + insert.
        r = await self.library_define(name)
        if not r.ok:
            return r
        return await self.block_insert(name, x, y, scale, rotation)

    # --- View ---

    async def get_screenshot(self) -> CommandResult:
        data = self._screenshot.capture()
        if data:
            return CommandResult(ok=True, payload=data)
        return CommandResult(ok=False, error="Screenshot render failed", category="internal_error")

    # --- Helpers ---

    @staticmethod
    def _color_to_int(color: str | int) -> int:
        # Thin wrapper kept for backward compat with existing call sites;
        # the actual mapping lives in `_colors.py` so the COM backend uses
        # the same table.
        return color_to_aci(color)
