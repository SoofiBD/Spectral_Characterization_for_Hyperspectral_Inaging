// LAMDA HyperSpectral Imaging System — lab HSI acquisition with ZWO ASI cameras.
//
// Build:  cmake -S . -B build && cmake --build build -j
// Run  :  ./build/lamda_hsi

#include "gui.h"

#include "imgui.h"
#include "imgui_impl_glfw.h"
#include "imgui_impl_opengl3.h"
#include "implot.h"

#define GL_SILENCE_DEPRECATION
#ifdef __APPLE__
  #include <OpenGL/gl3.h>
#else
  #include <GL/gl.h>
#endif

#include <GLFW/glfw3.h>

#include "stb_image.h"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>

#if defined(__linux__)
  #include <unistd.h>
#endif

#ifdef __APPLE__
namespace mc {
  void macSetDarkTitleBar(GLFWwindow*);
  void macSetDockIcon(const char* pngPath);
}
#endif

namespace {

#ifndef LAMDA_HSI_VERSION
#define LAMDA_HSI_VERSION "dev"
#endif

void glfwErrorCallback(int err, const char* desc) {
  std::fprintf(stderr, "GLFW error %d: %s\n", err, desc);
}

// Locate logo.png next to the executable (where CMake copies it). Returns
// empty string if the file isn't found.
std::string findLogoPath() {
  namespace fs = std::filesystem;
  #if defined(__linux__)
  char exePath[4096] = {0};
  ssize_t n = readlink("/proc/self/exe", exePath, sizeof(exePath) - 1);
  if (n > 0) {
    exePath[n] = 0;
    fs::path p = fs::path(exePath).parent_path() / "logo.png";
    if (fs::exists(p)) return fs::absolute(p).string();
  }
  #endif

  static const char* candidates[] = {
      "logo.png", "../logo.png", "../../logo.png"};
  for (const char* p : candidates) {
    if (fs::exists(p)) return fs::absolute(p).string();
  }
  return {};
}

void setAppIcon(GLFWwindow* window) {
  std::string path = findLogoPath();
  if (path.empty()) return;

#ifdef __APPLE__
  // macOS uses the Dock icon, not a per-window icon. GLFW would emit a
  // warning if we called glfwSetWindowIcon here, so route through Cocoa.
  (void)window;
  mc::macSetDockIcon(path.c_str());
#else
  int w = 0, h = 0, c = 0;
  unsigned char* pixels = stbi_load(path.c_str(), &w, &h, &c, 4);
  if (!pixels) return;
  GLFWimage img{w, h, pixels};
  glfwSetWindowIcon(window, 1, &img);
  stbi_image_free(pixels);
#endif
}

// Try a handful of well-known system font paths. Return the first one that
// exists, or nullptr if none found.
const char* findSystemFont() {
  static const char* candidates[] = {
      // macOS
      "/System/Library/Fonts/SFNS.ttf",
      "/System/Library/Fonts/Supplemental/Arial.ttf",
      "/Library/Fonts/Arial.ttf",
      // Linux (Ubuntu/Debian)
      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
      "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
      "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
      // Arch / Fedora
      "/usr/share/fonts/TTF/DejaVuSans.ttf",
      "/usr/share/fonts/dejavu/DejaVuSans.ttf",
  };
  for (const char* path : candidates) {
    if (std::filesystem::exists(path)) return path;
  }
  return nullptr;
}

}  // namespace

int main(int, char**) {
  glfwSetErrorCallback(glfwErrorCallback);
  if (!glfwInit()) {
    std::fprintf(stderr, "glfwInit failed\n");
    return EXIT_FAILURE;
  }

  // Request a 3.3 core context (works on both macOS and Linux Mesa).
  glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
  glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
  glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
  glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);
  #if defined(__linux__)
  glfwWindowHintString(GLFW_X11_CLASS_NAME, "lamda_hsi");
  glfwWindowHintString(GLFW_X11_INSTANCE_NAME, "lamda_hsi");
  #endif
  const char* glslVersion = "#version 330";

  std::string windowTitle =
      std::string("LAMDA HyperSpectral Imaging System v") + LAMDA_HSI_VERSION;
  GLFWwindow* window = glfwCreateWindow(
      1600, 1000, windowTitle.c_str(), nullptr, nullptr);
  if (!window) {
    std::fprintf(stderr, "glfwCreateWindow failed\n");
    glfwTerminate();
    return EXIT_FAILURE;
  }
  glfwMakeContextCurrent(window);
  glfwSwapInterval(1);  // vsync

  setAppIcon(window);

#ifdef __APPLE__
  mc::macSetDarkTitleBar(window);
