#pragma once

#include "core/BackendCapabilities.hpp"
#include "core/CommandResult.hpp"

#include <string>
#include <string_view>

namespace autocad_mcp::backends {

class IBackend {
 public:
  virtual ~IBackend() = default;

  [[nodiscard]] virtual std::string name() const = 0;
  [[nodiscard]] virtual core::BackendCapabilities capabilities() const = 0;

  virtual core::CommandResult initialize() = 0;
  [[nodiscard]] virtual core::CommandResult status() const = 0;

  virtual core::CommandResult call_tool_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) = 0;
};

}  // namespace autocad_mcp::backends

