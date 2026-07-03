cmake_minimum_required(VERSION 3.25)

project(autocad_mcp_native VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(autocad-mcp-native
  src/main.cpp
  src/backends/BackendManager.cpp
  src/backends/ComBackend.cpp
  src/backends/DxfBackend.cpp
  src/backends/FileIpcBackend.cpp
  src/backends/FileIpcTransport.cpp
  src/backends/NativeDxfDocument.cpp
  src/core/Config.cpp
  src/protocol/JsonRpc.cpp
  src/protocol/McpServer.cpp
  src/protocol/StdioTransport.cpp
  src/platform/win32/ComAutomation.cpp
  src/platform/win32/WindowMessaging.cpp
  src/tools/ToolRegistry.cpp
)

target_include_directories(autocad-mcp-native PRIVATE src)

if(MSVC)
  target_compile_options(autocad-mcp-native PRIVATE /W4 /permissive- /EHsc)
else()
  target_compile_options(autocad-mcp-native PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(WIN32)
  target_link_libraries(autocad-mcp-native PRIVATE user32 gdi32 ole32 oleaut32 windowscodecs)
endif()

enable_testing()

add_test(NAME native_smoke_version
  COMMAND autocad-mcp-native --version
)
