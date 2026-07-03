#include "platform/win32/WindowMessaging.hpp"

#include <algorithm>
#include <chrono>
#include <string>
#include <thread>
#include <vector>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace autocad_mcp::platform::win32 {
namespace {

std::string lower_ascii(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
    if (ch >= 'A' && ch <= 'Z') {
      return static_cast<char>(ch - 'A' + 'a');
    }
    return static_cast<char>(ch);
  });
  return value;
}

bool contains(std::string_view haystack, std::string_view needle) {
  return haystack.find(needle) != std::string_view::npos;
}

#if defined(_WIN32)
std::string narrow_lossy(const std::wstring& value) {
  if (value.empty()) {
    return {};
  }
  const int required = WideCharToMultiByte(
    CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
  if (required <= 0) {
    return {};
  }
  std::string out(static_cast<std::size_t>(required), '\0');
  WideCharToMultiByte(
    CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), required, nullptr, nullptr);
  return out;
}

std::string window_title(HWND hwnd) {
  const int length = GetWindowTextLengthW(hwnd);
  if (length <= 0) {
    return {};
  }
  std::wstring buffer(static_cast<std::size_t>(length) + 1, L'\0');
  const int copied = GetWindowTextW(hwnd, buffer.data(), static_cast<int>(buffer.size()));
  if (copied <= 0) {
    return {};
  }
  buffer.resize(static_cast<std::size_t>(copied));
  return narrow_lossy(buffer);
}

struct EnumState {
  std::vector<AutoCadWindow> matches;
  std::string hint;
};

BOOL CALLBACK enum_windows_proc(HWND hwnd, LPARAM lparam) {
  auto* state = reinterpret_cast<EnumState*>(lparam);
  if (IsWindowVisible(hwnd) == 0) {
    return TRUE;
  }
  const std::string title = window_title(hwnd);
  const std::string title_lower = lower_ascii(title);
  if (!contains(title_lower, "autocad")) {
    return TRUE;
  }
  if (!contains(title_lower, "drawing") && !contains(title_lower, ".dwg")) {
    return TRUE;
  }
  if (!state->hint.empty() && !contains(title_lower, state->hint)) {
    return TRUE;
  }
  state->matches.push_back({
    reinterpret_cast<std::uintptr_t>(hwnd),
    0,
    title,
  });
  return TRUE;
}

struct ChildState {
  HWND found{nullptr};
};

BOOL CALLBACK enum_child_proc(HWND child, LPARAM lparam) {
  auto* state = reinterpret_cast<ChildState*>(lparam);
  wchar_t class_name[128]{};
  if (GetClassNameW(child, class_name, static_cast<int>(std::size(class_name))) <= 0) {
    return TRUE;
  }
  if (std::wstring_view(class_name) == L"MDIClient") {
    state->found = child;
    return FALSE;
  }
  return TRUE;
}
#endif

}  // namespace

std::optional<AutoCadWindow> find_autocad_window(std::string_view title_hint) {
#if defined(_WIN32)
  EnumState state;
  state.hint = lower_ascii(std::string(title_hint));
  EnumWindows(enum_windows_proc, reinterpret_cast<LPARAM>(&state));
  if (state.matches.empty()) {
    return std::nullopt;
  }

  const auto foreground = reinterpret_cast<std::uintptr_t>(GetForegroundWindow());
  auto foreground_match = std::find_if(state.matches.begin(), state.matches.end(), [&](const AutoCadWindow& item) {
    return item.hwnd == foreground;
  });
  if (foreground_match != state.matches.end()) {
    foreground_match->command_hwnd = find_command_child(foreground_match->hwnd);
    return *foreground_match;
  }

  std::sort(state.matches.begin(), state.matches.end(), [](const AutoCadWindow& a, const AutoCadWindow& b) {
    return a.hwnd < b.hwnd;
  });
  state.matches.front().command_hwnd = find_command_child(state.matches.front().hwnd);
  return state.matches.front();
#else
  (void)title_hint;
  return std::nullopt;
#endif
}

std::uintptr_t find_command_child(std::uintptr_t hwnd) {
#if defined(_WIN32)
  if (hwnd == 0) {
    return 0;
  }
  ChildState state;
  EnumChildWindows(reinterpret_cast<HWND>(hwnd), enum_child_proc, reinterpret_cast<LPARAM>(&state));
  return reinterpret_cast<std::uintptr_t>(state.found);
#else
  (void)hwnd;
  return 0;
#endif
}

bool post_dispatch_trigger(std::uintptr_t hwnd) {
#if defined(_WIN32)
  if (hwnd == 0) {
    return false;
  }
  auto* target = reinterpret_cast<HWND>(hwnd);
  constexpr UINT kWmChar = 0x0102;
  constexpr UINT kWmKeyDown = 0x0100;
  constexpr UINT kWmKeyUp = 0x0101;
  constexpr WPARAM kEscape = 0x1B;
  for (int i = 0; i < 2; ++i) {
    PostMessageW(target, kWmKeyDown, kEscape, 0);
    PostMessageW(target, kWmKeyUp, kEscape, 0);
  }
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  for (const char ch : std::string_view("(c:mcp-dispatch)")) {
    PostMessageW(target, kWmChar, static_cast<WPARAM>(static_cast<unsigned char>(ch)), 0);
  }
  PostMessageW(target, kWmChar, 0x0D, 0);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  return true;
#else
  (void)hwnd;
  return false;
#endif
}

}  // namespace autocad_mcp::platform::win32
