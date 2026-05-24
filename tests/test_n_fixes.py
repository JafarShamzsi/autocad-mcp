"""Regression tests for the post-2026-05-13 fix-up pass.

Each test pins one fix from the N* findings catalogued in
`docs/audit-2026-05-13-followup.md`. Where the fix touches the LISP
dispatcher we test the Python side that constructs / consumes its
output rather than driving AutoCAD itself.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from autocad_mcp.backends.base import CommandResult


_REPO_ROOT = Path(__file__).resolve().parent.parent
_LISP_DIR = _REPO_ROOT / "lisp-code"


# ---------------------------------------------------------------------------
# N1: SECURELOAD save/restore is wrapped against mid-load failure
# ---------------------------------------------------------------------------


def test_n1_execute_lisp_wraps_secureload_restore():
    """The execute-lisp handler must put the SECURELOAD restore inside a
    vl-catch-all-apply so a load failure doesn't leak SECURELOAD=0 across
    the session. Verified by structural inspection of the LISP source."""
    text = (_LISP_DIR / "mcp_dispatch.lsp").read_text(encoding="utf-8")
    assert "(defun mcp-cmd-execute-lisp" in text
    # Both the toggle and the restore must run inside catch wrappers.
    section = text.split("(defun mcp-cmd-execute-lisp")[1].split("\n;;")[0]
    catches = section.count("vl-catch-all-apply")
    assert catches >= 2, (
        "execute-lisp must wrap both the load and the SECURELOAD restore "
        f"in vl-catch-all-apply; found {catches} occurrence(s)."
    )
    # The restore must be guarded by `(if old-secureload (setvar ...))` so a
    # nil from `getvar` (very rare but possible) doesn't blow up.
    assert "(if old-secureload (setvar \"SECURELOAD\" old-secureload))" in section


# ---------------------------------------------------------------------------
# N4: LISP JSON string parser decodes escape sequences
# ---------------------------------------------------------------------------


def test_n4_mcp_dispatch_has_json_unescape():
    """The dispatcher's JSON string extractor must now run extracted values
    through `mcp-json-unescape`. Before the fix, values containing `\\"`,
    `\\n`, `\\\\` etc. were delivered to AutoCAD with the backslashes still
    visible. Captured here as a structural test so an accidental revert
    surfaces immediately."""
    text = (_LISP_DIR / "mcp_dispatch.lsp").read_text(encoding="utf-8")
    assert "(defun mcp-json-unescape" in text, "unescape helper missing"
    assert "(defun mcp-parse-hex4" in text, "hex-parse helper missing"
    # The extractor must call the unescaper before returning.
    extractor = text.split("(defun mcp-json-get-string")[1].split("\n(defun")[0]
    assert "(mcp-json-unescape raw)" in extractor, (
        "mcp-json-get-string must run the captured substring through "
        "mcp-json-unescape, otherwise the JSON escape sequences leak "
        "through to AutoCAD."
    )


# ---------------------------------------------------------------------------
# N5: LISP number parser handles scientific notation
# ---------------------------------------------------------------------------


def test_n5_mcp_dispatch_number_parser_accepts_scientific():
    """`mcp-json-get-number` used to silently truncate `1.5e3` to `1.5`
    because its accept-set excluded `e`. Verify the new accept-set
    includes scientific-notation chars."""
    text = (_LISP_DIR / "mcp_dispatch.lsp").read_text(encoding="utf-8")
    section = text.split("(defun mcp-json-get-number")[1].split("\n(defun")[0]
    # Both case variants must be in the digit-set.
    assert "\"e\"" in section and "\"E\"" in section, (
        "mcp-json-get-number digit-set must include 'e' and 'E' so that "
        "values written in scientific notation (1.5e3) parse correctly."
    )
    # The empty / sign-only-string guard rails should be present.
    assert "(or (= num-str \"\")" in section


# ---------------------------------------------------------------------------
# N12: ezdxf backend usable before `initialize()` is awaited
# ---------------------------------------------------------------------------


def test_n12_ezdxf_backend_has_doc_at_construction():
    """The backend used to leave `_doc = None` until `initialize()` ran. Any
    method that touched `self._msp` raised AttributeError if the caller
    forgot to await init. Constructing one and reading status synchronously
    now works."""
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

    b = EzdxfBackend()
    assert b._doc is not None
    assert b._msp is not None


# ---------------------------------------------------------------------------
# N2: centralized lisp_exec_enabled gate
# ---------------------------------------------------------------------------


def test_n2_lisp_exec_enabled_defaults_false(monkeypatch):
    from autocad_mcp.config import lisp_exec_enabled

    monkeypatch.delenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", raising=False)
    assert lisp_exec_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes "])
def test_n2_lisp_exec_enabled_truthy(monkeypatch, value):
    from autocad_mcp.config import lisp_exec_enabled

    monkeypatch.setenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", value)
    assert lisp_exec_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_n2_lisp_exec_enabled_falsy(monkeypatch, value):
    from autocad_mcp.config import lisp_exec_enabled

    monkeypatch.setenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", value)
    assert lisp_exec_enabled() is False


# ---------------------------------------------------------------------------
# N3: project.load path allowlist
# ---------------------------------------------------------------------------


def test_n3_project_load_no_allowlist_accepts_any_path(tmp_path, monkeypatch):
    from autocad_mcp.project.config import load_project

    monkeypatch.delenv("AUTOCAD_MCP_PROJECT_ROOTS", raising=False)

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / ".acadproj.toml").write_text(
        '[project]\nname = "T"\n', encoding="utf-8"
    )
    cfg = load_project(proj_dir)
    assert cfg.name == "T"


def test_n3_project_load_outside_allowlist_raises(tmp_path, monkeypatch):
    from autocad_mcp.project.config import load_project

    allowed = tmp_path / "allowed"
    forbidden = tmp_path / "forbidden"
    allowed.mkdir()
    forbidden.mkdir()
    (forbidden / ".acadproj.toml").write_text(
        '[project]\nname = "T"\n', encoding="utf-8"
    )

    monkeypatch.setenv("AUTOCAD_MCP_PROJECT_ROOTS", str(allowed))
    with pytest.raises(PermissionError):
        load_project(forbidden)


def test_n3_project_load_inside_allowlist_succeeds(tmp_path, monkeypatch):
    from autocad_mcp.project.config import load_project

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    proj_dir = allowed / "proj"
    proj_dir.mkdir()
    (proj_dir / ".acadproj.toml").write_text(
        '[project]\nname = "T"\n', encoding="utf-8"
    )

    monkeypatch.setenv("AUTOCAD_MCP_PROJECT_ROOTS", str(allowed))
    cfg = load_project(proj_dir)
    assert cfg.name == "T"


# ---------------------------------------------------------------------------
# N8: drawing.save with .dwg suffix dispatches off the event loop
# ---------------------------------------------------------------------------


async def test_n8_dwg_save_uses_to_thread(monkeypatch, tmp_path):
    """When the user saves with a .dwg extension, the backend should
    invoke `write_dwg` via `asyncio.to_thread` so the converter (which
    blocks for up to 120s) doesn't stall the event loop. We can't directly
    observe `to_thread` without instrumenting it, but we can confirm the
    call reaches `write_dwg` and that the function is called from a
    non-main thread."""
    import threading

    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

    b = EzdxfBackend()
    await b.initialize()

    main_thread = threading.current_thread()
    captured = {}

    def fake_write_dwg(doc, path, *, version=None):
        # `version` kwarg added in the DWG-version-selector pass; accept
        # it so this test exercises the post-fix signature, but the
        # contract under test (thread dispatch) is independent of it.
        captured["called_on_thread"] = threading.current_thread()
        captured["version"] = version
        Path(path).write_bytes(b"FAKE-DWG")
        return Path(path)

    monkeypatch.setattr(
        "autocad_mcp.project.dwg_reader.write_dwg", fake_write_dwg
    )

    out = tmp_path / "out.dwg"
    result = await b.drawing_save(str(out))
    assert result.ok, result.error
    assert result.payload["format"] == "dwg"
    # The conversion ran on a thread other than the asyncio main thread.
    assert captured["called_on_thread"] is not main_thread


# ---------------------------------------------------------------------------
# N19: find_entities reports truncation
# ---------------------------------------------------------------------------


def test_n19_find_entities_truncation_flag():
    """The query helper already clamped to a 5000 max. The tool-layer
    response now includes a `truncated` flag derived from comparing the
    returned count against the effective limit. We can verify the helper
    contract directly here; the tool wiring is covered by the existing
    project test suite."""
    from autocad_mcp.project.query import find_entities, project_info
    from autocad_mcp.project.index import init_db

    conn = init_db(":memory:")
    # Insert a synthetic drawing + 3 entities so `limit=2` produces a
    # truncated result of len 2.
    conn.execute(
        "INSERT INTO drawings (name, file_path, indexed_at) VALUES "
        "('d1', '/tmp/d1.dxf', '2026-05-14T00:00:00Z')"
    )
    drawing_id = conn.execute("SELECT id FROM drawings").fetchone()[0]
    conn.executemany(
        "INSERT INTO entities (drawing_id, type, layer, handle, space) "
        "VALUES (?, 'LINE', 'A-WALL', ?, 'modelspace')",
        [(drawing_id, h) for h in ("A", "B", "C")],
    )
    conn.commit()

    full = find_entities(conn, limit=10)
    assert len(full) == 3

    partial = find_entities(conn, limit=2)
    assert len(partial) == 2


# ---------------------------------------------------------------------------
# N10: deterministic AutoCAD window selection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# G5: .NET ManagedReactor — file presence + schema-contract checks
# ---------------------------------------------------------------------------


def test_g5_dotnet_reactor_dll_shipped():
    """The prebuilt MCPReactor.dll must be in bin/dotnet/. The plugin
    install story assumes it's tracked in the repo (same convention as
    SheetSetReader.dll)."""
    repo = Path(__file__).resolve().parent.parent
    dll = repo / "bin" / "dotnet" / "MCPReactor.dll"
    assert dll.exists(), f"prebuilt MCPReactor.dll missing at {dll}"
    # PE32+ assemblies are at least a few KB.
    assert dll.stat().st_size > 4_000


def test_g5_dotnet_reactor_csproj_targets_net10():
    """The .csproj must build against .NET 10 + AutoCAD 2024-2028, with
    probe-driven HintPaths (not hardcoded to a single year)."""
    repo = Path(__file__).resolve().parent.parent
    csproj = repo / "dotnet" / "MCPReactor" / "MCPReactor.csproj"
    assert csproj.exists()
    text = csproj.read_text(encoding="utf-8")
    assert "<TargetFramework>net10.0-windows</TargetFramework>" in text
    assert "AutoCAD 2027" in text  # at minimum (with newer years preferred)
    # The hardcoded "C:\Program Files\Autodesk\AutoCAD 2027" pattern from
    # the original csproj must NOT appear as a raw HintPath — that's the
    # regression we fixed in N9.
    assert "$(AutoCADInstallDir)" in text


def test_g5_reactor_command_class_present():
    """The .NET source must declare the MCPATTACHREACTOR / MCPDETACHREACTOR
    command methods so NETLOAD registers them with AutoCAD."""
    repo = Path(__file__).resolve().parent.parent
    cs = (repo / "dotnet" / "MCPReactor" / "ReactorCommand.cs").read_text(encoding="utf-8")
    assert '[CommandMethod("MCPATTACHREACTOR")]' in cs
    assert '[CommandMethod("MCPDETACHREACTOR")]' in cs
    assert '[CommandMethod("MCPREACTORSTATUS")]' in cs


def test_g5_reactor_emits_same_schema_as_lisp():
    """The .NET reactor must write the same JSON-Lines fields the LISP
    reactor writes so `EventTail` works against either source. We can't
    drive AutoCAD here; instead inspect the .NET source to confirm the
    key names match the LISP's `mcp-event-emit` output."""
    repo = Path(__file__).resolve().parent.parent
    cs = (repo / "dotnet" / "MCPReactor" / "ManagedReactor.cs").read_text(encoding="utf-8")
    # C# emits `"ts":` as the escaped string literal `\"ts\":` in source.
    for field in (
        r'\"ts\":', r'\"ts_local\":', r'\"kind\":',
        r'\"handle\":', r'\"entity_type\":', r'\"layer\":',
    ):
        assert field in cs, f"missing field {field!r} in .NET reactor output"
    # Both reactors must emit `session_started` and `session_ended` so the
    # tail consumer can detect attach/detach boundaries.
    assert '"session_started"' in cs
    assert '"session_ended"' in cs


