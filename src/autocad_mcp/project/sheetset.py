"""Sheet set (.dst) reader.

AutoCAD sheet sets live in `.dst` files. **The format has changed:**

  - **Old (~AutoCAD 2013–pre-2024):** plain XML, parseable here.
  - **New (AutoCAD 2024+):** opaque binary (all three samples that ship
    with AutoCAD 2027 begin with `D0 CD 14 E3 …` — not XML, not OLE/CFB,
    not ZIP). Reading the binary form requires the AcSmSheetSetMgr COM
    object, which is COM-backend-only.

This reader handles the XML format. When it sees a binary header it
raises `SheetSetBinaryUnsupported` with a pointer to the COM path so the
caller can fall back gracefully.

Parsing strategy (XML): the .dst format has shifted across AutoCAD
releases and uses a flat ResourceId-graph for objects in some versions
and nested children in others. Rather than encode a specific schema,
this reader walks the whole XML tree and extracts every element whose
tag contains "Sheet" (case-insensitive) and whose immediate children
include enough of the recognised fields to be a real sheet entry.
That's tolerant enough to work across AutoCAD 2013-pre-2024 XML .dst
files in practice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


# The proprietary binary .dst format (AutoCAD 2024+ samples) always starts
# with these bytes. Detect the header so we can raise a clear error rather
# than letting `xml.etree.ElementTree.ParseError` confuse the caller.
_BINARY_DST_HEADER = bytes.fromhex("d0 cd 14 e3")


class SheetSetBinaryUnsupported(RuntimeError):
    """The .dst is in AutoCAD's proprietary binary format which this
    pure-Python reader cannot decode. Use the COM backend's
    AcSmSheetSetMgr or convert to XML format via a third-party tool."""


# Field-name → set of element-tag variants AutoCAD has used over the years.
# All comparisons are case-insensitive.
_FIELD_TAGS: dict[str, frozenset[str]] = {
    "number":     frozenset({"Number", "SheetNumber"}),
    "title":      frozenset({"Title", "SheetTitle", "Name"}),
    "description": frozenset({"Desc", "Description", "SheetDescription"}),
    "dwg_file":   frozenset({"FileName", "DwgFile", "Filename"}),
    "layout":     frozenset({"SheetLayoutName", "Layout", "LayoutName"}),
}


@dataclass
class Sheet:
    """One sheet within a sheet set."""

    number: str = ""
    title: str = ""
    description: str = ""
    dwg_file: str = ""           # raw value as written in the .dst
    layout: str = ""             # paperspace layout name within the dwg


@dataclass
class SheetSet:
    """Parsed contents of a .dst file."""

    name: str
    path: Path                    # absolute path to the .dst file
    sheets: list[Sheet] = field(default_factory=list)


def read_sheetset(path: str | Path) -> SheetSet:
    """Parse a `.dst` file into a SheetSet.

    Raises:
        FileNotFoundError: the .dst path doesn't exist.
        SheetSetBinaryUnsupported: the file is AutoCAD 2024+ binary format.
        xml.etree.ElementTree.ParseError: the file isn't valid XML.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f".dst not found: {p}")

    # Cheap header probe before we hand the file to ElementTree. The binary
    # format produces a confusing "not well-formed (invalid token)" parse
    # error otherwise.
    with p.open("rb") as fp:
        head = fp.read(len(_BINARY_DST_HEADER))
    if head == _BINARY_DST_HEADER:
        raise SheetSetBinaryUnsupported(
            f"{p.name} is in AutoCAD's proprietary binary .dst format "
            "(AutoCAD 2024+ samples all use this format). The pure-Python "
            "XML reader can't decode it. Workarounds:\n"
            "  • Use the COM backend's AcSmSheetSetMgr to read the file\n"
            "    inside a running AutoCAD instance.\n"
            "  • Open the .dst in AutoCAD's Sheet Set Manager, export the\n"
            "    sheet list manually, and feed that to the indexer.\n"
            "  • If you author your own .dst, AutoCAD 2018-era XML format\n"
            "    is parseable here."
        )

    tree = ET.parse(str(p))
    root = tree.getroot()

    name = _first_text(root, ("Name", "SheetSetName", "Title"), default="") or p.stem
    sheets = _extract_sheets(root)
    return SheetSet(name=name.strip() or p.stem, path=p, sheets=sheets)


