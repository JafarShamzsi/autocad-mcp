#include "backends/NativeDxfDocument.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace autocad_mcp::backends {
namespace {
constexpr double kPi = 3.14159265358979323846;


std::string json_string(std::string_view value) {
  return "\"" + core::escape_json(value) + "\"";
}

std::string number_json(double value) {
  if (std::fabs(value) < 1e-12) {
    value = 0.0;
  }
  std::ostringstream out;
  out << std::setprecision(12) << value;
  return out.str();
}

std::string point_json(const DxfPoint& point) {
  return "[" + number_json(point.x) + "," + number_json(point.y) + "]";
}

std::string points_json(const std::vector<DxfPoint>& points) {
  std::string out = "[";
  for (std::size_t i = 0; i < points.size(); ++i) {
    if (i != 0) {
      out += ",";
    }
    out += point_json(points[i]);
  }
  out += "]";
  return out;
}

std::string handle_upper(unsigned long long value) {
  std::ostringstream out;
  out << std::hex << std::uppercase << value;
  return out.str();
}

std::string upper_copy(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
    return static_cast<char>(std::toupper(ch));
  });
  return value;
}

DxfPoint rotate_point(DxfPoint point, double cx, double cy, double radians) {
  const double dx = point.x - cx;
  const double dy = point.y - cy;
  const double c = std::cos(radians);
  const double s = std::sin(radians);
  return {cx + dx * c - dy * s, cy + dx * s + dy * c};
}

DxfPoint scale_point(DxfPoint point, double cx, double cy, double factor) {
  return {cx + (point.x - cx) * factor, cy + (point.y - cy) * factor};
}

DxfPoint mirror_point(DxfPoint point, double x1, double y1, double x2, double y2) {
  const double dx = x2 - x1;
  const double dy = y2 - y1;
  const double length_sq = dx * dx + dy * dy;
  if (length_sq <= 0.0) {
    return point;
  }
  const double t = ((point.x - x1) * dx + (point.y - y1) * dy) / length_sq;
  const double proj_x = x1 + t * dx;
  const double proj_y = y1 + t * dy;
  return {2.0 * proj_x - point.x, 2.0 * proj_y - point.y};
}

DxfPoint polar_point(double x, double y, double distance, double angle_degrees) {
  const double radians = angle_degrees * kPi / 180.0;
  return {x + std::cos(radians) * distance, y + std::sin(radians) * distance};
}

void write_pair(std::ostream& output, int code, std::string_view value) {
  output << code << "\n" << value << "\n";
}

void write_pair(std::ostream& output, int code, double value) {
  output << code << "\n" << number_json(value) << "\n";
}

void write_entity_record(std::ostream& output, const NativeDxfEntity& entity, bool include_handle = true) {
  write_pair(output, 0, entity.type);
  if (include_handle && !entity.handle.empty()) {
    write_pair(output, 5, entity.handle);
  }
  write_pair(output, 8, entity.layer.empty() ? "0" : entity.layer);
  if (entity.type == "LINE" && entity.points.size() >= 2) {
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 11, entity.points[1].x);
    write_pair(output, 21, entity.points[1].y);
  } else if (entity.type == "CIRCLE" && !entity.points.empty()) {
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 40, entity.radius);
  } else if (entity.type == "ARC" && !entity.points.empty()) {
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 40, entity.radius);
    write_pair(output, 50, entity.start_angle);
    write_pair(output, 51, entity.end_angle);
  } else if (entity.type == "ELLIPSE" && !entity.points.empty()) {
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 11, entity.major_x);
    write_pair(output, 21, entity.major_y);
    write_pair(output, 31, "0");
    write_pair(output, 40, entity.ratio);
    write_pair(output, 41, "0");
    write_pair(output, 42, "6.28318530718");
  } else if (entity.type == "TEXT" && !entity.points.empty()) {
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 40, entity.height);
    write_pair(output, 1, entity.text);
    write_pair(output, 50, entity.rotation);
  } else if (entity.type == "MTEXT" && !entity.points.empty()) {
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 40, entity.height);
    write_pair(output, 41, entity.width);
    write_pair(output, 1, entity.text);
  } else if (entity.type == "LWPOLYLINE") {
    write_pair(output, 90, std::to_string(entity.points.size()));
    write_pair(output, 70, entity.closed ? "1" : "0");
    for (const auto& point : entity.points) {
      write_pair(output, 10, point.x);
      write_pair(output, 20, point.y);
    }
  } else if (entity.type == "INSERT" && !entity.points.empty()) {
    write_pair(output, 2, entity.block_name);
    write_pair(output, 10, entity.points[0].x);
    write_pair(output, 20, entity.points[0].y);
    write_pair(output, 41, entity.scale_x);
    write_pair(output, 42, entity.scale_y);
    write_pair(output, 50, entity.rotation);
    if (!entity.attributes.empty()) {
      write_pair(output, 66, "1");
      for (const auto& [tag, value] : entity.attributes) {
        write_pair(output, 0, "ATTRIB");
        write_pair(output, 8, entity.layer.empty() ? "0" : entity.layer);
        write_pair(output, 2, tag);
        write_pair(output, 1, value);
        write_pair(output, 10, entity.points[0].x);
        write_pair(output, 20, entity.points[0].y);
        write_pair(output, 40, entity.height > 0.0 ? entity.height : 2.5);
      }
      write_pair(output, 0, "SEQEND");
    }
  }
}

std::vector<std::pair<int, std::string>> read_pairs(const std::filesystem::path& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("Could not open DXF file");
  }
  std::vector<std::pair<int, std::string>> pairs;
  std::string code_line;
  std::string value_line;
  while (std::getline(input, code_line)) {
    if (!std::getline(input, value_line)) {
      break;
    }
    if (!code_line.empty() && code_line.back() == '\r') {
      code_line.pop_back();
    }
    if (!value_line.empty() && value_line.back() == '\r') {
      value_line.pop_back();
    }
    try {
      pairs.emplace_back(std::stoi(code_line), value_line);
    } catch (...) {
      continue;
    }
  }
  return pairs;
}

