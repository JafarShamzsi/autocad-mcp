#pragma once

#include <string>

namespace autocad_mcp::core {

struct BackendCapabilities {
  bool can_read_drawing{false};
  bool can_modify_entities{false};
  bool can_create_entities{true};
  bool can_screenshot{false};
  bool can_save{false};
  bool can_plot_pdf{false};
  bool can_zoom{false};
  bool can_query_entities{false};
  bool can_file_operations{false};
  bool can_undo{false};

  [[nodiscard]] std::string to_json() const {
    return std::string("{") +
      "\"can_read_drawing\":" + (can_read_drawing ? "true" : "false") + "," +
      "\"can_modify_entities\":" + (can_modify_entities ? "true" : "false") + "," +
      "\"can_create_entities\":" + (can_create_entities ? "true" : "false") + "," +
      "\"can_screenshot\":" + (can_screenshot ? "true" : "false") + "," +
      "\"can_save\":" + (can_save ? "true" : "false") + "," +
      "\"can_plot_pdf\":" + (can_plot_pdf ? "true" : "false") + "," +
      "\"can_zoom\":" + (can_zoom ? "true" : "false") + "," +
      "\"can_query_entities\":" + (can_query_entities ? "true" : "false") + "," +
      "\"can_file_operations\":" + (can_file_operations ? "true" : "false") + "," +
      "\"can_undo\":" + (can_undo ? "true" : "false") +
      "}";
  }
};

}  // namespace autocad_mcp::core

