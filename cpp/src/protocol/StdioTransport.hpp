#pragma once

#include <iosfwd>
#include <optional>
#include <string>
#include <string_view>

namespace autocad_mcp::protocol {

class StdioTransport {
 public:
  StdioTransport(std::istream& input, std::ostream& output);

  [[nodiscard]] std::optional<std::string> read_message();
  void write_message(std::string_view payload);

 private:
  std::istream& input_;
  std::ostream& output_;
};

}  // namespace autocad_mcp::protocol