double parse_double(std::string_view value) {
  return std::stod(std::string(value));
}

std::string entity_signature(const NativeDxfEntity& entity) {
  std::string sig = entity.type + "|" + entity.layer + "|" + entity.block_name + "|";
  for (const auto& point : entity.points) {
    sig += number_json(point.x) + "," + number_json(point.y) + ";";
  }
  sig += "|" + number_json(entity.radius) +
    "|" + number_json(entity.width) +
    "|" + number_json(entity.height) +
    "|" + number_json(entity.rotation) +
    "|" + number_json(entity.start_angle) +
    "|" + number_json(entity.end_angle) +
    "|" + number_json(entity.major_x) +
    "|" + number_json(entity.major_y) +
    "|" + number_json(entity.ratio) +
    "|" + (entity.closed ? "1" : "0") +
    "|" + entity.text;
  for (const auto& [tag, value] : entity.attributes) {
    sig += "|" + tag + "=" + value;
  }
  return sig;
}

}  // namespace

NativeDxfDocument::NativeDxfDocument() {
  reset();
}

void NativeDxfDocument::reset(std::string name) {
  name_ = std::move(name);
  if (name_.empty()) {
    name_ = "Drawing1";
  }
  save_path_.reset();
  layers_.clear();
  layers_.emplace("0", NativeDxfLayer{.name = "0"});
  blocks_.clear();
  current_layer_ = "0";
  entities_.clear();
  next_handle_ = 0x100;
}

core::CommandResult NativeDxfDocument::open(const std::filesystem::path& path) {
  try {
    const auto pairs = read_pairs(path);
    reset(path.stem().string());
    save_path_ = path;

    for (std::size_t i = 0; i < pairs.size(); ++i) {
      if (pairs[i].first != 0) {
        continue;
      }
      const std::string type = pairs[i].second;
      if (type != "LINE" && type != "CIRCLE" && type != "LWPOLYLINE" &&
          type != "ARC" && type != "ELLIPSE" && type != "TEXT" &&
          type != "MTEXT" && type != "INSERT") {
        continue;
      }

      NativeDxfEntity entity;
      entity.type = type;
      double x1 = 0.0;
      double y1 = 0.0;
      double x2 = 0.0;
      double y2 = 0.0;
      DxfPoint pending_point;
      bool have_pending_x = false;

      for (++i; i < pairs.size(); ++i) {
        if (pairs[i].first == 0) {
          --i;
          break;
        }
        const int code = pairs[i].first;
        const auto& value = pairs[i].second;
        if (code == 5) {
          entity.handle = value;
        } else if (code == 8) {
          entity.layer = value.empty() ? "0" : value;
        } else if (type == "LINE" && code == 10) {
          x1 = parse_double(value);
        } else if (type == "LINE" && code == 20) {
          y1 = parse_double(value);
        } else if (type == "LINE" && code == 11) {
          x2 = parse_double(value);
        } else if (type == "LINE" && code == 21) {
          y2 = parse_double(value);
        } else if (type == "CIRCLE" && code == 10) {
          x1 = parse_double(value);
        } else if (type == "CIRCLE" && code == 20) {
          y1 = parse_double(value);
        } else if (type == "CIRCLE" && code == 40) {
          entity.radius = parse_double(value);
        } else if (type == "ARC" && code == 10) {
          x1 = parse_double(value);
        } else if (type == "ARC" && code == 20) {
          y1 = parse_double(value);
        } else if (type == "ARC" && code == 40) {
          entity.radius = parse_double(value);
        } else if (type == "ARC" && code == 50) {
          entity.start_angle = parse_double(value);
        } else if (type == "ARC" && code == 51) {
          entity.end_angle = parse_double(value);
        } else if (type == "ELLIPSE" && code == 10) {
          x1 = parse_double(value);
        } else if (type == "ELLIPSE" && code == 20) {
          y1 = parse_double(value);
        } else if (type == "ELLIPSE" && code == 11) {
          entity.major_x = parse_double(value);
        } else if (type == "ELLIPSE" && code == 21) {
          entity.major_y = parse_double(value);
        } else if (type == "ELLIPSE" && code == 40) {
          entity.ratio = parse_double(value);
        } else if ((type == "TEXT" || type == "MTEXT") && code == 10) {
          x1 = parse_double(value);
        } else if ((type == "TEXT" || type == "MTEXT") && code == 20) {
          y1 = parse_double(value);
        } else if ((type == "TEXT" || type == "MTEXT") && code == 1) {
          entity.text = value;
        } else if ((type == "TEXT" || type == "MTEXT") && code == 40) {
          entity.height = parse_double(value);
        } else if (type == "MTEXT" && code == 41) {
          entity.width = parse_double(value);
        } else if (type == "TEXT" && code == 50) {
          entity.rotation = parse_double(value);
        } else if (type == "INSERT" && code == 2) {
          entity.block_name = value;
        } else if (type == "INSERT" && code == 10) {
          x1 = parse_double(value);
        } else if (type == "INSERT" && code == 20) {
          y1 = parse_double(value);
        } else if (type == "INSERT" && code == 41) {
          entity.scale_x = parse_double(value);
        } else if (type == "INSERT" && code == 42) {
          entity.scale_y = parse_double(value);
        } else if (type == "INSERT" && code == 50) {
          entity.rotation = parse_double(value);
        } else if (type == "LWPOLYLINE" && code == 70) {
          entity.closed = std::stoi(value) != 0;
        } else if (type == "LWPOLYLINE" && code == 10) {
          pending_point.x = parse_double(value);
          have_pending_x = true;
        } else if (type == "LWPOLYLINE" && code == 20 && have_pending_x) {
          pending_point.y = parse_double(value);
          entity.points.push_back(pending_point);
          have_pending_x = false;
        }
      }

      if (entity.handle.empty()) {
        entity.handle = next_handle();
      }
      ensure_layer(entity.layer);
      if (type == "LINE") {
        entity.points = {{x1, y1}, {x2, y2}};
      } else if (type == "CIRCLE" || type == "ARC" || type == "ELLIPSE" ||
                 type == "TEXT" || type == "MTEXT" || type == "INSERT") {
        entity.points = {{x1, y1}};
      }
      if (type == "INSERT" && !entity.block_name.empty() && !blocks_.contains(entity.block_name)) {
        blocks_.emplace(entity.block_name, NativeDxfBlockDefinition{.name = entity.block_name});
      }
      entities_.push_back(std::move(entity));
    }

    return core::CommandResult::success(
      "{\"path\":" + json_string(path.string()) +
      ",\"entity_count\":" + std::to_string(entities_.size()) + "}");
  } catch (const std::exception& ex) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, ex.what());
  }
}

