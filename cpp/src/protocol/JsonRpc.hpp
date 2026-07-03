#pragma once

#include <optional>
#include <string>
#include <string_view>

namespace autocad_mcp::protocol {

struct JsonRpcRequest {
  std::string id_json{"null"};
  std::string method;
  std::string params_json{"{}"};
};

std::optional<JsonRpcRequest> parse_request(std::string_view json);
std::string make_result_response(std::string_view id_json, std::string_view result_json);
std::string make_error_response(std::string_view id_json, int code, std::string_view message);
std::optional<std::string> extract_string_property(std::string_view json, std::string_view key);
std::optional<std::string> extract_object_property(std::string_view json, std::string_view key);
std::optional<bool> extract_bool_property(std::string_view json, std::string_view key);
std::optional<double> extract_number_property(std::string_view json, std::string_view key);

}  // namespace autocad_mcp::protocol
