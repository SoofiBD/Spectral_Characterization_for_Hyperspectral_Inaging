# LAMDA HyperSpectral Imaging System

Internal release: **v1.0.0**

LAMDA HyperSpectral Imaging System is a laboratory acquisition application for
camera-based hyperspectral imaging. The software controls a ZWO ASI monochrome
scientific CMOS camera and a Jarrell-Ash monochromator through a single
graphical interface for live preview, calibrated single-frame capture, and
wavelength-scanned hyperspectral cube acquisition.

The application is intended for controlled laboratory measurements where fixed
camera settings, repeatable dark/flat calibration, traceable metadata, and
standard cube export are more important than consumer-style image display.

## Hardware Configuration

### Camera

The current laboratory configuration uses a **ZWO ASI533 monochrome camera**
with the bundled **ZWO ASI SDK v1.41**.

Relevant acquisition specifications:

| Parameter | Value |
| --- | --- |
| Sensor | Sony IMX533 monochrome CMOS |
| Active resolution | 3008 x 3008 pixels |
| Pixel size | 3.76 um |
| Sensor format | Square 1-inch class sensor |
| ADC depth | 14-bit effective ADC |
| SDK raw format used | RAW16 container |
| RAW16 saturation level | 65532 for 14-bit data left-shifted into 16-bit scale |
| Cooling support | Sensor temperature readout, TEC on/off, target temperature, cooler power |
| ROI/binning | SDK ROI, start position, binning, and image format controls |

Important bit-depth note: the camera data should be interpreted as **14-bit ADC
data transported in a 16-bit RAW16 SDK container**. The software keeps RAW16 for
SDK compatibility and analysis convenience, but metadata records the effective
ADC depth and RAW16 saturation level.

Dark frames require physically covering the optical input. The ASI533-class
camera used here does not provide an internal mechanical shutter for automatic
dark exposures.

### Monochromator

The spectral selection unit is a **Jarrell-Ash monochromator** controlled by the
LAMDA serial firmware interface.

The software communicates with the controller using a line-based serial
protocol:

| Command | Purpose |
| --- | --- |
| `GOTO:<nm>` | Move to a target wavelength |
| `HOME` | Home the monochromator |
| `RESET:<nm>` | Set the current controller position |
| `POS` | Query current wavelength position |
| `STATUS` | Query controller status |

The scan workflow applies a dwell time after each monochromator move before the
camera exposure starts. The monochromator connection is established in Manual
Mode and reused by the Hyperspectral Acquisition workflow.

## Main Capabilities

- ZWO ASI camera discovery and connection
- Manual exposure, gain, offset, gamma, and USB bandwidth control
- Manual and SDK auto-control configuration
- ROI, binning, start position, image orientation, high-speed mode, and other
  advanced camera controls
- Sensor temperature monitoring and cooler target control
- Live preview with adjustable FPS limit and display downscaling
- Single-exposure SNAP capture
- Physical-cover dark master acquisition
- Flat-field master acquisition
- Live and acquisition-time calibration support
- Wavelength-scanned hyperspectral cube acquisition
- Cube viewer with wavelength slider and pixel spectrum plot
- ENVI standard cube export for downstream analysis

## Data Products

Each hyperspectral scan creates a timestamped session directory under the
configured scan root.

Typical session contents:

```text
captures/cubes/<timestamp>_<measurement_name>/
  cube.dat
  cube.hdr
  cube_metadata.json
  band_0001_<wavelength>nm.tif
  band_0002_<wavelength>nm.tif
  ...
```

### ENVI Cube

The primary scientific export is:

- `cube.dat`
- `cube.hdr`

The ENVI cube uses BSQ interleave and wavelength metadata in nanometers.

Storage mode:

- Uncalibrated/raw scans: `uint16` ENVI data, preserving the SDK RAW16 scale.
- Dark/flat calibrated scans: `float32` ENVI data, because calibrated values may
  be fractional.

The ENVI header records camera name, image type, exposure, gain, offset,
binning, calibration status, effective ADC bit depth, and wavelength list.