core::CommandResult NativeDxfDocument::save(const std::optional<std::filesystem::path>& path) {
  if (path) {
    save_path_ = *path;
  }
  if (!save_path_) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "No save path set");
  }

  std::ofstream output(*save_path_, std::ios::binary | std::ios::trunc);
  if (!output) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "Could not open DXF save path");
  }

  write_pair(output, 0, "SECTION");
  write_pair(output, 2, "HEADER");
  write_pair(output, 9, "$ACADVER");
  write_pair(output, 1, "AC1032");
  write_pair(output, 9, "$CLAYER");
  write_pair(output, 8, current_layer_);
  write_pair(output, 0, "ENDSEC");

  write_pair(output, 0, "SECTION");
  write_pair(output, 2, "TABLES");
  write_pair(output, 0, "TABLE");
  write_pair(output, 2, "LAYER");
  write_pair(output, 70, std::to_string(layers_.size()));
  for (const auto& [name, layer] : layers_) {
    write_pair(output, 0, "LAYER");
    write_pair(output, 2, name);
    write_pair(output, 70, layer.frozen ? "1" : "0");
    write_pair(output, 62, std::to_string(layer.color));
    write_pair(output, 6, layer.linetype);
  }
  write_pair(output, 0, "ENDTAB");
  write_pair(output, 0, "ENDSEC");

  write_pair(output, 0, "SECTION");
  write_pair(output, 2, "BLOCKS");
  for (const auto& [name, block] : blocks_) {
    write_pair(output, 0, "BLOCK");
    write_pair(output, 8, "0");
    write_pair(output, 2, name);
    write_pair(output, 70, "0");
    write_pair(output, 10, "0");
    write_pair(output, 20, "0");
    for (const auto& entity : block.entities) {
      write_entity_record(output, entity, false);
    }
    write_pair(output, 0, "ENDBLK");
  }
  write_pair(output, 0, "ENDSEC");

  write_pair(output, 0, "SECTION");
  write_pair(output, 2, "ENTITIES");
  for (const auto& entity : entities_) {
    write_entity_record(output, entity);
  }
  write_pair(output, 0, "ENDSEC");
  write_pair(output, 0, "EOF");

  return core::CommandResult::success("{\"path\":" + json_string(save_path_->string()) + "}");
}

core::CommandResult NativeDxfDocument::info() const {
  const std::string payload = std::string("{") +
    "\"name\":" + json_string(name_) + "," +
    "\"path\":" + (save_path_ ? json_string(save_path_->string()) : "null") + "," +
    "\"entity_count\":" + std::to_string(entities_.size()) + "," +
    "\"current_layer\":" + json_string(current_layer_) + "," +
    "\"layers\":" + layers_json() + "," +
    "\"extents\":" + extents_json() +
    "}";
  return core::CommandResult::success(payload);
}

core::CommandResult NativeDxfDocument::create_line(double x1, double y1, double x2, double y2, std::string layer) {
  NativeDxfEntity entity;
  entity.type = "LINE";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{x1, y1}, {x2, y2}};
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"LINE\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_circle(double cx, double cy, double radius, std::string layer) {
  if (radius <= 0.0) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "radius must be positive");
  }
  NativeDxfEntity entity;
  entity.type = "CIRCLE";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{cx, cy}};
  entity.radius = radius;
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"CIRCLE\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_polyline(std::vector<DxfPoint> points, bool closed, std::string layer) {
  if (points.size() < 2) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "polyline requires at least two points");
  }
  NativeDxfEntity entity;
  entity.type = "LWPOLYLINE";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = std::move(points);
  entity.closed = closed;
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"LWPOLYLINE\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_rectangle(double x1, double y1, double x2, double y2, std::string layer) {
  return create_polyline({{x1, y1}, {x2, y1}, {x2, y2}, {x1, y2}}, true, std::move(layer));
}

core::CommandResult NativeDxfDocument::create_arc(
    double cx,
    double cy,
    double radius,
    double start_angle,
    double end_angle,
    std::string layer) {
  if (radius <= 0.0) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "radius must be positive");
  }
  NativeDxfEntity entity;
  entity.type = "ARC";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{cx, cy}};
  entity.radius = radius;
  entity.start_angle = start_angle;
  entity.end_angle = end_angle;
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"ARC\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_ellipse(
    double cx,
    double cy,
    double major_x,
    double major_y,
    double ratio,
    std::string layer) {
  if (ratio <= 0.0) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "ratio must be positive");
  }
  NativeDxfEntity entity;
  entity.type = "ELLIPSE";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{cx, cy}};
  entity.major_x = major_x;
  entity.major_y = major_y;
  entity.ratio = ratio;
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"ELLIPSE\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_text(
    double x,
    double y,
    std::string text,
    double height,
    double rotation,
    std::string layer) {
  NativeDxfEntity entity;
  entity.type = "TEXT";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{x, y}};
  entity.text = std::move(text);
  entity.height = height <= 0.0 ? 2.5 : height;
  entity.rotation = rotation;
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"TEXT\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_mtext(
    double x,
    double y,
    double width,
    std::string text,
    double height,
    std::string layer) {
  NativeDxfEntity entity;
  entity.type = "MTEXT";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{x, y}};
  entity.text = std::move(text);
  entity.width = width <= 0.0 ? 50.0 : width;
  entity.height = height <= 0.0 ? 2.5 : height;
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"MTEXT\",\"handle\":" + json_string(added.handle) +
    ",\"layer\":" + json_string(added.layer) + "}");
}

