#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace autocad_mcp::platform::win32 {

struct AutoCadWindow {
  std::uintptr_t hwnd{0};
  std::uintptr_t command_hwnd{0};
  std::string title;
};

[[nodiscard]] std::optional<AutoCadWindow> find_autocad_window(std::string_view title_hint);
[[nodiscard]] std::uintptr_t find_command_child(std::uintptr_t hwnd);
[[nodiscard]] bool post_dispatch_trigger(std::uintptr_t hwnd);

}  // namespace autocad_mcp::platform::win32
