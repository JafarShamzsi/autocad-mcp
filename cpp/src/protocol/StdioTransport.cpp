#include "protocol/StdioTransport.hpp"

#include <cctype>
#include <cstdlib>
#include <iostream>
#include <string>

namespace autocad_mcp::protocol {

StdioTransport::StdioTransport(std::istream& input, std::ostream& output)
  : input_(input), output_(output) {}

std::optional<std::string> StdioTransport::read_message() {
  std::string line;
  std::size_t content_length = 0;

  while (std::getline(input_, line)) {
    if (!line.empty() && line.back() == '\r') {
      line.pop_back();
    }
    if (line.empty()) {
      break;
    }
    constexpr std::string_view kHeader = "Content-Length:";
    if (line.rfind(kHeader, 0) == 0) {
      std::string value = line.substr(kHeader.size());
      while (!value.empty() && std::isspace(static_cast<unsigned char>(value.front())) != 0) {
        value.erase(value.begin());
      }
      content_length = static_cast<std::size_t>(std::strtoull(value.c_str(), nullptr, 10));
    }
  }

  if (content_length == 0) {
    return std::nullopt;
  }

  std::string payload(content_length, '\0');
  input_.read(payload.data(), static_cast<std::streamsize>(payload.size()));
  if (input_.gcount() != static_cast<std::streamsize>(payload.size())) {
    return std::nullopt;
  }
  return payload;
}

void StdioTransport::write_message(std::string_view payload) {
  output_ << "Content-Length: " << payload.size() << "\r\n\r\n";
  output_ << payload;
  output_.flush();
}

}  // namespace autocad_mcp::protocol

