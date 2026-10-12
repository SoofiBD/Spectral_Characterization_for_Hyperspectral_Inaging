// macOS-specific window chrome adjustments. Compiled only on Apple platforms.
#ifdef __APPLE__

#import <Cocoa/Cocoa.h>

#define GLFW_EXPOSE_NATIVE_COCOA
#include <GLFW/glfw3.h>
#include <GLFW/glfw3native.h>

namespace mc {

// Force the NSWindow's appearance to dark, and make the title bar paint with
// the same colour as the window background so it visually merges with the
// app's dark theme.
void macSetDarkTitleBar(GLFWwindow* window) {
  NSWindow* nsw = glfwGetCocoaWindow(window);
  if (!nsw) return;
  nsw.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
  nsw.titlebarAppearsTransparent = YES;
  // Background colour ~ ImGui WindowBg from main.cpp's style refinement.
  nsw.backgroundColor = [NSColor colorWithSRGBRed:0.10
                                            green:0.10
                                             blue:0.11
                                            alpha:1.0];
}

// Replace the Dock icon (and cmd-tab switcher icon) with the given PNG.
void macSetDockIcon(const char* pngPath) {
  if (!pngPath) return;
  NSString* path = [NSString stringWithUTF8String:pngPath];
  NSImage* img = [[NSImage alloc] initWithContentsOfFile:path];
  if (!img) return;
  [NSApp setApplicationIconImage:img];
}

}  // namespace mc

#endif
