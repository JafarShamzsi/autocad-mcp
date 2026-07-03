#include "tools/ToolRegistry.hpp"

#include "core/BackendCapabilities.hpp"
#include "core/Config.hpp"
#include "core/Version.hpp"
#include "protocol/JsonRpc.hpp"

#include <algorithm>

namespace autocad_mcp::tools {
namespace {

using autocad_mcp::core::CommandResult;
using autocad_mcp::core::ErrorCategory;
using autocad_mcp::core::escape_json;

std::string string_array_json(const std::vector<std::string>& values) {
  std::string out = "[";
  for (std::size_t i = 0; i < values.size(); ++i) {
    if (i != 0) {
      out += ",";
    }
    out += "\"" + escape_json(values[i]) + "\"";
  }
  out += "]";
  return out;
}

std::string input_schema_json(const ToolSpec& spec) {
  return std::string("{\"type\":\"object\",\"properties\":{") +
    "\"operation\":{\"type\":\"string\",\"enum\":" + string_array_json(spec.operations) + "}," +
    "\"data\":{\"type\":\"object\"}," +
    "\"include_screenshot\":{\"type\":\"boolean\"}" +
    "},\"required\":[\"operation\"]}";
}

std::string operation_index_json(const std::vector<ToolSpec>& specs) {
  std::string out = "{";
  for (std::size_t i = 0; i < specs.size(); ++i) {
    if (i != 0) {
      out += ",";
    }
    out += "\"" + escape_json(specs[i].name) + "\":" + string_array_json(specs[i].operations);
  }
  out += "}";
  return out;
}

bool has_operation(const ToolSpec& spec, std::string_view operation) {
  return std::find(spec.operations.begin(), spec.operations.end(), operation) != spec.operations.end();
}

const ToolSpec* find_tool(const std::vector<ToolSpec>& specs, std::string_view name) {
  const auto it = std::find_if(specs.begin(), specs.end(), [&](const ToolSpec& spec) {
    return spec.name == name;
  });
  return it == specs.end() ? nullptr : &*it;
}

}  // namespace

ToolRegistry::ToolRegistry()
  : specs_{
      {"drawing", "AutoCAD Drawing Operations", "File and drawing management.", false,
       {"create", "open", "info", "save", "save_as_dxf", "save_as_dwg", "plot_pdf",
        "purge", "get_variables", "undo", "redo", "audit", "recover", "batch",
        "list_layouts", "create_layout", "set_active_layout", "create_viewport"}},
      {"entity", "AutoCAD Entity Operations", "Entity creation, query, selection, and modification.", false,
       {"select", "osnap", "create_line", "create_circle", "create_polyline",
        "create_rectangle", "create_arc", "create_ellipse", "create_mtext",
        "create_hatch", "list", "count", "get", "copy", "move", "rotate",
        "scale", "mirror", "offset", "array", "fillet", "chamfer", "erase",
        "region", "extrude", "revolve", "boolean"}},
      {"layer", "AutoCAD Layer Operations", "Layer management.", false,
       {"list", "create", "set_current", "set_properties", "freeze", "thaw", "lock", "unlock"}},
      {"block", "AutoCAD Block Operations", "Block insertion, definition, import, and attributes.", false,
       {"list", "insert", "insert_with_attributes", "get_attributes", "update_attribute",
        "define", "list_from_file", "import_from_file"}},
      {"annotation", "AutoCAD Annotation Operations", "Text, dimensions, leaders, and tables.", false,
       {"create_text", "create_dimension_linear", "create_dimension_aligned",
        "create_dimension_angular", "create_dimension_radius", "create_leader", "create_table"}},
      {"pid", "P&ID Operations (CTO Library)", "P&ID symbols and process drafting helpers.", false,
       {"setup_layers", "insert_symbol", "list_symbols", "draw_process_line",
        "connect_equipment", "add_flow_arrow", "add_equipment_tag", "add_line_number",
        "insert_valve", "insert_instrument", "insert_pump", "insert_tank"}},
      {"arch", "AutoCAD Architectural Primitives (AIA/NCS)", "Architectural drafting primitives and templates.", false,
       {"setup_layers", "wall", "wall_with_openings", "door", "window", "room",
        "dimension_chain", "stair", "column", "slab", "beam", "roof", "elevation",
        "section", "template", "hatch_region", "list_hatches"}},
      {"project", "AutoCAD Project Operations", "Multi-drawing project model, indexing, standards, sheets, and XREFs.", false,
       {"load", "unload", "sidecar", "index", "rebuild", "info", "list_drawings",
        "list_layers", "find_entities", "list_sheets", "find_sheets_for_drawing",
        "list_dimstyles", "list_textstyles", "find_style_inconsistencies",
        "list_linetypes", "list_blocks", "find_linetype_inconsistencies",
        "find_block_inconsistencies", "list_xrefs", "find_broken_xrefs",
        "check_standard", "xref_attach", "xref_detach", "xref_reload", "xref_bind"}},
      {"library", "AutoCAD Symbol Library", "Procedural and catalog-backed symbol library operations.", false,
       {"list", "define", "define_all", "insert", "scan_external", "search_external",
        "stats_external", "catalog_reindex", "catalog_list", "catalog_stats",
        "catalog_search", "catalog_get", "catalog_insert", "vendor_import"}},
      {"style", "AutoCAD Style Operations", "Dimstyle, textstyle, and mleaderstyle management.", false,
       {"list_dimstyles", "get_dimstyle", "create_dimstyle", "set_current_dimstyle",
        "list_textstyles", "get_textstyle", "create_textstyle", "set_current_textstyle",
        "list_mleaderstyles", "set_current_mleaderstyle"}},
      {"express", "AutoCAD Express Tools", "Express Tools wrappers and compatible emulations.", false,
       {"burst", "flatten", "overkill", "layiso", "txt2mtxt"}},
      {"events", "AutoCAD Event Stream", "Event log and reactor controls.", false,
       {"recent", "new", "clear", "start_recording"}},
      {"view", "AutoCAD View Operations", "Zoom, screenshot, named views, and layer states.", false,
       {"zoom_extents", "zoom_window", "get_screenshot", "list_named_views",
        "save_named_view", "restore_named_view", "delete_named_view",
        "list_layer_states", "save_layer_state", "restore_layer_state", "delete_layer_state"}},
      {"system", "AutoCAD MCP System", "Server status, diagnostics, tool help, turn lifecycle, and gated LISP execution.", false,
       {"status", "get_backend", "health", "runtime", "dwg_status", "preflight",
        "tool_help", "init", "turn_begin", "turn_end", "execute_lisp"}},
    } {}

std::string ToolRegistry::tools_list_json() const {
  std::string out = "{\"tools\":[";
  for (std::size_t i = 0; i < specs_.size(); ++i) {
    const auto& spec = specs_[i];
    if (i != 0) {
      out += ",";
    }
    out += "{";
    out += "\"name\":\"" + escape_json(spec.name) + "\",";
    out += "\"description\":\"" + escape_json(spec.description) + "\",";
    out += "\"inputSchema\":" + input_schema_json(spec);
    out += "}";
  }
  out += "]}";
  return out;
}

CommandResult ToolRegistry::call(
    std::string_view tool_name,
    std::string_view arguments_json,
    const std::function<CommandResult(std::string_view, std::string_view, std::string_view)>& backend_call) const {
  const ToolSpec* spec = find_tool(specs_, tool_name);
  if (spec == nullptr) {
    return CommandResult::failure(ErrorCategory::not_found, "Unknown native MCP tool");
  }

  auto operation = autocad_mcp::protocol::extract_string_property(arguments_json, "operation");
  if (!operation) {
    return CommandResult::failure(ErrorCategory::invalid_arg, "Tool call requires an operation string");
  }
  if (!has_operation(*spec, *operation)) {
    return CommandResult::failure(
      ErrorCategory::invalid_arg,
      "Unknown operation for native MCP tool: " + *operation);
  }

  if (spec->name == "system" && (*operation == "status" || *operation == "get_backend")) {
    auto status = backend_call(spec->name, *operation, arguments_json);
    if (!status.ok) {
      return status;
    }
    std::string backend_payload = status.payload_json;
    if (!backend_payload.empty() && backend_payload.back() == '}') {
      backend_payload.pop_back();
    }
    const std::string payload = backend_payload +
      ",\"tools_registered\":" + std::to_string(specs_.size()) +
      ",\"operation_index\":" + operation_index_json(specs_) + "}";
    return CommandResult::success(payload);
  }

  if (spec->name == "system" && *operation == "health") {
    return CommandResult::success("{\"status\":\"ok\",\"backend\":\"native_skeleton\"}");
  }

  if (spec->name == "system" && *operation == "runtime") {
    const auto config = core::load_runtime_config();
    return CommandResult::success(
      std::string("{\"runtime\":\"native-cpp\",\"mcp_transport\":\"stdio\",") +
      "\"implementation_stage\":\"native_backend_porting\",\"config\":" + config.to_json() + "}");
  }

  if (spec->name == "system" && *operation == "dwg_status") {
    const auto config = core::load_runtime_config();
    return CommandResult::success(
      std::string("{\"available\":") + (config.dwg_converters.any_available() ? "true" : "false") +
      ",\"converters\":" + config.dwg_converters.to_json() + "}");
  }

  if (spec->name == "system" && *operation == "preflight") {
    const auto config = core::load_runtime_config();
    const std::string payload = std::string("{") +
      "\"ok\":true," +
      "\"runtime\":\"native-cpp\"," +
      "\"backend_env\":\"" + escape_json(config.requested_backend) + "\"," +
      "\"selected_backend\":\"" + escape_json(config.selected_backend) + "\"," +
      "\"ipc_dir\":\"" + escape_json(config.ipc_dir) + "\"," +
      "\"ipc_timeout_seconds\":" + std::to_string(config.ipc_timeout_seconds) + "," +
      "\"window_hint\":" + (config.window_hint.empty() ? "null" : "\"" + escape_json(config.window_hint) + "\"") + "," +
      "\"lisp_exec_enabled\":" + (config.lisp_exec_enabled ? "true" : "false") + "," +
      "\"dwg_converters\":" + config.dwg_converters.to_json() + "," +
      "\"checks\":{" +
      "\"stdio\":true," +
      "\"tool_registry\":true," +
      "\"dwg_converter_available\":" + (config.dwg_converters.any_available() ? "true" : "false") +
      "}" +
      "}";
    return CommandResult::success(payload);
  }

  if (spec->name == "system" && *operation == "tool_help") {
    if (auto data = autocad_mcp::protocol::extract_object_property(arguments_json, "data")) {
      if (auto requested = autocad_mcp::protocol::extract_string_property(*data, "tool")) {
        if (const ToolSpec* requested_spec = find_tool(specs_, *requested)) {
          const std::string payload = std::string("{") +
            "\"tool\":\"" + escape_json(requested_spec->name) + "\"," +
            "\"description\":\"" + escape_json(requested_spec->description) + "\"," +
            "\"operations\":" + string_array_json(requested_spec->operations) +
            "}";
          return CommandResult::success(payload);
        }
        return CommandResult::failure(ErrorCategory::not_found, "Unknown tool requested for tool_help");
      }
    }
    const std::string payload = "{\"tools\":" + operation_index_json(specs_) + "}";
    return CommandResult::success(payload);
  }

  return backend_call(spec->name, *operation, arguments_json);
}

}  // namespace autocad_mcp::tools
