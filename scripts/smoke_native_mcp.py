"""Smoke-test the native C++ MCP executable over stdio framing."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "cpp" / "build" / "autocad-mcp-native.exe"


def _frame(message: dict) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


def _parse_frames(data: bytes) -> list[dict]:
    # Some Windows text-mode pipes surface CRLF as CRCRLF. Normalize before
    # reading Content-Length frames.
    data = data.replace(b"\r\r\n", b"\r\n")
    responses: list[dict] = []
    offset = 0
    while offset < len(data):
        header_end = data.find(b"\r\n\r\n", offset)
        if header_end < 0:
            raise AssertionError(f"Missing frame delimiter at byte {offset}: {data[offset:offset + 120]!r}")
        match = re.search(br"Content-Length:\s*(\d+)", data[offset:header_end])
        if not match:
            raise AssertionError(f"Missing Content-Length header: {data[offset:header_end]!r}")
        size = int(match.group(1))
        start = header_end + 4
        payload = data[start:start + size]
        if len(payload) != size:
            raise AssertionError(f"Short frame: expected {size}, got {len(payload)}")
        responses.append(json.loads(payload))
        offset = start + size
    return responses


def _read_frame(stream) -> dict:
    header = bytearray()
    while True:
        chunk = stream.read(1)
        if not chunk:
            raise AssertionError(f"Unexpected EOF while reading MCP header: {bytes(header)!r}")
        header += chunk
        if header.endswith(b"\r\n\r\n"):
            header_bytes = bytes(header[:-4])
            break
        if header.endswith(b"\r\r\n\r\r\n"):
            header_bytes = bytes(header[:-6]).replace(b"\r\r\n", b"\r\n")
            break
    match = re.search(br"Content-Length:\s*(\d+)", header_bytes)
    if not match:
        raise AssertionError(f"Missing Content-Length header: {header_bytes!r}")
    size = int(match.group(1))
    payload = stream.read(size)
    if len(payload) != size:
        raise AssertionError(f"Short frame: expected {size}, got {len(payload)}")
    return json.loads(payload)


def _tool_call(message_id: int, name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _text_result(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def _write_messages(proc: subprocess.Popen, messages: list[dict]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(b"".join(_frame(message) for message in messages))
    proc.stdin.flush()


def main() -> int:
    if not EXE.exists():
        print(f"native executable not found: {EXE}", file=sys.stderr)
        return 2

    out_path = Path(tempfile.gettempdir()) / "autocad-mcp-native-smoke.dxf"
    proc = subprocess.Popen(
        [str(EXE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None

    first_messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        _tool_call(2, "entity", {"operation": "create_line", "data": {"x1": 0, "y1": 0, "x2": 12, "y2": 5, "layer": "SMOKE"}}),
    ]
    _write_messages(proc, first_messages)
    responses = [_read_frame(proc.stdout) for _ in first_messages]
    line_result = _text_result(responses[1])
    assert line_result["ok"] is True, line_result
    line_handle = line_result["payload"]["handle"]

    messages = [
        _tool_call(3, "entity", {"operation": "create_arc", "data": {"cx": 4, "cy": 4, "radius": 3, "start_angle": 0, "end_angle": 90}}),
        _tool_call(4, "annotation", {"operation": "create_text", "data": {"x": 1, "y": 2, "text": "Native smoke", "height": 2.5}}),
        _tool_call(5, "entity", {"operation": "rotate", "entity_id": line_handle, "data": {"cx": 0, "cy": 0, "angle": 15}}),
        _tool_call(6, "entity", {"operation": "scale", "entity_id": line_handle, "data": {"cx": 0, "cy": 0, "factor": 1.1}}),
        _tool_call(7, "entity", {"operation": "mirror", "entity_id": line_handle, "data": {"x1": 0, "y1": 0, "x2": 1, "y2": 0}}),
        _tool_call(8, "entity", {"operation": "offset", "entity_id": line_handle, "data": {"distance": 2}}),
        _tool_call(9, "entity", {"operation": "array", "entity_id": line_handle, "data": {"rows": 2, "cols": 2, "row_dist": 8, "col_dist": 12}}),
        _tool_call(10, "block", {"operation": "define", "data": {"name": "SMOKE_TAG", "entities": [
            {"type": "LINE", "x1": -1, "y1": 0, "x2": 1, "y2": 0},
            {"type": "CIRCLE", "cx": 0, "cy": 0, "radius": 0.5},
        ]}}),
        _tool_call(11, "block", {"operation": "insert_with_attributes", "data": {
            "name": "SMOKE_TAG", "x": 3, "y": 9, "attributes": {"TAG": "P-100"}
        }}),
        _tool_call(12, "block", {"operation": "update_attribute", "entity_id": "last", "data": {"tag": "TAG", "value": "P-101"}}),
        _tool_call(13, "block", {"operation": "get_attributes", "data": {"entity_id": "last"}}),
        _tool_call(14, "pid", {"operation": "setup_layers"}),
        _tool_call(15, "pid", {"operation": "draw_process_line", "data": {"x1": 20, "y1": 0, "x2": 35, "y2": 0}}),
        _tool_call(16, "pid", {"operation": "add_flow_arrow", "data": {"x": 30, "y": 0, "rotation": 0}}),
        _tool_call(17, "pid", {"operation": "insert_instrument", "data": {"x": 40, "y": 0, "instrument_type": "PI", "tag_id": "PI-101"}}),
        _tool_call(18, "arch", {"operation": "setup_layers"}),
        _tool_call(19, "arch", {"operation": "wall", "data": {"x1": 0, "y1": 20, "x2": 40, "y2": 20, "thickness": 6}}),
        _tool_call(20, "arch", {"operation": "door", "data": {"x": 6, "y": 20, "width": 8, "angle": 0}}),
        _tool_call(21, "arch", {"operation": "window", "data": {"x1": 18, "y1": 20, "x2": 28, "y2": 20, "sill_thickness": 4}}),
        _tool_call(22, "arch", {"operation": "room", "data": {"boundary": [[0, 28], [40, 28], [40, 48], [0, 48]], "name": "Smoke Room"}}),
        _tool_call(23, "style", {"operation": "create_dimstyle", "data": {"name": "SMOKE_DIM", "attribs": {"dimscale": 1, "dimtxt": 2.5}}}),
        _tool_call(24, "style", {"operation": "set_current_dimstyle", "data": {"name": "SMOKE_DIM"}}),
        _tool_call(25, "style", {"operation": "create_textstyle", "data": {"name": "SMOKE_TEXT", "font": "arial.ttf", "height": 2.5}}),
        _tool_call(26, "style", {"operation": "set_current_textstyle", "data": {"name": "SMOKE_TEXT"}}),
        _tool_call(27, "view", {"operation": "save_named_view", "data": {"name": "SMOKE_VIEW", "x1": 0, "y1": 0, "x2": 50, "y2": 50}}),
        _tool_call(28, "view", {"operation": "restore_named_view", "data": {"name": "SMOKE_VIEW"}}),
        _tool_call(29, "view", {"operation": "save_layer_state", "data": {"name": "SMOKE_LAYERS"}}),
        _tool_call(30, "view", {"operation": "list_layer_states"}),
        _tool_call(31, "events", {"operation": "start_recording"}),
        _tool_call(32, "events", {"operation": "new"}),
        _tool_call(33, "library", {"operation": "list"}),
        _tool_call(34, "library", {"operation": "insert", "data": {"name": "ARCH-NORTH-ARROW", "x": 55, "y": 55, "scale": 1}}),
        _tool_call(35, "project", {"operation": "info"}),
        _tool_call(36, "project", {"operation": "list_layers"}),
        _tool_call(37, "project", {"operation": "check_standard"}),
        _tool_call(38, "project", {"operation": "list_blocks"}),
        _tool_call(39, "entity", {"operation": "list", "data": {"limit": 0}}),
        _tool_call(40, "drawing", {"operation": "save_as_dxf", "data": {"path": str(out_path)}}),
    ]

    _write_messages(proc, messages)
    proc.stdin.close()
    responses = [_read_frame(proc.stdout) for _ in messages]
    proc.wait(timeout=10)
    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr is not None else b""
        print(stderr.decode("utf-8", "replace"), file=sys.stderr)
        return proc.returncode

    tool_results = [_text_result(response) for response in responses]
    for result in tool_results:
        assert result["ok"] is True, result
    assert tool_results[36]["payload"]["count"] == 21, tool_results[36]
    assert len(tool_results[6]["payload"]["handles"]) == 3, tool_results[6]
    assert tool_results[10]["payload"]["attributes"]["TAG"] == "P-101", tool_results[10]
    assert tool_results[21]["payload"]["current_dimstyle"] == "SMOKE_DIM", tool_results[21]
    assert tool_results[23]["payload"]["current_textstyle"] == "SMOKE_TEXT", tool_results[23]
    assert tool_results[25]["payload"]["name"] == "SMOKE_VIEW", tool_results[25]
    assert tool_results[27]["payload"]["count"] == 1, tool_results[27]
    assert tool_results[29]["payload"]["count"] >= 1, tool_results[29]
    assert tool_results[30]["payload"]["count"] >= 10, tool_results[30]
    assert tool_results[31]["payload"]["entity_type"] == "INSERT", tool_results[31]
    assert tool_results[32]["payload"]["mode"] == "native-lite", tool_results[32]
    assert tool_results[34]["payload"]["ok"] is True, tool_results[34]
    assert tool_results[35]["payload"]["count"] >= 2, tool_results[35]
    assert out_path.exists(), out_path
    text = out_path.read_text(encoding="utf-8", errors="replace")
    for token in ("LINE", "ARC", "TEXT", "INSERT", "BLOCKS", "SMOKE_TAG", "ARCH-NORTH-ARROW", "P-101", "PID-PIPE", "A-WALL", "Smoke Room", "SMOKE"):
        assert token in text, token

    express_messages = [
        {"jsonrpc": "2.0", "id": 101, "method": "initialize"},
        _tool_call(102, "block", {"operation": "define", "data": {"name": "EXPRESS_TAG", "entities": [
            {"type": "LINE", "x1": 0, "y1": 0, "x2": 2, "y2": 0},
        ]}}),
        _tool_call(103, "block", {"operation": "insert_with_attributes", "data": {
            "name": "EXPRESS_TAG", "x": 0, "y": 0, "attributes": {"TAG": "E-1"}
        }}),
        _tool_call(104, "express", {"operation": "burst", "entity_id": "last"}),
        _tool_call(105, "express", {"operation": "txt2mtxt", "entity_id": "last"}),
        _tool_call(106, "express", {"operation": "flatten"}),
        _tool_call(107, "express", {"operation": "overkill"}),
        _tool_call(108, "entity", {"operation": "list", "data": {"limit": 0}}),
    ]
    proc = subprocess.run(
        [str(EXE)],
        input=b"".join(_frame(message) for message in express_messages),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return proc.returncode
    express_results = [_text_result(response) for response in _parse_frames(proc.stdout)[1:]]
    for result in express_results:
        assert result["ok"] is True, result
    assert express_results[-1]["payload"]["count"] == 1, express_results[-1]
    assert express_results[-1]["payload"]["entities"][0]["type"] == "MTEXT", express_results[-1]

    snap_messages = [
        {"jsonrpc": "2.0", "id": 201, "method": "initialize"},
        _tool_call(202, "entity", {"operation": "create_line", "data": {"x1": 10, "y1": 20, "x2": 30, "y2": 20, "layer": "SNAP"}}),
        _tool_call(203, "entity", {"operation": "select", "data": {"type": "LINE", "layer": "SNAP"}}),
        _tool_call(204, "entity", {"operation": "osnap", "data": {"x": 10.2, "y": 20.1, "modes": ["endpoint"], "tolerance": 1.0}}),
    ]
    proc = subprocess.run(
        [str(EXE)],
        input=b"".join(_frame(message) for message in snap_messages),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return proc.returncode
    snap_results = [_text_result(response) for response in _parse_frames(proc.stdout)[1:]]
    for result in snap_results:
        assert result["ok"] is True, result
    assert snap_results[1]["payload"]["count"] == 1, snap_results[1]
    assert snap_results[2]["payload"]["mode_used"] == "endpoint", snap_results[2]
    assert snap_results[2]["payload"]["entity_handle"] == snap_results[0]["payload"]["handle"], snap_results[2]

    table_messages = [
        {"jsonrpc": "2.0", "id": 301, "method": "initialize"},
        _tool_call(302, "annotation", {"operation": "create_table", "data": {
            "x": 0, "y": 0, "rows": 2, "cols": 2, "row_height": 1, "col_width": 4, "title": "SCHEDULE"
        }}),
        _tool_call(303, "drawing", {"operation": "purge"}),
        _tool_call(304, "drawing", {"operation": "audit"}),
        _tool_call(305, "entity", {"operation": "list", "data": {"limit": 0}}),
    ]
    proc = subprocess.run(
        [str(EXE)],
        input=b"".join(_frame(message) for message in table_messages),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return proc.returncode
    table_results = [_text_result(response) for response in _parse_frames(proc.stdout)[1:]]
    for result in table_results:
        assert result["ok"] is True, result
    assert table_results[0]["payload"]["kind"] == "grid_table", table_results[0]
    assert table_results[2]["payload"]["issues"] == [], table_results[2]
    assert table_results[-1]["payload"]["count"] == 7, table_results[-1]

    print(f"native MCP smoke passed: {out_path} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
