cmake_minimum_required(VERSION 3.20)
project(lamda_hsi VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# -----------------------------------------------------------------------------
# Platform detection: which ASI lib variant to link
# -----------------------------------------------------------------------------
set(ASI_SDK_ROOT "${CMAKE_SOURCE_DIR}/ASI_linux_mac_SDK_V1.41")
set(ASI_INCLUDE_DIR "${ASI_SDK_ROOT}/include")

if(APPLE)
  if(CMAKE_SYSTEM_PROCESSOR MATCHES "arm64|aarch64")
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/mac_arm64")
    set(ASI_LIB_FILE "${ASI_LIB_DIR}/libASICamera2.a")
    set(ASI_LIB_STATIC TRUE)
  else()
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/mac")
    set(ASI_LIB_FILE "${ASI_LIB_DIR}/libASICamera2.dylib")
    set(ASI_LIB_STATIC FALSE)
  endif()
elseif(UNIX)
  if(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|amd64")
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/x64")
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "i[3-6]86")
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/x86")
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/armv8")
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "armv7")
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/armv7")
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "armv6")
    set(ASI_LIB_DIR "${ASI_SDK_ROOT}/lib/armv6")
  else()
    message(FATAL_ERROR "Unsupported Linux architecture: ${CMAKE_SYSTEM_PROCESSOR}")
  endif()
  set(ASI_LIB_FILE "${ASI_LIB_DIR}/libASICamera2.so")
  set(ASI_LIB_STATIC FALSE)
else()
  message(FATAL_ERROR "Unsupported platform")
endif()

if(NOT EXISTS "${ASI_LIB_FILE}")
  message(FATAL_ERROR "ASI library not found: ${ASI_LIB_FILE}")
endif()
message(STATUS "ASI library: ${ASI_LIB_FILE}")

add_library(asi_sdk INTERFACE)
target_include_directories(asi_sdk INTERFACE "${ASI_INCLUDE_DIR}")
target_link_libraries(asi_sdk INTERFACE "${ASI_LIB_FILE}")
if(APPLE)
  target_compile_definitions(asi_sdk INTERFACE _MAC)
elseif(UNIX)
  target_compile_definitions(asi_sdk INTERFACE _LIN GLIBC_20)
endif()

# -----------------------------------------------------------------------------
# libusb-1.0 (required by the ASI driver)
# -----------------------------------------------------------------------------
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBUSB REQUIRED IMPORTED_TARGET libusb-1.0)
target_link_libraries(asi_sdk INTERFACE PkgConfig::LIBUSB)

# -----------------------------------------------------------------------------
# Platform-specific frameworks/libs the static ASI lib pulls in
# -----------------------------------------------------------------------------
if(APPLE)
  find_library(IOKIT_FRAMEWORK IOKit REQUIRED)
  find_library(COREFOUNDATION_FRAMEWORK CoreFoundation REQUIRED)
  find_library(SECURITY_FRAMEWORK Security REQUIRED)
  find_library(OBJC_LIB objc REQUIRED)
  target_link_libraries(asi_sdk INTERFACE
    ${IOKIT_FRAMEWORK} ${COREFOUNDATION_FRAMEWORK} ${SECURITY_FRAMEWORK} ${OBJC_LIB})
elseif(UNIX)
  find_package(Threads REQUIRED)
  target_link_libraries(asi_sdk INTERFACE Threads::Threads ${CMAKE_DL_LIBS} rt)
endif()

# -----------------------------------------------------------------------------
# Third-party: GLFW + Dear ImGui via FetchContent
# -----------------------------------------------------------------------------
include(FetchContent)
set(FETCHCONTENT_QUIET FALSE)