core::CommandResult NativeDxfDocument::create_leader(std::vector<DxfPoint> points, std::string text, std::string layer) {
  auto result = create_polyline(std::move(points), false, std::move(layer));
  if (!result.ok) {
    return result;
  }
  if (!text.empty() && !entities_.empty()) {
    entities_.back().text = std::move(text);
  }
  return core::CommandResult::success(
    "{\"entity_type\":\"LEADER\",\"handle\":" + json_string(entities_.back().handle) +
    ",\"layer\":" + json_string(entities_.back().layer) + "}");
}

core::CommandResult NativeDxfDocument::create_dimension_stub(std::string kind, std::vector<DxfPoint> points, std::string layer) {
  auto result = create_polyline(std::move(points), false, std::move(layer));
  if (!result.ok) {
    return result;
  }
  if (!entities_.empty()) {
    entities_.back().text = std::move(kind);
  }
  return core::CommandResult::success(
    "{\"entity_type\":\"DIMENSION\",\"handle\":" + json_string(entities_.back().handle) +
    ",\"layer\":" + json_string(entities_.back().layer) + "}");
}

core::CommandResult NativeDxfDocument::list_entities(std::optional<std::string> layer, int limit) const {
  if (limit < 0) {
    limit = 1000;
  }
  std::vector<const NativeDxfEntity*> filtered;
  for (const auto& entity : entities_) {
    if (!layer || entity.layer == *layer) {
      filtered.push_back(&entity);
    }
  }
  const bool unlimited = limit == 0;
  const std::size_t emitted = unlimited
    ? filtered.size()
    : std::min<std::size_t>(filtered.size(), static_cast<std::size_t>(limit));
  std::string entities = "[";
  for (std::size_t i = 0; i < emitted; ++i) {
    if (i != 0) {
      entities += ",";
    }
    entities += entity_json(*filtered[i]);
  }
  entities += "]";
  const bool truncated = !unlimited && emitted < filtered.size();
  const std::string payload = std::string("{") +
    "\"count\":" + std::to_string(emitted) + "," +
    "\"total\":" + std::to_string(filtered.size()) + "," +
    "\"truncated\":" + (truncated ? "true" : "false") + "," +
    "\"entities\":" + entities +
    "}";
  return core::CommandResult::success(payload);
}

core::CommandResult NativeDxfDocument::select_entities(std::optional<std::string> layer, std::optional<std::string> type, int limit) const {
  if (limit <= 0) {
    limit = 1000;
  }
  std::vector<const NativeDxfEntity*> matches;
  for (const auto& entity : entities_) {
    if (layer && entity.layer != *layer) {
      continue;
    }
    if (type && entity.type != upper_copy(*type)) {
      continue;
    }
    matches.push_back(&entity);
  }
  const std::size_t emitted = std::min<std::size_t>(matches.size(), static_cast<std::size_t>(limit));
  std::string handles = "[";
  for (std::size_t i = 0; i < emitted; ++i) {
    if (i != 0) {
      handles += ",";
    }
    handles += json_string(matches[i]->handle);
  }
  handles += "]";
  return core::CommandResult::success(
    "{\"handles\":" + handles +
    ",\"count\":" + std::to_string(emitted) +
    ",\"total\":" + std::to_string(matches.size()) +
    ",\"truncated\":" + (emitted < matches.size() ? "true" : "false") + "}");
}

core::CommandResult NativeDxfDocument::osnap(double x, double y, double tolerance, std::vector<std::string> modes) const {
  if (tolerance <= 0.0) {
    tolerance = 5.0;
  }
  if (modes.empty()) {
    modes = {"endpoint", "midpoint", "center"};
  }
  auto wants = [&](std::string_view mode) {
    return std::find(modes.begin(), modes.end(), mode) != modes.end();
  };
  struct Candidate {
    double distance{0.0};
    DxfPoint point;
    std::string mode;
    std::string handle;
  };
  std::optional<Candidate> best;
  auto consider = [&](DxfPoint point, std::string mode, std::string handle) {
    const double distance = std::hypot(point.x - x, point.y - y);
    if (distance > tolerance) {
      return;
    }
    if (!best || distance < best->distance) {
      best = Candidate{distance, point, std::move(mode), std::move(handle)};
    }
  };
  for (const auto& entity : entities_) {
    if (entity.type == "LINE" && entity.points.size() >= 2) {
      if (wants("endpoint")) {
        consider(entity.points[0], "endpoint", entity.handle);
        consider(entity.points[1], "endpoint", entity.handle);
      }
      if (wants("midpoint")) {
        consider({(entity.points[0].x + entity.points[1].x) / 2.0, (entity.points[0].y + entity.points[1].y) / 2.0}, "midpoint", entity.handle);
      }
    } else if ((entity.type == "CIRCLE" || entity.type == "ARC" || entity.type == "ELLIPSE") && !entity.points.empty()) {
      if (wants("center")) {
        consider(entity.points[0], "center", entity.handle);
      }
      if (entity.type == "CIRCLE" && wants("endpoint")) {
        consider({entity.points[0].x + entity.radius, entity.points[0].y}, "endpoint", entity.handle);
        consider({entity.points[0].x - entity.radius, entity.points[0].y}, "endpoint", entity.handle);
        consider({entity.points[0].x, entity.points[0].y + entity.radius}, "endpoint", entity.handle);
        consider({entity.points[0].x, entity.points[0].y - entity.radius}, "endpoint", entity.handle);
      }
      if (entity.type == "ARC" && wants("endpoint")) {
        consider(polar_point(entity.points[0].x, entity.points[0].y, entity.radius, entity.start_angle), "endpoint", entity.handle);
        consider(polar_point(entity.points[0].x, entity.points[0].y, entity.radius, entity.end_angle), "endpoint", entity.handle);
      }
    } else if (entity.type == "LWPOLYLINE" && !entity.points.empty()) {
      if (wants("endpoint")) {
        for (const auto& point : entity.points) {
          consider(point, "endpoint", entity.handle);
        }
      }
      if (wants("midpoint")) {
        for (std::size_t i = 1; i < entity.points.size(); ++i) {
          consider({(entity.points[i - 1].x + entity.points[i].x) / 2.0, (entity.points[i - 1].y + entity.points[i].y) / 2.0}, "midpoint", entity.handle);
        }
      }
    }
  }
  if (!best) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "No osnap point within tolerance");
  }
  return core::CommandResult::success(
    "{\"x\":" + number_json(best->point.x) +
    ",\"y\":" + number_json(best->point.y) +
    ",\"mode_used\":" + json_string(best->mode) +
    ",\"entity_handle\":" + json_string(best->handle) +
    ",\"distance\":" + number_json(best->distance) +
    ",\"tolerance\":" + number_json(tolerance) + "}");
}