### Band TIFF Files

The `band_*.tif` files are display-friendly 16-bit grayscale TIFF slices for
quick inspection and local cube viewing. They are not the primary standard
analysis export; use the ENVI cube for quantitative analysis.

Each band TIFF includes basic acquisition information in the TIFF description,
including wavelength, exposure, gain, calibration status, and simple pixel
statistics.

### SNAP Outputs

Manual SNAP capture can save:

- 16-bit grayscale TIFF
- FITS with acquisition metadata
- 8-bit PNG preview

## Recommended Measurement Practice

For quantitative hyperspectral measurements:

- Use fixed exposure, gain, and offset.
- Disable auto exposure and auto gain before scan acquisition.
- Use RAW16 camera format.
- Keep the same ROI, binning, exposure, gain, offset, and image format between
  dark/flat master acquisition and sample scans.
- Acquire dark masters with the optical input physically covered.
- Acquire flat masters with a stable uniform reference.
- Monitor sensor temperature and allow the camera to stabilize before
  high-precision measurements.
- Use ENVI `cube.dat` and `cube.hdr` as the primary analysis files.

## Build Targets

Supported development platforms:

- macOS on Apple Silicon
- Ubuntu Linux x86_64

The project uses C++17, CMake, GLFW, Dear ImGui docking, ImPlot, stb, libusb,
and the ZWO ASI SDK.

## Dependencies

### macOS

```sh
brew install cmake libusb pkg-config
```

The build system links the bundled ASI SDK library from:

```text
ASI_linux_mac_SDK_V1.41/lib/mac_arm64/libASICamera2.a
```

### Ubuntu

```sh
sudo apt update
sudo apt install build-essential cmake pkg-config git \
                 libusb-1.0-0-dev libgl1-mesa-dev \
                 xorg-dev libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev
```

Install ASI udev rules:

```sh
sudo install -m 0644 ASI_linux_mac_SDK_V1.41/lib/asi.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Add the user to `plugdev` if required by the local Linux configuration:

```sh
sudo usermod -aG plugdev $USER
```

Log out and log back in after changing group membership.

For high-throughput USB transfers, increase the Linux USBFS memory limit:

```sh
echo 200 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb
```

For a persistent setting, add `usbcore.usbfs_memory_mb=200` to the kernel
command line through the system bootloader configuration.

## Build and Run

```sh
cmake -S . -B build
cmake --build build -j
./build/lamda_hsi
```

The first CMake configure step downloads GLFW, Dear ImGui docking, ImPlot, and
stb through CMake FetchContent.

## Repository Layout

```text
ASI_linux_mac_SDK_V1.41/      Bundled ZWO ASI SDK
ASI533_Manual_EN_V1.2.pdf     Camera manual used for local reference
packaging/linux/              Linux desktop entry template
src/
  main.cpp                    GLFW/OpenGL/ImGui application entry point
  camera.h/.cpp               RAII wrapper around the ZWO ASI SDK
  capture_thread.h/.cpp       Background live-preview acquisition
  calibration.h/.cpp          Dark/flat master acquisition and correction
  cube.h/.cpp                 Hyperspectral scan thread, cube session, ENVI export
  gui.h/.cpp                  ImGui interface and acquisition workflow
  image_writer.h/.cpp         TIFF, FITS, PNG, and cube slice I/O
  monochromator.h/.cpp        Jarrell-Ash/LAMDA serial monochromator control
  serial_port.h/.cpp          Cross-platform serial port abstraction
  platform_mac.mm             macOS-specific window/icon support
CMakeLists.txt                Cross-platform build configuration
```

## Versioning

The current internal laboratory baseline is `1.0.0`. Version numbers should be
updated when acquisition behavior, file formats, metadata schema, or hardware
control semantics change.

## Notes

This software is a laboratory acquisition tool. It does not replace independent
instrument calibration, optical alignment checks, wavelength calibration,
radiometric calibration, or sample-specific validation. Measurement quality
depends on the camera settings, illumination stability, monochromator
calibration, dark/flat matching, optical throughput, and sample preparation.
