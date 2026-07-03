#pragma once

#include "backends/BackendManager.hpp"
#include "tools/ToolRegistry.hpp"

#include <string>
#include <string_view>

namespace autocad_mcp::protocol {

class McpServer {
 public:
  McpServer();

  [[nodiscard]] std::string handle(std::string_view json_request);

 private:
  backends::BackendManager backend_manager_;
  tools::ToolRegistry tools_;
};

}  // namespace autocad_mcp::protocol
