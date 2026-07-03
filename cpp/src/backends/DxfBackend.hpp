#pragma once

#include "backends/IBackend.hpp"
#include "backends/NativeDxfDocument.hpp"
#include "core/Config.hpp"

#include <map>
#include <string>
#include <vector>

namespace autocad_mcp::backends {

struct NativeNamedView {
  double x1{0.0};
  double y1{0.0};
  double x2{0.0};
  double y2{0.0};
};

class DxfBackend final : public IBackend {
 public:
  explicit DxfBackend(core::RuntimeConfig config);

  [[nodiscard]] std::string name() const override;
  [[nodiscard]] core::BackendCapabilities capabilities() const override;

  core::CommandResult initialize() override;
  [[nodiscard]] core::CommandResult status() const override;

  core::CommandResult call_tool_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) override;

 private:
  core::RuntimeConfig config_;
  NativeDxfDocument document_;
  std::map<std::string, std::string> dimstyles_;
  std::map<std::string, std::string> textstyles_;
  std::string current_dimstyle_{"Standard"};
  std::string current_textstyle_{"Standard"};
  std::map<std::string, NativeNamedView> named_views_;
  std::map<std::string, std::string> layer_states_;
  std::vector<std::string> events_;
  std::string project_path_;
  std::size_t event_cursor_{0};
  bool recording_events_{false};
  bool initialized_{false};
};

}  // namespace autocad_mcp::backends
