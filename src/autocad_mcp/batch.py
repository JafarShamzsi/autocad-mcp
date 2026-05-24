"""Multi-drawing batch operations via accoreconsole.exe.

`drawing(operation="batch", data={paths: [...], script: "..."})` lets
Claude run one LISP script against many DWG / DXF files without driving
the GUI 50 times. Each file gets its own accoreconsole subprocess —
isolated failure, serial by default (parallel is an option but rarely
necessary at this scale).

Use cases this enables:
  * Rename a layer across every sheet in a project
  * Mass-purge unused styles
  * Apply firm-standard dimstyle to N drawings
  * Run AUDIT + QSAVE across a folder of field-edited files

This is `execute_lisp`-shaped: the script is arbitrary user-supplied
LISP that runs with full AutoCAD authority. The same gate
(`AUTOCAD_MCP_ENABLE_LISP_EXEC=1`) is required.

The accoreconsole shell-out + .scr-script machinery is already proven
by `dwg_reader.py`'s DWG-conversion path; this module reuses the same
patterns.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class FileResult:
    """Outcome for one file in a batch run."""

    path: str
    ok: bool
    duration_s: float
    returncode: int | None = None
    error: str | None = None
    stdout_tail: str | None = None


@dataclass
class BatchResult:
    """Aggregate outcome across the whole batch."""

    files_total: int = 0
    files_ok: int = 0
    files_failed: int = 0
    total_duration_s: float = 0.0
    per_file: list[FileResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _accoreconsole_path() -> Path | None:
    """Reuse the discovery the DWG reader already does."""
    from autocad_mcp.project.dwg_reader import find_accoreconsole

    return find_accoreconsole()


def _write_lisp_file(lisp_code: str, ipc_dir: Path) -> Path:
    """Persist the LISP script to a temp file inside the IPC dir.

    accoreconsole's `(load "...")` reads the file each invocation, so a
    single shared LISP file across the whole batch is correct and avoids
    re-quoting the script per .scr.
    """
    ipc_dir.mkdir(parents=True, exist_ok=True)
    f = ipc_dir / f"batch_{uuid.uuid4().hex[:12]}.lsp"
    f.write_text(lisp_code, encoding="utf-8")
    return f


def _build_scr(lisp_path: Path, save: bool) -> str:
    """Build the .scr script body that accoreconsole runs against one file.

    Steps:
      1. `_FILEDIA 0` — disable any modal "Save changes?" prompt
      2. `(load "<lisp>")` — pull in the user's script
      3. `_QSAVE` if `save=True`, else skip the save
      4. `_QUIT Y` — exit cleanly even with unsaved changes
    """
    lines = [
        "_FILEDIA",
        "0",
        f'(load "{lisp_path.as_posix()}")',
    ]
    if save:
        lines.append("_QSAVE")
    lines.extend(["_QUIT", "Y", ""])
    return "\n".join(lines)


def _decode_acc_stdout(raw: bytes) -> str:
    """accoreconsole writes UTF-16LE on Windows. Empirically confirmed in
    earlier sessions. Fall back to UTF-8 with replacement when decode
    fails — the value is for diagnostics only."""
    try:
        return raw.decode("utf-16-le", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _run_one(
    accoreconsole: Path,
    dwg_path: Path,
    scr_path: Path,
    timeout_s: float,
) -> FileResult:
    """Run accoreconsole against a single file."""
    t0 = time.time()
    try:
        result = subprocess.run(
            [
                str(accoreconsole),
                "/i", str(dwg_path),
                "/s", str(scr_path),
            ],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        dt = time.time() - t0
        ok = result.returncode == 0
        stdout = _decode_acc_stdout(result.stdout or b"")
        # Trim to the last ~1500 chars — full transcripts are too noisy.
        tail = stdout[-1500:] if stdout else None
        err = None
        if not ok:
            err_decoded = _decode_acc_stdout(result.stderr or b"")
            err = (err_decoded or stdout or "")[-500:]
        return FileResult(
            path=str(dwg_path),
            ok=ok,
            duration_s=round(dt, 3),
            returncode=result.returncode,
            error=err if not ok else None,
            stdout_tail=tail,
        )
    except subprocess.TimeoutExpired as ex:
        dt = time.time() - t0
        return FileResult(
            path=str(dwg_path),
            ok=False,
            duration_s=round(dt, 3),
            returncode=None,
            error=f"timed out after {timeout_s:.1f}s",
        )
    except Exception as ex:
        dt = time.time() - t0
        return FileResult(
            path=str(dwg_path),
            ok=False,
            duration_s=round(dt, 3),
            returncode=None,
            error=str(ex)[:300],
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_batch_script(
    paths: Iterable[str | Path],
    lisp_code: str,
    *,
    save: bool = True,
    timeout_per_file: float = 60.0,
    ipc_dir: Path | None = None,
) -> BatchResult:
    """Run `lisp_code` against each of `paths` via accoreconsole.

    Args:
        paths: list of DWG / DXF paths.
        lisp_code: arbitrary AutoLISP to execute against each file. Use
            `(princ ...)` to write diagnostic lines; they're captured in
            the per-file `stdout_tail`.
        save: when True (default), every file is `_QSAVE`d before
            accoreconsole quits. Pass False for read-only batches
            (e.g. probing for entity counts).
        timeout_per_file: kill the subprocess after this many seconds.
            Default 60s; bump for files with heavy geometry. The hard
            ceiling protects against an infinite-loop script.
        ipc_dir: where to stage the temp `.scr` + `.lsp` files. Defaults
            to `<config.default_ipc_dir>`.

    Returns:
        `BatchResult` with per-file outcomes and aggregate counts.

    Raises:
        FileNotFoundError: accoreconsole isn't installed.
        ValueError: paths list is empty.
    """
    accoreconsole = _accoreconsole_path()
    if accoreconsole is None:
        raise FileNotFoundError(
            "accoreconsole.exe not found. Install full AutoCAD or set "
            "AUTOCAD_MCP_ACCORECONSOLE to its path."
        )

    paths_list = [Path(p).expanduser().resolve() for p in paths]
    if not paths_list:
        raise ValueError("batch script needs at least one path")

    # Resolve IPC dir lazily so tests can override without importing config.
    if ipc_dir is None:
        from autocad_mcp.config import default_ipc_dir
        ipc_dir = default_ipc_dir()

    lisp_path = _write_lisp_file(lisp_code, ipc_dir)
    scr_body = _build_scr(lisp_path, save=save)
    scr_path = ipc_dir / f"batch_{uuid.uuid4().hex[:12]}.scr"
    scr_path.write_text(scr_body, encoding="ascii")

    result = BatchResult(files_total=len(paths_list))
    t0 = time.time()
    try:
        for p in paths_list:
            if not p.exists():
                result.per_file.append(FileResult(
                    path=str(p), ok=False, duration_s=0.0,
                    error="file not found",
                ))
                result.files_failed += 1
                continue
            log.info("batch_processing_file", file=str(p))
            file_r = _run_one(accoreconsole, p, scr_path, timeout_per_file)
            result.per_file.append(file_r)
            if file_r.ok:
                result.files_ok += 1
            else:
                result.files_failed += 1
    finally:
        result.total_duration_s = round(time.time() - t0, 3)
        # Clean up temp scripts. Best effort — leftover files just sit
        # in the IPC dir until the next session's stale-files sweep.
        for f in (scr_path, lisp_path):
            try:
                f.unlink(missing_ok=True)
            except OSError as ex:
                log.debug("batch_temp_cleanup_failed", file=str(f), error=str(ex))

    return result


async def run_batch_script_async(
    paths: Iterable[str | Path],
    lisp_code: str,
    *,
    save: bool = True,
    timeout_per_file: float = 60.0,
    ipc_dir: Path | None = None,
) -> BatchResult:
    """Async wrapper — run the batch off the event loop.

    The synchronous version blocks for the full duration (could be
    minutes on a large batch). MCP tools call this version so concurrent
    MCP calls (status checks, etc.) aren't starved.
    """
    return await asyncio.to_thread(
        run_batch_script,
        paths, lisp_code,
        save=save,
        timeout_per_file=timeout_per_file,
        ipc_dir=ipc_dir,
    )
