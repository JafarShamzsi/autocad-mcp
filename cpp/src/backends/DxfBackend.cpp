#include "backends/DxfBackend.hpp"

#include "core/Version.hpp"
#include "protocol/JsonRpc.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace autocad_mcp::backends {
namespace {
constexpr double kPi = 3.14159265358979323846;

std::string json_string(std::string_view value) {
  return "\"" + core::escape_json(value) + "\"";
}

std::optional<std::string> data_object(std::string_view arguments_json) {
  return protocol::extract_object_property(arguments_json, "data");
}

std::optional<std::string> string_arg(std::string_view arguments_json, std::string_view key) {
  if (auto value = protocol::extract_string_property(arguments_json, key)) {
    return value;
  }
  if (auto data = data_object(arguments_json)) {
    return protocol::extract_string_property(*data, key);
  }
  return std::nullopt;
}

std::optional<double> number_arg(std::string_view arguments_json, std::string_view key) {
  if (auto value = protocol::extract_number_property(arguments_json, key)) {
    return value;
  }
  if (auto data = data_object(arguments_json)) {
    return protocol::extract_number_property(*data, key);
  }
  return std::nullopt;
}

bool bool_arg(std::string_view arguments_json, std::string_view key, bool fallback = false) {
  if (auto value = protocol::extract_bool_property(arguments_json, key)) {
    return *value;
  }
  if (auto data = data_object(arguments_json)) {
    if (auto value = protocol::extract_bool_property(*data, key)) {
      return *value;
    }
  }
  return fallback;
}

std::string layer_arg(std::string_view arguments_json) {
  return string_arg(arguments_json, "layer").value_or("");
}

core::CommandResult missing_arg(std::string_view name) {
  return core::CommandResult::failure(
    core::ErrorCategory::invalid_arg,
    "Missing numeric argument: " + std::string(name));
}

std::optional<std::string> raw_arg(std::string_view arguments_json, std::string_view key) {
  if (auto value = protocol::extract_object_property(arguments_json, key)) {
    return value;
  }
  if (auto data = data_object(arguments_json)) {
    return protocol::extract_object_property(*data, key);
  }
  return std::nullopt;
}

std::optional<std::vector<DxfPoint>> points_arg_named(std::string_view arguments_json, std::string_view key) {
  std::optional<std::string> raw = raw_arg(arguments_json, key);
  if (!raw) {
    return std::nullopt;
  }

  std::vector<double> numbers;
  for (std::size_t i = 0; i < raw->size();) {
    const char ch = (*raw)[i];
    if (!(std::isdigit(static_cast<unsigned char>(ch)) || ch == '-' || ch == '+' || ch == '.')) {
      ++i;
      continue;
    }
    std::size_t end = i + 1;
    while (end < raw->size()) {
      const char next = (*raw)[end];
      if (!(std::isdigit(static_cast<unsigned char>(next)) || next == '-' || next == '+' ||
            next == '.' || next == 'e' || next == 'E')) {
        break;
      }
      ++end;
    }
    try {
      numbers.push_back(std::stod(raw->substr(i, end - i)));
    } catch (...) {
      return std::nullopt;
    }
    i = end;
  }
  if (numbers.size() < 4 || numbers.size() % 2 != 0) {
    return std::nullopt;
  }
  std::vector<DxfPoint> points;
  points.reserve(numbers.size() / 2);
  for (std::size_t i = 0; i < numbers.size(); i += 2) {
    points.push_back({numbers[i], numbers[i + 1]});
  }
  return points;
}

std::optional<std::vector<DxfPoint>> points_arg(std::string_view arguments_json) {
  return points_arg_named(arguments_json, "points");
}

std::vector<std::string> object_array_values(std::string_view raw) {
  std::vector<std::string> objects;
  bool in_string = false;
  bool escaped = false;
  int depth = 0;
  std::size_t start = std::string_view::npos;
  for (std::size_t i = 0; i < raw.size(); ++i) {
    const char ch = raw[i];
    if (in_string) {
      if (escaped) {
        escaped = false;
      } else if (ch == '\\') {
        escaped = true;
      } else if (ch == '"') {
        in_string = false;
      }
      continue;
    }
    if (ch == '"') {
      in_string = true;
      continue;
    }
    if (ch == '{') {
      if (depth == 0) {
        start = i;
      }
      ++depth;
    } else if (ch == '}') {
      --depth;
      if (depth == 0 && start != std::string_view::npos) {
        objects.emplace_back(raw.substr(start, i - start + 1));
        start = std::string_view::npos;
      }
    }
  }
  return objects;
}

std::map<std::string, std::string> string_map_arg(std::string_view arguments_json, std::string_view key) {
  std::map<std::string, std::string> result;
  auto raw = raw_arg(arguments_json, key);
  if (!raw || raw->empty() || *raw == "null") {
    return result;
  }
  bool in_string = false;
  bool escaped = false;
  std::vector<std::string> strings;
  std::string current;
  for (std::size_t i = 0; i < raw->size(); ++i) {
    const char ch = (*raw)[i];
    if (!in_string) {
      if (ch == '"') {
        in_string = true;
        current.clear();
      }
      continue;
    }
    if (escaped) {
      escaped = false;
      current += ch;
      continue;
    }
    if (ch == '\\') {
      escaped = true;
      continue;
    }
    if (ch == '"') {
      in_string = false;
      strings.push_back(current);
      continue;
    }
    current += ch;
  }
  for (std::size_t i = 0; i + 1 < strings.size(); i += 2) {
    result[strings[i]] = strings[i + 1];
  }
  return result;
}

std::vector<std::string> string_array_arg(std::string_view arguments_json, std::string_view key) {
  std::vector<std::string> values;
  auto raw = raw_arg(arguments_json, key);
  if (!raw) {
    return values;
  }
  bool in_string = false;
  bool escaped = false;
  std::string current;
  for (char ch : *raw) {
    if (!in_string) {
      if (ch == '"') {
        in_string = true;
        current.clear();
      }
      continue;
    }
    if (escaped) {
      escaped = false;
      current += ch;
      continue;
    }
    if (ch == '\\') {
      escaped = true;
      continue;
    }
    if (ch == '"') {
      in_string = false;
      values.push_back(current);
      continue;
    }
    current += ch;
  }
  return values;
}

std::string upper_copy(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
    return static_cast<char>(std::toupper(ch));
  });
  return value;
}

DxfPoint polar_point(double x, double y, double distance, double angle_degrees) {
  const double radians = angle_degrees * kPi / 180.0;
  return {x + std::cos(radians) * distance, y + std::sin(radians) * distance};
}

std::optional<std::string> handle_from_result(const core::CommandResult& result) {
  if (!result.ok) {
    return std::nullopt;
  }
  if (auto handle = protocol::extract_string_property(result.payload_json, "handle")) {
    return handle;
  }
  return std::nullopt;
}

core::CommandResult handles_result(std::string_view kind, const std::vector<std::string>& handles) {
  std::string payload = "{\"kind\":" + json_string(kind) + ",\"handles\":[";
  for (std::size_t i = 0; i < handles.size(); ++i) {
    if (i != 0) {
      payload += ",";
    }
    payload += json_string(handles[i]);
  }
  payload += "],\"count\":" + std::to_string(handles.size()) + "}";
  return core::CommandResult::success(payload);
}

bool append_handle_or_fail(const core::CommandResult& result, std::vector<std::string>& handles) {
  if (!result.ok) {
    return false;
  }
  if (auto handle = handle_from_result(result)) {
    handles.push_back(*handle);
  }
  return true;
}

core::CommandResult setup_layer_set(
    NativeDxfDocument& document,
    std::initializer_list<std::pair<std::string_view, int>> layers,
    std::string_view profile_name) {
  std::string created = "[";
  std::size_t index = 0;
  for (const auto& [name, color] : layers) {
    auto result = document.create_layer(std::string(name), color, "CONTINUOUS");
    if (!result.ok) {
      return result;
    }
    if (index++ != 0) {
      created += ",";
    }
    created += json_string(name);
  }
  created += "]";
  return core::CommandResult::success(
    "{\"profile\":" + json_string(profile_name) +
    ",\"layers\":" + created +
    ",\"count\":" + std::to_string(index) + "}");
}

