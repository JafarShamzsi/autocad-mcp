#pragma once

#include "backends/IBackend.hpp"
#include "core/Config.hpp"

namespace autocad_mcp::backends {

class ComBackend final : public IBackend {
 public:
  explicit ComBackend(core::RuntimeConfig config);

  [[nodiscard]] std::string name() const override;
  [[nodiscard]] core::BackendCapabilities capabilities() const override;

  core::CommandResult initialize() override;
  [[nodiscard]] core::CommandResult status() const override;

  core::CommandResult call_tool_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) override;

 private:
  core::RuntimeConfig config_;
  std::string application_name_;
  std::string application_version_;
  std::string attach_error_;
  bool initialized_{false};
};

}  // namespace autocad_mcp::backends