# ---------------------------------------------------------------------------
# Extraction internals
# ---------------------------------------------------------------------------


def _localname(elem: ET.Element) -> str:
    """Return the element's tag with any XML namespace prefix stripped."""
    tag = elem.tag
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _first_text(parent: ET.Element, tag_options: tuple[str, ...], default: str = "") -> str:
    """Return the text of the first descendant whose local-name matches any of
    `tag_options` (case-insensitive), without crossing into nested sheets.

    The bounded walk matters when a sheet element has nested sheet-like children
    (e.g. an AcSmSheet wrapping an inner AcSmSheet). Without bounding, the
    outer sheet's `_first_text("Number")` would return the nested sheet's
    number — a silent bleed. The nested sheet is picked up separately by the
    outer `root.iter()` in `_extract_sheets`.
    """
    wanted = {t.lower() for t in tag_options}
    for child in _bounded_iter(parent):
        if _localname(child).lower() in wanted:
            return (child.text or "").strip()
    return default


# Local-name patterns for actual sheet elements (as opposed to field elements
# like SheetLayoutName/SheetNumber or container elements like Sheets).
# Real-world .dst files use "Sheet" or "AcSmSheet"; nothing else.
_SHEET_ELEMENT_NAMES = frozenset({"sheet", "acsmsheet"})


def _bounded_iter(elem: ET.Element):
    """Yield `elem` and its descendants, but stop at nested sheet boundaries.

    A child is a sheet boundary if its local-name matches an actual sheet
    element name (`Sheet` / `AcSmSheet`) — NOT just any element containing
    the substring "sheet" (which would falsely match field names like
    SheetLayoutName / SheetTitle and stop bounded iter early).
    The nested sheet is picked up separately by the outer `root.iter()`
    in `_extract_sheets`.
    """
    yield elem
    for child in elem:
        if _localname(child).lower() in _SHEET_ELEMENT_NAMES:
            continue
        yield from _bounded_iter(child)


# Containers/groupings whose tag includes "sheet" but which aren't individual
# sheets themselves. These are excluded from sheet detection.
_NON_SHEET_CONTAINERS = frozenset({
    "sheetset",
    "sheetsetset",
    "acsmsheetset",
    "sheets",            # <Sheets> wrapper that holds the actual sheet list
    "acsmsubset",        # subset folders within a sheet set
    "subsets",
})


def _extract_sheets(root: ET.Element) -> list[Sheet]:
    """Walk the tree and pull every element that looks like a sheet.

    Heuristic: an element is a sheet if its local-name contains "Sheet" and
    its descendants include at least one of FileName / DwgFile (a sheet
    without a drawing reference is meaningless to us). Container elements
    (Sheets, AcSmSheetSet, subsets) are filtered out via `_NON_SHEET_CONTAINERS`.
    """
    sheets: list[Sheet] = []
    for elem in root.iter():
        local = _localname(elem).lower()
        if "sheet" not in local:
            continue
        if local in _NON_SHEET_CONTAINERS:
            continue

        # Must have a DwgFile / FileName somewhere underneath.
        dwg = _first_text(elem, tuple(_FIELD_TAGS["dwg_file"]))
        if not dwg:
            continue

        sheet = Sheet(
            number=_first_text(elem, tuple(_FIELD_TAGS["number"])),
            title=_first_text(elem, tuple(_FIELD_TAGS["title"])),
            description=_first_text(elem, tuple(_FIELD_TAGS["description"])),
            dwg_file=dwg,
            layout=_first_text(elem, tuple(_FIELD_TAGS["layout"])),
        )
        sheets.append(sheet)
    return sheets