core::CommandResult NativeDxfDocument::count_entities(std::optional<std::string> layer) const {
  std::size_t count = 0;
  for (const auto& entity : entities_) {
    if (!layer || entity.layer == *layer) {
      ++count;
    }
  }
  return core::CommandResult::success("{\"count\":" + std::to_string(count) + "}");
}

core::CommandResult NativeDxfDocument::get_entity(std::string_view handle) const {
  const auto* entity = find_entity(handle);
  if (entity == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  return core::CommandResult::success(entity_json(*entity));
}

core::CommandResult NativeDxfDocument::erase_entity(std::string_view handle) {
  if (handle == "last") {
    if (entities_.empty()) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, "No entities to erase");
    }
    const auto erased = entities_.back().handle;
    entities_.pop_back();
    return core::CommandResult::success("{\"erased\":" + json_string(erased) + "}");
  }
  const auto it = std::find_if(entities_.begin(), entities_.end(), [&](const NativeDxfEntity& entity) {
    return entity.handle == handle;
  });
  if (it == entities_.end()) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  const auto erased = it->handle;
  entities_.erase(it);
  return core::CommandResult::success("{\"erased\":" + json_string(erased) + "}");
}

core::CommandResult NativeDxfDocument::copy_entity(std::string_view handle, double dx, double dy) {
  const auto* source = find_entity(handle);
  if (source == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  NativeDxfEntity copy = *source;
  copy.handle.clear();
  for (auto& point : copy.points) {
    point.x += dx;
    point.y += dy;
  }
  const auto& added = add_entity(std::move(copy));
  return core::CommandResult::success(
    "{\"handle\":" + json_string(added.handle) +
    ",\"entity_type\":" + json_string(added.type) + "}");
}

core::CommandResult NativeDxfDocument::move_entity(std::string_view handle, double dx, double dy) {
  auto* entity = find_entity(handle);
  if (entity == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  for (auto& point : entity->points) {
    point.x += dx;
    point.y += dy;
  }
  return core::CommandResult::success(entity_json(*entity));
}

core::CommandResult NativeDxfDocument::rotate_entity(std::string_view handle, double cx, double cy, double angle_degrees) {
  auto* entity = find_entity(handle);
  if (entity == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  const double radians = angle_degrees * kPi / 180.0;
  for (auto& point : entity->points) {
    point = rotate_point(point, cx, cy, radians);
  }
  entity->rotation += angle_degrees;
  if (entity->type == "ARC") {
    entity->start_angle += angle_degrees;
    entity->end_angle += angle_degrees;
  }
  if (entity->type == "ELLIPSE") {
    const auto major = rotate_point({entity->major_x, entity->major_y}, 0.0, 0.0, radians);
    entity->major_x = major.x;
    entity->major_y = major.y;
  }
  return core::CommandResult::success(entity_json(*entity));
}

core::CommandResult NativeDxfDocument::scale_entity(std::string_view handle, double cx, double cy, double factor) {
  if (factor <= 0.0) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "scale factor must be positive");
  }
  auto* entity = find_entity(handle);
  if (entity == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  for (auto& point : entity->points) {
    point = scale_point(point, cx, cy, factor);
  }
  entity->radius *= factor;
  entity->width *= factor;
  entity->height *= factor;
  entity->major_x *= factor;
  entity->major_y *= factor;
  return core::CommandResult::success(entity_json(*entity));
}

core::CommandResult NativeDxfDocument::mirror_entity(std::string_view handle, double x1, double y1, double x2, double y2) {
  if (x1 == x2 && y1 == y2) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "mirror axis points must differ");
  }
  const auto* source = find_entity(handle);
  if (source == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  NativeDxfEntity copy = *source;
  copy.handle.clear();
  for (auto& point : copy.points) {
    point = mirror_point(point, x1, y1, x2, y2);
  }
  if (copy.type == "ELLIPSE") {
    const auto major = mirror_point({copy.major_x, copy.major_y}, 0.0, 0.0, x2 - x1, y2 - y1);
    copy.major_x = major.x;
    copy.major_y = major.y;
  }
  const auto& added = add_entity(std::move(copy));
  return core::CommandResult::success(
    "{\"handle\":" + json_string(added.handle) +
    ",\"entity_type\":" + json_string(added.type) +
    ",\"mirrored\":true}");
}

core::CommandResult NativeDxfDocument::offset_entity(std::string_view handle, double distance) {
  const auto* source = find_entity(handle);
  if (source == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  if (source->type != "LINE" || source->points.size() < 2) {
    return core::CommandResult::failure(core::ErrorCategory::not_supported, "offset is currently supported for LINE only");
  }
  const auto a = source->points[0];
  const auto b = source->points[1];
  const double dx = b.x - a.x;
  const double dy = b.y - a.y;
  const double length = std::hypot(dx, dy);
  if (length <= 0.0) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "cannot offset zero-length line");
  }
  const double ox = -dy / length * distance;
  const double oy = dx / length * distance;
  NativeDxfEntity copy = *source;
  copy.handle.clear();
  copy.points = {{a.x + ox, a.y + oy}, {b.x + ox, b.y + oy}};
  const auto& added = add_entity(std::move(copy));
  return core::CommandResult::success(
    "{\"handle\":" + json_string(added.handle) +
    ",\"entity_type\":\"LINE\",\"start\":" + point_json(added.points[0]) +
    ",\"end\":" + point_json(added.points[1]) + "}");
}

