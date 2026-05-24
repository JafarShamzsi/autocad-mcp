"""Tests for Cluster B — multi-drawing batch operations.

Covers:
  * `batch.run_batch_script` — the underlying helper. Validates the
    .scr / .lsp construction + the per-file result shape WITHOUT
    actually invoking accoreconsole (we mock the subprocess call so
    these tests run in <1s instead of N × 3s).
  * `drawing(operation="batch")` MCP op — env-var gate + payload validation.

For an actual accoreconsole round-trip, see `scripts/smoke_batch.py`
(documented in the README; needs a working AutoCAD install).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _force_ezdxf(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    from autocad_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_backend", None)


# ---------------------------------------------------------------------------
# Unit tests for batch.py helpers
# ---------------------------------------------------------------------------


def test_build_scr_includes_qsave_when_save_true(tmp_path):
    from autocad_mcp.batch import _build_scr

    lisp = tmp_path / "x.lsp"
    scr = _build_scr(lisp, save=True)
    assert "_FILEDIA" in scr
    assert "(load " in scr
    assert "_QSAVE" in scr
    assert scr.endswith("\n")  # trailing newline matters for accoreconsole


def test_build_scr_omits_qsave_when_save_false(tmp_path):
    from autocad_mcp.batch import _build_scr

    lisp = tmp_path / "x.lsp"
    scr = _build_scr(lisp, save=False)
    assert "_QSAVE" not in scr
    assert "_QUIT" in scr


def test_write_lisp_file_persists_content(tmp_path):
    from autocad_mcp.batch import _write_lisp_file

    code = '(command "_LAYER" "_M" "X" "")'
    f = _write_lisp_file(code, tmp_path)
    assert f.exists()
    assert f.suffix == ".lsp"
    assert f.read_text(encoding="utf-8") == code


def test_decode_acc_stdout_handles_utf16le():
    from autocad_mcp.batch import _decode_acc_stdout

    # Simulate what accoreconsole actually emits on Windows.
    sample = "Command: _QSAVE\nQUIT\n".encode("utf-16-le")
    decoded = _decode_acc_stdout(sample)
    assert "Command:" in decoded
    assert "_QSAVE" in decoded


# ---------------------------------------------------------------------------
# Integration of run_batch_script with subprocess mocking
# ---------------------------------------------------------------------------


def test_run_batch_script_calls_accoreconsole_per_file(tmp_path, monkeypatch):
    """Mock accoreconsole + verify we hit it once per file with the right
    arguments. Confirms the orchestration without paying real shell-out cost."""
    from autocad_mcp import batch as batch_mod

    # Fake accoreconsole location.
    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)

    # Two real files (so the existence check passes).
    f1 = tmp_path / "a.dwg"; f1.touch()
    f2 = tmp_path / "b.dwg"; f2.touch()

    calls: list[list[str]] = []

    class FakeCompleted:
        returncode = 0
        stdout = "Command: ok".encode("utf-16-le")
        stderr = b""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = batch_mod.run_batch_script(
        [f1, f2], '(command "_LAYER" "_M" "X" "")',
        save=True, ipc_dir=tmp_path,
    )
    assert result.files_total == 2
    assert result.files_ok == 2
    assert result.files_failed == 0
    assert len(calls) == 2
    # Each call has /i <path> /s <scr>.
    for args, expected in zip(calls, [f1, f2]):
        assert str(fake_acc) in args
        assert "/i" in args
        assert str(expected) in args
        assert "/s" in args


def test_run_batch_script_records_per_file_failure(tmp_path, monkeypatch):
    from autocad_mcp import batch as batch_mod

    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)

    f1 = tmp_path / "good.dwg"; f1.touch()
    f2 = tmp_path / "bad.dwg"; f2.touch()

    class FakeCompletedOK:
        returncode = 0
        stdout = b""
        stderr = b""

    class FakeCompletedBad:
        returncode = 1
        stdout = "LISP error".encode("utf-16-le")
        stderr = b"err"

    # First call OK, second fails.
    responses = iter([FakeCompletedOK(), FakeCompletedBad()])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: next(responses))

    result = batch_mod.run_batch_script(
        [f1, f2], "(princ)", ipc_dir=tmp_path,
    )
    assert result.files_ok == 1
    assert result.files_failed == 1
    assert result.per_file[0].ok is True
    assert result.per_file[1].ok is False
    assert result.per_file[1].returncode == 1
    assert result.per_file[1].error is not None


def test_run_batch_script_handles_timeout(tmp_path, monkeypatch):
    from autocad_mcp import batch as batch_mod

    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)

    f1 = tmp_path / "stuck.dwg"; f1.touch()

    def fake_run_timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 60))

    monkeypatch.setattr(subprocess, "run", fake_run_timeout)

    result = batch_mod.run_batch_script(
        [f1], "(princ)", timeout_per_file=2.0, ipc_dir=tmp_path,
    )
    assert result.files_ok == 0
    assert result.files_failed == 1
    assert "timed out" in result.per_file[0].error


def test_run_batch_script_skips_missing_files(tmp_path, monkeypatch):
    from autocad_mcp import batch as batch_mod

    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)

    missing = tmp_path / "ghost.dwg"  # never created

    # subprocess.run shouldn't get called for missing files.
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: pytest.fail("subprocess.run called for missing file"),
    )
    result = batch_mod.run_batch_script(
        [missing], "(princ)", ipc_dir=tmp_path,
    )
    assert result.files_failed == 1
    assert "not found" in result.per_file[0].error


def test_run_batch_script_no_accoreconsole_raises(monkeypatch, tmp_path):
    from autocad_mcp import batch as batch_mod

    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: None)
    f = tmp_path / "x.dwg"; f.touch()
    with pytest.raises(FileNotFoundError, match="accoreconsole"):
        batch_mod.run_batch_script([f], "(princ)", ipc_dir=tmp_path)


def test_run_batch_script_empty_paths_raises(tmp_path, monkeypatch):
    from autocad_mcp import batch as batch_mod

    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)
    with pytest.raises(ValueError, match="at least one path"):
        batch_mod.run_batch_script([], "(princ)", ipc_dir=tmp_path)


def test_run_batch_script_cleans_up_temp_files(tmp_path, monkeypatch):
    """Temp .scr + .lsp files must be deleted after the batch — they
    shouldn't accumulate in the IPC dir across runs."""
    from autocad_mcp import batch as batch_mod

    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)
    f1 = tmp_path / "a.dwg"; f1.touch()

    class FakeCompleted:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompleted())

    before = set(tmp_path.iterdir())
    batch_mod.run_batch_script([f1], "(princ)", ipc_dir=tmp_path)
    after = set(tmp_path.iterdir())
    # No extra files left behind. (The fake accoreconsole + the .dwg were
    # present before too — set difference confirms cleanup.)
    assert after == before


