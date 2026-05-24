"""Drawing-units awareness for `arch.*` composites.

The `arch` toolbox used to hardcode imperial conventions (`SF` for area
labels, inch-scaled text offsets, 10-foot scale bars). For non-US projects
those defaults are wrong: metric drawings want `m²` and millimeter-scaled
text.

This module derives a `UnitSystem` from a document's `$INSUNITS` header
variable so each composite can read the right defaults without the caller
having to override every parameter.

Adoption is opt-in per composite. `arch.room` (area suffix) is the first
caller; the rest pass through whatever the caller provides today and can
adopt this module incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# DXF $INSUNITS code → human label. Subset of the DXF spec; we only care
# about the units that produce sensible architectural drawings.
_INSUNITS_LABEL = {
    0: "unitless",
    1: "inches",
    2: "feet",
    3: "miles",
    4: "millimeters",
    5: "centimeters",
    6: "meters",
    14: "decimeters",
}


@dataclass(frozen=True)
class UnitSystem:
    """Default conventions tied to the drawing's `$INSUNITS`.

    Fields:
      insunits      — raw DXF code (1-14) or 0 for unitless
      label         — human-readable name ("feet", "millimeters", ...)
      area_suffix   — appears after the area number in `arch.room` labels
      area_factor   — multiplier for raw drawing-unit area before display
                      (e.g. inches → "SF" needs /144; mm → "m²" needs /1e6)
      text_height   — default `char_height` for room labels and similar
      dim_offset    — default offset for `dimension_chain`
      tolerant      — when True (unitless / 0), prefer caller-supplied
                      values silently rather than overriding them

    Construct via `UnitSystem.from_doc(doc)` or `UnitSystem.imperial()`
    / `UnitSystem.metric()` for the common cases.
    """

    insunits: int
    label: str
    area_suffix: str
    area_factor: float
    text_height: float
    dim_offset: float
    tolerant: bool = False

    @classmethod
    def imperial_inches(cls) -> "UnitSystem":
        # Drawing is in inches. Area is in² → divide by 144 to display as SF.
        return cls(
            insunits=1, label="inches",
            area_suffix="SF", area_factor=1.0 / 144.0,
            text_height=6.0, dim_offset=24.0,
        )

    @classmethod
    def imperial_feet(cls) -> "UnitSystem":
        # Drawing is in feet. Area is ft² already → no conversion.
        return cls(
            insunits=2, label="feet",
            area_suffix="SF", area_factor=1.0,
            text_height=0.5, dim_offset=2.0,
        )

    @classmethod
    def metric_mm(cls) -> "UnitSystem":
        # Drawing is in millimeters. Area is mm² → /1e6 to display as m².
        return cls(
            insunits=4, label="millimeters",
            area_suffix="m²", area_factor=1.0 / 1_000_000.0,
            text_height=150.0, dim_offset=600.0,
        )

    @classmethod
    def metric_meters(cls) -> "UnitSystem":
        return cls(
            insunits=6, label="meters",
            area_suffix="m²", area_factor=1.0,
            text_height=0.15, dim_offset=0.6,
        )

    @classmethod
    def unitless(cls) -> "UnitSystem":
        # Backwards-compat: the historical default was to suffix areas with
        # "SF" regardless of units. We keep that for unitless drawings so
        # existing callers don't see their labels suddenly change. Metric
        # users get "m²" by setting $INSUNITS=4/5/6 on the doc.
        return cls(
            insunits=0, label="unitless",
            area_suffix="SF", area_factor=1.0,
            text_height=6.0, dim_offset=24.0,
            tolerant=True,
        )

    @classmethod
    def from_doc(cls, doc: Any) -> "UnitSystem":
        """Read `$INSUNITS` from an ezdxf document and pick a UnitSystem."""
        try:
            insunits = int(doc.header.get("$INSUNITS", 0))
        except Exception:
            insunits = 0
        return cls.from_insunits(insunits)

    @classmethod
    def from_insunits(cls, code: int) -> "UnitSystem":
        """Build a UnitSystem from a raw `$INSUNITS` integer."""
        if code == 1:
            return cls.imperial_inches()
        if code == 2:
            return cls.imperial_feet()
        if code == 4:
            return cls.metric_mm()
        if code == 5:
            # cm — round-trips to m² via /10_000
            return cls(
                insunits=5, label="centimeters",
                area_suffix="m²", area_factor=1.0 / 10_000.0,
                text_height=15.0, dim_offset=60.0,
            )
        if code == 6:
            return cls.metric_meters()
        if code == 14:
            return cls(
                insunits=14, label="decimeters",
                area_suffix="m²", area_factor=1.0 / 100.0,
                text_height=1.5, dim_offset=6.0,
            )
        return cls.unitless()

    def format_area(self, raw_area: float) -> str:
        """Format a raw drawing-unit area with the right suffix.

        `raw_area` is in (drawing units)² — i.e. whatever the geometry
        helpers returned. The factor converts to the display unit
        (typically SF or m²).
        """
        value = raw_area * self.area_factor
        # 0 decimals for large areas, 2 for small. Heuristic but readable.
        if abs(value) >= 100:
            num = f"{value:.0f}"
        else:
            num = f"{value:.1f}"
        if self.area_suffix:
            return f"{num} {self.area_suffix}"
        return num