# GLFW
set(GLFW_BUILD_DOCS     OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(GLFW_INSTALL        OFF CACHE BOOL "" FORCE)
# On Linux, build X11 only — Wayland needs extra dev packages and XWayland
# works fine for our use case.
set(GLFW_BUILD_WAYLAND  OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_X11      ON  CACHE BOOL "" FORCE)
FetchContent_Declare(glfw
  GIT_REPOSITORY https://github.com/glfw/glfw.git
  GIT_TAG        3.4
  GIT_SHALLOW    TRUE)
FetchContent_MakeAvailable(glfw)

# Dear ImGui (docking branch — better windowing for our multi-panel UI)
FetchContent_Declare(imgui
  GIT_REPOSITORY https://github.com/ocornut/imgui.git
  GIT_TAG        v1.91.5-docking
  GIT_SHALLOW    TRUE)
FetchContent_MakeAvailable(imgui)

add_library(imgui STATIC
  ${imgui_SOURCE_DIR}/imgui.cpp
  ${imgui_SOURCE_DIR}/imgui_draw.cpp
  ${imgui_SOURCE_DIR}/imgui_tables.cpp
  ${imgui_SOURCE_DIR}/imgui_widgets.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_glfw.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_opengl3.cpp)
target_include_directories(imgui PUBLIC
  ${imgui_SOURCE_DIR}
  ${imgui_SOURCE_DIR}/backends)
target_link_libraries(imgui PUBLIC glfw)
if(APPLE)
  target_link_libraries(imgui PUBLIC "-framework OpenGL")
elseif(UNIX)
  find_package(OpenGL REQUIRED)
  target_link_libraries(imgui PUBLIC OpenGL::GL)
endif()

# ImPlot — scientific plotting addon for Dear ImGui.
FetchContent_Declare(implot
  GIT_REPOSITORY https://github.com/epezent/implot.git
  GIT_TAG        v0.16
  GIT_SHALLOW    TRUE)
FetchContent_MakeAvailable(implot)

add_library(implot STATIC
  ${implot_SOURCE_DIR}/implot.cpp
  ${implot_SOURCE_DIR}/implot_items.cpp)
target_include_directories(implot PUBLIC ${implot_SOURCE_DIR})
target_link_libraries(implot PUBLIC imgui)

# stb_image_write (single header, public domain) — PNG export
FetchContent_Declare(stb
  GIT_REPOSITORY https://github.com/nothings/stb.git
  GIT_TAG        master
  GIT_SHALLOW    TRUE)
FetchContent_MakeAvailable(stb)

add_library(stb_image_write STATIC
  ${CMAKE_SOURCE_DIR}/src/third_party/stb_image_write_impl.cpp)
target_include_directories(stb_image_write PUBLIC ${stb_SOURCE_DIR})

# -----------------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------------
set(MC_SOURCES
  src/main.cpp
  src/camera.cpp
  src/capture_thread.cpp
  src/gui.cpp
  src/image_writer.cpp
  src/calibration.cpp
  src/serial_port.cpp
  src/monochromator.cpp
  src/cube.cpp)
if(APPLE)
  list(APPEND MC_SOURCES src/platform_mac.mm)
endif()

add_executable(lamda_hsi ${MC_SOURCES})

if(APPLE)
  find_library(COCOA_FRAMEWORK Cocoa REQUIRED)
  target_link_libraries(lamda_hsi PRIVATE ${COCOA_FRAMEWORK})
endif()

# Copy logo.png next to the binary at build time so we can load it at runtime.
if(EXISTS "${CMAKE_SOURCE_DIR}/logo.png")
  add_custom_command(TARGET lamda_hsi POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${CMAKE_SOURCE_DIR}/logo.png"
            "$<TARGET_FILE_DIR:lamda_hsi>/logo.png"
    COMMENT "Copying app icon next to binary")
endif()

if(UNIX AND NOT APPLE)
  set(MC_DESKTOP_EXEC "$<TARGET_FILE:lamda_hsi>")
  set(MC_DESKTOP_ICON "$<TARGET_FILE_DIR:lamda_hsi>/logo.png")
  file(GENERATE
    OUTPUT "${CMAKE_BINARY_DIR}/lamda_hsi.desktop"
    INPUT "${CMAKE_SOURCE_DIR}/packaging/linux/lamda_hsi.desktop.in")
endif()

target_include_directories(lamda_hsi PRIVATE src)
target_compile_definitions(lamda_hsi PRIVATE
  LAMDA_HSI_VERSION="${PROJECT_VERSION}")
target_link_libraries(lamda_hsi PRIVATE
  asi_sdk imgui implot stb_image_write)

# On Linux when linking the .so, embed rpath so the binary finds it at runtime
if(UNIX AND NOT APPLE AND NOT ASI_LIB_STATIC)
  set_target_properties(lamda_hsi PROPERTIES
    BUILD_RPATH "${ASI_LIB_DIR}"
    INSTALL_RPATH "${ASI_LIB_DIR}")
endif()

if(APPLE AND NOT ASI_LIB_STATIC)
  set_target_properties(lamda_hsi PROPERTIES
    BUILD_RPATH "${ASI_LIB_DIR}")
endif()

# Warnings
target_compile_options(lamda_hsi PRIVATE
  -Wall -Wextra -Wpedantic -Wno-unused-parameter)

message(STATUS "")
message(STATUS "  LAMDA HyperSpectral Imaging System configuration")
message(STATUS "    version    : ${PROJECT_VERSION}")
message(STATUS "    system     : ${CMAKE_SYSTEM_NAME} ${CMAKE_SYSTEM_PROCESSOR}")
message(STATUS "    build type : ${CMAKE_BUILD_TYPE}")
message(STATUS "    ASI lib    : ${ASI_LIB_FILE}")
message(STATUS "    libusb     : ${LIBUSB_LIBRARIES}")
message(STATUS "")
