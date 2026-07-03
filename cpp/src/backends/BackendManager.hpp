#pragma once

#include "backends/IBackend.hpp"

#include <memory>

namespace autocad_mcp::backends {

class BackendManager {
 public:
  BackendManager();

  [[nodiscard]] IBackend& backend();
  [[nodiscard]] const IBackend& backend() const;
  void reset();

 private:
  std::unique_ptr<IBackend> backend_;
};

}  // namespace autocad_mcp::backends

