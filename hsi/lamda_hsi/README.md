# ASI camera process and legacy controller

The Python application uses `asi_camd`, a C++17 process wrapping the ZWO ASI SDK.
It controls the camera only. Scan motion belongs to `acquisition/coordinator.py`.
The older ImGui controller is kept here for reference and is excluded from the default build.
**Do not run it alongside the Python application on the same devices.**

## Build the camera process

Run these commands from this directory. Install CMake, pkg-config and libusb first:

```sh
# macOS
brew install cmake libusb pkg-config

# Ubuntu
sudo apt install build-essential cmake pkg-config libusb-1.0-0-dev
```

Place the ZWO SDK v1.41 library for your platform under `ASI_linux_mac_SDK_V1.41/lib/`.
For example, Apple Silicon uses `lib/mac_arm64/libASICamera2.a` and Linux x86_64
uses `lib/x64/libASICamera2.so`. Runtime libraries are not included in Git.

```sh
cmake -S . -B build -DLAMDA_HSI_BUILD_GUI=OFF
cmake --build build --target asi_camd -j
```

The Python client starts this process when connecting to a real camera. See the
[usage guide](../../docs/USAGE.md) for USB permissions, setup and the command protocol.
Simulation does not require the SDK library or this build.

## Camera data

The lab camera is a ZWO ASI533 monochrome camera. Its 14-bit ADC values are carried
in RAW16; the saturation level is 65532. The daemon writes uint16 `.npy` frames.
Dark frames require a covered optical input because the camera has no mechanical shutter.
Dark and sample settings, exposure and frame shape must match.

## Source layout

- `src/asi_camd.cpp`: command protocol used by the Python client.
- `src/camera.h`, `src/camera.cpp`: ASI SDK wrapper.
- `src/main.cpp`, `src/gui.*`, `src/capture_thread.*`: legacy ImGui application and preview.
- `src/calibration.*`, `src/cube.*`, `src/image_writer.*`: legacy calibration and TIFF/FITS/ENVI output.
- `src/monochromator.*`, `src/serial_port.*`: legacy serial controller.
- `ASI_linux_mac_SDK_V1.41/`: vendor header, examples, documentation and license.

## Legacy GUI

To build the archived controller, configure with `-DLAMDA_HSI_BUILD_GUI=ON` and
build the `lamda_hsi` target. This downloads GLFW, Dear ImGui, ImPlot and stb using
CMake FetchContent. On Linux it also requires OpenGL and X11 development packages.
The legacy controller writes TIFF bands and ENVI cubes under `captures/`; these
are separate from the Python application's manifest-based sessions.