#endif

  IMGUI_CHECKVERSION();
  ImGui::CreateContext();
  ImPlot::CreateContext();
  ImGuiIO& io = ImGui::GetIO();
  io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
  io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;

  // Load a nicer system font at ImGui's default size. No DPI scaling — the
  // built-in style sizes stay as-is.
  if (const char* fontPath = findSystemFont()) {
    ImFontConfig cfg;
    cfg.OversampleH = 2;
    cfg.OversampleV = 2;
    io.Fonts->AddFontFromFileTTF(fontPath, 14.0f, &cfg);
  } else {
    io.Fonts->AddFontDefault();
  }

  ImGui::StyleColorsDark();
  // Subtle UI refinement — rounded corners, tighter accents, deeper background.
  {
    ImGuiStyle& s = ImGui::GetStyle();
    s.WindowRounding   = 5.0f;
    s.FrameRounding    = 4.0f;
    s.GrabRounding     = 4.0f;
    s.PopupRounding    = 4.0f;
    s.TabRounding      = 4.0f;
    s.ScrollbarRounding= 4.0f;
    s.WindowPadding    = ImVec2(10, 10);
    s.FramePadding     = ImVec2(8, 4);
    s.ItemSpacing      = ImVec2(8, 6);
    s.ItemInnerSpacing = ImVec2(6, 4);
    s.IndentSpacing    = 18.0f;
    s.ScrollbarSize    = 12.0f;
    s.GrabMinSize      = 12.0f;
    s.FrameBorderSize  = 0.0f;
    s.WindowBorderSize = 1.0f;
    s.TabBorderSize    = 0.0f;
    s.WindowTitleAlign = ImVec2(0.0f, 0.5f);

    ImVec4* c = s.Colors;
    c[ImGuiCol_WindowBg]          = ImVec4(0.10f, 0.10f, 0.11f, 1.00f);
    c[ImGuiCol_ChildBg]           = ImVec4(0.12f, 0.12f, 0.13f, 1.00f);
    c[ImGuiCol_PopupBg]           = ImVec4(0.13f, 0.13f, 0.14f, 0.98f);
    c[ImGuiCol_Border]            = ImVec4(0.22f, 0.22f, 0.24f, 1.00f);
    c[ImGuiCol_FrameBg]           = ImVec4(0.16f, 0.16f, 0.18f, 1.00f);
    c[ImGuiCol_FrameBgHovered]    = ImVec4(0.22f, 0.22f, 0.25f, 1.00f);
    c[ImGuiCol_FrameBgActive]     = ImVec4(0.26f, 0.26f, 0.30f, 1.00f);
    c[ImGuiCol_TitleBg]           = ImVec4(0.10f, 0.10f, 0.11f, 1.00f);
    c[ImGuiCol_TitleBgActive]     = ImVec4(0.15f, 0.18f, 0.24f, 1.00f);
    c[ImGuiCol_MenuBarBg]         = ImVec4(0.12f, 0.12f, 0.13f, 1.00f);
    c[ImGuiCol_Header]            = ImVec4(0.20f, 0.30f, 0.45f, 0.55f);
    c[ImGuiCol_HeaderHovered]     = ImVec4(0.26f, 0.42f, 0.62f, 0.80f);
    c[ImGuiCol_HeaderActive]      = ImVec4(0.30f, 0.50f, 0.74f, 1.00f);
    c[ImGuiCol_Button]            = ImVec4(0.18f, 0.20f, 0.24f, 1.00f);
    c[ImGuiCol_ButtonHovered]     = ImVec4(0.25f, 0.40f, 0.58f, 1.00f);
    c[ImGuiCol_ButtonActive]      = ImVec4(0.30f, 0.50f, 0.72f, 1.00f);
    c[ImGuiCol_Tab]               = ImVec4(0.13f, 0.13f, 0.15f, 1.00f);
    c[ImGuiCol_TabHovered]        = ImVec4(0.28f, 0.45f, 0.66f, 1.00f);
    c[ImGuiCol_TabSelected]       = ImVec4(0.22f, 0.33f, 0.49f, 1.00f);
    c[ImGuiCol_TabDimmed]         = ImVec4(0.10f, 0.10f, 0.11f, 1.00f);
    c[ImGuiCol_TabDimmedSelected] = ImVec4(0.15f, 0.20f, 0.30f, 1.00f);
    c[ImGuiCol_SliderGrab]        = ImVec4(0.55f, 0.75f, 1.00f, 1.00f);
    c[ImGuiCol_SliderGrabActive]  = ImVec4(0.70f, 0.85f, 1.00f, 1.00f);
    c[ImGuiCol_CheckMark]         = ImVec4(0.55f, 0.85f, 1.00f, 1.00f);
    c[ImGuiCol_Separator]         = ImVec4(0.22f, 0.22f, 0.24f, 1.00f);
    c[ImGuiCol_Text]              = ImVec4(0.92f, 0.92f, 0.94f, 1.00f);
    c[ImGuiCol_TextDisabled]      = ImVec4(0.55f, 0.55f, 0.58f, 1.00f);
  }
  ImGui_ImplGlfw_InitForOpenGL(window, true);
  ImGui_ImplOpenGL3_Init(glslVersion);

  {
    mc::Gui gui;

    while (!glfwWindowShouldClose(window)) {
      // When there's no active work (no live video, no scan, no calibration
      // run), block for up to ~33ms waiting for input events. This drops CPU
      // to ~0% and removes any per-frame allocation pressure that could
      // accumulate over a long idle session. While work is active, fall back
      // to polling so the UI redraws every frame at the vsync rate.
      if (gui.needsContinuousRedraw()) {
        glfwPollEvents();
      } else {
        glfwWaitEventsTimeout(0.033);
      }

      ImGui_ImplOpenGL3_NewFrame();
      ImGui_ImplGlfw_NewFrame();
      ImGui::NewFrame();

      gui.draw(window);

      ImGui::Render();
      int w, h;
      glfwGetFramebufferSize(window, &w, &h);
      glViewport(0, 0, w, h);
      glClearColor(0.07f, 0.07f, 0.08f, 1.0f);
      glClear(GL_COLOR_BUFFER_BIT);
      ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
      glfwSwapBuffers(window);
    }
  }

  ImGui_ImplOpenGL3_Shutdown();
  ImGui_ImplGlfw_Shutdown();
  ImPlot::DestroyContext();
  ImGui::DestroyContext();
  glfwDestroyWindow(window);
  glfwTerminate();
  return EXIT_SUCCESS;
}
