#pragma once

#include <string>

namespace autocad_mcp::core {

struct DwgConverters {
  std::string accoreconsole;
  std::string oda_file_converter;

  [[nodiscard]] bool any_available() const {
    return !accoreconsole.empty() || !oda_file_converter.empty();
  }

  [[nodiscard]] std::string to_json() const;
};

struct RuntimeConfig {
  std::string requested_backend{"auto"};
  std::string selected_backend{"dxf"};
  bool lisp_exec_enabled{false};
  bool only_text_feedback{false};
  std::string ipc_dir;
  double ipc_timeout_seconds{10.0};
  std::string window_hint;
  std::string project_roots;
  DwgConverters dwg_converters;

  [[nodiscard]] std::string to_json() const;
};

RuntimeConfig load_runtime_config();

}  // namespace autocad_mcp::core
