#include "core/Version.hpp"
#include "protocol/McpServer.hpp"
#include "protocol/StdioTransport.hpp"

#include <iostream>
#include <string>

int main(int argc, char** argv) {
  if (argc > 1 && std::string(argv[1]) == "--version") {
    std::cout << autocad_mcp::core::kNativeServerName << " "
              << autocad_mcp::core::kNativeServerVersion << "\n";
    return 0;
  }

  autocad_mcp::protocol::McpServer server;
  autocad_mcp::protocol::StdioTransport transport(std::cin, std::cout);

  while (auto message = transport.read_message()) {
    transport.write_message(server.handle(*message));
  }

  return 0;
}