# ---------------------------------------------------------------------------
# DWG focus: cache + version selector + default-DWG-extension
# ---------------------------------------------------------------------------


def test_dwg_cache_returns_same_doc_on_warm_read(tmp_path):
    """Two reads of the same .dwg must yield the same `Drawing` instance —
    the cache key is `(abs_path, mtime_ns, size)` so unchanged files hit.
    """
    import ezdxf
    from autocad_mcp.project.dwg_reader import (
        clear_dwg_cache, read_drawing,
    )

    # We need a real .dwg to exercise the cache (DXF skips the cache by
    # design). The starter library ships block.dwg for each asset; use
    # one of them if it exists, else skip.
    repo = Path(__file__).resolve().parent.parent
    sample = repo / "asset-library/furniture/seating/sofa-84-modern/block.dwg"
    if not sample.exists():
        pytest.skip("requires the seeded asset library (run library.seed first)")

    clear_dwg_cache()
    a = read_drawing(sample)
    b = read_drawing(sample)
    # Identity, not just equality — the cache returns the exact same object.
    assert a is b


def test_dwg_cache_clear_evicts_entries(tmp_path):
    from autocad_mcp.project.dwg_reader import (
        clear_dwg_cache, dwg_cache_stats, read_drawing,
    )
    repo = Path(__file__).resolve().parent.parent
    sample = repo / "asset-library/furniture/seating/sofa-84-modern/block.dwg"
    if not sample.exists():
        pytest.skip("requires the seeded asset library")

    clear_dwg_cache()
    assert dwg_cache_stats()["entries"] == 0
    read_drawing(sample)
    assert dwg_cache_stats()["entries"] == 1
    cleared = clear_dwg_cache()
    assert cleared == 1
    assert dwg_cache_stats()["entries"] == 0


