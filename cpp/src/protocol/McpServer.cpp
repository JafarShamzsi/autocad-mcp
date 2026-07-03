#include "protocol/McpServer.hpp"

#include "core/CommandResult.hpp"
#include "core/Version.hpp"
#include "protocol/JsonRpc.hpp"

namespace autocad_mcp::protocol {
namespace {

std::string server_info_json() {
  return std::string("{\"protocolVersion\":\"2024-11-05\",") +
    "\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"" +
    core::kNativeServerName + "\",\"version\":\"" +
    core::kNativeServerVersion + "\"}}";
}

std::string text_content_json(std::string_view text_json_payload) {
  return std::string("{\"content\":[{\"type\":\"text\",\"text\":\"") +
    core::escape_json(std::string(text_json_payload)) + "\"}]}";
}

}  // namespace

McpServer::McpServer() = default;

std::string McpServer::handle(std::string_view json_request) {
  const auto request = parse_request(json_request);
  if (!request) {
    return make_error_response("null", -32700, "Parse error");
  }

  if (request->method == "initialize") {
    return make_result_response(request->id_json, server_info_json());
  }
  if (request->method == "tools/list") {
    return make_result_response(request->id_json, tools_.tools_list_json());
  }
  if (request->method == "tools/call") {
    const auto tool_name = extract_string_property(request->params_json, "name");
    if (!tool_name) {
      auto result = core::CommandResult::failure(
        core::ErrorCategory::invalid_arg,
        "tools/call requires params.name");
      return make_result_response(request->id_json, text_content_json(result.to_json()));
    }
    std::string arguments = "{}";
    if (auto args = extract_object_property(request->params_json, "arguments")) {
      arguments = *args;
    }
    const auto result = tools_.call(
      *tool_name,
      arguments,
      [&](std::string_view routed_tool, std::string_view routed_operation, std::string_view routed_args) {
        return backend_manager_.backend().call_tool_operation(routed_tool, routed_operation, routed_args);
      });
    return make_result_response(request->id_json, text_content_json(result.to_json()));
  }

  return make_error_response(request->id_json, -32601, "Method not found");
}

}  // namespace autocad_mcp::protocol
