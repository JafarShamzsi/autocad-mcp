"""Drawing reader that supports both DXF and DWG.

The indexer was originally DXF-only (`ezdxf.readfile`). Real arch projects
contain `.dwg` files — for those we need a converter. Two paths supported,
checked in order:

  1. **accoreconsole.exe** — AutoCAD's headless console, ships with every
     full-AutoCAD install. Preferred because it's already on the user's
     machine and produces a DXF identical to what AutoCAD itself would
     write. No GUI, no license consumption beyond AutoCAD's normal seat.

  2. **ODA File Converter** — free standalone tool from the Open Design
     Alliance. Used when accoreconsole isn't available (AutoCAD LT, or
     AutoCAD not installed). Single-binary, headless.

A single public entry point — `read_drawing(path)` — picks the right path
based on file extension and converter availability. Callers (chiefly the
indexer) don't need to know which path was taken.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import ezdxf
import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Converter discovery
# ---------------------------------------------------------------------------


def find_accoreconsole() -> Path | None:
    """Locate accoreconsole.exe in any installed AutoCAD year.

    Search order:
      1. `AUTOCAD_MCP_ACCORECONSOLE` env var (explicit override).
      2. Standard install roots:
           `C:\\Program Files\\Autodesk\\AutoCAD <year>\\`
           `C:\\Program Files (x86)\\Autodesk\\AutoCAD <year>\\`
           `D:\\Program Files\\Autodesk\\AutoCAD <year>\\`
    Returns the newest-year install if multiple are present.
    """
    if sys.platform != "win32":
        return None

    explicit = os.environ.get("AUTOCAD_MCP_ACCORECONSOLE", "").strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        log.debug("accoreconsole_env_path_missing", path=explicit)

    search_roots = [
        Path(r"C:\Program Files\Autodesk"),
        Path(r"C:\Program Files (x86)\Autodesk"),
        Path(r"D:\Program Files\Autodesk"),
    ]
    candidates: list[tuple[str, Path]] = []
    for autodesk_root in search_roots:
        if not autodesk_root.is_dir():
            continue
        for child in autodesk_root.iterdir():
            if not child.is_dir() or not child.name.startswith("AutoCAD "):
                continue
            exe = child / "accoreconsole.exe"
            if exe.exists():
                candidates.append((child.name, exe))
    if not candidates:
        return None
    # Pick the newest by directory name (lexical works for "AutoCAD YYYY").
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def find_oda_converter() -> Path | None:
    """Locate the ODA File Converter executable.

    Searches `C:\\Program Files\\ODA\\ODAFileConverter <version>\\`.
    """
    if sys.platform != "win32":
        return None
    for root in (r"C:\Program Files\ODA", r"C:\Program Files (x86)\ODA"):
        p = Path(root)
        if not p.is_dir():
            continue
        for child in p.iterdir():
            if not child.is_dir():
                continue
            for exe_name in ("ODAFileConverter.exe", "ODAFileConverter"):
                exe = child / exe_name
                if exe.exists():
                    return exe
    return None


def available_dwg_converters() -> dict[str, str | None]:
    """Diagnostic: report which DWG converters this system has."""
    acc = find_accoreconsole()
    oda = find_oda_converter()
    return {
        "accoreconsole": str(acc) if acc else None,
        "oda_file_converter": str(oda) if oda else None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class DWGConverterMissing(RuntimeError):
    """Raised when a .dwg needs converting but no converter is installed."""


# ---------------------------------------------------------------------------
# DWG conversion cache (process-local LRU)
# ---------------------------------------------------------------------------
#
# `read_drawing` for a DWG shells out to accoreconsole (~3s per file). When
# the same DWG is read repeatedly within a session — vendor indexing, catalog
# reindexes, repeated `catalog_insert` against the same source — those costs
# add up fast.
#
# The cache is keyed by `(abs_path, mtime_ns, size)` so any edit to the file
# invalidates the entry automatically. Capacity is bounded so a long-running
# server can't accumulate hundreds of cached docs. The default of 32 entries
# is plenty for typical project sizes; override via the env var.
#
# Cached values are full `ezdxf.document.Drawing` instances. They aren't
# copies — callers must treat them as read-only because mutations bleed into
# subsequent cache hits. The current call sites all do read-only inspection
# (block enumeration, layer listing) so this is fine in practice; new
# call sites that intend to mutate must call `clear_dwg_cache()` first or
# read the file through a non-cached path.

from collections import OrderedDict
import threading


_DWG_CACHE_LOCK = threading.Lock()
_DWG_CACHE: "OrderedDict[tuple[str, int, int], ezdxf.document.Drawing]" = OrderedDict()


def _dwg_cache_max_entries() -> int:
    raw = os.environ.get("AUTOCAD_MCP_DWG_CACHE_SIZE", "32").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 32
    return max(0, value)


def clear_dwg_cache() -> int:
    """Empty the read-side DWG conversion cache. Returns entries cleared.

    Callers that mutate a previously-read document should clear the cache
    so subsequent reads see the new state. Tests also use this to keep
    runs independent.
    """
    with _DWG_CACHE_LOCK:
        n = len(_DWG_CACHE)
        _DWG_CACHE.clear()
        return n


def dwg_cache_stats() -> dict:
    """Return current cache size + capacity. Useful for diagnostics."""
    with _DWG_CACHE_LOCK:
        return {
            "entries": len(_DWG_CACHE),
            "capacity": _dwg_cache_max_entries(),
        }


def read_drawing(path: str | os.PathLike) -> "ezdxf.document.Drawing":
    """Read a `.dxf` or `.dwg` into an in-memory ezdxf document.

    For `.dxf` files this is just `ezdxf.readfile`. For `.dwg`, the file is
    converted to a temporary DXF first via the best available converter
    (accoreconsole → ODA File Converter), then read. DWG reads are cached
    on `(absolute_path, mtime_ns, size)`; the second access is near-instant.

    The cache is **shared across callers** and yields the same `Drawing`
    object each time — DO NOT mutate it unless you're prepared to call
    `clear_dwg_cache()` afterwards.

    Raises:
        DWGConverterMissing: a .dwg was passed but no converter is installed.
        FileNotFoundError: the input path doesn't exist.
        ezdxf.DXFError: the converted DXF is invalid.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)

    suffix = p.suffix.lower()
    if suffix == ".dxf":
        # No cache for DXF — ezdxf.readfile is already cheap and the
        # cache invariant (shared, read-only) is a footgun for callers
        # that legitimately mutate the result.
        return ezdxf.readfile(str(p))
    if suffix == ".dwg":
        stat = p.stat()
        key = (str(p), stat.st_mtime_ns, stat.st_size)
        with _DWG_CACHE_LOCK:
            cached = _DWG_CACHE.get(key)
            if cached is not None:
                _DWG_CACHE.move_to_end(key)
                return cached
        # Conversion happens outside the lock so concurrent readers of
        # *different* files don't serialize on each other.
        with tempfile.TemporaryDirectory() as tmpd:
            dxf_out = Path(tmpd) / (p.stem + ".dxf")
            _convert_dwg(p, dxf_out)
            doc = ezdxf.readfile(str(dxf_out))
        with _DWG_CACHE_LOCK:
            # Re-check: another thread may have populated this key while
            # we were converting. Either is fine — we just want one entry
            # at the end and the LRU order updated.
            _DWG_CACHE[key] = doc
            _DWG_CACHE.move_to_end(key)
            cap = _dwg_cache_max_entries()
            while cap and len(_DWG_CACHE) > cap:
                _DWG_CACHE.popitem(last=False)
        return doc

    raise ValueError(f"Unsupported drawing extension: {p.suffix} (expected .dxf or .dwg)")


