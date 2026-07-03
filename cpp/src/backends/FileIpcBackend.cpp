#include "backends/FileIpcBackend.hpp"

#include "core/Version.hpp"
#include "platform/win32/WindowMessaging.hpp"
#include "protocol/JsonRpc.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <unordered_map>

namespace autocad_mcp::backends {
namespace {

using autocad_mcp::core::CommandResult;
using autocad_mcp::core::ErrorCategory;

std::string make_request_id() {
  static std::random_device random_device;
  static std::mt19937_64 rng(random_device());
  static std::mutex rng_mutex;

  std::lock_guard<std::mutex> lock(rng_mutex);
  const auto now = std::chrono::steady_clock::now().time_since_epoch().count();
  const auto random = rng();
  const auto mixed = static_cast<unsigned long long>(now) ^ random;
  std::ostringstream out;
  out << std::hex << std::nouppercase << std::setw(12) << std::setfill('0') << (mixed & 0xffffffffffffULL);
  return out.str();
}

std::string normalize_command_name(std::string_view tool_name, std::string_view operation) {
  const std::string key = std::string(tool_name) + "." + std::string(operation);
  static const std::unordered_map<std::string, std::string> kCommands{
    {"drawing.create", "drawing-create"},
    {"drawing.info", "drawing-info"},
    {"drawing.save", "drawing-save"},
    {"drawing.save_as_dxf", "drawing-save-as-dxf"},
    {"drawing.plot_pdf", "drawing-plot-pdf"},
    {"drawing.purge", "drawing-purge"},
    {"drawing.get_variables", "drawing-get-variables"},
    {"drawing.open", "drawing-open"},
    {"drawing.undo", "undo"},
    {"drawing.redo", "redo"},
    {"entity.create_line", "create-line"},
    {"entity.create_circle", "create-circle"},
    {"entity.create_polyline", "create-polyline"},
    {"entity.create_rectangle", "create-rectangle"},
    {"entity.create_arc", "create-arc"},
    {"entity.create_ellipse", "create-ellipse"},
    {"entity.create_mtext", "create-mtext"},
    {"entity.create_hatch", "create-hatch"},
    {"entity.list", "entity-list"},
    {"entity.count", "entity-count"},
    {"entity.get", "entity-get"},
    {"entity.erase", "entity-erase"},
    {"entity.copy", "entity-copy"},
    {"entity.move", "entity-move"},
    {"entity.rotate", "entity-rotate"},
    {"entity.scale", "entity-scale"},
    {"entity.mirror", "entity-mirror"},
    {"entity.offset", "entity-offset"},
    {"entity.array", "entity-array"},
    {"entity.fillet", "entity-fillet"},
    {"entity.chamfer", "entity-chamfer"},
    {"layer.list", "layer-list"},
    {"layer.create", "layer-create"},
    {"layer.set_current", "layer-set-current"},
    {"layer.set_properties", "layer-set-properties"},
    {"layer.freeze", "layer-freeze"},
    {"layer.thaw", "layer-thaw"},
    {"layer.lock", "layer-lock"},
    {"layer.unlock", "layer-unlock"},
    {"block.list", "block-list"},
    {"block.insert", "block-insert"},
    {"block.insert_with_attributes", "block-insert-with-attributes"},
    {"block.get_attributes", "block-get-attributes"},
    {"block.update_attribute", "block-update-attribute"},
    {"block.define", "block-define"},
    {"annotation.create_text", "create-text"},
    {"annotation.create_dimension_linear", "create-dimension-linear"},
    {"annotation.create_dimension_aligned", "create-dimension-aligned"},
    {"annotation.create_dimension_angular", "create-dimension-angular"},
    {"annotation.create_dimension_radius", "create-dimension-radius"},
    {"annotation.create_leader", "create-leader"},
    {"pid.setup_layers", "pid-setup-layers"},
    {"pid.insert_symbol", "pid-insert-symbol"},
    {"pid.list_symbols", "pid-list-symbols"},
    {"pid.draw_process_line", "pid-draw-process-line"},
    {"pid.connect_equipment", "pid-connect-equipment"},
    {"pid.add_flow_arrow", "pid-add-flow-arrow"},
    {"pid.add_equipment_tag", "pid-add-equipment-tag"},
    {"pid.add_line_number", "pid-add-line-number"},
    {"pid.insert_valve", "pid-insert-valve"},
    {"pid.insert_instrument", "pid-insert-instrument"},
    {"pid.insert_pump", "pid-insert-pump"},
    {"pid.insert_tank", "pid-insert-tank"},
    {"arch.setup_layers", "arch-setup-layers"},
    {"arch.wall", "arch-wall"},
    {"arch.wall_with_openings", "arch-wall-with-openings"},
    {"arch.door", "arch-door"},
    {"arch.window", "arch-window"},
    {"arch.room", "arch-room"},
    {"arch.dimension_chain", "arch-dimension-chain"},
    {"library.define", "library-define"},
    {"view.zoom_extents", "zoom-extents"},
    {"view.zoom_window", "zoom-window"},
    {"system.execute_lisp", "execute-lisp"},
  };
  if (const auto it = kCommands.find(key); it != kCommands.end()) {
    return it->second;
  }
  return {};
}

std::string params_from_arguments(std::string_view arguments_json) {
  if (auto data = protocol::extract_object_property(arguments_json, "data")) {
    return *data;
  }
  return "{}";
}

ErrorCategory category_from_result(std::string_view result_json) {
  if (auto category = protocol::extract_string_property(result_json, "category")) {
    if (*category == "not_found") {
      return ErrorCategory::not_found;
    }
    if (*category == "invalid_arg") {
      return ErrorCategory::invalid_arg;
    }
    if (*category == "backend_unavailable") {
      return ErrorCategory::backend_unavailable;
    }
    if (*category == "timeout") {
      return ErrorCategory::timeout;
    }
    if (*category == "not_supported") {
      return ErrorCategory::not_supported;
    }
  }
  return ErrorCategory::internal_error;
}

CommandResult command_result_from_ipc_json(std::string_view result_json) {
  const auto ok = protocol::extract_bool_property(result_json, "ok");
  if (!ok) {
    return CommandResult::failure(ErrorCategory::internal_error, "IPC result is missing boolean ok field");
  }
  const std::string payload = protocol::extract_object_property(result_json, "payload").value_or("null");
  if (*ok) {
    return CommandResult::success(payload);
  }
  const std::string error = protocol::extract_string_property(result_json, "error")
    .value_or("AutoCAD file IPC command failed");
  return CommandResult::failure(category_from_result(result_json), error);
}

std::string path_to_lisp_string(std::filesystem::path path) {
  std::string value = path.string();
  std::replace(value.begin(), value.end(), '\\', '/');
  return value;
}

std::filesystem::path lisp_temp_path(const FileIpcTransport& transport, std::string_view request_id) {
  return transport.ipc_dir() / ("autocad_mcp_lisp_" + std::string(request_id) + ".lsp");
}

void write_text_file(const std::filesystem::path& path, std::string_view text) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output) {
    throw std::runtime_error("Failed to open temp LISP file");
  }
  output.write(text.data(), static_cast<std::streamsize>(text.size()));
  output.flush();
  if (!output) {
    throw std::runtime_error("Failed to write temp LISP file");
  }
}

}  // namespace