core::CommandResult NativeDxfDocument::array_entity(
    std::string_view handle,
    int rows,
    int cols,
    double row_dist,
    double col_dist) {
  if (rows < 1 || cols < 1) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "rows and cols must be >= 1");
  }
  const auto* source = find_entity(handle);
  if (source == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Entity not found");
  }
  std::vector<std::string> handles;
  for (int row = 0; row < rows; ++row) {
    for (int col = 0; col < cols; ++col) {
      if (row == 0 && col == 0) {
        continue;
      }
      NativeDxfEntity copy = *source;
      copy.handle.clear();
      for (auto& point : copy.points) {
        point.x += static_cast<double>(col) * col_dist;
        point.y += static_cast<double>(row) * row_dist;
      }
      const auto& added = add_entity(std::move(copy));
      handles.push_back(added.handle);
    }
  }
  std::string out = "{\"copies\":" + std::to_string(handles.size()) + ",\"handles\":[";
  for (std::size_t i = 0; i < handles.size(); ++i) {
    if (i != 0) {
      out += ",";
    }
    out += json_string(handles[i]);
  }
  out += "]}";
  return core::CommandResult::success(out);
}

core::CommandResult NativeDxfDocument::list_layers() const {
  std::string layers = "[";
  std::size_t i = 0;
  for (const auto& [name, layer] : layers_) {
    if (i++ != 0) {
      layers += ",";
    }
    layers += "{\"name\":" + json_string(name) +
      ",\"color\":" + std::to_string(layer.color) +
      ",\"linetype\":" + json_string(layer.linetype) +
      ",\"frozen\":" + (layer.frozen ? "true" : "false") +
      ",\"locked\":" + (layer.locked ? "true" : "false") +
      ",\"current\":" + (name == current_layer_ ? "true" : "false") +
      "}";
  }
  layers += "]";
  return core::CommandResult::success("{\"layers\":" + layers + "}");
}

core::CommandResult NativeDxfDocument::create_layer(std::string name, int color, std::string linetype) {
  if (name.empty()) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "layer name is required");
  }
  const bool existed = layers_.contains(name);
  if (!existed) {
    layers_.emplace(name, NativeDxfLayer{.name = name, .color = color, .linetype = std::move(linetype)});
  }
  const auto& layer = layers_.at(name);
  return core::CommandResult::success(
    "{\"name\":" + json_string(name) +
    ",\"color\":" + std::to_string(layer.color) +
    ",\"linetype\":" + json_string(layer.linetype) +
    ",\"existed\":" + (existed ? "true" : "false") +
    "}");
}

core::CommandResult NativeDxfDocument::set_current_layer(std::string name) {
  if (!layers_.contains(name)) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Layer not found");
  }
  current_layer_ = std::move(name);
  return core::CommandResult::success("{\"current_layer\":" + json_string(current_layer_) + "}");
}

core::CommandResult NativeDxfDocument::set_layer_properties(
    std::string name,
    std::optional<int> color,
    std::optional<std::string> linetype) {
  auto it = layers_.find(name);
  if (it == layers_.end()) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Layer not found");
  }
  if (color) {
    it->second.color = *color;
  }
  if (linetype) {
    it->second.linetype = *linetype;
  }
  return create_layer(name, it->second.color, it->second.linetype);
}

core::CommandResult NativeDxfDocument::set_layer_frozen(std::string name, bool frozen) {
  auto it = layers_.find(name);
  if (it == layers_.end()) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Layer not found");
  }
  it->second.frozen = frozen;
  return core::CommandResult::success("{\"name\":" + json_string(name) + ",\"frozen\":" + (frozen ? "true" : "false") + "}");
}

core::CommandResult NativeDxfDocument::set_layer_locked(std::string name, bool locked) {
  auto it = layers_.find(name);
  if (it == layers_.end()) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Layer not found");
  }
  it->second.locked = locked;
  return core::CommandResult::success("{\"name\":" + json_string(name) + ",\"locked\":" + (locked ? "true" : "false") + "}");
}

core::CommandResult NativeDxfDocument::list_blocks() const {
  std::string blocks = "[";
  std::size_t index = 0;
  for (const auto& [name, block] : blocks_) {
    if (index++ != 0) {
      blocks += ",";
    }
    blocks += "{\"name\":" + json_string(name) +
      ",\"entity_count\":" + std::to_string(block.entities.size()) + "}";
  }
  blocks += "]";
  return core::CommandResult::success("{\"blocks\":" + blocks + ",\"count\":" + std::to_string(blocks_.size()) + "}");
}

core::CommandResult NativeDxfDocument::define_block(std::string name, std::vector<NativeDxfEntity> entities) {
  if (name.empty()) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "block name is required");
  }
  for (auto& entity : entities) {
    entity.handle.clear();
    entity.layer = entity.layer.empty() ? "0" : entity.layer;
  }
  const bool replaced = blocks_.contains(name);
  blocks_[name] = NativeDxfBlockDefinition{.name = name, .entities = std::move(entities)};
  return core::CommandResult::success(
    "{\"block\":" + json_string(name) +
    ",\"entity_count\":" + std::to_string(blocks_.at(name).entities.size()) +
    ",\"replaced\":" + (replaced ? "true" : "false") + "}");
}