# ---------------------------------------------------------------------------
# DWG conversion paths
# ---------------------------------------------------------------------------


def _convert_dwg(dwg_path: Path, dxf_out: Path) -> None:
    """Convert dwg → dxf_out via the best available path."""
    acc = find_accoreconsole()
    if acc is not None:
        _convert_with_accoreconsole(acc, dwg_path, dxf_out)
        if dxf_out.exists():
            return
        log.warning("dwg_accoreconsole_produced_no_output", input=str(dwg_path))

    oda = find_oda_converter()
    if oda is not None:
        _convert_with_oda(oda, dwg_path, dxf_out)
        if dxf_out.exists():
            return
        log.warning("dwg_oda_produced_no_output", input=str(dwg_path))

    raise DWGConverterMissing(
        "Cannot read .dwg files — no converter found. Install one of:\n"
        "  • Full AutoCAD (provides `accoreconsole.exe` — preferred)\n"
        "  • ODA File Converter (free, "
        "https://www.opendesign.com/guestfiles/oda_file_converter)\n"
        f"After install, retry. Diagnostic: {available_dwg_converters()}"
    )


def _convert_with_accoreconsole(
    accoreconsole: Path,
    dwg_path: Path,
    dxf_out: Path,
) -> None:
    """Use AutoCAD's headless console to convert dwg → dxf.

    Builds a `.scr` script (line-per-command) that disables file dialogs,
    runs DXFOUT with explicit version + precision, then quits cleanly.

    The prompt sequence below (FILEDIA → DXFOUT → V → 2018 → 16) is verified
    against AutoCAD 2026/2027. Older AutoCAD releases may have prompted
    differently; if you bump the minimum supported AutoCAD version, re-run a
    sample conversion and confirm the prompt order still matches.
    """
    # Forward slashes in paths — AutoCAD command line tolerates both, but
    # forward slashes avoid any backslash-escape ambiguity in the .scr file.
    script_lines = [
        "_FILEDIA",                          # disable file dialogs
        "0",
        "_DXFOUT",
        dxf_out.as_posix(),                  # output filename
        "V",                                 # version sub-option
        "2018",                              # DXF release
        "16",                                # decimal precision
        "_QUIT",
        "Y",                                 # don't save changes to the source
        "",                                  # trailing newline
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".scr", delete=False, encoding="ascii"
    ) as fp:
        fp.write("\n".join(script_lines))
        script_path = Path(fp.name)

    try:
        # /i input drawing, /s script.  accoreconsole exits 0 on success.
        # Paths in argv are consumed by accoreconsole's Win32 arg parser
        # (backslashes are fine); only paths *inside* the script body
        # need `as_posix()` because that's where AutoCAD's command-line
        # interpreter chokes on `\` (escape char in command strings).
        result = subprocess.run(
            [str(accoreconsole), "/i", str(dwg_path), "/s", str(script_path)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")[:500]
            log.warning(
                "dwg_accoreconsole_nonzero_exit",
                returncode=result.returncode,
                stderr=stderr,
            )
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass


def _convert_with_oda(
    oda: Path,
    dwg_path: Path,
    dxf_out: Path,
) -> None:
    """Use ODA File Converter to convert dwg → dxf_out.

    ODA takes a directory and converts every drawing in it that matches a
    filter pattern. We point it at a temp directory containing a single
    copy of the input drawing, then move the resulting DXF into place.
    """
    import shutil

    with tempfile.TemporaryDirectory() as input_dir, tempfile.TemporaryDirectory() as output_dir:
        staged = Path(input_dir) / dwg_path.name
        shutil.copyfile(dwg_path, staged)

        # ODA CLI: <input_dir> <output_dir> <output_version> <output_format>
        #         <recurse> <audit> [filter]
        # output_version: ACAD2018; output_format: DXF; recurse=0; audit=0
        result = subprocess.run(
            [
                str(oda),
                str(input_dir),
                str(output_dir),
                "ACAD2018",
                "DXF",
                "0",  # recurse
                "1",  # audit
                dwg_path.name,
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                "dwg_oda_nonzero_exit",
                returncode=result.returncode,
                stderr=(result.stderr or b"").decode("utf-8", errors="replace")[:500],
            )

        produced = Path(output_dir) / (dwg_path.stem + ".dxf")
        if produced.exists():
            shutil.move(str(produced), str(dxf_out))


# ---------------------------------------------------------------------------
# DXF → DWG write
# ---------------------------------------------------------------------------


# DWG-format version codes accepted by accoreconsole's `_SAVEAS _DWG` prompt.
# These are the "broadly recognized" file-format generations — older versions
# work but readers stop accepting them after a few years. 2018 is the current
# default (AutoCAD 2018-2027 all read/write it natively).
DWG_VERSIONS: dict[str, str] = {
    "2018": "2018",
    "2013": "2013",
    "2010": "2010",
    "2007": "2007",
    "2004": "2004",
    "2000": "2000",
    # Aliases for users who type the AutoCAD year not knowing which format
    # generation that maps to. Source: Autodesk DWG format reference.
    "2027": "2018", "2026": "2018", "2025": "2018", "2024": "2018",
    "2023": "2018", "2022": "2018", "2021": "2018", "2020": "2018", "2019": "2018",
    "2017": "2013", "2016": "2013", "2015": "2013",
    "2012": "2010", "2011": "2010",
    "2009": "2007", "2008": "2007",
    "2006": "2004", "2005": "2004",
    "2003": "2000", "2002": "2000", "2001": "2000",
}

DEFAULT_DWG_VERSION = "2018"


def _normalize_dwg_version(version: str | None) -> str:
    """Map an arbitrary version label to a SAVEAS-accepted code.

    Unknown values fall back to the default rather than failing, on the
    assumption that the caller meant "modern DWG" and we should produce
    output. Logged at debug so misuse is diagnosable.
    """
    if not version:
        return DEFAULT_DWG_VERSION
    v = str(version).strip()
    if v in DWG_VERSIONS:
        return DWG_VERSIONS[v]
    log.debug("dwg_version_unknown_using_default",
              requested=version, default=DEFAULT_DWG_VERSION)
    return DEFAULT_DWG_VERSION


def write_dwg(
    doc: "ezdxf.document.Drawing",
    dwg_path: str | os.PathLike,
    *,
    version: str | None = None,
) -> Path:
    """Save an ezdxf document as a `.dwg` file via accoreconsole or ODA.

    The flow:
      1. saveas a temp .dxf
      2. convert to .dwg via the best available converter
      3. move result to the requested path

    Args:
        doc: ezdxf document to save.
        dwg_path: target .dwg filename (or any path; suffix is added if missing).
        version: DWG format version. Accepts "2018"/"2013"/.../"2000" or an
            AutoCAD year (e.g. "2024"). Defaults to "2018" — broadly readable
            by every AutoCAD version since AutoCAD 2018.

    Returns:
        The actual path that was written.

    Raises:
        DWGConverterMissing: no converter installed.
        RuntimeError: conversion failed.
    """
    out_path = Path(dwg_path).expanduser().resolve()
    if out_path.suffix.lower() != ".dwg":
        out_path = out_path.with_suffix(".dwg")
    version_code = _normalize_dwg_version(version)

    with tempfile.TemporaryDirectory() as tmpd:
        tmp_dxf = Path(tmpd) / (out_path.stem + ".dxf")
        doc.saveas(str(tmp_dxf))
        _convert_dxf_to_dwg(tmp_dxf, out_path, version=version_code)
    if not out_path.exists():
        raise RuntimeError(
            f"DWG conversion produced no output at {out_path}. Diagnostic: "
            f"{available_dwg_converters()}"
        )
    return out_path


def _convert_dxf_to_dwg(dxf_path: Path, dwg_out: Path, *, version: str = DEFAULT_DWG_VERSION) -> None:
    """Convert dxf → dwg_out via the best available path."""
    acc = find_accoreconsole()
    if acc is not None:
        _convert_dxf_to_dwg_with_accoreconsole(acc, dxf_path, dwg_out, version=version)
        if dwg_out.exists():
            return
        log.warning("dxf_accoreconsole_produced_no_output", input=str(dxf_path))

    oda = find_oda_converter()
    if oda is not None:
        _convert_dxf_to_dwg_with_oda(oda, dxf_path, dwg_out, version=version)
        if dwg_out.exists():
            return
        log.warning("dxf_oda_produced_no_output", input=str(dxf_path))

    raise DWGConverterMissing(
        "Cannot write .dwg files — no converter found. Install one of:\n"
        "  • Full AutoCAD (provides `accoreconsole.exe` — preferred)\n"
        "  • ODA File Converter (free, "
        "https://www.opendesign.com/guestfiles/oda_file_converter)\n"
        f"After install, retry. Diagnostic: {available_dwg_converters()}"
    )


def _convert_dxf_to_dwg_with_accoreconsole(
    accoreconsole: Path,
    dxf_path: Path,
    dwg_out: Path,
    *,
    version: str = DEFAULT_DWG_VERSION,
) -> None:
    """Use accoreconsole.exe to convert dxf → dwg via SAVEAS.

    The `SAVEAS` prompt in AutoCAD 2018+ is:
      `Enter file format [R14/2000/2004/2007/2010/2013/2018/.../DXF/Template]
       <2018>:`
    Format codes are version numbers — NOT `_DWG` (which we used to send and
    which caused the prompt to hang waiting for a valid keyword).
    """
    script_lines = [
        "_FILEDIA",
        "0",
        "_SAVEAS",
        version,    # version code IS the format keyword (2018/2013/.../R14)
        dwg_out.as_posix(),
        "Y",        # overwrite if exists
        "_QUIT",
        "Y",
        "",
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".scr", delete=False, encoding="ascii"
    ) as fp:
        fp.write("\n".join(script_lines))
        script_path = Path(fp.name)
    try:
        result = subprocess.run(
            [str(accoreconsole), "/i", str(dxf_path), "/s", str(script_path)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")[:500]
            log.warning(
                "dxf_accoreconsole_nonzero_exit",
                returncode=result.returncode,
                stderr=stderr,
            )
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass


def _convert_dxf_to_dwg_with_oda(
    oda: Path,
    dxf_path: Path,
    dwg_out: Path,
    *,
    version: str = DEFAULT_DWG_VERSION,
) -> None:
    """Use ODA File Converter to convert dxf → dwg_out.

    ODA accepts a slightly different version label (`ACAD<year>`); the
    code is the same generation number we use for accoreconsole.
    """
    import shutil

    oda_version = f"ACAD{version}"
    with tempfile.TemporaryDirectory() as input_dir, tempfile.TemporaryDirectory() as output_dir:
        staged = Path(input_dir) / dxf_path.name
        shutil.copyfile(dxf_path, staged)
        result = subprocess.run(
            [
                str(oda),
                str(input_dir),
                str(output_dir),
                oda_version,
                "DWG",
                "0",  # recurse
                "1",  # audit
                dxf_path.name,
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                "dxf_oda_nonzero_exit",
                returncode=result.returncode,
                stderr=(result.stderr or b"").decode("utf-8", errors="replace")[:500],
            )
        produced = Path(output_dir) / (dxf_path.stem + ".dwg")
        if produced.exists():
            shutil.move(str(produced), str(dwg_out))