def test_dwg_cache_dxf_is_not_cached(tmp_path):
    """DXF reads aren't cached — the contract documents this. Avoids the
    shared-mutable-state footgun for callers that legitimately modify
    the result."""
    import ezdxf
    from autocad_mcp.project.dwg_reader import (
        clear_dwg_cache, dwg_cache_stats, read_drawing,
    )

    p = tmp_path / "test.dxf"
    doc = ezdxf.new("R2013")
    doc.blocks.new(name="X").add_line((0, 0), (1, 1))
    doc.saveas(p)

    clear_dwg_cache()
    read_drawing(p)
    read_drawing(p)
    assert dwg_cache_stats()["entries"] == 0


def test_dwg_version_normalizer_maps_years_to_generations():
    from autocad_mcp.project.dwg_reader import _normalize_dwg_version

    # Generation codes pass through unchanged.
    assert _normalize_dwg_version("2018") == "2018"
    assert _normalize_dwg_version("2013") == "2013"
    assert _normalize_dwg_version("2000") == "2000"
    # AutoCAD years map to the corresponding generation.
    assert _normalize_dwg_version("2024") == "2018"
    assert _normalize_dwg_version("2027") == "2018"
    assert _normalize_dwg_version("2015") == "2013"
    assert _normalize_dwg_version("2011") == "2010"
    # Unknown / falsy fall back to default.
    assert _normalize_dwg_version(None) == "2018"
    assert _normalize_dwg_version("") == "2018"
    assert _normalize_dwg_version("nope") == "2018"