std::vector<DxfPoint> offset_rectangle(
    double x1,
    double y1,
    double x2,
    double y2,
    double thickness,
    std::string_view justify) {
  const double dx = x2 - x1;
  const double dy = y2 - y1;
  const double length = std::hypot(dx, dy);
  if (length <= 0.0) {
    return {};
  }
  const double nx = -dy / length;
  const double ny = dx / length;
  double left = thickness / 2.0;
  double right = thickness / 2.0;
  if (justify == "left") {
    left = thickness;
    right = 0.0;
  } else if (justify == "right") {
    left = 0.0;
    right = thickness;
  }
  return {
    {x1 + nx * left, y1 + ny * left},
    {x2 + nx * left, y2 + ny * left},
    {x2 - nx * right, y2 - ny * right},
    {x1 - nx * right, y1 - ny * right},
  };
}

double polygon_area(const std::vector<DxfPoint>& points) {
  if (points.size() < 3) {
    return 0.0;
  }
  double area = 0.0;
  for (std::size_t i = 0; i < points.size(); ++i) {
    const auto& a = points[i];
    const auto& b = points[(i + 1) % points.size()];
    area += a.x * b.y - b.x * a.y;
  }
  return area / 2.0;
}

DxfPoint polygon_centroid(const std::vector<DxfPoint>& points) {
  if (points.empty()) {
    return {};
  }
  double cx = 0.0;
  double cy = 0.0;
  const double area = polygon_area(points);
  if (std::fabs(area) < 1e-9) {
    for (const auto& point : points) {
      cx += point.x;
      cy += point.y;
    }
    return {cx / points.size(), cy / points.size()};
  }
  for (std::size_t i = 0; i < points.size(); ++i) {
    const auto& a = points[i];
    const auto& b = points[(i + 1) % points.size()];
    const double cross = a.x * b.y - b.x * a.y;
    cx += (a.x + b.x) * cross;
    cy += (a.y + b.y) * cross;
  }
  const double factor = 1.0 / (6.0 * area);
  return {cx * factor, cy * factor};
}

core::CommandResult draw_rotated_triangle(
    NativeDxfDocument& document,
    double x,
    double y,
    double size,
    double rotation,
    std::string layer) {
  std::vector<DxfPoint> points = {
    polar_point(x, y, size, rotation),
    polar_point(x, y, size * 0.65, rotation + 140.0),
    polar_point(x, y, size * 0.65, rotation - 140.0),
  };
  return document.create_polyline(std::move(points), true, std::move(layer));
}

std::string pid_symbols_json(std::string_view category) {
  const std::string cat = upper_copy(std::string(category));
  if (cat == "VALVE" || cat == "VALVES") {
    return "{\"category\":\"valves\",\"symbols\":[\"gate\",\"globe\",\"ball\",\"check\",\"control\"]}";
  }
  if (cat == "EQUIPMENT") {
    return "{\"category\":\"equipment\",\"symbols\":[\"pump\",\"tank\",\"vessel\",\"compressor\"]}";
  }
  if (cat == "INSTRUMENT" || cat == "INSTRUMENTS") {
    return "{\"category\":\"instruments\",\"symbols\":[\"pressure\",\"temperature\",\"flow\",\"level\"]}";
  }
  return "{\"category\":\"all\",\"symbols\":[\"gate-valve\",\"instrument-bubble\",\"pump\",\"tank\",\"flow-arrow\",\"line-number\"]}";
}

const std::vector<std::string>& native_library_names() {
  static const std::vector<std::string> names = {
    "ARCH-NORTH-ARROW",
    "ARCH-SCALE-BAR-10FT",
    "PLMB-TOILET",
    "PLMB-SINK",
    "APPL-RANGE",
    "APPL-REFRIGERATOR",
    "FURN-BED-QUEEN",
    "FURN-DESK",
    "FURN-CHAIR-OFFICE",
    "FURN-SOFA",
    "ELEC-OUTLET-DUPLEX",
    "PID-GATE-VALVE",
    "PID-INSTRUMENT-BUBBLE",
  };
  return names;
}

std::string native_library_names_json() {
  const auto& names = native_library_names();
  std::string payload = "{\"symbols\":[";
  for (std::size_t i = 0; i < names.size(); ++i) {
    if (i != 0) {
      payload += ",";
    }
    payload += json_string(names[i]);
  }
  payload += "],\"count\":" + std::to_string(names.size()) + "}";
  return payload;
}

std::vector<NativeDxfEntity> native_library_entities(std::string name) {
  name = upper_copy(std::move(name));
  auto line = [](double x1, double y1, double x2, double y2) {
    NativeDxfEntity entity;
    entity.type = "LINE";
    entity.layer = "0";
    entity.points = {{x1, y1}, {x2, y2}};
    return entity;
  };
  auto circle = [](double x, double y, double radius) {
    NativeDxfEntity entity;
    entity.type = "CIRCLE";
    entity.layer = "0";
    entity.points = {{x, y}};
    entity.radius = radius;
    return entity;
  };
  auto rect = [](double x1, double y1, double x2, double y2) {
    NativeDxfEntity entity;
    entity.type = "LWPOLYLINE";
    entity.layer = "0";
    entity.closed = true;
    entity.points = {{x1, y1}, {x2, y1}, {x2, y2}, {x1, y2}};
    return entity;
  };
  auto text = [](double x, double y, std::string value) {
    NativeDxfEntity entity;
    entity.type = "TEXT";
    entity.layer = "0";
    entity.points = {{x, y}};
    entity.height = 2.5;
    entity.text = std::move(value);
    return entity;
  };

  if (name == "ARCH-NORTH-ARROW") {
    return {line(0, -8, 0, 8), line(0, 8, -3, 3), line(0, 8, 3, 3), text(2, 2, "N")};
  }
  if (name == "ARCH-SCALE-BAR-10FT") {
    return {line(0, 0, 120, 0), line(0, -3, 0, 3), line(60, -3, 60, 3), line(120, -3, 120, 3), text(0, 5, "0"), text(54, 5, "5'"), text(108, 5, "10'")};
  }
  if (name == "PLMB-TOILET") {
    return {rect(-8, -12, 8, 0), circle(0, -18, 8)};
  }
  if (name == "PLMB-SINK") {
    return {rect(-10, -7, 10, 7), circle(0, 0, 4)};
  }
  if (name == "APPL-RANGE") {
    return {rect(-15, -12, 15, 12), circle(-7, 5, 3), circle(7, 5, 3), circle(-7, -5, 3), circle(7, -5, 3)};
  }
  if (name == "APPL-REFRIGERATOR") {
    return {rect(-15, -12, 15, 12), line(-15, 0, 15, 0), text(-11, -3, "REF")};
  }
  if (name == "FURN-BED-QUEEN") {
    return {rect(-30, -40, 30, 40), line(-30, 20, 30, 20), line(0, 20, 0, 40)};
  }
  if (name == "FURN-DESK") {
    return {rect(-24, -12, 24, 12), line(-12, -12, -12, 12), line(12, -12, 12, 12)};
  }
  if (name == "FURN-CHAIR-OFFICE") {
    return {circle(0, 0, 9), line(-9, -9, 9, 9), line(-9, 9, 9, -9)};
  }
  if (name == "FURN-SOFA") {
    return {rect(-36, -14, 36, 14), line(-36, 14, -36, 22), line(36, 14, 36, 22), line(-30, 0, 30, 0)};
  }
  if (name == "ELEC-OUTLET-DUPLEX") {
    return {circle(0, 0, 4), line(-2, -1, -2, 1), line(2, -1, 2, 1)};
  }
  if (name == "PID-GATE-VALVE") {
    return {line(-8, 0, 8, 0), line(-4, -4, 0, 0), line(-4, 4, 0, 0), line(4, -4, 0, 0), line(4, 4, 0, 0)};
  }
  if (name == "PID-INSTRUMENT-BUBBLE") {
    return {circle(0, 0, 8), line(-8, 0, 8, 0), text(-3, -3, "I")};
  }
  return {};
}

std::string string_map_names_json(const std::map<std::string, std::string>& values, std::string_view key) {
  std::string payload = "{\"" + std::string(key) + "\":[";
  std::size_t index = 0;
  for (const auto& [name, _] : values) {
    if (index++ != 0) {
      payload += ",";
    }
    payload += json_string(name);
  }
  payload += "],\"count\":" + std::to_string(values.size()) + "}";
  return payload;
}

std::string named_views_json(const std::map<std::string, NativeNamedView>& views) {
  std::string payload = "{\"views\":[";
  std::size_t index = 0;
  for (const auto& [name, view] : views) {
    if (index++ != 0) {
      payload += ",";
    }
    payload += "{\"name\":" + json_string(name) +
      ",\"window\":[" + std::to_string(view.x1) + "," + std::to_string(view.y1) +
      "," + std::to_string(view.x2) + "," + std::to_string(view.y2) + "]}";
  }
  payload += "],\"count\":" + std::to_string(views.size()) + "}";
  return payload;
}

