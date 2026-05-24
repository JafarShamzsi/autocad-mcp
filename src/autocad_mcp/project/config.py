"""`.acadproj.toml` loader + ProjectConfig dataclass.

Schema (example):

    [project]
    name = "Example House"
    units = "feet"
    scale = 0.25                # 1/4" = 1'-0"

    [standards]
    layer_standard = "AIA"      # or "NCS"
    dws_file = "./standards.dws"  # optional

    [sheetset]
    file = "./ProjectSheets.dst"  # optional

    [paths]
    support = [".", "./blocks"]
    dwg_root = "."

    [[drawings]]
    name = "FloorPlan-1"
    file = "fp1.dxf"
    discipline = "A"

    [[drawings]]
    name = "Elevations"
    file = "elev.dxf"
    discipline = "A"

The default file name `.acadproj.toml` lives at the project root. All paths
inside the file are resolved relative to that root.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _allowed_project_roots() -> list[Path] | None:
    """Return the configured project-root allowlist or None when unset.

    Parses `AUTOCAD_MCP_PROJECT_ROOTS` (path-separator-delimited list). When
    set, `load_project` refuses any toml that doesn't live under one of the
    listed roots. Unset means "no restriction" — the historic behavior and
    the right default for a single-user dev workstation.
    """
    raw = os.environ.get("AUTOCAD_MCP_PROJECT_ROOTS", "").strip()
    if not raw:
        return None
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def _within_any(target: Path, roots: list[Path]) -> bool:
    """True iff `target` is `r` or a descendant of `r` for some r in roots."""
    for r in roots:
        try:
            target.relative_to(r)
            return True
        except ValueError:
            continue
    return False


@dataclass
class DrawingRef:
    """One drawing in a project."""

    name: str
    file: Path                 # absolute path after resolution
    discipline: str = "A"      # AIA discipline code (A, S, M, E, P, C, L)
    raw_file: str = ""         # original (unresolved) value as written in toml


@dataclass
class ProjectConfig:
    """Loaded `.acadproj.toml` with paths resolved against the project root."""

    name: str
    root: Path                            # directory containing the toml
    config_path: Path                     # the toml itself
    units: str = "feet"
    scale: float = 0.25
    layer_standard: str = "AIA"
    dws_file: Path | None = None
    sheetset_path: Path | None = None     # resolved path to the .dst, if any
    support_paths: list[Path] = field(default_factory=list)
    drawings: list[DrawingRef] = field(default_factory=list)


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file; uses stdlib tomllib (3.11+) or tomli fallback."""
    if sys.version_info >= (3, 11):
        import tomllib  # type: ignore[import-not-found]
    else:  # pragma: no cover - exercised only on 3.10
        try:
            import tomli as tomllib  # type: ignore[import-not-found]
        except ImportError as ex:
            raise RuntimeError(
                "Loading .acadproj.toml on Python 3.10 needs `pip install tomli`."
            ) from ex
    with path.open("rb") as fp:
        return tomllib.load(fp)


def load_project(path: str | Path) -> ProjectConfig:
    """Parse `.acadproj.toml` at `path` and return a resolved ProjectConfig.

    `path` may point at the toml file or the directory containing it. Paths in
    the toml are resolved relative to the toml's directory.

    Raises:
        FileNotFoundError: toml or a referenced drawing file is missing.
        ValueError: required fields missing or malformed.
    """
    config_path = Path(path).expanduser().resolve()
    if config_path.is_dir():
        config_path = config_path / ".acadproj.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"No .acadproj.toml at {config_path}")

    # Optional allowlist for shared / multi-user deployments. Unset env var =
    # no restriction (matches the historic single-user-dev behavior).
    allowlist = _allowed_project_roots()
    if allowlist is not None and not _within_any(config_path, allowlist):
        raise PermissionError(
            f"{config_path} is outside the configured AUTOCAD_MCP_PROJECT_ROOTS "
            f"allowlist ({[str(p) for p in allowlist]})."
        )

    data = _load_toml(config_path)
    root = config_path.parent

    project = data.get("project") or {}
    if "name" not in project:
        raise ValueError(f"{config_path}: [project].name is required")

    standards = data.get("standards") or {}
    paths_cfg = data.get("paths") or {}
    drawings_cfg = data.get("drawings") or []
    sheetset_cfg = data.get("sheetset") or {}

    dws_value = standards.get("dws_file")
    dws_file = (root / dws_value).resolve() if dws_value else None

    sheetset_value = sheetset_cfg.get("file")
    sheetset_path = (root / sheetset_value).resolve() if sheetset_value else None

    support_paths = [
        (root / p).resolve() for p in (paths_cfg.get("support") or [])
    ]

    drawings: list[DrawingRef] = []
    for i, d in enumerate(drawings_cfg):
        if "file" not in d:
            raise ValueError(
                f"{config_path}: [[drawings]] entry #{i} missing 'file'"
            )
        raw_file = str(d["file"])
        abs_file = (root / raw_file).resolve()
        drawings.append(
            DrawingRef(
                name=str(d.get("name", abs_file.stem)),
                file=abs_file,
                discipline=str(d.get("discipline", "A")).upper(),
                raw_file=raw_file,
            )
        )

    return ProjectConfig(
        name=str(project["name"]),
        root=root,
        config_path=config_path,
        units=str(project.get("units", "feet")),
        scale=float(project.get("scale", 0.25)),
        layer_standard=str(standards.get("layer_standard", "AIA")),
        dws_file=dws_file,
        sheetset_path=sheetset_path,
        support_paths=support_paths,
        drawings=drawings,
    )
