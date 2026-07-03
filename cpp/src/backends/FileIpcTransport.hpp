#pragma once

#include <chrono>
#include <filesystem>
#include <optional>
#include <string>

namespace autocad_mcp::backends {

struct FileIpcPaths {
  std::filesystem::path command_file;
  std::filesystem::path result_file;
  std::filesystem::path temp_file;
};

class FileIpcTransport {
 public:
  explicit FileIpcTransport(std::filesystem::path ipc_dir);

  [[nodiscard]] const std::filesystem::path& ipc_dir() const { return ipc_dir_; }
  [[nodiscard]] FileIpcPaths paths_for(std::string_view request_id) const;

  void ensure_directory() const;
  void cleanup_stale_files(std::chrono::seconds max_age) const;
  void write_command_atomic(std::string_view request_id, std::string_view command, std::string_view params_json) const;
  [[nodiscard]] std::optional<std::string> read_result_if_available(std::string_view request_id) const;
  void remove_request_files(std::string_view request_id) const;

 private:
  std::filesystem::path ipc_dir_;
};

}  // namespace autocad_mcp::backends