core::CommandResult NativeDxfDocument::insert_block(
    std::string name,
    double x,
    double y,
    double scale,
    double rotation,
    std::map<std::string, std::string> attributes,
    std::string layer) {
  if (name.empty()) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "block name is required");
  }
  if (scale <= 0.0) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "block scale must be positive");
  }
  if (!blocks_.contains(name)) {
    blocks_.emplace(name, NativeDxfBlockDefinition{.name = name});
  }
  NativeDxfEntity entity;
  entity.type = "INSERT";
  entity.layer = layer.empty() ? current_layer_ : std::move(layer);
  entity.points = {{x, y}};
  entity.block_name = std::move(name);
  entity.scale_x = scale;
  entity.scale_y = scale;
  entity.rotation = rotation;
  entity.height = 2.5;
  entity.attributes = std::move(attributes);
  const auto& added = add_entity(std::move(entity));
  return core::CommandResult::success(
    "{\"entity_type\":\"INSERT\",\"handle\":" + json_string(added.handle) +
    ",\"block\":" + json_string(added.block_name) +
    ",\"attribute_count\":" + std::to_string(added.attributes.size()) + "}");
}

core::CommandResult NativeDxfDocument::get_block_attributes(std::string_view handle) const {
  const auto* entity = find_entity(handle);
  if (entity == nullptr || entity->type != "INSERT") {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id must reference an INSERT entity");
  }
  std::string attrs = "{";
  std::size_t index = 0;
  for (const auto& [tag, value] : entity->attributes) {
    if (index++ != 0) {
      attrs += ",";
    }
    attrs += json_string(tag) + ":" + json_string(value);
  }
  attrs += "}";
  return core::CommandResult::success("{\"entity_id\":" + json_string(entity->handle) + ",\"attributes\":" + attrs + "}");
}

core::CommandResult NativeDxfDocument::update_block_attribute(std::string_view handle, std::string tag, std::string value) {
  if (tag.empty()) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "attribute tag is required");
  }
  auto* entity = find_entity(handle);
  if (entity == nullptr || entity->type != "INSERT") {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id must reference an INSERT entity");
  }
  entity->attributes[std::move(tag)] = std::move(value);
  return get_block_attributes(handle);
}

core::CommandResult NativeDxfDocument::isolate_layers(const std::vector<std::string>& names) {
  if (names.empty()) {
    return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "layers array cannot be empty");
  }
  std::set<std::string> keep(names.begin(), names.end());
  std::size_t frozen = 0;
  for (auto& [name, layer] : layers_) {
    const bool should_freeze = !keep.contains(name);
    if (layer.frozen != should_freeze) {
      layer.frozen = should_freeze;
      ++frozen;
    }
  }
  std::string kept = "[";
  for (std::size_t i = 0; i < names.size(); ++i) {
    if (i != 0) {
      kept += ",";
    }
    kept += json_string(names[i]);
  }
  kept += "]";
  return core::CommandResult::success(
    "{\"isolated\":" + kept +
    ",\"layers_changed\":" + std::to_string(frozen) + "}");
}

core::CommandResult NativeDxfDocument::remove_duplicate_entities() {
  std::set<std::string> seen;
  std::vector<NativeDxfEntity> unique;
  unique.reserve(entities_.size());
  std::size_t removed = 0;
  for (auto& entity : entities_) {
    const auto signature = entity_signature(entity);
    if (seen.contains(signature)) {
      ++removed;
      continue;
    }
    seen.insert(signature);
    unique.push_back(std::move(entity));
  }
  entities_ = std::move(unique);
  return core::CommandResult::success("{\"removed\":" + std::to_string(removed) + ",\"remaining\":" + std::to_string(entities_.size()) + "}");
}

core::CommandResult NativeDxfDocument::burst_inserts(const std::vector<std::string>& handles) {
  std::vector<std::string> targets = handles;
  if (targets.empty()) {
    for (const auto& entity : entities_) {
      if (entity.type == "INSERT") {
        targets.push_back(entity.handle);
      }
    }
  }
  std::vector<std::string> created;
  std::vector<std::string> erased;
  for (const auto& handle : targets) {
    const auto* source = find_entity(handle);
    if (source == nullptr || source->type != "INSERT" || source->points.empty()) {
      continue;
    }
    const auto source_handle = source->handle;
    const auto insert = source->points[0];
    const auto layer = source->layer;
    const auto attrs = source->attributes;
    double offset = 0.0;
    for (const auto& [tag, value] : attrs) {
      NativeDxfEntity text;
      text.type = "TEXT";
      text.layer = layer;
      text.points = {{insert.x, insert.y + offset}};
      text.height = 2.5;
      text.text = tag + "=" + value;
      const auto& added = add_entity(std::move(text));
      created.push_back(added.handle);
      offset += 3.0;
    }
    if (erase_entity(source_handle).ok) {
      erased.push_back(source_handle);
    }
  }
  std::string created_json = "[";
  for (std::size_t i = 0; i < created.size(); ++i) {
    if (i != 0) created_json += ",";
    created_json += json_string(created[i]);
  }
  created_json += "]";
  std::string erased_json = "[";
  for (std::size_t i = 0; i < erased.size(); ++i) {
    if (i != 0) erased_json += ",";
    erased_json += json_string(erased[i]);
  }
  erased_json += "]";
  return core::CommandResult::success("{\"created_text\":" + created_json + ",\"erased_inserts\":" + erased_json + "}");
}

core::CommandResult NativeDxfDocument::text_to_mtext(const std::vector<std::string>& handles) {
  std::vector<std::string> targets = handles;
  if (targets.empty()) {
    for (const auto& entity : entities_) {
      if (entity.type == "TEXT") {
        targets.push_back(entity.handle);
      }
    }
  }
  if (targets.empty()) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "No TEXT entities selected");
  }
  std::string combined;
  DxfPoint insert{0.0, 0.0};
  std::string layer = current_layer_;
  double height = 2.5;
  bool have_text = false;
  for (const auto& handle : targets) {
    const auto* entity = find_entity(handle);
    if (entity == nullptr || entity->type != "TEXT" || entity->points.empty()) {
      continue;
    }
    if (!have_text) {
      insert = entity->points[0];
      layer = entity->layer;
      height = entity->height;
      have_text = true;
    }
    if (!combined.empty()) {
      combined += "\\P";
    }
    combined += entity->text;
  }
  if (!have_text) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "No TEXT entities selected");
  }
  for (const auto& handle : targets) {
    (void)erase_entity(handle);
  }
  return create_mtext(insert.x, insert.y, 80.0, combined, height, layer);
}