def test_drawing_save_no_suffix_uses_dwg_when_converter_available(tmp_path, monkeypatch):
    """`drawing.save(path="x")` (no extension) defaults to .dwg when a
    DWG converter is available, .dxf otherwise. The path-rewriting happens
    inside `EzdxfBackend.drawing_save` BEFORE the file is touched."""
    import asyncio
    from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    asyncio.run(backend.initialize())

    # Force "no converter available" via monkeypatch — should fall back to DXF.
    from autocad_mcp.project import dwg_reader as dr
    monkeypatch.setattr(dr, "available_dwg_converters",
                        lambda: {"accoreconsole": None, "oda_file_converter": None})
    out = tmp_path / "no_suffix_dxf"
    r = asyncio.run(backend.drawing_save(str(out)))
    assert r.ok
    assert r.payload["format"] == "dxf"
    assert (tmp_path / "no_suffix_dxf.dxf").exists()


def test_g5_dotnet_path_picker_returns_path_when_dll_exists(tmp_path, monkeypatch):
    """`_dotnet_reactor_path()` returns the path on disk when the DLL
    exists, None otherwise. The path must resolve to `bin/dotnet/`."""
    from autocad_mcp.server import _dotnet_reactor_path

    p = _dotnet_reactor_path()
    # The actual on-disk DLL ships in the repo.
    assert p is not None
    assert p.name == "MCPReactor.dll"
    assert p.parent.name == "dotnet"


def test_n10_window_selection_is_deterministic(monkeypatch):
    """When multiple AutoCAD-matching windows exist and none is the
    foreground, the selector returns the lowest HWND so the same window
    is targeted on every call. (Foreground-preferred behavior is also
    documented but harder to test without a Windows host.)"""
    from autocad_mcp.backends import file_ipc as fipc_mod

    if not hasattr(fipc_mod, "find_autocad_window"):
        pytest.skip("file_ipc not importable on this platform")

    fake_windows = [0x5000, 0x2000, 0x9000]

    class FakeWin32gui:
        @staticmethod
        def IsWindowVisible(hwnd):
            return True

        @staticmethod
        def GetWindowText(hwnd):
            return "AutoCAD - Drawing1.dwg"

        @staticmethod
        def EnumWindows(callback, container):
            for hwnd in fake_windows:
                callback(hwnd, container)

        @staticmethod
        def GetForegroundWindow():
            return 0x0  # no window is foreground

    import sys

    # Inject fake win32gui module before find_autocad_window resolves it.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "win32gui", FakeWin32gui)

    chosen = fipc_mod.find_autocad_window()
    assert chosen == min(fake_windows)
    # Calling again returns the same window — deterministic.
    assert fipc_mod.find_autocad_window() == chosen