# ---------------------------------------------------------------------------
# MCP op gating
# ---------------------------------------------------------------------------


async def test_batch_op_gated_off_by_default(tmp_path, monkeypatch):
    """Without `AUTOCAD_MCP_ENABLE_LISP_EXEC=1`, the batch op refuses."""
    _force_ezdxf(monkeypatch)
    monkeypatch.delenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", raising=False)

    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="batch", data={
        "paths": [str(tmp_path / "x.dwg")],
        "script": "(princ)",
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert parsed.get("category") == "not_supported"
    assert "ENABLE_LISP_EXEC" in parsed["error"]


async def test_batch_op_requires_paths(monkeypatch):
    _force_ezdxf(monkeypatch)
    monkeypatch.setenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", "1")
    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="batch", data={"script": "(princ)"})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert "paths" in parsed["error"].lower()


async def test_batch_op_requires_script(monkeypatch, tmp_path):
    _force_ezdxf(monkeypatch)
    monkeypatch.setenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", "1")
    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="batch", data={"paths": [str(tmp_path / "x.dwg")]})
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert "script" in parsed["error"].lower()


async def test_batch_op_returns_per_file_payload(monkeypatch, tmp_path):
    """End-to-end through the MCP op surface, with subprocess mocked."""
    _force_ezdxf(monkeypatch)
    monkeypatch.setenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", "1")

    from autocad_mcp import batch as batch_mod
    fake_acc = tmp_path / "accoreconsole.exe"
    fake_acc.touch()
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: fake_acc)

    f1 = tmp_path / "a.dwg"; f1.touch()
    f2 = tmp_path / "b.dwg"; f2.touch()

    class FakeCompleted:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompleted())

    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="batch", data={
        "paths": [str(f1), str(f2)],
        "script": '(princ "ok")',
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert parsed["ok"] is True
    p = parsed["payload"]
    assert p["files_total"] == 2
    assert p["files_ok"] == 2
    assert p["files_failed"] == 0
    assert len(p["per_file"]) == 2
    assert all(f["ok"] for f in p["per_file"])


async def test_batch_op_no_converter_returns_backend_unavailable(
    monkeypatch, tmp_path,
):
    """When accoreconsole isn't installed, the op returns a clear
    `backend_unavailable` error rather than crashing."""
    _force_ezdxf(monkeypatch)
    monkeypatch.setenv("AUTOCAD_MCP_ENABLE_LISP_EXEC", "1")

    from autocad_mcp import batch as batch_mod
    monkeypatch.setattr(batch_mod, "_accoreconsole_path", lambda: None)

    f1 = tmp_path / "a.dwg"; f1.touch()
    from autocad_mcp.server import drawing as drawing_tool
    fn = drawing_tool.fn if hasattr(drawing_tool, "fn") else drawing_tool

    r = await fn(operation="batch", data={
        "paths": [str(f1)], "script": "(princ)",
    })
    parsed = json.loads(r) if isinstance(r, str) else json.loads(r[0].text)
    assert "error" in parsed
    assert parsed.get("category") == "backend_unavailable"
    assert "accoreconsole" in parsed["error"].lower()
