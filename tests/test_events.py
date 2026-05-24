"""Tests for the event-tail reader + `events` MCP tool dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocad_mcp.events import EventTail, default_event_log_path


# ---------------------------------------------------------------------------
# EventTail
# ---------------------------------------------------------------------------


class TestEventTail:
    def test_missing_log_returns_empty(self, tmp_path):
        tail = EventTail(path=tmp_path / "does-not-exist.log")
        assert tail.read_new() == []
        assert tail.read_recent(50) == []

    def test_read_new_returns_appended_records(self, tmp_path):
        log = tmp_path / "events.log"
        log.write_text(
            '{"kind":"object_appended","handle":"AA"}\n'
            '{"kind":"object_modified","handle":"BB"}\n'
        )
        tail = EventTail(path=log)
        first = tail.read_new()
        assert len(first) == 2
        assert first[0]["handle"] == "AA"
        assert first[1]["handle"] == "BB"

        # Second call returns nothing — offset has been advanced.
        assert tail.read_new() == []

        # Append one more — only the new line should come back.
        with log.open("a", encoding="utf-8") as fp:
            fp.write('{"kind":"object_erased","handle":"CC"}\n')
        new = tail.read_new()
        assert len(new) == 1
        assert new[0]["handle"] == "CC"

    def test_read_new_skips_malformed_lines(self, tmp_path):
        log = tmp_path / "events.log"
        log.write_text(
            '{"kind":"ok","handle":"A"}\n'
            'this-is-not-json\n'
            '{"kind":"also_ok","handle":"B"}\n'
        )
        tail = EventTail(path=log)
        events = tail.read_new()
        # Two valid records; the malformed line is silently dropped.
        assert len(events) == 2
        handles = [e["handle"] for e in events]
        assert handles == ["A", "B"]

    def test_truncation_resets_offset(self, tmp_path):
        log = tmp_path / "events.log"
        log.write_text(
            '{"kind":"first","handle":"A"}\n'
            '{"kind":"second","handle":"B"}\n'
        )
        tail = EventTail(path=log)
        tail.read_new()  # advance offset past both records

        # Truncate + rewrite (simulates `events.clear()` + new activity)
        log.write_text('{"kind":"after_truncate","handle":"X"}\n')

        events = tail.read_new()
        assert len(events) == 1
        assert events[0]["handle"] == "X"

    def test_read_recent_returns_last_n_without_advancing(self, tmp_path):
        log = tmp_path / "events.log"
        lines = [
            json.dumps({"kind": f"e{i}", "handle": f"H{i}"})
            for i in range(10)
        ]
        log.write_text("\n".join(lines) + "\n")

        tail = EventTail(path=log)
        recent = tail.read_recent(limit=3)
        assert len(recent) == 3
        assert [e["handle"] for e in recent] == ["H7", "H8", "H9"]

        # read_new still returns everything from the start of the file (offset
        # cursor is independent of read_recent).
        all_events = tail.read_new()
        assert len(all_events) == 10

    def test_clear_truncates_and_resets(self, tmp_path):
        log = tmp_path / "events.log"
        log.write_text('{"kind":"x","handle":"A"}\n')
        tail = EventTail(path=log)
        tail.read_new()  # set offset > 0

        tail.clear()
        assert not log.exists()
        assert tail.offset == 0

    def test_limit_clamped(self, tmp_path):
        log = tmp_path / "events.log"
        log.write_text('{"kind":"x"}\n' * 5)
        tail = EventTail(path=log)
        # limit=0 should clamp to 1; ridiculous limits clamp to 1000.
        assert len(tail.read_recent(limit=0)) <= 5
        assert len(tail.read_recent(limit=99999)) <= 5


class TestDefaultPath:
    def test_default_log_path_respects_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOCAD_MCP_IPC_DIR", str(tmp_path))
        p = default_event_log_path()
        assert p == Path(tmp_path) / "autocad_mcp_events.log"


class TestLispReactorFile:
    """Catch typos in `mcp_reactors.lsp` at unit-test time. We can't actually
    *run* VLR reactors without AutoCAD, but we can statically confirm the
    file references the expected reactor keywords and callback names —
    that catches the most likely class of silent failure (mistyped
    `:vlr-objectappended` becomes a never-fired reactor with no error)."""

    @pytest.fixture
    def lisp_path(self):
        return (
            Path(__file__).resolve().parent.parent
            / "lisp-code" / "mcp_reactors.lsp"
        )

    def test_file_exists(self, lisp_path):
        assert lisp_path.exists()

    def test_declares_attach_and_detach_commands(self, lisp_path):
        text = lisp_path.read_text(encoding="utf-8")
        assert "(defun c:mcp-attach-reactors" in text
        assert "(defun c:mcp-detach-reactors" in text

    def test_subscribes_to_expected_reactor_keywords(self, lisp_path):
        """If any of these keywords is mistyped, the reactor never fires and
        no error appears at load time — making it the silent-failure mode
        most worth catching."""
        text = lisp_path.read_text(encoding="utf-8")
        for keyword in (
            ":vlr-objectappended",
            ":vlr-objectmodified",
            ":vlr-objecterased",
            ":vlr-commandwillstart",
            ":vlr-commandended",
        ):
            assert keyword in text, f"Reactor keyword missing: {keyword}"

    def test_uses_local_epoch_for_timestamps(self, lisp_path):
        """We want consumers (Python tail reader, downstream) to get
        seconds-since-1970, not AutoCAD's CDATE / DATE raw format.
        `mcp-local-epoch` is now CDATE-based — AutoCAD 2027 returns full
        Julian Day Number from `(getvar "DATE")` (~2,461,000 today), not
        the Excel-epoch days the older formula assumed; CDATE
        (YYYYMMDD.HHMMSS) is constant across versions and that's what
        we parse now."""
        text = lisp_path.read_text(encoding="utf-8")
        assert "mcp-local-epoch" in text
        assert "(mcp-local-epoch)" in text
        # Implementation must use CDATE (stable across versions), not DATE
        # (changes representation between MJD / Excel-epoch / full JDN).
        assert '(getvar "CDATE")' in text

    def test_compensates_for_acdb_reactor_deprecation(self, lisp_path):
        """AutoCAD 2024+ deprecated the legacy vlr-acdb-reactor events for
        command-driven entity creation. The reactor file now compensates
        by scanning modelspace at command_ended and emitting object_appended
        for newly-appended handles. This test catches removal of that
        safety net."""
        text = lisp_path.read_text(encoding="utf-8")
        assert "mcp-scan-new-entities" in text, (
            "AcDb-reactor-deprecation compensator missing — entities created "
            "via commands on AutoCAD 2024+ will not emit object_appended."
        )
        # The scan must be invoked from the command-ended handler.
        assert "(mcp-scan-new-entities)" in text
        # And the high-water-mark must be snapshotted at attach to avoid
        # re-emitting every existing entity on the first command.
        assert "*mcp-last-handle-int*" in text

    def test_event_log_path_uses_ipc_dir(self, lisp_path):
        """The event log must live under *mcp-ipc-dir* so users who set
        AUTOCAD_MCP_IPC_DIR have it honored on both sides."""
        text = lisp_path.read_text(encoding="utf-8")
        assert "*mcp-ipc-dir*" in text
        assert "autocad_mcp_events.log" in text

    def test_json_escape_handles_control_chars(self, lisp_path):
        """Regression for N7: the JSON escape function originally only
        escaped \" and \\, so a layer name containing \\n produced invalid
        JSON and the Python tail silently dropped the record. The fix added
        \\n \\r \\t \\b \\f and \\u00XX for codes < 0x20."""
        text = lisp_path.read_text(encoding="utf-8")
        # The escape function must reference the newline + carriage-return
        # escape sequences. Both must appear as `\\n` (i.e., the two chars
        # backslash-n) somewhere in the file.
        for pattern in ("\\\\n", "\\\\r", "\\\\t"):
            assert pattern in text, (
                f"Expected JSON escape sequence {pattern!r} in mcp_reactors.lsp"
            )
        # And the catch-all hex path for unprintable codes.
        assert "\\\\u00" in text


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestEventsToolDispatch:
    """Drive the `events` tool function directly; reset the module-level tail
    via monkeypatch so each test gets a clean slate."""

    def _install_tail(self, monkeypatch, tmp_path) -> EventTail:
        from autocad_mcp import server as server_mod

        tail = EventTail(path=tmp_path / "events.log")
        monkeypatch.setattr(server_mod, "_event_tail", tail)
        return tail

    async def test_recent(self, monkeypatch, tmp_path):
        tail = self._install_tail(monkeypatch, tmp_path)
        tail.path.write_text(
            '{"kind":"a","handle":"X"}\n'
            '{"kind":"b","handle":"Y"}\n'
        )
        from autocad_mcp.server import events as events_tool

        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool
        result = await fn(operation="recent")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["count"] == 2

    async def test_new_advances_cursor(self, monkeypatch, tmp_path):
        tail = self._install_tail(monkeypatch, tmp_path)
        tail.path.write_text('{"kind":"first"}\n')

        from autocad_mcp.server import events as events_tool

        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool

        r1 = await fn(operation="new")
        p1 = json.loads(r1) if isinstance(r1, str) else json.loads(r1[0].text)
        assert p1["payload"]["count"] == 1

        r2 = await fn(operation="new")
        p2 = json.loads(r2) if isinstance(r2, str) else json.loads(r2[0].text)
        # No new events since last call.
        assert p2["payload"]["count"] == 0

    async def test_clear(self, monkeypatch, tmp_path):
        tail = self._install_tail(monkeypatch, tmp_path)
        tail.path.write_text('{"kind":"x"}\n')

        from autocad_mcp.server import events as events_tool

        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool
        result = await fn(operation="clear")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert not tail.path.exists()

    async def test_start_recording_sends_lisp(self, monkeypatch, tmp_path):
        """start_recording with `prefer='lisp'` must drive the backend's
        execute_lisp with a load + attach call. We mock execute_lisp and
        inspect what was sent.

        (The auto-mode default may prefer the .NET reactor; this test
        pins the LISP path so the assertion is precise.)"""
        self._install_tail(monkeypatch, tmp_path)

        from autocad_mcp.backends.base import CommandResult
        from autocad_mcp import client as client_mod

        sent: list[str] = []

        class _FakeBackend:
            name = "file_ipc"  # LT path — auto-mode picks LISP regardless.

            async def execute_lisp(self, code):
                sent.append(code)
                return CommandResult(ok=True, payload={"sent": True})

        # Patch get_backend to return our fake.
        async def _fake_get_backend():
            return _FakeBackend()

        monkeypatch.setattr(client_mod, "get_backend", _fake_get_backend)
        # The server module imported get_backend at module load time, so patch
        # it there too.
        from autocad_mcp import server as server_mod
        monkeypatch.setattr(server_mod, "get_backend", _fake_get_backend)

        from autocad_mcp.server import events as events_tool

        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool
        result = await fn(
            operation="start_recording", data={"prefer": "lisp"},
        )
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        assert parsed["payload"]["reactor_impl"] == "lisp"
        # The LISP that was dispatched must reference the reactor file +
        # attach function.
        assert len(sent) == 1
        assert "mcp_reactors.lsp" in sent[0]
        assert "mcp-attach-reactors" in sent[0]

    async def test_start_recording_prefers_dotnet_on_com_when_dll_exists(
        self, monkeypatch, tmp_path,
    ):
        """G5 — auto-mode picks the .NET reactor when the backend is COM
        and `bin/dotnet/MCPReactor.dll` exists. The dispatched code must
        NETLOAD the DLL and invoke MCPATTACHREACTOR."""
        self._install_tail(monkeypatch, tmp_path)

        from autocad_mcp.backends.base import CommandResult
        from autocad_mcp import client as client_mod
        from autocad_mcp import server as server_mod

        # Pretend the DLL is there.
        fake_dll = tmp_path / "MCPReactor.dll"
        fake_dll.write_bytes(b"FAKE")
        monkeypatch.setattr(
            server_mod, "_dotnet_reactor_path", lambda: fake_dll,
        )

        sent: list[str] = []

        class _FakeBackend:
            name = "com"

            async def execute_lisp(self, code):
                sent.append(code)
                return CommandResult(ok=True, payload={"sent": True})

        async def _fake_get_backend():
            return _FakeBackend()

        monkeypatch.setattr(client_mod, "get_backend", _fake_get_backend)
        monkeypatch.setattr(server_mod, "get_backend", _fake_get_backend)

        from autocad_mcp.server import events as events_tool
        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool

        result = await fn(operation="start_recording")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True, parsed
        assert parsed["payload"]["reactor_impl"] == "dotnet"
        assert len(sent) == 1
        assert "_NETLOAD" in sent[0]
        assert "MCPATTACHREACTOR" in sent[0]
        assert "MCPReactor.dll" in sent[0]

    async def test_start_recording_dotnet_falls_back_when_dll_missing(
        self, monkeypatch, tmp_path,
    ):
        """Auto-mode on COM should fall back to LISP if the .NET DLL is
        absent — the user shouldn't have to know about the .NET path."""
        self._install_tail(monkeypatch, tmp_path)

        from autocad_mcp.backends.base import CommandResult
        from autocad_mcp import client as client_mod
        from autocad_mcp import server as server_mod

        monkeypatch.setattr(server_mod, "_dotnet_reactor_path", lambda: None)

        sent: list[str] = []

        class _FakeBackend:
            name = "com"

            async def execute_lisp(self, code):
                sent.append(code)
                return CommandResult(ok=True, payload={"sent": True})

        async def _fake_get_backend():
            return _FakeBackend()

        monkeypatch.setattr(client_mod, "get_backend", _fake_get_backend)
        monkeypatch.setattr(server_mod, "get_backend", _fake_get_backend)

        from autocad_mcp.server import events as events_tool
        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool

        result = await fn(operation="start_recording")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert parsed["ok"] is True
        # No DLL → must use LISP.
        assert parsed["payload"]["reactor_impl"] == "lisp"
        assert "mcp_reactors.lsp" in sent[0]

    async def test_unknown_operation(self, monkeypatch, tmp_path):
        self._install_tail(monkeypatch, tmp_path)
        from autocad_mcp.server import events as events_tool

        fn = events_tool.fn if hasattr(events_tool, "fn") else events_tool
        result = await fn(operation="bogus")
        parsed = json.loads(result) if isinstance(result, str) else json.loads(result[0].text)
        assert "error" in parsed
