#pragma once

#include "capture_thread.h"

#include <cstdint>
#include <optional>
#include <string>

namespace mc {

// Metadata captured at the moment of snap; embedded into FITS / TIFF tags
// where possible. Optional fields are written only if present.
struct CaptureMetadata {
  std::string cameraName;
  std::string sdkVersion;
  std::optional<std::string> serialNumber;
  std::optional<long> gain;
  std::optional<long> offset;
  std::optional<long> exposureUs;
  std::optional<long> gamma;
  std::optional<long> usbBandwidth;
  std::optional<float> sensorTempC;
  std::optional<long> bitDepth;
  std::optional<float> pixelSizeUm;
  int bin = 1;
};

class ImageWriter {
public:
  // Write a 16-bit greyscale TIFF (RAW16 / Y8 -> 16-bit promoted).
  // Returns the path written.
  static std::string writeTiff(const std::string& dir, const std::string& prefix,
                               const Frame& frame, const CaptureMetadata& meta);

  // Write a FITS file with a header containing rich metadata. Greyscale only.
  static std::string writeFits(const std::string& dir, const std::string& prefix,
                               const Frame& frame, const CaptureMetadata& meta);

  // Write an 8-bit PNG preview (RAW16 is downscaled to 8-bit for preview).
  static std::string writePng(const std::string& dir, const std::string& prefix,
                              const Frame& frame);

  // 32-bit float TIFF — used for calibration masters and corrected science
  // frames. `path` is the full path (no timestamp suffix added). `desc` ends
  // up in the ImageDescription tag for ImageJ etc.
  static void writeFloatTiff(const std::string& path, int width, int height,
                             const float* pixels, const std::string& desc);

  // Display-friendly 16-bit greyscale TIFF at an explicit path. The caller
  // supplies already clipped/scaled pixels.
  static void writeGrey16Tiff(const std::string& path, int width, int height,
                              const std::uint16_t* pixels,
                              const std::string& desc);

  // Read a 32-bit float TIFF written by writeFloatTiff. Throws on failure.
  // out_pixels is resized to width*height.
  static void readFloatTiff(const std::string& path, int& width, int& height,
                            std::vector<float>& out_pixels);

  static void ensureDir(const std::string& dir);
};

}  // namespace mc
