#include "backends/ComBackend.hpp"

#include "core/Version.hpp"
#include "platform/win32/ComAutomation.hpp"
#include "protocol/JsonRpc.hpp"

namespace autocad_mcp::backends {

ComBackend::ComBackend(core::RuntimeConfig config)
  : config_(std::move(config)) {}

std::string ComBackend::name() const {
  return "com";
}

core::BackendCapabilities ComBackend::capabilities() const {
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

core::CommandResult ComBackend::initialize() {
  const auto probe = platform::win32::probe_autocad_application();
  application_name_ = probe.name;
  application_version_ = probe.version;
  attach_error_ = probe.error;
  initialized_ = probe.available;
  if (!probe.available) {
    return core::CommandResult::failure(core::ErrorCategory::backend_unavailable, probe.error);
  }
  return status();
}

core::CommandResult ComBackend::status() const {
  const auto caps = capabilities();
  const std::string payload = std::string("{") +
    "\"backend\":\"com\"," +
    "\"server\":\"" + core::escape_json(core::kNativeServerName) + "\"," +
    "\"version\":\"" + core::escape_json(core::kNativeServerVersion) + "\"," +
    "\"initialized\":" + (initialized_ ? "true" : "false") + "," +
    "\"implementation_stage\":\"native_com_operations\"," +
    "\"application_name\":" + (application_name_.empty() ? "null" : "\"" + core::escape_json(application_name_) + "\"") + "," +
    "\"application_version\":" + (application_version_.empty() ? "null" : "\"" + core::escape_json(application_version_) + "\"") + "," +
    "\"attach_error\":" + (attach_error_.empty() ? "null" : "\"" + core::escape_json(attach_error_) + "\"") + "," +
    "\"capabilities\":" + caps.to_json() + "," +
    "\"config\":" + config_.to_json() +
    "}";
  return core::CommandResult::success(payload);
}

core::CommandResult ComBackend::call_tool_operation(
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
      attach_error_.empty() ? "COM backend is not initialized" : attach_error_);
  }

  if (tool_name == "system" && operation == "execute_lisp" && !config_.lisp_exec_enabled) {
    return core::CommandResult::failure(
      core::ErrorCategory::not_supported,
      "COM execute_lisp is disabled; set AUTOCAD_MCP_ENABLE_LISP_EXEC=1 to allow command execution");
  }

  return platform::win32::execute_autocad_com_operation(tool_name, operation, arguments_json);
}

}  // namespace autocad_mcp::backends
