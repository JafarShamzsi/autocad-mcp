#include "protocol/JsonRpc.hpp"

#include "core/CommandResult.hpp"

#include <cctype>
#include <cstdlib>

namespace autocad_mcp::protocol {
namespace {

void skip_ws(std::string_view s, std::size_t& pos) {
  while (pos < s.size() && std::isspace(static_cast<unsigned char>(s[pos])) != 0) {
    ++pos;
  }
}

std::optional<std::size_t> find_property_value(std::string_view json, std::string_view key) {
  const std::string needle = "\"" + std::string(key) + "\"";
  std::size_t pos = json.find(needle);
  if (pos == std::string_view::npos) {
    return std::nullopt;
  }
  pos = json.find(':', pos + needle.size());
  if (pos == std::string_view::npos) {
    return std::nullopt;
  }
  ++pos;
  skip_ws(json, pos);
  return pos;
}

std::size_t scan_json_string(std::string_view json, std::size_t pos) {
  ++pos;
  bool escaped = false;
  while (pos < json.size()) {
    const char ch = json[pos++];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch == '\\') {
      escaped = true;
      continue;
    }
    if (ch == '"') {
      return pos;
    }
  }
  return std::string_view::npos;
}

std::size_t scan_json_value(std::string_view json, std::size_t pos) {
  if (pos >= json.size()) {
    return std::string_view::npos;
  }
  if (json[pos] == '"') {
    return scan_json_string(json, pos);
  }
  if (json[pos] == '{' || json[pos] == '[') {
    const char open = json[pos];
    const char close = open == '{' ? '}' : ']';
    int depth = 0;
    bool in_string = false;
    bool escaped = false;
    for (; pos < json.size(); ++pos) {
      const char ch = json[pos];
      if (in_string) {
        if (escaped) {
          escaped = false;
        } else if (ch == '\\') {
          escaped = true;
        } else if (ch == '"') {
          in_string = false;
        }
        continue;
      }
      if (ch == '"') {
        in_string = true;
      } else if (ch == open) {
        ++depth;
      } else if (ch == close) {
        --depth;
        if (depth == 0) {
          return pos + 1;
        }
      }
    }
    return std::string_view::npos;
  }
  while (pos < json.size() && json[pos] != ',' && json[pos] != '}' && json[pos] != ']') {
    ++pos;
  }
  return pos;
}

std::string trim_copy(std::string_view value) {
  std::size_t begin = 0;
  std::size_t end = value.size();
  while (begin < end && std::isspace(static_cast<unsigned char>(value[begin])) != 0) {
    ++begin;
  }
  while (end > begin && std::isspace(static_cast<unsigned char>(value[end - 1])) != 0) {
    --end;
  }
  return std::string(value.substr(begin, end - begin));
}

std::optional<std::string> unquote_json_string(std::string_view value) {
  if (value.size() < 2 || value.front() != '"' || value.back() != '"') {
    return std::nullopt;
  }
  std::string out;
  out.reserve(value.size() - 2);
  bool escaped = false;
  for (std::size_t i = 1; i + 1 < value.size(); ++i) {
    const char ch = value[i];
    if (!escaped) {
      if (ch == '\\') {
        escaped = true;
      } else {
        out += ch;
      }
      continue;
    }
    escaped = false;
    switch (ch) {
      case '"': out += '"'; break;
      case '\\': out += '\\'; break;
      case '/': out += '/'; break;
      case 'b': out += '\b'; break;
      case 'f': out += '\f'; break;
      case 'n': out += '\n'; break;
      case 'r': out += '\r'; break;
      case 't': out += '\t'; break;
      default: out += ch; break;
    }
  }
  return out;
}

}  // namespace

std::optional<std::string> extract_string_property(std::string_view json, std::string_view key) {
  const auto value_pos = find_property_value(json, key);
  if (!value_pos || *value_pos >= json.size() || json[*value_pos] != '"') {
    return std::nullopt;
  }
  const std::size_t end = scan_json_string(json, *value_pos);
  if (end == std::string_view::npos) {
    return std::nullopt;
  }
  return unquote_json_string(json.substr(*value_pos, end - *value_pos));
}

std::optional<std::string> extract_object_property(std::string_view json, std::string_view key) {
  const auto value_pos = find_property_value(json, key);
  if (!value_pos) {
    return std::nullopt;
  }
  const std::size_t end = scan_json_value(json, *value_pos);
  if (end == std::string_view::npos) {
    return std::nullopt;
  }
  return trim_copy(json.substr(*value_pos, end - *value_pos));
}

std::optional<bool> extract_bool_property(std::string_view json, std::string_view key) {
  const auto value_pos = find_property_value(json, key);
  if (!value_pos) {
    return std::nullopt;
  }
  if (json.substr(*value_pos, 4) == "true") {
    return true;
  }
  if (json.substr(*value_pos, 5) == "false") {
    return false;
  }
  return std::nullopt;
}

std::optional<double> extract_number_property(std::string_view json, std::string_view key) {
  const auto value_pos = find_property_value(json, key);
  if (!value_pos) {
    return std::nullopt;
  }
  const std::size_t end = scan_json_value(json, *value_pos);
  if (end == std::string_view::npos) {
    return std::nullopt;
  }
  const std::string text = trim_copy(json.substr(*value_pos, end - *value_pos));
  char* parsed_end = nullptr;
  const double value = std::strtod(text.c_str(), &parsed_end);
  if (parsed_end == text.c_str()) {
    return std::nullopt;
  }
  return value;
}

std::optional<JsonRpcRequest> parse_request(std::string_view json) {
  auto method = extract_string_property(json, "method");
  if (!method) {
    return std::nullopt;
  }
  JsonRpcRequest request;
  request.method = *method;
  if (auto id = extract_object_property(json, "id")) {
    request.id_json = *id;
  }
  if (auto params = extract_object_property(json, "params")) {
    request.params_json = *params;
  }
  return request;
}

std::string make_result_response(std::string_view id_json, std::string_view result_json) {
  return "{\"jsonrpc\":\"2.0\",\"id\":" + std::string(id_json) +
    ",\"result\":" + std::string(result_json) + "}";
}

std::string make_error_response(std::string_view id_json, int code, std::string_view message) {
  return "{\"jsonrpc\":\"2.0\",\"id\":" + std::string(id_json) +
    ",\"error\":{\"code\":" + std::to_string(code) +
    ",\"message\":\"" + autocad_mcp::core::escape_json(message) + "\"}}";
}

}  // namespace autocad_mcp::protocol