NativeDxfEntity& NativeDxfDocument::add_entity(NativeDxfEntity entity) {
  entity.handle = entity.handle.empty() ? next_handle() : entity.handle;
  entity.layer = entity.layer.empty() ? current_layer_ : entity.layer;
  ensure_layer(entity.layer);
  entities_.push_back(std::move(entity));
  return entities_.back();
}

void NativeDxfDocument::ensure_layer(std::string name) {
  if (name.empty()) {
    name = "0";
  }
  if (!layers_.contains(name)) {
    layers_.emplace(name, NativeDxfLayer{.name = name});
  }
}

std::string NativeDxfDocument::next_handle() {
  return handle_upper(next_handle_++);
}

const NativeDxfEntity* NativeDxfDocument::find_entity(std::string_view handle) const {
  if (handle == "last") {
    return entities_.empty() ? nullptr : &entities_.back();
  }
  const auto it = std::find_if(entities_.begin(), entities_.end(), [&](const NativeDxfEntity& entity) {
    return entity.handle == handle;
  });
  return it == entities_.end() ? nullptr : &*it;
}

NativeDxfEntity* NativeDxfDocument::find_entity(std::string_view handle) {
  if (handle == "last") {
    return entities_.empty() ? nullptr : &entities_.back();
  }
  const auto it = std::find_if(entities_.begin(), entities_.end(), [&](const NativeDxfEntity& entity) {
    return entity.handle == handle;
  });
  return it == entities_.end() ? nullptr : &*it;
}

std::string NativeDxfDocument::entity_json(const NativeDxfEntity& entity) const {
  std::string payload = std::string("{") +
    "\"handle\":" + json_string(entity.handle) + "," +
    "\"type\":" + json_string(entity.type) + "," +
    "\"layer\":" + json_string(entity.layer);
  if (entity.type == "LINE" && entity.points.size() >= 2) {
    payload += ",\"start\":" + point_json(entity.points[0]) +
      ",\"end\":" + point_json(entity.points[1]);
  } else if (entity.type == "CIRCLE" && !entity.points.empty()) {
    payload += ",\"center\":" + point_json(entity.points[0]) +
      ",\"radius\":" + number_json(entity.radius);
  } else if (entity.type == "ARC" && !entity.points.empty()) {
    payload += ",\"center\":" + point_json(entity.points[0]) +
      ",\"radius\":" + number_json(entity.radius) +
      ",\"start_angle\":" + number_json(entity.start_angle) +
      ",\"end_angle\":" + number_json(entity.end_angle);
  } else if (entity.type == "ELLIPSE" && !entity.points.empty()) {
    payload += ",\"center\":" + point_json(entity.points[0]) +
      ",\"major_axis\":" + point_json({entity.major_x, entity.major_y}) +
      ",\"ratio\":" + number_json(entity.ratio);
  } else if ((entity.type == "TEXT" || entity.type == "MTEXT") && !entity.points.empty()) {
    payload += ",\"insert\":" + point_json(entity.points[0]) +
      ",\"text\":" + json_string(entity.text) +
      ",\"height\":" + number_json(entity.height);
    if (entity.type == "TEXT") {
      payload += ",\"rotation\":" + number_json(entity.rotation);
    } else {
      payload += ",\"width\":" + number_json(entity.width);
    }
  } else if (entity.type == "LWPOLYLINE") {
    payload += ",\"points\":" + points_json(entity.points) +
      ",\"closed\":" + (entity.closed ? "true" : "false");
    if (!entity.text.empty()) {
      payload += ",\"text\":" + json_string(entity.text);
    }
  } else if (entity.type == "INSERT" && !entity.points.empty()) {
    payload += ",\"insert\":" + point_json(entity.points[0]) +
      ",\"block\":" + json_string(entity.block_name) +
      ",\"scale\":" + number_json(entity.scale_x) +
      ",\"rotation\":" + number_json(entity.rotation) +
      ",\"attribute_count\":" + std::to_string(entity.attributes.size());
  }
  payload += "}";
  return payload;
}

std::string NativeDxfDocument::layers_json() const {
  std::string out = "[";
  std::size_t i = 0;
  for (const auto& [name, _] : layers_) {
    if (i++ != 0) {
      out += ",";
    }
    out += json_string(name);
  }
  out += "]";
  return out;
}

std::string NativeDxfDocument::extents_json() const {
  if (entities_.empty()) {
    return "null";
  }
  double min_x = std::numeric_limits<double>::infinity();
  double min_y = std::numeric_limits<double>::infinity();
  double max_x = -std::numeric_limits<double>::infinity();
  double max_y = -std::numeric_limits<double>::infinity();
  auto add_point = [&](double x, double y) {
    min_x = std::min(min_x, x);
    min_y = std::min(min_y, y);
    max_x = std::max(max_x, x);
    max_y = std::max(max_y, y);
  };
  for (const auto& entity : entities_) {
    if (entity.type == "CIRCLE" && !entity.points.empty()) {
      add_point(entity.points[0].x - entity.radius, entity.points[0].y - entity.radius);
      add_point(entity.points[0].x + entity.radius, entity.points[0].y + entity.radius);
      continue;
    }
    for (const auto& point : entity.points) {
      add_point(point.x, point.y);
    }
  }
  return std::string("{") +
    "\"min\":" + point_json({min_x, min_y}) + "," +
    "\"max\":" + point_json({max_x, max_y}) +
    "}";
}

}  // namespace autocad_mcp::backends
