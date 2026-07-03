#pragma once

#include <string>
#include <utility>

namespace autocad_mcp::core {

enum class ErrorCategory {
  not_supported,
  not_found,
  invalid_arg,
  backend_unavailable,
  timeout,
  internal_error,
};

inline std::string escape_json(std::string_view text) {
  std::string out;
  out.reserve(text.size() + 8);
  for (const char ch : text) {
    const auto byte = static_cast<unsigned char>(ch);
    switch (ch) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (byte < 0x20) {
          out += "\\u00";
          constexpr char kHex[] = "0123456789abcdef";
          out += kHex[(byte >> 4) & 0x0f];
          out += kHex[byte & 0x0f];
        } else {
          out += ch;
        }
    }
  }
  return out;
}

inline std::string category_name(ErrorCategory category) {
  switch (category) {
    case ErrorCategory::not_supported: return "not_supported";
    case ErrorCategory::not_found: return "not_found";
    case ErrorCategory::invalid_arg: return "invalid_arg";
    case ErrorCategory::backend_unavailable: return "backend_unavailable";
    case ErrorCategory::timeout: return "timeout";
    case ErrorCategory::internal_error: return "internal_error";
  }
  return "internal_error";
}

struct CommandResult {
  bool ok{false};
  std::string payload_json{"null"};
  std::string error;
  ErrorCategory category{ErrorCategory::internal_error};

  static CommandResult success(std::string payload = "null") {
    CommandResult result;
    result.ok = true;
    result.payload_json = std::move(payload);
    return result;
  }

  static CommandResult failure(
      ErrorCategory error_category,
      std::string message) {
    CommandResult result;
    result.ok = false;
    result.category = error_category;
    result.error = std::move(message);
    return result;
  }

  [[nodiscard]] std::string to_json() const {
    if (ok) {
      return "{\"ok\":true,\"payload\":" + payload_json + "}";
    }
    return "{\"ok\":false,\"error\":\"" + escape_json(error) +
      "\",\"category\":\"" + category_name(category) + "\"}";
  }
};

inline CommandResult not_supported(std::string_view detail) {
  std::string message = "Not supported on native backend";
  if (!detail.empty()) {
    message += ": ";
    message += detail;
  }
  return CommandResult::failure(ErrorCategory::not_supported, message);
}

}  // namespace autocad_mcp::core
