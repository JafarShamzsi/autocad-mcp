#pragma once

#include "backends/FileIpcTransport.hpp"
#include "backends/IBackend.hpp"
#include "core/Config.hpp"

#include <cstdint>
#include <mutex>
#include <optional>

namespace autocad_mcp::backends {

class FileIpcBackend final : public IBackend {
 public:
  explicit FileIpcBackend(core::RuntimeConfig config);

  [[nodiscard]] std::string name() const override;
  [[nodiscard]] core::BackendCapabilities capabilities() const override;

  core::CommandResult initialize() override;
  [[nodiscard]] core::CommandResult status() const override;

  core::CommandResult call_tool_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) override;

 private:
  [[nodiscard]] core::CommandResult dispatch(
    std::string_view command,
    std::string_view params_json);
  [[nodiscard]] core::CommandResult execute_lisp(std::string_view arguments_json);

  core::RuntimeConfig config_;
  FileIpcTransport transport_;
  std::optional<std::string> window_title_;
  std::uintptr_t hwnd_{0};
  std::uintptr_t command_hwnd_{0};
  std::mutex dispatch_mutex_;
  bool initialized_{false};
};

}  // namespace autocad_mcp::backends