FileIpcBackend::FileIpcBackend(core::RuntimeConfig config)
  : config_(std::move(config)), transport_(config_.ipc_dir) {}

std::string FileIpcBackend::name() const {
  return "file_ipc";
}

core::BackendCapabilities FileIpcBackend::capabilities() const {
  core::BackendCapabilities caps;
  caps.can_read_drawing = true;
  caps.can_modify_entities = true;
  caps.can_create_entities = true;
  caps.can_screenshot = true;
  caps.can_save = true;
  caps.can_plot_pdf = true;
  caps.can_zoom = true;
  caps.can_query_entities = true;
  caps.can_file_operations = true;
  caps.can_undo = true;
  return caps;
}

core::CommandResult FileIpcBackend::initialize() {
  try {
    transport_.ensure_directory();
    transport_.cleanup_stale_files(std::chrono::seconds(60));

    const auto window = platform::win32::find_autocad_window(config_.window_hint);
    if (!window) {
      initialized_ = false;
      return core::CommandResult::failure(
        core::ErrorCategory::backend_unavailable,
        "AutoCAD LT window not found");
    }
    hwnd_ = window->hwnd;
    command_hwnd_ = window->command_hwnd != 0 ? window->command_hwnd : window->hwnd;
    window_title_ = window->title;

    const auto ping = dispatch("ping", "{}");
    if (!ping.ok) {
      initialized_ = false;
      return core::CommandResult::failure(
        core::ErrorCategory::backend_unavailable,
        "AutoCAD LT detected but mcp_dispatch.lsp did not answer ping. "
        "Load lisp-code/mcp_dispatch.lsp in AutoCAD LT and retry. Last error: " + ping.error);
    }

    initialized_ = true;
    return status();
  } catch (const std::exception& ex) {
    return core::CommandResult::failure(core::ErrorCategory::backend_unavailable, ex.what());
  }
}

