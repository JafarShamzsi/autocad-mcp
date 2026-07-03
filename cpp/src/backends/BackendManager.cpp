#include "backends/BackendManager.hpp"

#include "backends/ComBackend.hpp"
#include "backends/DxfBackend.hpp"
#include "backends/FileIpcBackend.hpp"
#include "core/Config.hpp"

#include <memory>

namespace autocad_mcp::backends {

BackendManager::BackendManager() {
  reset();
}

IBackend& BackendManager::backend() {
  return *backend_;
}

const IBackend& BackendManager::backend() const {
  return *backend_;
}

void BackendManager::reset() {
  auto config = core::load_runtime_config();
  std::unique_ptr<IBackend> next;
  if (config.selected_backend == "com") {
    next = std::make_unique<ComBackend>(std::move(config));
  } else if (config.selected_backend == "file_ipc") {
    next = std::make_unique<FileIpcBackend>(std::move(config));
  } else {
    next = std::make_unique<DxfBackend>(std::move(config));
  }
  (void)next->initialize();
  backend_ = std::move(next);
}

}  // namespace autocad_mcp::backends
