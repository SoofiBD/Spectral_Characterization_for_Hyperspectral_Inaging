#include "camera.h"

#include <cstring>
#include <memory>

namespace mc {

const char* AsiError::errorString(ASI_ERROR_CODE c) {
  switch (c) {
    case ASI_SUCCESS:                       return "success";
    case ASI_ERROR_INVALID_INDEX:           return "invalid camera index";
    case ASI_ERROR_INVALID_ID:              return "invalid camera id";
    case ASI_ERROR_INVALID_CONTROL_TYPE:    return "invalid control type";
    case ASI_ERROR_CAMERA_CLOSED:           return "camera closed";
    case ASI_ERROR_CAMERA_REMOVED:          return "camera removed";
    case ASI_ERROR_INVALID_PATH:            return "invalid path";
    case ASI_ERROR_INVALID_FILEFORMAT:      return "invalid file format";
    case ASI_ERROR_INVALID_SIZE:            return "invalid size";
    case ASI_ERROR_INVALID_IMGTYPE:         return "invalid image type";
    case ASI_ERROR_OUTOF_BOUNDARY:          return "start pos out of boundary";
    case ASI_ERROR_TIMEOUT:                 return "timeout";
    case ASI_ERROR_INVALID_SEQUENCE:        return "invalid sequence (stop capture first)";
    case ASI_ERROR_BUFFER_TOO_SMALL:        return "buffer too small";
    case ASI_ERROR_VIDEO_MODE_ACTIVE:       return "video mode active";
    case ASI_ERROR_EXPOSURE_IN_PROGRESS:    return "exposure in progress";
    case ASI_ERROR_GENERAL_ERROR:           return "general error (value out of range?)";
    case ASI_ERROR_INVALID_MODE:            return "invalid mode";
    default:                                return "unknown error";
  }
}

const char* Camera::controlName(ASI_CONTROL_TYPE t) {
  switch (t) {
    case ASI_GAIN:                  return "Gain";
    case ASI_EXPOSURE:              return "Exposure (us)";
    case ASI_GAMMA:                 return "Gamma";
    case ASI_WB_R:                  return "WB Red";
    case ASI_WB_B:                  return "WB Blue";
    case ASI_OFFSET:                return "Offset / Brightness";
    case ASI_BANDWIDTHOVERLOAD:     return "USB Bandwidth %";
    case ASI_OVERCLOCK:             return "Overclock";
    case ASI_TEMPERATURE:           return "Sensor Temperature (0.1C)";
    case ASI_FLIP:                  return "Flip";
    case ASI_AUTO_MAX_GAIN:         return "Auto Max Gain";
    case ASI_AUTO_MAX_EXP:          return "Auto Max Exposure (us)";
    case ASI_AUTO_TARGET_BRIGHTNESS:return "Auto Target Brightness";
    case ASI_HARDWARE_BIN:          return "Hardware Bin";
    case ASI_HIGH_SPEED_MODE:       return "High Speed Mode";
    case ASI_COOLER_POWER_PERC:     return "Cooler Power %";
    case ASI_TARGET_TEMP:           return "Target Temp (C)";
    case ASI_COOLER_ON:             return "Cooler On";
    case ASI_MONO_BIN:              return "Mono Bin";
    case ASI_FAN_ON:                return "Fan On";
    case ASI_PATTERN_ADJUST:        return "Pattern Adjust";
    case ASI_ANTI_DEW_HEATER:       return "Anti-Dew Heater";
    case ASI_FAN_ADJUST:            return "Fan Speed";
    case ASI_PWRLED_BRIGNT:         return "Power LED Brightness";
    case ASI_USBHUB_RESET:          return "USB Hub Reset";
    case ASI_GPS_SUPPORT:           return "GPS Support";
    case ASI_GPS_START_LINE:        return "GPS Start Line";
    case ASI_GPS_END_LINE:          return "GPS End Line";
    case ASI_ROLLING_INTERVAL:      return "Rolling Interval (us)";
    default:                        return "Unknown";
  }
}

const char* Camera::imageTypeName(ASI_IMG_TYPE t) {
  switch (t) {
    case ASI_IMG_RAW8:  return "RAW8";
    case ASI_IMG_RGB24: return "RGB24";
    case ASI_IMG_RAW16: return "RAW16";
    case ASI_IMG_Y8:    return "Y8";
    default:            return "?";
  }
}

int Camera::bytesPerPixel(ASI_IMG_TYPE t) {
  switch (t) {
    case ASI_IMG_RAW8:
    case ASI_IMG_Y8:    return 1;
    case ASI_IMG_RAW16: return 2;
    case ASI_IMG_RGB24: return 3;
    default:            return 0;
  }
}

Camera::Camera(int cameraId, const ASI_CAMERA_INFO& info)
    : cameraId_(cameraId), info_(info) {
  asiCheck(ASIOpenCamera(cameraId_), "ASIOpenCamera");
  ASI_ERROR_CODE rc = ASIInitCamera(cameraId_);
  if (rc != ASI_SUCCESS) {
    ASICloseCamera(cameraId_);
    throw AsiError("ASIInitCamera", rc);
  }

  int nctrl = 0;
  asiCheck(ASIGetNumOfControls(cameraId_, &nctrl), "ASIGetNumOfControls");
  controls_.reserve(nctrl);
  for (int i = 0; i < nctrl; ++i) {
    ASI_CONTROL_CAPS caps{};
    asiCheck(ASIGetControlCaps(cameraId_, i, &caps), "ASIGetControlCaps");
    ControlDescriptor d;
    d.type = caps.ControlType;
    d.name = caps.Name;
    d.description = caps.Description;
    d.minValue = caps.MinValue;
    d.maxValue = caps.MaxValue;
    d.defaultValue = caps.DefaultValue;
    d.isAutoSupported = caps.IsAutoSupported == ASI_TRUE;
    d.isWritable = caps.IsWritable == ASI_TRUE;
    controls_.push_back(std::move(d));
  }
}

Camera::~Camera() {
  if (videoCapturing_) {
    ASIStopVideoCapture(cameraId_);
  }
  ASICloseCamera(cameraId_);
}

ControlValue Camera::getControl(ASI_CONTROL_TYPE t) const {
  ControlValue v{};
  ASI_BOOL isAuto = ASI_FALSE;
  asiCheck(ASIGetControlValue(cameraId_, t, &v.value, &isAuto), "ASIGetControlValue");
  v.isAuto = (isAuto == ASI_TRUE);
  return v;
}

void Camera::setControl(ASI_CONTROL_TYPE t, long value, bool isAuto) {
  asiCheck(ASISetControlValue(cameraId_, t, value, isAuto ? ASI_TRUE : ASI_FALSE),
           "ASISetControlValue");
}

RoiFormat Camera::getRoi() const {
  RoiFormat r{};
  asiCheck(ASIGetROIFormat(cameraId_, &r.width, &r.height, &r.bin, &r.imageType),
           "ASIGetROIFormat");
  return r;
}

void Camera::setRoi(const RoiFormat& r) {
  asiCheck(ASISetROIFormat(cameraId_, r.width, r.height, r.bin, r.imageType),
           "ASISetROIFormat");
}

StartPos Camera::getStartPos() const {
  StartPos p{};
  asiCheck(ASIGetStartPos(cameraId_, &p.x, &p.y), "ASIGetStartPos");
  return p;
}

void Camera::setStartPos(const StartPos& p) {
  asiCheck(ASISetStartPos(cameraId_, p.x, p.y), "ASISetStartPos");
}

std::size_t Camera::frameBytes() const {
  RoiFormat r = getRoi();
  return static_cast<std::size_t>(r.width) * r.height * bytesPerPixel(r.imageType);
}

ASI_CAMERA_MODE Camera::getMode() const {
  ASI_CAMERA_MODE m = ASI_MODE_NORMAL;
  asiCheck(ASIGetCameraMode(cameraId_, &m), "ASIGetCameraMode");
  return m;
}

void Camera::setMode(ASI_CAMERA_MODE m) {
  asiCheck(ASISetCameraMode(cameraId_, m), "ASISetCameraMode");
}

void Camera::startVideoCapture() {
  if (videoCapturing_) return;
  asiCheck(ASIStartVideoCapture(cameraId_), "ASIStartVideoCapture");
  videoCapturing_ = true;
}

void Camera::stopVideoCapture() {
  if (!videoCapturing_) return;
  asiCheck(ASIStopVideoCapture(cameraId_), "ASIStopVideoCapture");
  videoCapturing_ = false;
}

bool Camera::getVideoData(std::uint8_t* buffer, std::size_t bytes, int timeoutMs) {
  ASI_ERROR_CODE rc = ASIGetVideoData(cameraId_, buffer,
                                      static_cast<long>(bytes), timeoutMs);
  if (rc == ASI_SUCCESS) return true;
  if (rc == ASI_ERROR_TIMEOUT) return false;
  throw AsiError("ASIGetVideoData", rc);
}

void Camera::startExposure(bool isDark) {
  asiCheck(ASIStartExposure(cameraId_, isDark ? ASI_TRUE : ASI_FALSE),
           "ASIStartExposure");
}

void Camera::stopExposure() {
  asiCheck(ASIStopExposure(cameraId_), "ASIStopExposure");
}

ASI_EXPOSURE_STATUS Camera::expStatus() const {
  ASI_EXPOSURE_STATUS s = ASI_EXP_IDLE;
  asiCheck(ASIGetExpStatus(cameraId_, &s), "ASIGetExpStatus");
  return s;
}

void Camera::getDataAfterExp(std::uint8_t* buffer, std::size_t bytes) {
  asiCheck(ASIGetDataAfterExp(cameraId_, buffer, static_cast<long>(bytes)),
           "ASIGetDataAfterExp");
}

std::optional<std::string> Camera::serialNumber() const {
  ASI_SN sn{};
  ASI_ERROR_CODE rc = ASIGetSerialNumber(cameraId_, &sn);
  if (rc != ASI_SUCCESS) return std::nullopt;
  char hex[2 * sizeof(sn.id) + 1] = {0};
  for (std::size_t i = 0; i < sizeof(sn.id); ++i) {
    std::snprintf(hex + i * 2, 3, "%02x",
                  static_cast<unsigned>(static_cast<unsigned char>(sn.id[i])));
  }
  return std::string(hex);
}

int Camera::droppedFrames() const {
  int n = 0;
  if (ASIGetDroppedFrames(cameraId_, &n) != ASI_SUCCESS) return 0;
  return n;
}

std::optional<float> Camera::temperatureCelsius() const {
  ControlValue v{};
  try {
    v = getControl(ASI_TEMPERATURE);
  } catch (...) {
    return std::nullopt;
  }
  return v.value / 10.0f;
}

// ----------------------------------------------------------------------------
// CameraManager
// ----------------------------------------------------------------------------

CameraManager::CameraManager() { refresh(); }
CameraManager::~CameraManager() { close(); }

void CameraManager::refresh() {
  close();
  devices_.clear();
  int n = ASIGetNumOfConnectedCameras();
  devices_.reserve(n);
  for (int i = 0; i < n; ++i) {
    ASI_CAMERA_INFO info{};
    if (ASIGetCameraProperty(&info, i) == ASI_SUCCESS) {
      devices_.push_back(info);
    }
  }
}

Camera* CameraManager::open(int index) {
  if (index < 0 || index >= static_cast<int>(devices_.size())) {
    throw std::out_of_range("camera index");
  }
  close();
  open_ = std::make_unique<Camera>(devices_[index].CameraID, devices_[index]);
  return open_.get();
}

void CameraManager::close() { open_.reset(); }

std::string CameraManager::sdkVersion() {
  const char* v = ASIGetSDKVersion();
  return v ? std::string(v) : std::string("?");
}

}  // namespace mc
