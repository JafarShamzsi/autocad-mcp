#pragma once

#include "core/CommandResult.hpp"

#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace autocad_mcp::backends {

struct DxfPoint {
  double x{0.0};
  double y{0.0};
};

struct NativeDxfEntity {
  std::string handle;
  std::string type;
  std::string layer{"0"};
  std::string text;
  std::vector<DxfPoint> points;
  double radius{0.0};
  double width{0.0};
  double height{0.0};
  double rotation{0.0};
  double start_angle{0.0};
  double end_angle{0.0};
  double major_x{0.0};
  double major_y{0.0};
  double ratio{1.0};
  bool closed{false};
  std::string block_name;
  double scale_x{1.0};
  double scale_y{1.0};
  std::map<std::string, std::string> attributes;
};

struct NativeDxfLayer {
  std::string name;
  int color{7};
  std::string linetype{"CONTINUOUS"};
  bool frozen{false};
  bool locked{false};
};

struct NativeDxfBlockDefinition {
  std::string name;
  std::vector<NativeDxfEntity> entities;
};

class NativeDxfDocument {
 public:
  NativeDxfDocument();

  void reset(std::string name = "Drawing1");
  [[nodiscard]] core::CommandResult open(const std::filesystem::path& path);
  [[nodiscard]] core::CommandResult save(const std::optional<std::filesystem::path>& path);
  [[nodiscard]] core::CommandResult info() const;

  [[nodiscard]] core::CommandResult create_line(double x1, double y1, double x2, double y2, std::string layer);
  [[nodiscard]] core::CommandResult create_circle(double cx, double cy, double radius, std::string layer);
  [[nodiscard]] core::CommandResult create_polyline(std::vector<DxfPoint> points, bool closed, std::string layer);
  [[nodiscard]] core::CommandResult create_rectangle(double x1, double y1, double x2, double y2, std::string layer);
  [[nodiscard]] core::CommandResult create_arc(double cx, double cy, double radius, double start_angle, double end_angle, std::string layer);
  [[nodiscard]] core::CommandResult create_ellipse(double cx, double cy, double major_x, double major_y, double ratio, std::string layer);
  [[nodiscard]] core::CommandResult create_text(double x, double y, std::string text, double height, double rotation, std::string layer);
  [[nodiscard]] core::CommandResult create_mtext(double x, double y, double width, std::string text, double height, std::string layer);
  [[nodiscard]] core::CommandResult create_leader(std::vector<DxfPoint> points, std::string text, std::string layer);
  [[nodiscard]] core::CommandResult create_dimension_stub(std::string kind, std::vector<DxfPoint> points, std::string layer);

  [[nodiscard]] core::CommandResult list_entities(std::optional<std::string> layer, int limit) const;
  [[nodiscard]] core::CommandResult select_entities(std::optional<std::string> layer, std::optional<std::string> type, int limit) const;
  [[nodiscard]] core::CommandResult osnap(double x, double y, double tolerance, std::vector<std::string> modes) const;
  [[nodiscard]] core::CommandResult count_entities(std::optional<std::string> layer) const;
  [[nodiscard]] core::CommandResult get_entity(std::string_view handle) const;
  [[nodiscard]] core::CommandResult erase_entity(std::string_view handle);
  [[nodiscard]] core::CommandResult copy_entity(std::string_view handle, double dx, double dy);
  [[nodiscard]] core::CommandResult move_entity(std::string_view handle, double dx, double dy);
  [[nodiscard]] core::CommandResult rotate_entity(std::string_view handle, double cx, double cy, double angle_degrees);
  [[nodiscard]] core::CommandResult scale_entity(std::string_view handle, double cx, double cy, double factor);
  [[nodiscard]] core::CommandResult mirror_entity(std::string_view handle, double x1, double y1, double x2, double y2);
  [[nodiscard]] core::CommandResult offset_entity(std::string_view handle, double distance);
  [[nodiscard]] core::CommandResult array_entity(std::string_view handle, int rows, int cols, double row_dist, double col_dist);

  [[nodiscard]] core::CommandResult list_layers() const;
  [[nodiscard]] core::CommandResult create_layer(std::string name, int color = 7, std::string linetype = "CONTINUOUS");
  [[nodiscard]] core::CommandResult set_current_layer(std::string name);
  [[nodiscard]] core::CommandResult set_layer_properties(std::string name, std::optional<int> color, std::optional<std::string> linetype);
  [[nodiscard]] core::CommandResult set_layer_frozen(std::string name, bool frozen);
  [[nodiscard]] core::CommandResult set_layer_locked(std::string name, bool locked);

  [[nodiscard]] core::CommandResult list_blocks() const;
  [[nodiscard]] core::CommandResult define_block(std::string name, std::vector<NativeDxfEntity> entities = {});
  [[nodiscard]] core::CommandResult insert_block(
      std::string name,
      double x,
      double y,
      double scale,
      double rotation,
      std::map<std::string, std::string> attributes,
      std::string layer);
  [[nodiscard]] core::CommandResult get_block_attributes(std::string_view handle) const;
  [[nodiscard]] core::CommandResult update_block_attribute(std::string_view handle, std::string tag, std::string value);

  [[nodiscard]] core::CommandResult isolate_layers(const std::vector<std::string>& names);
  [[nodiscard]] core::CommandResult remove_duplicate_entities();
  [[nodiscard]] core::CommandResult burst_inserts(const std::vector<std::string>& handles);
  [[nodiscard]] core::CommandResult text_to_mtext(const std::vector<std::string>& handles);

  [[nodiscard]] std::size_t entity_count() const { return entities_.size(); }
  [[nodiscard]] const std::string& current_layer() const { return current_layer_; }

 private:
  NativeDxfEntity& add_entity(NativeDxfEntity entity);
  void ensure_layer(std::string name);
  [[nodiscard]] std::string next_handle();
  [[nodiscard]] const NativeDxfEntity* find_entity(std::string_view handle) const;
  [[nodiscard]] NativeDxfEntity* find_entity(std::string_view handle);
  [[nodiscard]] std::string entity_json(const NativeDxfEntity& entity) const;
  [[nodiscard]] std::string layers_json() const;
  [[nodiscard]] std::string extents_json() const;

  std::string name_{"Drawing1"};
  std::optional<std::filesystem::path> save_path_;
  std::map<std::string, NativeDxfLayer> layers_;
  std::map<std::string, NativeDxfBlockDefinition> blocks_;
  std::string current_layer_{"0"};
  std::vector<NativeDxfEntity> entities_;
  unsigned long long next_handle_{0x100};
};

}  // namespace autocad_mcp::backends