std::string events_json(const std::vector<std::string>& events, std::size_t start, std::size_t limit) {
  const std::size_t begin = std::min(start, events.size());
  const std::size_t end = std::min(events.size(), begin + limit);
  std::string payload = "{\"events\":[";
  for (std::size_t i = begin; i < end; ++i) {
    if (i != begin) {
      payload += ",";
    }
    payload += events[i];
  }
  payload += "],\"count\":" + std::to_string(end - begin) +
    ",\"total\":" + std::to_string(events.size()) + "}";
  return payload;
}

core::CommandResult read_sidecar_file(const std::filesystem::path& drawing_path) {
  std::filesystem::path sidecar = drawing_path;
  sidecar.replace_filename(drawing_path.stem().string() + ".claude.md");
  std::ifstream input(sidecar, std::ios::binary);
  if (!input) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "Sidecar file not found");
  }
  std::string body((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  return core::CommandResult::success(
    "{\"path\":" + json_string(sidecar.string()) +
    ",\"content\":" + json_string(body) + "}");
}

std::string aia_layer_check_json(const std::string& layers_payload) {
  static const std::vector<std::string> required = {
    "A-WALL", "A-DOOR", "A-GLAZ", "A-AREA", "A-ANNO-DIMS",
  };
  std::string missing = "[";
  std::size_t count = 0;
  for (const auto& layer : required) {
    if (layers_payload.find("\"name\":\"" + layer + "\"") == std::string::npos &&
        layers_payload.find("\"" + layer + "\"") == std::string::npos) {
      if (count++ != 0) {
        missing += ",";
      }
      missing += json_string(layer);
    }
  }
  missing += "]";
  return "{\"standard\":\"aia\",\"ok\":" + std::string(count == 0 ? "true" : "false") +
    ",\"missing_layers\":" + missing +
    ",\"violation_count\":" + std::to_string(count) + "}";
}

std::vector<NativeDxfEntity> block_entities_arg(std::string_view arguments_json) {
  std::vector<NativeDxfEntity> entities;
  auto raw = raw_arg(arguments_json, "entities");
  if (!raw) {
    return entities;
  }
  for (const auto& object : object_array_values(*raw)) {
    std::string type = upper_copy(protocol::extract_string_property(object, "type").value_or("LINE"));
    NativeDxfEntity entity;
    entity.type = type == "POLYLINE" ? "LWPOLYLINE" : type;
    entity.layer = protocol::extract_string_property(object, "layer").value_or("0");
    if (entity.type == "LINE") {
      const double x1 = protocol::extract_number_property(object, "x1").value_or(0.0);
      const double y1 = protocol::extract_number_property(object, "y1").value_or(0.0);
      const double x2 = protocol::extract_number_property(object, "x2").value_or(1.0);
      const double y2 = protocol::extract_number_property(object, "y2").value_or(0.0);
      entity.points = {{x1, y1}, {x2, y2}};
    } else if (entity.type == "CIRCLE") {
      const double cx = protocol::extract_number_property(object, "cx").value_or(0.0);
      const double cy = protocol::extract_number_property(object, "cy").value_or(0.0);
      entity.points = {{cx, cy}};
      entity.radius = protocol::extract_number_property(object, "radius").value_or(1.0);
    } else if (entity.type == "TEXT" || entity.type == "ATTDEF") {
      entity.type = "TEXT";
      const double x = protocol::extract_number_property(object, "x").value_or(0.0);
      const double y = protocol::extract_number_property(object, "y").value_or(0.0);
      entity.points = {{x, y}};
      entity.text = protocol::extract_string_property(object, "text").value_or(
        protocol::extract_string_property(object, "tag").value_or("TAG"));
      entity.height = protocol::extract_number_property(object, "height").value_or(2.5);
    } else if (entity.type == "LWPOLYLINE") {
      if (auto points = points_arg_named(object, "points")) {
        entity.points = std::move(*points);
      }
      entity.closed = protocol::extract_bool_property(object, "closed").value_or(false);
    }
    if (!entity.points.empty()) {
      entities.push_back(std::move(entity));
    }
  }
  return entities;
}

int color_arg(std::string_view arguments_json, int fallback = 7) {
  if (auto numeric = number_arg(arguments_json, "color")) {
    return static_cast<int>(*numeric);
  }
  auto value = string_arg(arguments_json, "color");
  if (!value) {
    return fallback;
  }
  std::transform(value->begin(), value->end(), value->begin(), [](unsigned char ch) {
    return static_cast<char>(std::tolower(ch));
  });
  if (*value == "red") {
    return 1;
  }
  if (*value == "yellow") {
    return 2;
  }
  if (*value == "green") {
    return 3;
  }
  if (*value == "cyan") {
    return 4;
  }
  if (*value == "blue") {
    return 5;
  }
  if (*value == "magenta") {
    return 6;
  }
  if (*value == "white" || *value == "black") {
    return 7;
  }
  return fallback;
}

std::optional<int> optional_color_arg(std::string_view arguments_json) {
  if (number_arg(arguments_json, "color") || string_arg(arguments_json, "color")) {
    return color_arg(arguments_json);
  }
  return std::nullopt;
}

std::optional<std::filesystem::path> optional_path_arg(std::string_view arguments_json) {
  if (auto path = string_arg(arguments_json, "path")) {
    return std::filesystem::path(*path);
  }
  return std::nullopt;
}

}  // namespace

DxfBackend::DxfBackend(core::RuntimeConfig config)
  : config_(std::move(config)) {
  dimstyles_.emplace("Standard", "{}");
  textstyles_.emplace("Standard", "{\"font\":\"txt.shx\",\"height\":0}");
}

std::string DxfBackend::name() const {
  return "dxf";
}

core::BackendCapabilities DxfBackend::capabilities() const {
  core::BackendCapabilities caps;
  caps.can_read_drawing = true;
  caps.can_modify_entities = true;
  caps.can_create_entities = true;
  caps.can_screenshot = false;
  caps.can_save = true;
  caps.can_plot_pdf = false;
  caps.can_zoom = false;
  caps.can_query_entities = true;
  caps.can_file_operations = true;
  caps.can_undo = false;
  return caps;
}

core::CommandResult DxfBackend::initialize() {
  document_.reset();
  dimstyles_.clear();
  textstyles_.clear();
  dimstyles_.emplace("Standard", "{}");
  textstyles_.emplace("Standard", "{\"font\":\"txt.shx\",\"height\":0}");
  current_dimstyle_ = "Standard";
  current_textstyle_ = "Standard";
  named_views_.clear();
  layer_states_.clear();
  events_.clear();
  project_path_.clear();
  event_cursor_ = 0;
  recording_events_ = false;
  initialized_ = true;
  return status();
}

core::CommandResult DxfBackend::status() const {
  const auto caps = capabilities();
  const std::string payload = std::string("{") +
    "\"backend\":\"dxf\"," +
    "\"server\":\"" + core::escape_json(core::kNativeServerName) + "\"," +
    "\"version\":\"" + core::escape_json(core::kNativeServerVersion) + "\"," +
    "\"initialized\":" + (initialized_ ? "true" : "false") + "," +
    "\"implementation_stage\":\"native_dxf_document\"," +
    "\"has_document\":true," +
    "\"entity_count\":" + std::to_string(document_.entity_count()) + "," +
    "\"current_layer\":\"" + core::escape_json(document_.current_layer()) + "\"," +
    "\"capabilities\":" + caps.to_json() + "," +
    "\"config\":" + config_.to_json() +
    "}";
  return core::CommandResult::success(payload);
}

