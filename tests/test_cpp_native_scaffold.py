"""Source-level checks for the native C++ MCP scaffold.

These tests do not require a C++ compiler. They keep Milestone 1 honest on
machines where only CMake is installed by verifying that the native scaffold
contains the expected API boundary and build entrypoints.
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
CPP = ROOT / "cpp"
REGISTRY = CPP / "src" / "tools" / "ToolRegistry.cpp"


EXPECTED_TOOLS = {
    "drawing",
    "entity",
    "layer",
    "block",
    "annotation",
    "pid",
    "arch",
    "project",
    "library",
    "style",
    "express",
    "events",
    "view",
    "system",
}


EXPECTED_OPERATIONS = {
    "drawing": {"create", "open", "info", "save", "save_as_dxf", "save_as_dwg", "batch"},
    "entity": {"select", "osnap", "create_line", "create_circle", "list", "erase", "boolean"},
    "project": {"load", "index", "list_xrefs", "check_standard", "xref_attach", "xref_bind"},
    "library": {"catalog_search", "catalog_insert", "vendor_import", "scan_external"},
    "system": {"status", "health", "runtime", "tool_help", "execute_lisp"},
}


def test_native_cmake_entrypoint_exists():
    cmake = CPP / "CMakeLists.txt"
    assert cmake.exists()
    text = cmake.read_text(encoding="utf-8")
    assert "project(autocad_mcp_native" in text
    assert "add_executable(autocad-mcp-native" in text
    assert "CXX_STANDARD 20" in text
    assert "WindowMessaging.cpp" in text
    assert "ComAutomation.cpp" in text
    assert "NativeDxfDocument.cpp" in text
    assert "ole32" in text
    assert "oleaut32" in text
    assert "user32" in text
    assert "gdi32" in text
    assert "windowscodecs" in text


def test_native_launch_profile_exists_and_preserves_mcp_stdout():
    profile = ROOT / ".mcp.native.json"
    runner = ROOT / "scripts" / "run_native_mcp.ps1"
    assert profile.exists()
    assert runner.exists()
    profile_text = profile.read_text(encoding="utf-8")
    runner_text = runner.read_text(encoding="utf-8")
    assert "autocad-native" in profile_text
    assert "run_native_mcp.ps1" in profile_text
    assert "build_native.ps1" in runner_text
    assert "[Console]::Error.WriteLine" in runner_text
    assert "ValueFromRemainingArguments" in runner_text


def test_native_protocol_sources_exist():
    for rel in [
        "src/main.cpp",
        "src/backends/BackendManager.cpp",
        "src/backends/ComBackend.cpp",
        "src/backends/DxfBackend.cpp",
        "src/backends/FileIpcBackend.cpp",
        "src/backends/FileIpcTransport.cpp",
        "src/backends/NativeDxfDocument.cpp",
        "src/backends/NativeDxfDocument.hpp",
        "src/core/Config.cpp",
        "src/protocol/JsonRpc.cpp",
        "src/protocol/McpServer.cpp",
        "src/protocol/StdioTransport.cpp",
        "src/platform/win32/ComAutomation.cpp",
        "src/platform/win32/ComAutomation.hpp",
        "src/platform/win32/WindowMessaging.cpp",
        "src/platform/win32/WindowMessaging.hpp",
        "src/core/CommandResult.hpp",
        "src/backends/IBackend.hpp",
        "src/backends/BackendManager.hpp",
        "src/backends/ComBackend.hpp",
        "src/backends/DxfBackend.hpp",
        "src/backends/FileIpcBackend.hpp",
        "src/backends/FileIpcTransport.hpp",
        "src/core/BackendCapabilities.hpp",
        "src/tools/ToolRegistry.cpp",
    ]:
        assert (CPP / rel).exists(), rel


def test_native_registry_contains_all_current_tools():
    text = REGISTRY.read_text(encoding="utf-8")
    for tool in EXPECTED_TOOLS:
        assert f'{{"{tool}"' in text


def test_native_registry_contains_representative_current_operations():
    text = REGISTRY.read_text(encoding="utf-8")
    for tool, operations in EXPECTED_OPERATIONS.items():
        assert f'{{"{tool}"' in text
        for operation in operations:
            assert f'"{operation}"' in text


def test_native_mcp_methods_are_implemented():
    text = (CPP / "src" / "protocol" / "McpServer.cpp").read_text(encoding="utf-8")
    assert 'request->method == "initialize"' in text
    assert 'request->method == "tools/list"' in text
    assert 'request->method == "tools/call"' in text


def test_native_config_preserves_backend_env_contract():
    text = (CPP / "src" / "core" / "Config.cpp").read_text(encoding="utf-8")
    assert "AUTOCAD_MCP_BACKEND" in text
    assert "AUTOCAD_MCP_ENABLE_LISP_EXEC" in text
    assert "AUTOCAD_MCP_PROJECT_ROOTS" in text
    assert "AUTOCAD_MCP_WINDOW_HINT" in text
    assert 'requested == "ezdxf"' in text
    assert "AUTOCAD_MCP_ACCORECONSOLE" in text


def test_native_file_ipc_transport_preserves_file_protocol_names():
    text = (CPP / "src" / "backends" / "FileIpcTransport.cpp").read_text(encoding="utf-8")
    assert "autocad_mcp_cmd_" in text
    assert "autocad_mcp_result_" in text
    assert '\\"request_id\\"' in text
    assert '\\"command\\"' in text
    assert '\\"params\\"' in text
    assert "write_command_atomic" in text
    assert '\\"ts\\"' in text
    assert "remove_request_files" in text


def test_native_file_ipc_backend_ports_python_dispatch_flow():
    text = (CPP / "src" / "backends" / "FileIpcBackend.cpp").read_text(encoding="utf-8")
    for needle in [
        "find_autocad_window",
        "post_dispatch_trigger",
        "dispatch(\"ping\", \"{}\")",
        "std::chrono::milliseconds(100)",
        "config_.ipc_timeout_seconds",
        "command_result_from_ipc_json",
        "AUTOCAD_MCP_ENABLE_LISP_EXEC=1",
    ]:
        assert needle in text


def test_native_file_ipc_command_map_covers_lisp_dispatch_commands():
    text = (CPP / "src" / "backends" / "FileIpcBackend.cpp").read_text(encoding="utf-8")
    for command in [
        "drawing-info",
        "drawing-save-as-dxf",
        "drawing-open",
        "create-line",
        "entity-offset",
        "layer-set-current",
        "block-insert-with-attributes",
        "create-dimension-linear",
        "pid-insert-valve",
        "arch-wall-with-openings",
        "library-define",
        "zoom-window",
        "execute-lisp",
    ]:
        assert command in text


def test_native_win32_window_messaging_is_isolated_and_stubbed():
    text = (CPP / "src" / "platform" / "win32" / "WindowMessaging.cpp").read_text(encoding="utf-8")
    assert "#if defined(_WIN32)" in text
    assert "EnumWindows" in text
    assert "GetForegroundWindow" in text
    assert "MDIClient" in text
    assert "PostMessageW" in text
    assert "(c:mcp-dispatch)" in text
    assert "return std::nullopt" in text


def test_native_com_backend_uses_raii_and_operation_layer():
    backend = (CPP / "src" / "backends" / "ComBackend.cpp").read_text(encoding="utf-8")
    probe = (CPP / "src" / "platform" / "win32" / "ComAutomation.cpp").read_text(encoding="utf-8")
    assert "probe_autocad_application" in backend
    assert "native_com_operations" in backend
    assert "execute_autocad_com_operation" in backend
    assert "lisp_exec_enabled" in backend
    for needle in [
        "CoInitializeEx",
        "CoUninitialize",
        "CLSIDFromProgID",
        "GetActiveObject",
        "QueryInterface",
        "VariantClear",
        "SysFreeString",
        "SafeArrayCreateVector",
        "Release",
    ]:
        assert needle in probe
    for needle in [
        "Documents",
        "ModelSpace",
        "AddLine",
        "AddCircle",
        "AddLightWeightPolyline",
        "AddText",
        "AddMText",
        "AddDimRotated",
        "AddDimAligned",
        "AddDimRadial",
        "AddDimAngular",
        "AddLeader",
        "AddTable",
        "AddHatch",
        "AppendOuterLoop",
        "AddRegion",
        "AddExtrudedSolid",
        "AddRevolvedSolid",
        "ArrayRectangular",
        "Boolean",
        "HandleToObject",
        "StartPoint",
        "EndPoint",
        "Coordinates",
        "std::hypot",
        "Layers",
        "InsertBlock",
        "SendCommand",
        "PlotToFile",
        "Layouts",
        "ActiveLayout",
        "AddPViewport",
        "ZoomExtents",
        "ZoomWindow",
        "DimStyles",
        "TextStyles",
        "MLeaderStyles",
        "ActiveDimStyle",
        "ActiveTextStyle",
        "ActiveMLeaderStyle",
        "IsXRef",
        "AttachExternalReference",
        "Detach",
        "Reload",
        "Bind",
        "PrintWindow",
        "CreateCompatibleBitmap",
        "IWICImagingFactory",
        "GUID_ContainerFormatPng",
        "CreateBitmapFromHBITMAP",
        "base64_encode",
    ]:
        assert needle in probe


def test_native_dxf_backend_has_real_document_operations():
    backend = (CPP / "src" / "backends" / "DxfBackend.cpp").read_text(encoding="utf-8")
    document = (CPP / "src" / "backends" / "NativeDxfDocument.cpp").read_text(encoding="utf-8")
    for needle in [
        "document_.create_line",
        "document_.create_circle",
        "document_.create_polyline",
        "document_.create_rectangle",
        "document_.create_arc",
        "document_.create_ellipse",
        "document_.create_mtext",
        "document_.create_text",
        "document_.create_leader",
        "document_.create_dimension_stub",
        "document_.select_entities",
        "document_.osnap",
        "document_.list_entities",
        "document_.erase_entity",
        "document_.copy_entity",
        "document_.move_entity",
        "document_.rotate_entity",
        "document_.scale_entity",
        "document_.mirror_entity",
        "document_.offset_entity",
        "document_.array_entity",
        "document_.create_layer",
        "document_.list_blocks",
        "document_.define_block",
        "document_.insert_block",
        "document_.get_block_attributes",
        "document_.update_block_attribute",
        "document_.save",
        "document_.open",
        "document_.remove_duplicate_entities",
    ]:
        assert needle in backend
    for dxf_group in [
        '"SECTION"',
        '"HEADER"',
        '"TABLES"',
        '"ENTITIES"',
        '"LINE"',
        '"CIRCLE"',
        '"LWPOLYLINE"',
        '"ARC"',
        '"ELLIPSE"',
        '"TEXT"',
        '"MTEXT"',
        '"EOF"',
        "NativeDxfDocument::rotate_entity",
        "NativeDxfDocument::scale_entity",
        "NativeDxfDocument::mirror_entity",
        "NativeDxfDocument::offset_entity",
        "NativeDxfDocument::array_entity",
        "NativeDxfDocument::select_entities",
        "NativeDxfDocument::osnap",
        "NativeDxfDocument::insert_block",
        '"BLOCKS"',
        '"INSERT"',
        '"ATTRIB"',
    ]:
        assert dxf_group in document
    for needle in [
        'operation == "create_table"',
        'operation == "purge"',
        'operation == "audit"',
        'operation == "recover"',
    ]:
        assert needle in backend


def test_native_dxf_backend_ports_pid_and_arch_procedural_helpers():
    backend = (CPP / "src" / "backends" / "DxfBackend.cpp").read_text(encoding="utf-8")
    for needle in [
        'tool_name == "pid"',
        'tool_name == "arch"',
        "setup_layer_set",
        "pid_symbols_json",
        "draw_rotated_triangle",
        "offset_rectangle",
        "polygon_centroid",
        '"PID-PIPE"',
        '"PID-INSTR"',
        '"A-WALL"',
        '"A-DOOR"',
        '"A-GLAZ"',
        '"A-ANNO-DIMS"',
    ]:
        assert needle in backend


def test_native_dxf_backend_ports_style_view_and_event_state():
    header = (CPP / "src" / "backends" / "DxfBackend.hpp").read_text(encoding="utf-8")
    backend = (CPP / "src" / "backends" / "DxfBackend.cpp").read_text(encoding="utf-8")
    for needle in [
        "dimstyles_",
        "textstyles_",
        "current_dimstyle_",
        "current_textstyle_",
        "named_views_",
        "layer_states_",
        "events_",
        "event_cursor_",
    ]:
        assert needle in header
    for needle in [
        'tool_name == "style"',
        'tool_name == "view"',
        'tool_name == "events"',
        "string_map_names_json",
        "named_views_json",
        "events_json",
        "create_dimstyle",
        "set_current_textstyle",
        "save_named_view",
        "restore_layer_state",
        "start_recording",
    ]:
        assert needle in backend


def test_native_dxf_backend_ports_procedural_library_subset():
    backend = (CPP / "src" / "backends" / "DxfBackend.cpp").read_text(encoding="utf-8")
    for needle in [
        "native_library_names",
        "native_library_entities",
        'tool_name == "library"',
        '"ARCH-NORTH-ARROW"',
        '"PLMB-TOILET"',
        '"FURN-SOFA"',
        '"PID-GATE-VALVE"',
        "document_.define_block",
        "document_.insert_block",
        "catalog_insert",
        "vendor_import",
    ]:
        assert needle in backend


def test_native_dxf_backend_ports_native_lite_project_queries():
    backend = (CPP / "src" / "backends" / "DxfBackend.cpp").read_text(encoding="utf-8")
    header = (CPP / "src" / "backends" / "DxfBackend.hpp").read_text(encoding="utf-8")
    assert "project_path_" in header
    for needle in [
        'tool_name == "project"',
        "aia_layer_check_json",
        "read_sidecar_file",
        "native-lite",
        "list_drawings",
        "find_entities",
        "check_standard",
        "list_xrefs",
        "xref_attach",
    ]:
        assert needle in backend


def test_native_dxf_backend_ports_express_headless_emulations():
    backend = (CPP / "src" / "backends" / "DxfBackend.cpp").read_text(encoding="utf-8")
    document = (CPP / "src" / "backends" / "NativeDxfDocument.cpp").read_text(encoding="utf-8")
    header = (CPP / "src" / "backends" / "NativeDxfDocument.hpp").read_text(encoding="utf-8")
    for needle in [
        "isolate_layers",
        "remove_duplicate_entities",
        "burst_inserts",
        "text_to_mtext",
    ]:
        assert needle in header
        assert f"NativeDxfDocument::{needle}" in document
    for needle in [
        'tool_name == "express"',
        "string_array_arg",
        "entity_ids",
        "flattened",
    ]:
        assert needle in backend


def _tool_section(text: str, tool: str, next_tool: str | None) -> str:
    start = text.index(f"async def {tool}(")
    if next_tool is None:
        return text[start:]
    end = text.index(f"@mcp.tool", text.index(f"async def {next_tool}(") - 200)
    return text[start:end]


def _python_operations() -> dict[str, set[str]]:
    server = (ROOT / "src" / "autocad_mcp" / "server.py").read_text(encoding="utf-8")
    tools = [
        "drawing",
        "entity",
        "layer",
        "block",
        "annotation",
        "pid",
        "arch",
        "project",
        "library",
        "style",
        "express",
        "events",
        "view",
        "system",
    ]
    result: dict[str, set[str]] = {}
    for idx, tool in enumerate(tools):
        next_tool = tools[idx + 1] if idx + 1 < len(tools) else None
        section = _tool_section(server, tool, next_tool)
        result[tool] = set(re.findall(r'operation\s*==\s*"([^"]+)"', section))
    return result


def _native_registry_strings_for_tool(tool: str) -> set[str]:
    registry = REGISTRY.read_text(encoding="utf-8")
    marker = f'      {{"{tool}"'
    start = registry.index(marker)
    next_match = re.search(r'\n      \{"[a-z_]+"', registry[start + len(marker) :])
    end = start + len(marker) + next_match.start() if next_match else len(registry)
    section = registry[start:end]
    return set(re.findall(r'"([^"]+)"', section))


def test_native_registry_covers_python_operation_branches():
    python_ops = _python_operations()
    for tool, operations in python_ops.items():
        native_strings = _native_registry_strings_for_tool(tool)
        missing = operations - native_strings
        assert not missing, f"{tool} missing native operations: {sorted(missing)}"