core::CommandResult FileIpcBackend::status() const {
  const auto caps = capabilities();
  const std::string payload = std::string("{") +
    "\"backend\":\"file_ipc\"," +
    "\"server\":\"" + core::escape_json(core::kNativeServerName) + "\"," +
    "\"version\":\"" + core::escape_json(core::kNativeServerVersion) + "\"," +
    "\"initialized\":" + (initialized_ ? "true" : "false") + "," +
    "\"implementation_stage\":\"native_file_ipc_dispatch\"," +
    "\"hwnd\":" + std::to_string(hwnd_) + "," +
    "\"command_hwnd\":" + std::to_string(command_hwnd_) + "," +
    "\"window_title\":" + (window_title_ ? "\"" + core::escape_json(*window_title_) + "\"" : "null") + "," +
    "\"ipc_dir\":\"" + core::escape_json(transport_.ipc_dir().string()) + "\"," +
    "\"capabilities\":" + caps.to_json() + "," +
    "\"config\":" + config_.to_json() +
    "}";
  return core::CommandResult::success(payload);
}

core::CommandResult FileIpcBackend::call_tool_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) {
  if (tool_name == "system" && (operation == "status" || operation == "get_backend")) {
    return status();
  }
  if (tool_name == "system" && operation == "init") {
    return initialize();
  }
  if (!initialized_) {
    return core::CommandResult::failure(
      core::ErrorCategory::backend_unavailable,
      "file_ipc backend is not initialized");
  }
  if (tool_name == "system" && operation == "execute_lisp") {
    return execute_lisp(arguments_json);
  }

  const std::string command = normalize_command_name(tool_name, operation);
  if (command.empty()) {
    return core::not_supported(
      std::string("file_ipc.") + std::string(tool_name) + "." + std::string(operation) +
      " has no LISP dispatcher command in this native port yet");
  }
  return dispatch(command, params_from_arguments(arguments_json));
}

core::CommandResult FileIpcBackend::dispatch(
    std::string_view command,
    std::string_view params_json) {
  std::lock_guard<std::mutex> lock(dispatch_mutex_);
  const std::string request_id = make_request_id();
  try {
    transport_.write_command_atomic(request_id, command, params_json);

    if (!platform::win32::post_dispatch_trigger(command_hwnd_ != 0 ? command_hwnd_ : hwnd_)) {
      transport_.remove_request_files(request_id);
      return core::CommandResult::failure(
        core::ErrorCategory::backend_unavailable,
        "Could not post AutoCAD LT dispatch trigger");
    }

    const auto deadline = std::chrono::steady_clock::now() +
      std::chrono::milliseconds(static_cast<int>(config_.ipc_timeout_seconds * 1000.0));
    while (std::chrono::steady_clock::now() < deadline) {
      if (auto result = transport_.read_result_if_available(request_id)) {
        const auto parsed = command_result_from_ipc_json(*result);
        transport_.remove_request_files(request_id);
        return parsed;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    transport_.remove_request_files(request_id);
    return core::CommandResult::failure(
      core::ErrorCategory::timeout,
      "Timeout waiting for result (request_id=" + request_id + ")");
  } catch (const std::exception& ex) {
    transport_.remove_request_files(request_id);
    return core::CommandResult::failure(core::ErrorCategory::internal_error, ex.what());
  }
}

core::CommandResult FileIpcBackend::execute_lisp(std::string_view arguments_json) {
  if (!config_.lisp_exec_enabled) {
    return core::CommandResult::failure(
      core::ErrorCategory::not_supported,
      "execute_lisp is disabled. Set AUTOCAD_MCP_ENABLE_LISP_EXEC=1 to enable arbitrary AutoLISP execution.");
  }
  const auto data = protocol::extract_object_property(arguments_json, "data");
  const auto code = data ? protocol::extract_string_property(*data, "code") : std::nullopt;
  if (!code || code->empty()) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "data.code is required");
  }

  const std::string code_id = make_request_id();
  const auto code_file = lisp_temp_path(transport_, code_id);
  try {
    write_text_file(code_file, *code);
    const std::string params = "{\"code_file\":\"" + core::escape_json(path_to_lisp_string(code_file)) + "\"}";
    auto result = dispatch("execute-lisp", params);
    std::error_code ec;
    std::filesystem::remove(code_file, ec);
    return result;
  } catch (const std::exception& ex) {
    std::error_code ec;
    std::filesystem::remove(code_file, ec);
    return core::CommandResult::failure(core::ErrorCategory::internal_error, ex.what());
  }
}

}  // namespace autocad_mcp::backends
