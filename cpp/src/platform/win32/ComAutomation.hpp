#pragma once

#include "core/CommandResult.hpp"

#include <string>
#include <string_view>

namespace autocad_mcp::platform::win32 {

struct ComProbeResult {
  bool available{false};
  std::string error;
  std::string prog_id;
  std::string name;
  std::string version;
};

[[nodiscard]] ComProbeResult probe_autocad_application();
[[nodiscard]] core::CommandResult execute_autocad_com_operation(
  std::string_view tool_name,
  std::string_view operation,
  std::string_view arguments_json);

}  // namespace autocad_mcp::platform::win32
