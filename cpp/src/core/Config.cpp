#include "core/Config.hpp"

#include "core/CommandResult.hpp"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

namespace autocad_mcp::core {
namespace {

std::string env_string(const char* name) {
#ifdef _MSC_VER
  char* raw = nullptr;
  std::size_t size = 0;
  if (_dupenv_s(&raw, &size, name) != 0 || raw == nullptr) {
    return {};
  }
  std::string value(raw, size > 0 ? size - 1 : 0);
  std::free(raw);
  return value;
#else
  const char* value = std::getenv(name);
  return value == nullptr ? std::string{} : std::string(value);
#endif
}

std::string lower_copy(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
    return static_cast<char>(std::tolower(ch));
  });
  return value;
}

bool env_truthy(const char* name) {
  const std::string value = lower_copy(env_string(name));
  return value == "1" || value == "true" || value == "yes";
}

double env_timeout() {
  const std::string value = env_string("AUTOCAD_MCP_IPC_TIMEOUT");
  if (value.empty()) {
    return 10.0;
  }
  try {
    const double parsed = std::stod(value);
    if (parsed < 1.0) {
      return 1.0;
    }
    if (parsed > 300.0) {
      return 300.0;
    }
    return parsed;
  } catch (...) {
    return 10.0;
  }
}

std::string default_ipc_dir() {
  if (const auto temp = env_string("TEMP"); !temp.empty()) {
    return (std::filesystem::path(temp) / "autocad-mcp").string();
  }
  if (const auto tmp = env_string("TMP"); !tmp.empty()) {
    return (std::filesystem::path(tmp) / "autocad-mcp").string();
  }
  return "C:\\temp\\autocad-mcp";
}

std::string first_existing_file(const std::vector<std::filesystem::path>& candidates) {
  for (const auto& candidate : candidates) {
    std::error_code ec;
    if (std::filesystem::is_regular_file(candidate, ec)) {
      return candidate.string();
    }
  }
  return {};
}

std::string find_accoreconsole() {
  const std::string explicit_path = env_string("AUTOCAD_MCP_ACCORECONSOLE");
  if (!explicit_path.empty()) {
    std::error_code ec;
    if (std::filesystem::is_regular_file(explicit_path, ec)) {
      return explicit_path;
    }
  }

  std::vector<std::filesystem::path> candidates;
  for (int year = 2028; year >= 2018; --year) {
    candidates.emplace_back(
      "C:\\Program Files\\Autodesk\\AutoCAD " + std::to_string(year) + "\\accoreconsole.exe");
    candidates.emplace_back(
      "C:\\Program Files (x86)\\Autodesk\\AutoCAD " + std::to_string(year) + "\\accoreconsole.exe");
    candidates.emplace_back(
      "D:\\Program Files\\Autodesk\\AutoCAD " + std::to_string(year) + "\\accoreconsole.exe");
  }
  return first_existing_file(candidates);
}

std::string find_oda_file_converter() {
  std::vector<std::filesystem::path> candidates;
  for (const auto& root : {
         std::filesystem::path("C:\\Program Files\\ODA"),
         std::filesystem::path("C:\\Program Files (x86)\\ODA"),
       }) {
    std::error_code ec;
    if (!std::filesystem::is_directory(root, ec)) {
      continue;
    }
    for (const auto& entry : std::filesystem::directory_iterator(root, ec)) {
      if (ec || !entry.is_directory()) {
        continue;
      }
      candidates.emplace_back(entry.path() / "ODAFileConverter.exe");
      candidates.emplace_back(entry.path() / "ODAFileConverter");
    }
  }
  return first_existing_file(candidates);
}

std::string normalize_backend(std::string requested) {
  requested = lower_copy(std::move(requested));
  if (requested.empty()) {
    return "auto";
  }
  if (requested == "ezdxf") {
    return "dxf";
  }
  if (requested == "auto" || requested == "com" || requested == "file_ipc" || requested == "dxf") {
    return requested;
  }
  return "auto";
}

std::string select_backend(const std::string& requested) {
  if (requested != "auto") {
    return requested;
  }
  // Milestone 1 has no live backend attach yet. Keep the native fallback
  // deterministic while later milestones add COM and File IPC probes.
  return "dxf";
}

std::string json_quote(std::string_view value) {
  return "\"" + escape_json(value) + "\"";
}

}  // namespace

std::string DwgConverters::to_json() const {
  return std::string("{") +
    "\"accoreconsole\":" + (accoreconsole.empty() ? "null" : json_quote(accoreconsole)) + "," +
    "\"oda_file_converter\":" + (oda_file_converter.empty() ? "null" : json_quote(oda_file_converter)) +
    "}";
}

std::string RuntimeConfig::to_json() const {
  return std::string("{") +
    "\"requested_backend\":" + json_quote(requested_backend) + "," +
    "\"selected_backend\":" + json_quote(selected_backend) + "," +
    "\"lisp_exec_enabled\":" + (lisp_exec_enabled ? "true" : "false") + "," +
    "\"only_text_feedback\":" + (only_text_feedback ? "true" : "false") + "," +
    "\"ipc_dir\":" + json_quote(ipc_dir) + "," +
    "\"ipc_timeout_seconds\":" + std::to_string(ipc_timeout_seconds) + "," +
    "\"window_hint\":" + (window_hint.empty() ? "null" : json_quote(window_hint)) + "," +
    "\"project_roots\":" + (project_roots.empty() ? "null" : json_quote(project_roots)) + "," +
    "\"dwg_converters\":" + dwg_converters.to_json() +
    "}";
}

RuntimeConfig load_runtime_config() {
  RuntimeConfig config;
  config.requested_backend = normalize_backend(env_string("AUTOCAD_MCP_BACKEND"));
  config.selected_backend = select_backend(config.requested_backend);
  config.lisp_exec_enabled = env_truthy("AUTOCAD_MCP_ENABLE_LISP_EXEC");
  config.only_text_feedback = env_truthy("AUTOCAD_MCP_ONLY_TEXT");
  config.ipc_dir = env_string("AUTOCAD_MCP_IPC_DIR");
  if (config.ipc_dir.empty()) {
    config.ipc_dir = default_ipc_dir();
  }
  config.ipc_timeout_seconds = env_timeout();
  config.window_hint = env_string("AUTOCAD_MCP_WINDOW_HINT");
  config.project_roots = env_string("AUTOCAD_MCP_PROJECT_ROOTS");
  config.dwg_converters.accoreconsole = find_accoreconsole();
  config.dwg_converters.oda_file_converter = find_oda_file_converter();
  return config;
}

}  // namespace autocad_mcp::core
