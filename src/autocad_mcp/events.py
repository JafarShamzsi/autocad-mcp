"""Event tail reader for the AutoCAD reactor event log.

The companion `lisp-code/mcp_reactors.lsp` appends one JSON record per
line to `<ipc-dir>/autocad_mcp_events.log` whenever the user adds /
modifies / erases entities or runs commands. This module tails that file
from Python so the `events` MCP tool can surface recent activity to
Claude — the foundation of "Claude watches my draw."

Two reads of the same `EventTail` instance return only *new* records
between calls (tracked via byte offset). If the file is truncated
(e.g., the user calls `events(operation="clear")`) the tail resets to
the start of the new file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# Default location — matches `*mcp-events-path*` in the LISP reactor file.
def default_event_log_path() -> Path:
    """Return `<AUTOCAD_MCP_IPC_DIR>/autocad_mcp_events.log`.

    Resolves the IPC dir the same way `config.py` does so both ends of the
    file IPC use a consistent default (`<tempdir>/autocad-mcp`).
    """
    from autocad_mcp.config import default_ipc_dir

    env_val = os.environ.get("AUTOCAD_MCP_IPC_DIR")
    ipc_dir = Path(env_val) if env_val else default_ipc_dir()
    return ipc_dir / "autocad_mcp_events.log"


@dataclass
class EventTail:
    """File-based tail reader for the AutoCAD reactor event log.

    Each call to `read_new()` returns only the JSON records appended since
    the previous call. Survives the file being truncated (resets to 0 if
    the current offset exceeds the new file size).
    """

    path: Path = field(default_factory=default_event_log_path)
    offset: int = 0

    def read_new(self) -> list[dict]:
        """Return every record appended since the previous call. Empty list
        when no new events (or the log doesn't exist yet).

        Malformed lines (parse errors) are skipped silently — the LISP side
        sometimes writes mid-buffer when AutoCAD is in a tight reactor loop,
        and a single corrupt line shouldn't poison the whole tail."""
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            # File was truncated (clear / log rotation). Restart from the top.
            self.offset = 0
        if size == self.offset:
            return []

        with self.path.open("r", encoding="utf-8", errors="replace") as fp:
            fp.seek(self.offset)
            chunk = fp.read()
            self.offset = fp.tell()

        events: list[dict] = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    # Average bytes per event record — used as a seek heuristic by
    # `read_recent`. Real-world records (line-delimited JSON with handle
    # strings + a timestamp) cluster around 150-250 bytes; 200 gives a
    # comfortable margin so the seek-and-parse usually finds enough records
    # to satisfy `limit` on the first try.
    _RECENT_BYTES_PER_RECORD: int = 200

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Return up to `limit` of the most recent records — irrespective of
        the offset cursor. Useful for "show me the last N events" UX.

        Reads from the tail of the file using a seek heuristic
        (`limit * _RECENT_BYTES_PER_RECORD * 2`) so the cost stays bounded
        when the log has grown to many MB. Falls back to a full read if the
        first window yields fewer than `limit` records.

        Does not advance the offset; subsequent `read_new()` calls still
        return only the truly-new records.
        """
        if not self.path.exists():
            return []
        limit = max(1, min(int(limit), 1000))
        size = self.path.stat().st_size
        # 2× the average gives headroom for variable record sizes.
        window = limit * self._RECENT_BYTES_PER_RECORD * 2

        events: list[dict] = []
        with self.path.open("rb") as fp:
            if size > window:
                # Seek back from the end and discard the first (possibly
                # partial) line — JSON-Lines records are self-delimiting,
                # so dropping a half-line is safe.
                fp.seek(size - window)
                fp.readline()
            for raw in fp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # If the heuristic window didn't capture enough records (very small
        # average size or many blank lines), fall back to a full read.
        if len(events) < limit and size > window:
            events = []
            with self.path.open("r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return events[-limit:]

    def clear(self) -> None:
        """Truncate the event log and reset the tail offset.

        The LISP side opens the log append-mode per write, so it tolerates
        the file disappearing — no need to coordinate with AutoCAD here.
        """
        if self.path.exists():
            self.path.unlink()
        self.offset = 0
