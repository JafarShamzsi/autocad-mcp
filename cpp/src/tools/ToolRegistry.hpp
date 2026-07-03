#pragma once

#include "core/CommandResult.hpp"

#include <functional>
#include <string>
#include <string_view>
#include <vector>

namespace autocad_mcp::tools {

struct ToolSpec {
  std::string name;
  std::string title;
  std::string description;
  bool read_only_hint{false};
  std::vector<std::string> operations;
};

class ToolRegistry {
 public:
  ToolRegistry();

  [[nodiscard]] std::string tools_list_json() const;
  [[nodiscard]] core::CommandResult call(
    std::string_view tool_name,
    std::string_view arguments_json,
    const std::function<core::CommandResult(std::string_view, std::string_view, std::string_view)>& backend_call) const;
  [[nodiscard]] const std::vector<ToolSpec>& specs() const { return specs_; }

 private:
  std::vector<ToolSpec> specs_;
};

}  // namespace autocad_mcp::tools
