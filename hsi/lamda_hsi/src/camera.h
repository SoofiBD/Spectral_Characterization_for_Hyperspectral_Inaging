#pragma once

#include <ASICamera2.h>

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace mc {

class AsiError : public std::runtime_error {
public:
  AsiError(const std::string& what, ASI_ERROR_CODE code)
      : std::runtime_error(what + ": " + errorString(code) + " (ASI error " +
                           std::to_string(static_cast<int>(code)) + ")"),
        code_(code) {}
  ASI_ERROR_CODE code() const noexcept { return code_; }
  static const char* errorString(ASI_ERROR_CODE c);

private:
  ASI_ERROR_CODE code_;
};

inline void asiCheck(ASI_ERROR_CODE rc, const char* what) {
  if (rc != ASI_SUCCESS) throw AsiError(what, rc);
}

struct ControlDescriptor {
  ASI_CONTROL_TYPE type;
  std::string name;
  std::string description;
  long minValue;
  long maxValue;
  long defaultValue;
  bool isAutoSupported;
  bool isWritable;
};

struct RoiFormat {
  int width;
  int height;
  int bin;
  ASI_IMG_TYPE imageType;
};

struct StartPos {
  int x;
  int y;
};

struct ControlValue {
  long value;
  bool isAuto;
};

class Camera {
public:
  Camera(int cameraId, const ASI_CAMERA_INFO& info);
  ~Camera();

  Camera(const Camera&) = delete;
  Camera& operator=(const Camera&) = delete;
  Camera(Camera&&) = delete;
  Camera& operator=(Camera&&) = delete;

  int id() const noexcept { return cameraId_; }
  const ASI_CAMERA_INFO& info() const noexcept { return info_; }
  const std::vector<ControlDescriptor>& controls() const noexcept { return controls_; }

  // --- Controls ---------------------------------------------------------
  ControlValue getControl(ASI_CONTROL_TYPE t) const;
  void setControl(ASI_CONTROL_TYPE t, long value, bool isAuto = false);

  // --- ROI / format -----------------------------------------------------
  RoiFormat getRoi() const;
  void setRoi(const RoiFormat& roi);
  StartPos getStartPos() const;
  void setStartPos(const StartPos& p);

  ASI_IMG_TYPE imageType() const { return getRoi().imageType; }
  std::size_t frameBytes() const;

  // --- Camera mode ------------------------------------------------------
  ASI_CAMERA_MODE getMode() const;
  void setMode(ASI_CAMERA_MODE m);

  // --- Video stream -----------------------------------------------------
  void startVideoCapture();
  void stopVideoCapture();
  // Returns true if a frame was received before the timeout.
  bool getVideoData(std::uint8_t* buffer, std::size_t bytes, int timeoutMs);

  // --- Snap (single long exposure) -------------------------------------
  void startExposure(bool isDark);
  void stopExposure();
  ASI_EXPOSURE_STATUS expStatus() const;
  void getDataAfterExp(std::uint8_t* buffer, std::size_t bytes);

  // --- Misc -------------------------------------------------------------
  std::optional<std::string> serialNumber() const;
  int droppedFrames() const;

  // Convenience: read sensor temperature in deg C (ASI returns 10*temp).
  std::optional<float> temperatureCelsius() const;

  // Best-effort name lookup for a control (display only).
  static const char* controlName(ASI_CONTROL_TYPE t);
  static const char* imageTypeName(ASI_IMG_TYPE t);
  static int bytesPerPixel(ASI_IMG_TYPE t);

private:
  void readControls();

  int cameraId_;
  ASI_CAMERA_INFO info_;
  std::vector<ControlDescriptor> controls_;
  bool videoCapturing_ = false;
};

class CameraManager {
public:
  CameraManager();
  ~CameraManager();

  // Re-enumerate cameras. Closes any open camera first.
  void refresh();

  const std::vector<ASI_CAMERA_INFO>& devices() const noexcept { return devices_; }
  // Count reported by ASIGetNumOfConnectedCameras (devices() holds only readable properties).
  int reportedCount() const noexcept { return reported_; }

  // Open a camera by index in devices(). Returns shared ownership-ish:
  // there is only one open camera at a time. Caller must not delete.
  Camera* open(int deviceIndex);
  void close();
  Camera* current() noexcept { return open_.get(); }
  const Camera* current() const noexcept { return open_.get(); }

  static std::string sdkVersion();

private:
  std::vector<ASI_CAMERA_INFO> devices_;
  int reported_ = 0;
  std::unique_ptr<Camera> open_;
};

}  // namespace mc
