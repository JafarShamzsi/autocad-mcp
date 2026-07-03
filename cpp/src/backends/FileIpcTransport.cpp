#include "backends/FileIpcTransport.hpp"

#include "core/CommandResult.hpp"
#include "protocol/JsonRpc.hpp"

#include <chrono>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <system_error>

namespace autocad_mcp::backends {
namespace {

std::string command_payload_json(std::string_view request_id, std::string_view command, std::string_view params_json) {
  const auto now = std::chrono::system_clock::now().time_since_epoch();
  const auto seconds = std::chrono::duration<double>(now).count();
  std::ostringstream timestamp;
  timestamp << std::fixed << std::setprecision(6) << seconds;
  return std::string("{") +
    "\"request_id\":\"" + core::escape_json(request_id) + "\"," +
    "\"command\":\"" + core::escape_json(command) + "\"," +
    "\"params\":" + (params_json.empty() ? "{}" : std::string(params_json)) + "," +
    "\"ts\":" + timestamp.str() +
    "}";
}

std::string read_all_text(const std::filesystem::path& path) {
  std::ifstream input(path, std::ios::binary);
  return std::string(
    std::istreambuf_iterator<char>(input),
    std::istreambuf_iterator<char>());
}

bool file_stem_has_ipc_prefix(const std::filesystem::path& path) {
  const auto name = path.filename().string();
  return name.rfind("autocad_mcp_cmd_", 0) == 0 ||
    name.rfind("autocad_mcp_result_", 0) == 0 ||
    name.rfind("autocad_mcp_lisp_", 0) == 0;
}

}  // namespace

FileIpcTransport::FileIpcTransport(std::filesystem::path ipc_dir)
  : ipc_dir_(std::move(ipc_dir)) {}

FileIpcPaths FileIpcTransport::paths_for(std::string_view request_id) const {
  const std::string id(request_id);
  FileIpcPaths paths;
  paths.command_file = ipc_dir_ / ("autocad_mcp_cmd_" + id + ".json");
  paths.result_file = ipc_dir_ / ("autocad_mcp_result_" + id + ".json");
  paths.temp_file = paths.command_file;
  paths.temp_file.replace_extension(".tmp");
  return paths;
}

void FileIpcTransport::ensure_directory() const {
  std::error_code ec;
  std::filesystem::create_directories(ipc_dir_, ec);
  if (ec) {
    throw std::runtime_error("Failed to create IPC directory: " + ec.message());
  }
}

void FileIpcTransport::cleanup_stale_files(std::chrono::seconds max_age) const {
  std::error_code ec;
  if (!std::filesystem::is_directory(ipc_dir_, ec)) {
    return;
  }

  const auto now = std::filesystem::file_time_type::clock::now();
  for (const auto& entry : std::filesystem::directory_iterator(ipc_dir_, ec)) {
    if (ec || !entry.is_regular_file() || !file_stem_has_ipc_prefix(entry.path())) {
      continue;
    }
    const auto modified = entry.last_write_time(ec);
    if (ec) {
      continue;
    }
    if (now - modified > max_age) {
      std::filesystem::remove(entry.path(), ec);
      ec.clear();
    }
  }
}

void FileIpcTransport::write_command_atomic(
    std::string_view request_id,
    std::string_view command,
    std::string_view params_json) const {
  ensure_directory();
  const auto paths = paths_for(request_id);
  {
    std::ofstream output(paths.temp_file, std::ios::binary | std::ios::trunc);
    if (!output) {
      throw std::runtime_error("Failed to open IPC temp command file");
    }
    const auto payload = command_payload_json(request_id, command, params_json);
    output.write(payload.data(), static_cast<std::streamsize>(payload.size()));
    output.flush();
    if (!output) {
      throw std::runtime_error("Failed to write IPC temp command file");
    }
  }

  std::error_code ec;
  std::filesystem::rename(paths.temp_file, paths.command_file, ec);
  if (ec) {
    std::filesystem::remove(paths.command_file, ec);
    ec.clear();
    std::filesystem::rename(paths.temp_file, paths.command_file, ec);
  }
  if (ec) {
    throw std::runtime_error("Failed to publish IPC command file: " + ec.message());
  }
}

std::optional<std::string> FileIpcTransport::read_result_if_available(std::string_view request_id) const {
  const auto paths = paths_for(request_id);
  std::error_code ec;
  if (!std::filesystem::is_regular_file(paths.result_file, ec)) {
    return std::nullopt;
  }
  const auto text = read_all_text(paths.result_file);
  const auto parsed_id = protocol::extract_string_property(text, "request_id");
  if (!parsed_id || *parsed_id != request_id) {
    return std::nullopt;
  }
  return text;
}

void FileIpcTransport::remove_request_files(std::string_view request_id) const {
  const auto paths = paths_for(request_id);
  std::error_code ec;
  std::filesystem::remove(paths.command_file, ec);
  ec.clear();
  std::filesystem::remove(paths.result_file, ec);
  ec.clear();
  std::filesystem::remove(paths.temp_file, ec);
}

}  // namespace autocad_mcp::backends