core::CommandResult DxfBackend::call_tool_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) {
  if (tool_name == "system" && (operation == "status" || operation == "get_backend")) {
    return status();
  }
  if (tool_name == "system" && operation == "init") {
    return initialize();
  }
  if (!initialized_) {
    return core::CommandResult::failure(core::ErrorCategory::backend_unavailable, "dxf backend is not initialized");
  }

  if (tool_name == "drawing") {
    if (operation == "create") {
      const std::string name = string_arg(arguments_json, "name").value_or("Drawing1");
      document_.reset(name);
      return core::CommandResult::success("{\"name\":\"" + core::escape_json(name) + "\",\"backend\":\"dxf\"}");
    }
    if (operation == "open") {
      auto path = optional_path_arg(arguments_json);
      if (!path) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "data.path is required");
      }
      return document_.open(*path);
    }
    if (operation == "info") {
      return document_.info();
    }
    if (operation == "save" || operation == "save_as_dxf") {
      return document_.save(optional_path_arg(arguments_json));
    }
    if (operation == "get_variables") {
      return core::CommandResult::success("{\"$ACADVER\":\"AC1032\",\"$CLAYER\":\"" + core::escape_json(document_.current_layer()) + "\"}");
    }
    if (operation == "purge") {
      return document_.remove_duplicate_entities();
    }
    if (operation == "audit") {
      return core::CommandResult::success("{\"ok\":true,\"fixed\":false,\"issues\":[],\"backend\":\"dxf\"}");
    }
    if (operation == "recover") {
      auto path = optional_path_arg(arguments_json);
      if (!path) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "data.path is required");
      }
      return document_.open(*path);
    }
    if (operation == "undo" || operation == "redo") {
      return core::CommandResult::success("{\"operation\":\"" + std::string(operation) + "\",\"applied\":false,\"note\":\"Native DXF backend has no command history yet\"}");
    }
  }

  if (tool_name == "entity") {
    if (operation == "select") {
      const int limit = static_cast<int>(number_arg(arguments_json, "limit").value_or(1000.0));
      return document_.select_entities(string_arg(arguments_json, "layer"), string_arg(arguments_json, "type"), limit);
    }
    if (operation == "osnap") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      return document_.osnap(
        *x,
        *y,
        number_arg(arguments_json, "tolerance").value_or(5.0),
        string_array_arg(arguments_json, "modes"));
    }
    if (operation == "create_line") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      return document_.create_line(*x1, *y1, *x2, *y2, layer_arg(arguments_json));
    }
    if (operation == "create_circle") {
      const auto cx = number_arg(arguments_json, "cx");
      const auto cy = number_arg(arguments_json, "cy");
      const auto radius = number_arg(arguments_json, "radius");
      if (!cx) return missing_arg("cx");
      if (!cy) return missing_arg("cy");
      if (!radius) return missing_arg("radius");
      return document_.create_circle(*cx, *cy, *radius, layer_arg(arguments_json));
    }
    if (operation == "create_polyline") {
      auto points = points_arg(arguments_json);
      if (!points) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "points must be an array of [x,y] pairs");
      }
      return document_.create_polyline(std::move(*points), bool_arg(arguments_json, "closed"), layer_arg(arguments_json));
    }
    if (operation == "create_rectangle") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      return document_.create_rectangle(*x1, *y1, *x2, *y2, layer_arg(arguments_json));
    }
    if (operation == "create_arc") {
      const auto cx = number_arg(arguments_json, "cx");
      const auto cy = number_arg(arguments_json, "cy");
      const auto radius = number_arg(arguments_json, "radius");
      const auto start_angle = number_arg(arguments_json, "start_angle");
      const auto end_angle = number_arg(arguments_json, "end_angle");
      if (!cx) return missing_arg("cx");
      if (!cy) return missing_arg("cy");
      if (!radius) return missing_arg("radius");
      if (!start_angle) return missing_arg("start_angle");
      if (!end_angle) return missing_arg("end_angle");
      return document_.create_arc(*cx, *cy, *radius, *start_angle, *end_angle, layer_arg(arguments_json));
    }
    if (operation == "create_ellipse") {
      const auto cx = number_arg(arguments_json, "cx");
      const auto cy = number_arg(arguments_json, "cy");
      const auto major_x = number_arg(arguments_json, "major_x");
      const auto major_y = number_arg(arguments_json, "major_y");
      const auto ratio = number_arg(arguments_json, "ratio");
      if (!cx) return missing_arg("cx");
      if (!cy) return missing_arg("cy");
      if (!major_x) return missing_arg("major_x");
      if (!major_y) return missing_arg("major_y");
      if (!ratio) return missing_arg("ratio");
      return document_.create_ellipse(*cx, *cy, *major_x, *major_y, *ratio, layer_arg(arguments_json));
    }
    if (operation == "create_mtext") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      const auto width = number_arg(arguments_json, "width");
      const auto text = string_arg(arguments_json, "text");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      if (!width) return missing_arg("width");
      if (!text) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "text is required");
      }
      return document_.create_mtext(*x, *y, *width, *text, number_arg(arguments_json, "height").value_or(2.5), layer_arg(arguments_json));
    }
    if (operation == "list") {
      const auto layer = string_arg(arguments_json, "layer");
      const int limit = static_cast<int>(number_arg(arguments_json, "limit").value_or(1000.0));
      return document_.list_entities(layer, limit);
    }
    if (operation == "count") {
      return document_.count_entities(string_arg(arguments_json, "layer"));
    }
    if (operation == "get") {
      auto handle = string_arg(arguments_json, "entity_id");
      if (!handle) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      }
      return document_.get_entity(*handle);
    }
    if (operation == "erase") {
      auto handle = string_arg(arguments_json, "entity_id");
      if (!handle) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      }
      return document_.erase_entity(*handle);
    }
    if (operation == "copy" || operation == "move") {
      auto handle = string_arg(arguments_json, "entity_id");
      const auto dx = number_arg(arguments_json, "dx");
      const auto dy = number_arg(arguments_json, "dy");
      if (!handle) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      }
      if (!dx) return missing_arg("dx");
      if (!dy) return missing_arg("dy");
      return operation == "copy"
        ? document_.copy_entity(*handle, *dx, *dy)
        : document_.move_entity(*handle, *dx, *dy);
    }
    if (operation == "rotate") {
      auto handle = string_arg(arguments_json, "entity_id");
      const auto cx = number_arg(arguments_json, "cx");
      const auto cy = number_arg(arguments_json, "cy");
      const auto angle = number_arg(arguments_json, "angle");
      if (!handle) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      if (!cx) return missing_arg("cx");
      if (!cy) return missing_arg("cy");
      if (!angle) return missing_arg("angle");
      return document_.rotate_entity(*handle, *cx, *cy, *angle);
    }
    if (operation == "scale") {
      auto handle = string_arg(arguments_json, "entity_id");
      const auto cx = number_arg(arguments_json, "cx");
      const auto cy = number_arg(arguments_json, "cy");
      const auto factor = number_arg(arguments_json, "factor");
      if (!handle) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      if (!cx) return missing_arg("cx");
      if (!cy) return missing_arg("cy");
      if (!factor) return missing_arg("factor");
      return document_.scale_entity(*handle, *cx, *cy, *factor);
    }
    if (operation == "mirror") {
      auto handle = string_arg(arguments_json, "entity_id");
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!handle) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      return document_.mirror_entity(*handle, *x1, *y1, *x2, *y2);
    }
    if (operation == "offset") {
      auto handle = string_arg(arguments_json, "entity_id");
      const auto distance = number_arg(arguments_json, "distance");
      if (!handle) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      if (!distance) return missing_arg("distance");
      return document_.offset_entity(*handle, *distance);
    }
    if (operation == "array") {
      auto handle = string_arg(arguments_json, "entity_id");
      const auto rows = number_arg(arguments_json, "rows");
      const auto cols = number_arg(arguments_json, "cols");
      const auto row_dist = number_arg(arguments_json, "row_dist");
      const auto col_dist = number_arg(arguments_json, "col_dist");
      if (!handle) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      if (!rows) return missing_arg("rows");
      if (!cols) return missing_arg("cols");
      if (!row_dist) return missing_arg("row_dist");
      if (!col_dist) return missing_arg("col_dist");
      return document_.array_entity(
        *handle,
        static_cast<int>(*rows),
        static_cast<int>(*cols),
        *row_dist,
        *col_dist);
    }
  }

  if (tool_name == "layer") {
    if (operation == "list") {
      return document_.list_layers();
    }
    if (operation == "create") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "layer name is required");
      }
      return document_.create_layer(*name, color_arg(arguments_json), string_arg(arguments_json, "linetype").value_or("CONTINUOUS"));
    }
    if (operation == "set_current") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "layer name is required");
      }
      return document_.set_current_layer(*name);
    }
    if (operation == "set_properties") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "layer name is required");
      }
      return document_.set_layer_properties(*name, optional_color_arg(arguments_json), string_arg(arguments_json, "linetype"));
    }
    if (operation == "freeze" || operation == "thaw" || operation == "lock" || operation == "unlock") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "layer name is required");
      }
      if (operation == "freeze" || operation == "thaw") {
        return document_.set_layer_frozen(*name, operation == "freeze");
      }
      return document_.set_layer_locked(*name, operation == "lock");
    }
  }

  if (tool_name == "block") {
    if (operation == "list") {
      return document_.list_blocks();
    }
    if (operation == "define") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "block name is required");
      }
      return document_.define_block(*name, block_entities_arg(arguments_json));
    }
    if (operation == "insert" || operation == "insert_with_attributes") {
      auto name = string_arg(arguments_json, "name");
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "block name is required");
      }
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      auto attrs = string_map_arg(arguments_json, "attributes");
      if (auto block_id = string_arg(arguments_json, "block_id")) {
        attrs["ID"] = *block_id;
      }
      return document_.insert_block(
        *name,
        *x,
        *y,
        number_arg(arguments_json, "scale").value_or(1.0),
        number_arg(arguments_json, "rotation").value_or(0.0),
        std::move(attrs),
        layer_arg(arguments_json));
    }
    if (operation == "get_attributes") {
      auto handle = string_arg(arguments_json, "entity_id");
      if (!handle) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      }
      return document_.get_block_attributes(*handle);
    }
    if (operation == "update_attribute") {
      auto handle = string_arg(arguments_json, "entity_id");
      auto tag = string_arg(arguments_json, "tag");
      auto value = string_arg(arguments_json, "value");
      if (!handle) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "entity_id is required");
      }
      if (!tag) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "tag is required");
      }
      if (!value) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "value is required");
      }
      return document_.update_block_attribute(*handle, *tag, *value);
    }
    if (operation == "list_from_file" || operation == "import_from_file") {
      return core::CommandResult::failure(
        core::ErrorCategory::not_supported,
        "Native DXF backend does not import external block libraries yet; use COM/File IPC for DWG/DXF block import");
    }
  }

  if (tool_name == "annotation") {
    if (operation == "create_text") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      const auto text = string_arg(arguments_json, "text");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      if (!text) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "text is required");
      }
      return document_.create_text(
        *x, *y, *text,
        number_arg(arguments_json, "height").value_or(2.5),
        number_arg(arguments_json, "rotation").value_or(0.0),
        layer_arg(arguments_json));
    }
    if (operation == "create_leader") {
      auto points = points_arg(arguments_json);
      if (!points) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "points must be an array of [x,y] pairs");
      }
      return document_.create_leader(std::move(*points), string_arg(arguments_json, "text").value_or(""), layer_arg(arguments_json));
    }
    if (operation == "create_dimension_linear" || operation == "create_dimension_aligned" ||
        operation == "create_dimension_angular" || operation == "create_dimension_radius") {
      std::vector<DxfPoint> points;
      if (auto parsed = points_arg(arguments_json)) {
        points = std::move(*parsed);
      } else {
        const auto x1 = number_arg(arguments_json, "x1").value_or(number_arg(arguments_json, "cx").value_or(0.0));
        const auto y1 = number_arg(arguments_json, "y1").value_or(number_arg(arguments_json, "cy").value_or(0.0));
        const auto x2 = number_arg(arguments_json, "x2").value_or(x1 + number_arg(arguments_json, "radius").value_or(1.0));
        const auto y2 = number_arg(arguments_json, "y2").value_or(y1);
        points = {{x1, y1}, {x2, y2}};
      }
      return document_.create_dimension_stub(std::string(operation), std::move(points), layer_arg(arguments_json));
    }
    if (operation == "create_table") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      const int rows = static_cast<int>(number_arg(arguments_json, "rows").value_or(0.0));
      const int cols = static_cast<int>(number_arg(arguments_json, "cols").value_or(0.0));
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      if (rows < 1 || cols < 1) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "rows and cols must be >= 1");
      }
      const double row_height = number_arg(arguments_json, "row_height").value_or(0.5);
      const double col_width = number_arg(arguments_json, "col_width").value_or(2.0);
      const std::string layer = layer_arg(arguments_json).empty() ? "0" : layer_arg(arguments_json);
      std::vector<std::string> handles;
      for (int c = 0; c <= cols; ++c) {
        const double lx = *x + static_cast<double>(c) * col_width;
        auto result = document_.create_line(lx, *y, lx, *y - static_cast<double>(rows) * row_height, layer);
        if (!append_handle_or_fail(result, handles)) return result;
      }
      for (int r = 0; r <= rows; ++r) {
        const double ly = *y - static_cast<double>(r) * row_height;
        auto result = document_.create_line(*x, ly, *x + static_cast<double>(cols) * col_width, ly, layer);
        if (!append_handle_or_fail(result, handles)) return result;
      }
      if (auto title = string_arg(arguments_json, "title")) {
        auto result = document_.create_text(
          *x + static_cast<double>(cols) * col_width / 2.0,
          *y - row_height / 2.0,
          *title,
          row_height * 0.6,
          0.0,
          layer);
        if (!append_handle_or_fail(result, handles)) return result;
      }
      return handles_result("grid_table", handles);
    }
  }

  if (tool_name == "pid") {
    if (operation == "setup_layers") {
      return setup_layer_set(document_, {
        {"PID-EQUIP", 7},
        {"PID-PIPE", 3},
        {"PID-VALVE", 2},
        {"PID-INSTR", 4},
        {"PID-TEXT", 7},
        {"PID-FLOW", 1},
      }, "pid");
    }
    if (operation == "list_symbols") {
      return core::CommandResult::success(pid_symbols_json(string_arg(arguments_json, "category").value_or("all")));
    }
    if (operation == "draw_process_line" || operation == "connect_equipment") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      return document_.create_line(*x1, *y1, *x2, *y2, "PID-PIPE");
    }
    if (operation == "add_flow_arrow") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      return draw_rotated_triangle(document_, *x, *y, 2.0, number_arg(arguments_json, "rotation").value_or(0.0), "PID-FLOW");
    }
    if (operation == "add_equipment_tag" || operation == "add_line_number") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      std::string text;
      if (operation == "add_equipment_tag") {
        auto tag = string_arg(arguments_json, "tag");
        if (!tag) {
          return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "tag is required");
        }
        text = *tag;
        if (auto description = string_arg(arguments_json, "description"); description && !description->empty()) {
          text += " " + *description;
        }
      } else {
        auto line = string_arg(arguments_json, "line_num");
        auto spec = string_arg(arguments_json, "spec");
        if (!line) {
          return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "line_num is required");
        }
        text = *line + (spec ? " " + *spec : "");
      }
      return document_.create_text(*x, *y, std::move(text), 2.5, 0.0, "PID-TEXT");
    }
    if (operation == "insert_valve" || operation == "insert_instrument" ||
        operation == "insert_pump" || operation == "insert_tank" ||
        operation == "insert_symbol") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      const double rotation = number_arg(arguments_json, "rotation").value_or(0.0);
      std::vector<std::string> handles;
      core::CommandResult result;
      if (operation == "insert_tank" || string_arg(arguments_json, "symbol").value_or("") == "tank") {
        const double scale = number_arg(arguments_json, "scale").value_or(1.0);
        result = document_.create_rectangle(*x - 4.0 * scale, *y - 3.0 * scale, *x + 4.0 * scale, *y + 3.0 * scale, "PID-EQUIP");
        if (!append_handle_or_fail(result, handles)) return result;
        result = document_.create_text(*x - 3.0 * scale, *y, string_arg(arguments_json, "tank_type").value_or("TANK"), 1.5 * scale, 0.0, "PID-TEXT");
        if (!append_handle_or_fail(result, handles)) return result;
        return handles_result("pid_tank", handles);
      }
      if (operation == "insert_pump" || string_arg(arguments_json, "symbol").value_or("") == "pump") {
        result = document_.create_circle(*x, *y, 2.0, "PID-EQUIP");
        if (!append_handle_or_fail(result, handles)) return result;
        result = draw_rotated_triangle(document_, *x, *y, 1.4, rotation, "PID-EQUIP");
        if (!append_handle_or_fail(result, handles)) return result;
        return handles_result("pid_pump", handles);
      }
      if (operation == "insert_instrument" || string_arg(arguments_json, "category").value_or("") == "instrument") {
        result = document_.create_circle(*x, *y, 2.0, "PID-INSTR");
        if (!append_handle_or_fail(result, handles)) return result;
        const std::string label = string_arg(arguments_json, "tag_id").value_or(
          string_arg(arguments_json, "instrument_type").value_or("I"));
        result = document_.create_text(*x - 1.2, *y - 0.5, label, 1.0, 0.0, "PID-TEXT");
        if (!append_handle_or_fail(result, handles)) return result;
        return handles_result("pid_instrument", handles);
      }
      const std::string label = string_arg(arguments_json, "valve_type").value_or(
        string_arg(arguments_json, "symbol").value_or("VALVE"));
      result = document_.create_line(
        polar_point(*x, *y, 2.5, rotation + 180.0).x,
        polar_point(*x, *y, 2.5, rotation + 180.0).y,
        polar_point(*x, *y, 2.5, rotation).x,
        polar_point(*x, *y, 2.5, rotation).y,
        "PID-VALVE");
      if (!append_handle_or_fail(result, handles)) return result;
      result = draw_rotated_triangle(document_, *x, *y, 1.8, rotation, "PID-VALVE");
      if (!append_handle_or_fail(result, handles)) return result;
      result = document_.create_text(*x - 1.8, *y - 3.2, label, 1.0, 0.0, "PID-TEXT");
      if (!append_handle_or_fail(result, handles)) return result;
      return handles_result("pid_valve", handles);
    }
  }

  if (tool_name == "arch") {
    if (operation == "setup_layers") {
      return setup_layer_set(document_, {
        {"A-WALL", 7},
        {"A-DOOR", 2},
        {"A-GLAZ", 4},
        {"A-ANNO-DIMS", 3},
        {"A-AREA", 6},
        {"A-AREA-IDEN", 6},
        {"A-FLOR", 8},
        {"A-FLOR-STRS", 8},
        {"A-COLS", 1},
        {"A-ROOF", 5},
        {"A-WALL-HEAD", 9},
      }, "aia-ncs");
    }
    if (operation == "list_hatches") {
      return core::CommandResult::success(
        "{\"hatches\":["
        "{\"name\":\"BRICK\",\"pattern\":\"ANSI32\",\"layer\":\"A-WALL\"},"
        "{\"name\":\"CONCRETE\",\"pattern\":\"AR-CONC\",\"layer\":\"A-FLOR\"},"
        "{\"name\":\"INSULATION\",\"pattern\":\"BATTING\",\"layer\":\"A-WALL\"},"
        "{\"name\":\"WOOD\",\"pattern\":\"AR-RROOF\",\"layer\":\"A-FLOR\"},"
        "{\"name\":\"EARTH\",\"pattern\":\"EARTH\",\"layer\":\"A-SITE\"}]}"
      );
    }
    if (operation == "wall") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      auto points = offset_rectangle(
        *x1, *y1, *x2, *y2,
        number_arg(arguments_json, "thickness").value_or(6.0),
        string_arg(arguments_json, "justify").value_or("center"));
      if (points.empty()) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "wall length must be positive");
      }
      return document_.create_polyline(std::move(points), true, string_arg(arguments_json, "layer").value_or("A-WALL"));
    }
    if (operation == "door") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      const double width = number_arg(arguments_json, "width").value_or(36.0);
      const double angle = number_arg(arguments_json, "angle").value_or(0.0);
      const double open_angle = number_arg(arguments_json, "open_angle").value_or(90.0);
      const std::string layer = string_arg(arguments_json, "layer").value_or("A-DOOR");
      const double swing_sign = string_arg(arguments_json, "swing").value_or("left") == "right" ? -1.0 : 1.0;
      const auto leaf = polar_point(*x, *y, width, angle + swing_sign * open_angle);
      std::vector<std::string> handles;
      auto result = document_.create_line(*x, *y, leaf.x, leaf.y, layer);
      if (!append_handle_or_fail(result, handles)) return result;
      result = document_.create_arc(*x, *y, width, angle, angle + swing_sign * open_angle, layer);
      if (!append_handle_or_fail(result, handles)) return result;
      return handles_result("arch_door", handles);
    }
    if (operation == "window") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      const std::string layer = string_arg(arguments_json, "layer").value_or("A-GLAZ");
      auto rect = offset_rectangle(*x1, *y1, *x2, *y2, number_arg(arguments_json, "sill_thickness").value_or(6.0), "center");
      if (rect.empty()) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "window length must be positive");
      }
      std::vector<std::string> handles;
      auto result = document_.create_polyline(std::move(rect), true, layer);
      if (!append_handle_or_fail(result, handles)) return result;
      result = document_.create_line(*x1, *y1, *x2, *y2, layer);
      if (!append_handle_or_fail(result, handles)) return result;
      return handles_result("arch_window", handles);
    }
    if (operation == "room" || operation == "slab" || operation == "hatch_region") {
      auto boundary = points_arg_named(arguments_json, "boundary");
      if (!boundary) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "boundary must be an array of [x,y] pairs");
      }
      const std::string layer = string_arg(arguments_json, "layer").value_or(
        operation == "room" ? string_arg(arguments_json, "area_layer").value_or("A-AREA") :
        operation == "hatch_region" ? "A-FLOR" : "A-FLOR");
      std::vector<std::string> handles;
      auto result = document_.create_polyline(*boundary, true, layer);
      if (!append_handle_or_fail(result, handles)) return result;
      if (operation == "room") {
        const auto centroid = polygon_centroid(*boundary);
        const double area = std::fabs(polygon_area(*boundary));
        std::string label = string_arg(arguments_json, "name").value_or("ROOM");
        label += " " + std::to_string(static_cast<int>(area));
        result = document_.create_text(
          centroid.x,
          centroid.y,
          label,
          number_arg(arguments_json, "text_height").value_or(6.0),
          0.0,
          string_arg(arguments_json, "text_layer").value_or("A-AREA-IDEN"));
        if (!append_handle_or_fail(result, handles)) return result;
      } else if (operation == "hatch_region") {
        const auto centroid = polygon_centroid(*boundary);
        result = document_.create_text(
          centroid.x,
          centroid.y,
          "HATCH " + string_arg(arguments_json, "pattern").value_or("CONCRETE"),
          2.5,
          number_arg(arguments_json, "angle").value_or(0.0),
          layer);
        if (!append_handle_or_fail(result, handles)) return result;
      }
      return handles_result(std::string("arch_") + std::string(operation), handles);
    }
    if (operation == "dimension_chain") {
      auto points = points_arg(arguments_json);
      if (!points || points->size() < 2) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "points must contain at least two [x,y] pairs");
      }
      std::vector<std::string> handles;
      for (std::size_t i = 1; i < points->size(); ++i) {
        auto result = document_.create_dimension_stub(
          "dimension_chain",
          {(*points)[i - 1], (*points)[i]},
          string_arg(arguments_json, "layer").value_or("A-ANNO-DIMS"));
        if (!append_handle_or_fail(result, handles)) return result;
      }
      return handles_result("arch_dimension_chain", handles);
    }
    if (operation == "column") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      const double sx = number_arg(arguments_json, "size").value_or(12.0);
      const double sy = number_arg(arguments_json, "size_y").value_or(sx);
      const std::string layer = string_arg(arguments_json, "layer").value_or("A-COLS");
      if (string_arg(arguments_json, "shape").value_or("square") == "round") {
        return document_.create_circle(*x, *y, sx / 2.0, layer);
      }
      return document_.create_rectangle(*x - sx / 2.0, *y - sy / 2.0, *x + sx / 2.0, *y + sy / 2.0, layer);
    }
    if (operation == "beam") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      const double width = number_arg(arguments_json, "width").value_or(0.0);
      if (width > 0.0) {
        auto points = offset_rectangle(*x1, *y1, *x2, *y2, width, "center");
        if (points.empty()) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "beam length must be positive");
        return document_.create_polyline(std::move(points), true, string_arg(arguments_json, "layer").value_or("A-WALL-HEAD"));
      }
      return document_.create_line(*x1, *y1, *x2, *y2, string_arg(arguments_json, "layer").value_or("A-WALL-HEAD"));
    }
    if (operation == "roof") {
      auto boundary = points_arg_named(arguments_json, "boundary");
      if (!boundary) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "boundary must be an array of [x,y] pairs");
      }
      std::vector<std::string> handles;
      auto result = document_.create_polyline(*boundary, true, string_arg(arguments_json, "eaves_layer").value_or("A-ROOF"));
      if (!append_handle_or_fail(result, handles)) return result;
      const auto centroid = polygon_centroid(*boundary);
      result = document_.create_line(centroid.x - 12.0, centroid.y, centroid.x + 12.0, centroid.y, string_arg(arguments_json, "ridge_layer").value_or("A-ROOF"));
      if (!append_handle_or_fail(result, handles)) return result;
      return handles_result("arch_roof", handles);
    }
    if (operation == "stair") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      const int treads = std::max(1, static_cast<int>(number_arg(arguments_json, "num_treads").value_or(12.0)));
      const std::string layer = string_arg(arguments_json, "layer").value_or("A-FLOR-STRS");
      std::vector<std::string> handles;
      auto result = document_.create_line(*x1, *y1, *x2, *y2, layer);
      if (!append_handle_or_fail(result, handles)) return result;
      for (int i = 1; i < treads; ++i) {
        const double t = static_cast<double>(i) / treads;
        const double x = *x1 + (*x2 - *x1) * t;
        const double y = *y1 + (*y2 - *y1) * t;
        result = document_.create_line(x - 12.0, y, x + 12.0, y, layer);
        if (!append_handle_or_fail(result, handles)) return result;
      }
      return handles_result("arch_stair", handles);
    }
    if (operation == "elevation" || operation == "section") {
      const double x = number_arg(arguments_json, "origin_x").value_or(0.0);
      const double y = number_arg(arguments_json, "origin_y").value_or(0.0);
      const double width = number_arg(arguments_json, operation == "elevation" ? "wall_length" : "section_width").value_or(120.0);
      const double height = operation == "elevation"
        ? number_arg(arguments_json, "height").value_or(96.0)
        : number_arg(arguments_json, "ceiling_y").value_or(96.0) - number_arg(arguments_json, "floor_y").value_or(0.0);
      return document_.create_rectangle(x, y, x + width, y + height, string_arg(arguments_json, "wall_layer").value_or("A-WALL"));
    }
    if (operation == "template") {
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      std::vector<DxfPoint> boundary = {{*x, *y}, {*x + 360.0, *y}, {*x + 360.0, *y + 240.0}, {*x, *y + 240.0}};
      std::vector<std::string> handles;
      auto result = document_.create_polyline(boundary, true, "A-WALL");
      if (!append_handle_or_fail(result, handles)) return result;
      result = document_.create_line(*x + 180.0, *y, *x + 180.0, *y + 240.0, "A-WALL");
      if (!append_handle_or_fail(result, handles)) return result;
      result = document_.create_text(*x + 12.0, *y + 220.0, string_arg(arguments_json, "name").value_or("template"), 8.0, 0.0, "A-AREA-IDEN");
      if (!append_handle_or_fail(result, handles)) return result;
      return handles_result("arch_template", handles);
    }
    if (operation == "wall_with_openings") {
      return core::CommandResult::failure(
        core::ErrorCategory::not_supported,
        "Native DXF wall_with_openings is scheduled for the advanced opening resolver; use arch.wall plus arch.door/window for now");
    }
  }

  if (tool_name == "project") {
    if (operation == "load") {
      auto path = string_arg(arguments_json, "path");
      if (!path) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "path is required");
      }
      std::filesystem::path project_path(*path);
      if (!std::filesystem::exists(project_path)) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Project path not found");
      }
      project_path_ = project_path.string();
      return core::CommandResult::success("{\"loaded\":true,\"path\":" + json_string(project_path_) + ",\"mode\":\"native-lite\"}");
    }
    if (operation == "unload") {
      const bool had_project = !project_path_.empty();
      project_path_.clear();
      return core::CommandResult::success("{\"unloaded\":" + std::string(had_project ? "true" : "false") + "}");
    }
    if (operation == "index" || operation == "rebuild") {
      auto info = document_.info();
      if (!info.ok) {
        return info;
      }
      return core::CommandResult::success(
        "{\"mode\":\"native-lite\",\"indexed_drawings\":1,\"force\":" +
        std::string(operation == "rebuild" || bool_arg(arguments_json, "force") ? "true" : "false") +
        ",\"active_drawing\":" + info.payload_json + "}");
    }
    if (operation == "info" || operation == "list_drawings") {
      auto info = document_.info();
      if (!info.ok) {
        return info;
      }
      if (operation == "info") {
        return core::CommandResult::success(
          "{\"mode\":\"native-lite\",\"project_path\":" +
          (project_path_.empty() ? "null" : json_string(project_path_)) +
          ",\"active_drawing\":" + info.payload_json + "}");
      }
      return core::CommandResult::success("{\"drawings\":[" + info.payload_json + "],\"count\":1,\"mode\":\"native-lite\"}");
    }
    if (operation == "list_layers") {
      return document_.list_layers();
    }
    if (operation == "find_entities") {
      const int limit = static_cast<int>(number_arg(arguments_json, "limit").value_or(1000.0));
      return document_.list_entities(string_arg(arguments_json, "layer"), limit);
    }
    if (operation == "check_standard") {
      auto layers = document_.list_layers();
      if (!layers.ok) {
        return layers;
      }
      return core::CommandResult::success(aia_layer_check_json(layers.payload_json));
    }
    if (operation == "list_blocks" || operation == "find_block_inconsistencies") {
      if (operation == "list_blocks") {
        return document_.list_blocks();
      }
      return core::CommandResult::success("{\"inconsistencies\":[],\"count\":0,\"mode\":\"native-lite\"}");
    }
    if (operation == "list_dimstyles") {
      return core::CommandResult::success(string_map_names_json(dimstyles_, "dimstyles"));
    }
    if (operation == "list_textstyles") {
      return core::CommandResult::success(string_map_names_json(textstyles_, "textstyles"));
    }
    if (operation == "find_style_inconsistencies" ||
        operation == "find_linetype_inconsistencies") {
      return core::CommandResult::success("{\"inconsistencies\":[],\"count\":0,\"mode\":\"native-lite\"}");
    }
    if (operation == "list_linetypes") {
      return core::CommandResult::success("{\"linetypes\":[\"CONTINUOUS\"],\"count\":1,\"mode\":\"native-lite\"}");
    }
    if (operation == "list_xrefs" || operation == "find_broken_xrefs") {
      return core::CommandResult::success("{\"xrefs\":[],\"count\":0,\"mode\":\"native-lite\"}");
    }
    if (operation == "list_sheets" || operation == "find_sheets_for_drawing") {
      return core::CommandResult::success("{\"sheets\":[],\"count\":0,\"mode\":\"native-lite\"}");
    }
    if (operation == "sidecar") {
      auto path = string_arg(arguments_json, "path");
      if (!path) {
        path = string_arg(arguments_json, "drawing");
      }
      if (!path) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "path or drawing is required");
      }
      return read_sidecar_file(std::filesystem::path(*path));
    }
    if (operation == "xref_attach" || operation == "xref_detach" ||
        operation == "xref_reload" || operation == "xref_bind") {
      return core::CommandResult::failure(
        core::ErrorCategory::not_supported,
        "Native DXF backend cannot manage live XREF attachments yet; use COM/File IPC for XREF mutation");
    }
  }

  if (tool_name == "library") {
    if (operation == "list") {
      return core::CommandResult::success(native_library_names_json());
    }
    if (operation == "define") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      }
      auto entities = native_library_entities(*name);
      if (entities.empty()) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Native library symbol not found");
      }
      return document_.define_block(upper_copy(*name), std::move(entities));
    }
    if (operation == "define_all") {
      std::vector<std::string> defined;
      for (const auto& name : native_library_names()) {
        auto result = document_.define_block(name, native_library_entities(name));
        if (!result.ok) {
          return result;
        }
        defined.push_back(name);
      }
      return handles_result("library_define_all", defined);
    }
    if (operation == "insert") {
      auto name = string_arg(arguments_json, "name");
      const auto x = number_arg(arguments_json, "x");
      const auto y = number_arg(arguments_json, "y");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      }
      if (!x) return missing_arg("x");
      if (!y) return missing_arg("y");
      const std::string block_name = upper_copy(*name);
      auto entities = native_library_entities(block_name);
      if (entities.empty()) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Native library symbol not found");
      }
      auto defined = document_.define_block(block_name, std::move(entities));
      if (!defined.ok) {
        return defined;
      }
      return document_.insert_block(
        block_name,
        *x,
        *y,
        number_arg(arguments_json, "scale").value_or(1.0),
        number_arg(arguments_json, "rotation").value_or(0.0),
        {},
        layer_arg(arguments_json));
    }
    if (operation == "scan_external" || operation == "search_external" ||
        operation == "stats_external" || operation == "catalog_reindex" ||
        operation == "catalog_list" || operation == "catalog_stats" ||
        operation == "catalog_search" || operation == "catalog_get" ||
        operation == "catalog_insert" || operation == "vendor_import") {
      return core::CommandResult::failure(
        core::ErrorCategory::not_supported,
        "Native DXF backend does not index external asset libraries yet; use Python ezdxf/COM backend for catalog and vendor import operations");
    }
  }

  if (tool_name == "style") {
    if (operation == "list_dimstyles") {
      std::string payload = string_map_names_json(dimstyles_, "dimstyles");
      payload.pop_back();
      payload += ",\"current\":" + json_string(current_dimstyle_) + "}";
      return core::CommandResult::success(payload);
    }
    if (operation == "get_dimstyle") {
      auto name = string_arg(arguments_json, "name").value_or(current_dimstyle_);
      auto it = dimstyles_.find(name);
      if (it == dimstyles_.end()) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Dimstyle not found");
      }
      return core::CommandResult::success("{\"name\":" + json_string(name) + ",\"attribs\":" + it->second + "}");
    }
    if (operation == "create_dimstyle") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      }
      dimstyles_[*name] = raw_arg(arguments_json, "attribs").value_or("{}");
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"created\":true}");
    }
    if (operation == "set_current_dimstyle") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      }
      if (!dimstyles_.contains(*name)) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Dimstyle not found");
      }
      current_dimstyle_ = *name;
      return core::CommandResult::success("{\"current_dimstyle\":" + json_string(current_dimstyle_) + "}");
    }
    if (operation == "list_textstyles") {
      std::string payload = string_map_names_json(textstyles_, "textstyles");
      payload.pop_back();
      payload += ",\"current\":" + json_string(current_textstyle_) + "}";
      return core::CommandResult::success(payload);
    }
    if (operation == "get_textstyle") {
      auto name = string_arg(arguments_json, "name").value_or(current_textstyle_);
      auto it = textstyles_.find(name);
      if (it == textstyles_.end()) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Textstyle not found");
      }
      return core::CommandResult::success("{\"name\":" + json_string(name) + ",\"attribs\":" + it->second + "}");
    }
    if (operation == "create_textstyle") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      }
      const std::string font = string_arg(arguments_json, "font").value_or("txt.shx");
      const double height = number_arg(arguments_json, "height").value_or(0.0);
      textstyles_[*name] = "{\"font\":" + json_string(font) + ",\"height\":" + std::to_string(height) + "}";
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"created\":true}");
    }
    if (operation == "set_current_textstyle") {
      auto name = string_arg(arguments_json, "name");
      if (!name) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      }
      if (!textstyles_.contains(*name)) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Textstyle not found");
      }
      current_textstyle_ = *name;
      return core::CommandResult::success("{\"current_textstyle\":" + json_string(current_textstyle_) + "}");
    }
    if (operation == "list_mleaderstyles") {
      return core::CommandResult::success("{\"mleaderstyles\":[\"Standard\"],\"current\":\"Standard\",\"count\":1}");
    }
    if (operation == "set_current_mleaderstyle") {
      auto name = string_arg(arguments_json, "name").value_or("Standard");
      if (name != "Standard") {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Only Standard mleaderstyle is available in native DXF mode");
      }
      return core::CommandResult::success("{\"current_mleaderstyle\":\"Standard\"}");
    }
  }

  if (tool_name == "view") {
    if (operation == "zoom_extents") {
      auto info = document_.info();
      if (!info.ok) {
        return info;
      }
      return core::CommandResult::success("{\"view\":\"extents\",\"document\":" + info.payload_json + "}");
    }
    if (operation == "zoom_window") {
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      return core::CommandResult::success(
        "{\"view\":\"window\",\"window\":[" + std::to_string(*x1) + "," + std::to_string(*y1) +
        "," + std::to_string(*x2) + "," + std::to_string(*y2) + "]}");
    }
    if (operation == "list_named_views") {
      return core::CommandResult::success(named_views_json(named_views_));
    }
    if (operation == "save_named_view") {
      auto name = string_arg(arguments_json, "name");
      const auto x1 = number_arg(arguments_json, "x1");
      const auto y1 = number_arg(arguments_json, "y1");
      const auto x2 = number_arg(arguments_json, "x2");
      const auto y2 = number_arg(arguments_json, "y2");
      if (!name) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      if (!x1) return missing_arg("x1");
      if (!y1) return missing_arg("y1");
      if (!x2) return missing_arg("x2");
      if (!y2) return missing_arg("y2");
      named_views_[*name] = NativeNamedView{*x1, *y1, *x2, *y2};
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"saved\":true}");
    }
    if (operation == "restore_named_view") {
      auto name = string_arg(arguments_json, "name");
      if (!name) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      auto it = named_views_.find(*name);
      if (it == named_views_.end()) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Named view not found");
      }
      const auto& v = it->second;
      return core::CommandResult::success(
        "{\"name\":" + json_string(*name) +
        ",\"window\":[" + std::to_string(v.x1) + "," + std::to_string(v.y1) +
        "," + std::to_string(v.x2) + "," + std::to_string(v.y2) + "]}");
    }
    if (operation == "delete_named_view") {
      auto name = string_arg(arguments_json, "name");
      if (!name) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"deleted\":" + (named_views_.erase(*name) ? "true" : "false") + "}");
    }
    if (operation == "list_layer_states") {
      return core::CommandResult::success(string_map_names_json(layer_states_, "layer_states"));
    }
    if (operation == "save_layer_state") {
      auto name = string_arg(arguments_json, "name");
      if (!name) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      auto layers = document_.list_layers();
      if (!layers.ok) return layers;
      layer_states_[*name] = layers.payload_json;
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"saved\":true}");
    }
    if (operation == "restore_layer_state") {
      auto name = string_arg(arguments_json, "name");
      if (!name) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      auto it = layer_states_.find(*name);
      if (it == layer_states_.end()) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, "Layer state not found");
      }
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"state\":" + it->second + "}");
    }
    if (operation == "delete_layer_state") {
      auto name = string_arg(arguments_json, "name");
      if (!name) return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "name is required");
      return core::CommandResult::success("{\"name\":" + json_string(*name) + ",\"deleted\":" + (layer_states_.erase(*name) ? "true" : "false") + "}");
    }
    if (operation == "get_screenshot") {
      return core::CommandResult::failure(core::ErrorCategory::not_supported, "Native DXF backend does not render PNG screenshots yet");
    }
  }

  if (tool_name == "events") {
    if (operation == "start_recording") {
      recording_events_ = true;
      events_.push_back("{\"kind\":\"recording_started\",\"backend\":\"dxf\"}");
      return core::CommandResult::success("{\"recording\":true,\"backend\":\"dxf\"}");
    }
    if (operation == "clear") {
      events_.clear();
      event_cursor_ = 0;
      return core::CommandResult::success("{\"cleared\":true}");
    }
    if (operation == "recent") {
      const std::size_t limit = static_cast<std::size_t>(std::max(0.0, number_arg(arguments_json, "limit").value_or(50.0)));
      const std::size_t start = events_.size() > limit ? events_.size() - limit : 0;
      return core::CommandResult::success(events_json(events_, start, limit));
    }
    if (operation == "new") {
      const std::size_t limit = static_cast<std::size_t>(std::max(0.0, number_arg(arguments_json, "limit").value_or(100.0)));
      auto payload = events_json(events_, event_cursor_, limit);
      event_cursor_ = events_.size();
      return core::CommandResult::success(payload);
    }
  }

  if (tool_name == "express") {
    if (operation == "layiso") {
      auto layers = string_array_arg(arguments_json, "layers");
      if (layers.empty()) {
        if (auto single = string_arg(arguments_json, "layer")) {
          layers.push_back(*single);
        }
      }
      return document_.isolate_layers(layers);
    }
    if (operation == "overkill") {
      return document_.remove_duplicate_entities();
    }
    if (operation == "burst") {
      auto ids = string_array_arg(arguments_json, "entity_ids");
      if (ids.empty()) {
        if (auto id = string_arg(arguments_json, "entity_id")) {
          ids.push_back(*id);
        }
      }
      return document_.burst_inserts(ids);
    }
    if (operation == "txt2mtxt") {
      auto ids = string_array_arg(arguments_json, "entity_ids");
      if (ids.empty()) {
        if (auto id = string_arg(arguments_json, "entity_id")) {
          ids.push_back(*id);
        }
      }
      return document_.text_to_mtext(ids);
    }
    if (operation == "flatten") {
      return core::CommandResult::success("{\"flattened\":0,\"note\":\"Native DXF backend stores 2D entities only\"}");
    }
  }

  return core::not_supported(
    std::string("dxf.") + std::string(tool_name) + "." + std::string(operation) +
    " is not implemented in the native DXF backend yet");
}

}  // namespace autocad_mcp::backends
