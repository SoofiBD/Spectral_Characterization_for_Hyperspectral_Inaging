#include "gui.h"

#include "image_writer.h"

#include "imgui.h"
#include "imgui_internal.h"
#include "implot.h"

#define GL_SILENCE_DEPRECATION
#ifdef __APPLE__
  #include <OpenGL/gl3.h>
#else
  #include <GL/gl.h>
  #include <GL/glext.h>
#endif

#include <GLFW/glfw3.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <memory>
#include <sstream>
#include <thread>

namespace mc {

namespace {
bool isReadOnly(ASI_CONTROL_TYPE t) {
  return t == ASI_TEMPERATURE || t == ASI_COOLER_POWER_PERC;
}

std::optional<ControlValue> tryGetControl(const Camera& cam, ASI_CONTROL_TYPE t) {
  try {
    return cam.getControl(t);
  } catch (...) {
    return std::nullopt;
  }
}

const char* controlHelp(ASI_CONTROL_TYPE t, const ControlDescriptor& d) {
  switch (t) {
    case ASI_EXPOSURE:
      return "Exposure time in microseconds. Longer exposures collect more light but increase motion risk and saturation.";
    case ASI_GAIN:
      return "Analog camera gain. Gain 0 keeps the most full-well capacity; gain 100 is a useful low-noise ASI533 starting point.";
    case ASI_OFFSET:
      return "Pixel bias offset. Keep it high enough that dark frames do not clip to zero, but not unnecessarily high.";
    case ASI_GAMMA:
      return "Camera gamma control. For quantitative scientific capture, keep the default unless you have a measured reason to change it.";
    case ASI_AUTO_MAX_EXP:
      return "Maximum exposure the SDK may use when auto exposure is enabled. Auto exposure is meaningful in live video mode.";
    case ASI_AUTO_MAX_GAIN:
      return "Maximum gain the SDK may use when auto gain is enabled. Auto gain is meaningful in live video mode.";
    case ASI_AUTO_TARGET_BRIGHTNESS:
      return "Target brightness for SDK auto exposure/gain in video mode.";
    case ASI_BANDWIDTHOVERLOAD:
      return "USB traffic limit. Lower values reduce transfer pressure but can reduce frame rate.";
    case ASI_HIGH_SPEED_MODE:
      return "High speed readout mode if supported. It can increase frame rate, sometimes with different noise behavior.";
    case ASI_HARDWARE_BIN:
      return "Hardware binning switch if supported. ASI533 manual notes hardware bin output uses 12-bit ADC mode.";
    case ASI_MONO_BIN:
      return "Mono binning mode option exposed by the SDK. Mostly relevant to color-camera binning behavior.";
    case ASI_FLIP:
      return "Image flip mode from the SDK.";
    case ASI_TEMPERATURE:
      return "Sensor temperature. ASI SDK returns this control as 10 times degrees Celsius; the UI converts it to degrees Celsius.";
    case ASI_COOLER_ON:
      return "Turns TEC cooling on or off. Cooled ASI cameras need external 12V power for cooling.";
    case ASI_TARGET_TEMP:
      return "Cooler target temperature in degrees Celsius.";
    case ASI_COOLER_POWER_PERC:
      return "Current cooler power percentage reported by the camera.";
    case ASI_FAN_ON:
      return "Cooling fan control if supported by the camera.";
    case ASI_ANTI_DEW_HEATER:
      return "Anti-dew heater control if supported by the camera.";
    case ASI_PWRLED_BRIGNT:
      return "Power LED brightness.";
    default:
      return d.description.empty() ? "Camera SDK control." : d.description.c_str();
  }
}

std::string formatExposure(long long us) {
  char buf[64];
  if (us >= 1000000) {
    std::snprintf(buf, sizeof(buf), "%.3f s", us / 1000000.0);
  } else if (us >= 1000) {
    std::snprintf(buf, sizeof(buf), "%.3f ms", us / 1000.0);
  } else {
    std::snprintf(buf, sizeof(buf), "%lld us", us);
  }
  return buf;
}

ImU32 wavelengthColor(double nm) {
  double r = 0.45, g = 0.45, b = 0.48;
  if (nm < 380.0) {
    r = 0.35; g = 0.28; b = 0.70;
  } else if (nm < 440.0) {
    r = (440.0 - nm) / 60.0; g = 0.0; b = 1.0;
  } else if (nm < 490.0) {
    r = 0.0; g = (nm - 440.0) / 50.0; b = 1.0;
  } else if (nm < 510.0) {
    r = 0.0; g = 1.0; b = (510.0 - nm) / 20.0;
  } else if (nm < 580.0) {
    r = (nm - 510.0) / 70.0; g = 1.0; b = 0.0;
  } else if (nm < 645.0) {
    r = 1.0; g = (645.0 - nm) / 65.0; b = 0.0;
  } else if (nm <= 700.0) {
    r = 1.0; g = 0.0; b = 0.0;
  } else {
    r = 0.72; g = 0.16; b = 0.12;
  }

  double factor = 1.0;
  if (nm >= 380.0 && nm < 420.0) factor = 0.35 + 0.65 * (nm - 380.0) / 40.0;
  if (nm > 645.0 && nm <= 700.0) factor = 0.35 + 0.65 * (700.0 - nm) / 55.0;
  auto gamma = [&](double c) {
    return static_cast<int>(std::clamp(std::pow(c * factor, 0.8) * 255.0,
                                       0.0, 255.0));
  };
  return IM_COL32(gamma(r), gamma(g), gamma(b), 255);
}

// ---------------------------------------------------------------------------
//  Reusable polished widgets
// ---------------------------------------------------------------------------

// A section header — bold-ish coloured title followed by a separator. Use
// at the top of a logical group inside a panel.
void SectionHeader(const char* title) {
  ImGui::Dummy(ImVec2(0, 2));
  ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.65f, 0.85f, 1.0f, 1.0f));
  ImGui::TextUnformatted(title);
  ImGui::PopStyleColor();
  ImGui::Separator();
  ImGui::Dummy(ImVec2(0, 2));
}

// Status badge: a coloured filled circle followed by a label, suitable for
// indicating connection / readiness states.
enum class BadgeKind { Ok, Warn, Err, Idle };

void StatusBadge(BadgeKind kind, const char* label) {
  ImVec4 col;
  switch (kind) {
    case BadgeKind::Ok:   col = ImVec4(0.40f, 0.90f, 0.50f, 1.0f); break;
    case BadgeKind::Warn: col = ImVec4(1.00f, 0.78f, 0.20f, 1.0f); break;
    case BadgeKind::Err:  col = ImVec4(1.00f, 0.40f, 0.40f, 1.0f); break;
    default:              col = ImVec4(0.55f, 0.55f, 0.58f, 1.0f); break;
  }
  ImDrawList* dl = ImGui::GetWindowDrawList();
  ImVec2 p = ImGui::GetCursorScreenPos();
  float r = ImGui::GetTextLineHeight() * 0.42f;
  float yMid = p.y + ImGui::GetTextLineHeight() * 0.5f;
  dl->AddCircleFilled(ImVec2(p.x + r + 1, yMid), r, ImGui::GetColorU32(col), 16);
  ImGui::Dummy(ImVec2(r * 2 + 6, ImGui::GetTextLineHeight()));
  ImGui::SameLine();
  ImGui::TextUnformatted(label);
}

// Small "(?)" hover icon — pops up the given text as a tooltip.
void HelpMarker(const char* desc) {
  ImGui::TextDisabled("(?)");
  if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort)) {
    ImGui::BeginTooltip();
    ImGui::PushTextWrapPos(ImGui::GetFontSize() * 30);
    ImGui::TextUnformatted(desc);
    ImGui::PopTextWrapPos();
    ImGui::EndTooltip();
  }
}

}  // namespace

Gui::Gui() {
  calib_ = std::make_unique<CalibrationManager>(saveDir_ + "/calib");
  cube_ = std::make_shared<CubeSession>();
  cube_->setSessionDir(scanRootDir_);
  refreshSerialPorts();
}

bool Gui::needsContinuousRedraw() const {
  // Live preview is throttled in Gui::draw(); letting the main loop wait up
  // to 33 ms avoids burning a CPU core just to redraw unchanged frames.
  if (scanThread_) {
    auto st = scanThread_->status();
    if (st == ScanThread::Status::Running ||
        st == ScanThread::Status::Exporting) return true;
  }
  if (calibJob_ &&
      calibJob_->status() == MasterAcquisition::Status::Running) return true;
  return false;
}

void Gui::refreshSerialPorts() {
  serialPorts_ = SerialPort::listPorts();
  if (!serialPorts_.empty() && selectedSerialPort_ < 0) selectedSerialPort_ = 0;
  if (selectedSerialPort_ >= static_cast<int>(serialPorts_.size())) {
    selectedSerialPort_ = serialPorts_.empty() ? -1 : 0;
  }
}

Gui::~Gui() {
  mono_.cancelPendingOperation();
  if (monoJob_.joinable()) monoJob_.join();
  closeCamera();
  if (previewTex_) {
    glDeleteTextures(1, &previewTex_);
    previewTex_ = 0;
  }
}

void Gui::appendLog(const std::string& msg) {
  log_.push_back(msg);
  if (log_.size() > 500) log_.erase(log_.begin(), log_.begin() + 100);
}

void Gui::startMonoJob(std::string label, std::function<MoveResult()> job) {
  if (monoJobRunning_.load()) return;
  if (monoJob_.joinable()) monoJob_.join();
  appendLog(label);
  mono_.prepareOperation();
  monoJobRunning_.store(true);
  monoJob_ = std::thread([this, label = std::move(label), job = std::move(job)]() mutable {
    std::vector<std::string> lines;
    try {
      MoveResult r = job();
      if (r.status == MoveResult::Status::Done) {
        lines.push_back(label + " done at " + std::to_string(r.position) + " nm");
      } else {
        lines.push_back(label + " failed: " + r.message);
      }
    } catch (const std::exception& e) {
      lines.push_back(label + " exception: " + e.what());
    }
    {
      std::lock_guard<std::mutex> lock(monoJobMutex_);
      monoJobLog_.insert(monoJobLog_.end(), lines.begin(), lines.end());
    }
    monoJobRunning_.store(false);
  });
}

void Gui::drainMonoJobLog() {
  std::vector<std::string> lines;
  {
    std::lock_guard<std::mutex> lock(monoJobMutex_);
    lines.swap(monoJobLog_);
  }
  for (const auto& line : lines) appendLog(line);
  if (!monoJobRunning_.load() && monoJob_.joinable()) monoJob_.join();
}

void Gui::openSelectedCamera() {
  try {
    auto* cam = manager_.open(selectedDevice_);
    appendLog("Opened: " + std::string(cam->info().Name));
    // Initial ROI = full sensor, RAW16 if supported else first format.
    supportedImgTypes_.clear();
    for (int i = 0; i < 8; ++i) {
      ASI_IMG_TYPE t = cam->info().SupportedVideoFormat[i];
      if (t == ASI_IMG_END) break;
      supportedImgTypes_.push_back(t);
    }
    // Prefer RAW16 for monochrome scientific work.
    ASI_IMG_TYPE pick = supportedImgTypes_.empty() ? ASI_IMG_RAW8
                                                   : supportedImgTypes_.front();
    for (auto t : supportedImgTypes_) {
      if (t == ASI_IMG_RAW16) { pick = ASI_IMG_RAW16; break; }
    }
    roiImgType_ = 0;
    for (std::size_t i = 0; i < supportedImgTypes_.size(); ++i) {
      if (supportedImgTypes_[i] == pick) {
        roiImgType_ = static_cast<int>(i);
        break;
      }
    }
    roiWidth_ = cam->info().MaxWidth;
    roiHeight_ = cam->info().MaxHeight;
    roiBin_ = 1;
    roiX_ = 0;
    roiY_ = 0;
    cam->setRoi({roiWidth_, roiHeight_, roiBin_, pick});
    cam->setStartPos({0, 0});
    // Sensible defaults.
    try { cam->setControl(ASI_BANDWIDTHOVERLOAD, 40); } catch (...) {}
    try { cam->setControl(ASI_EXPOSURE, 50000); } catch (...) {}
    try { cam->setControl(ASI_GAIN, 100); } catch (...) {}
    if (videoMode_) {
      capture_ = std::make_unique<CaptureThread>(*cam);
      capture_->start();
    }
    if (!calib_) {
      calib_ = std::make_unique<CalibrationManager>(saveDir_ + "/calib");
    }
    tryAutoLoadCalibration();
  } catch (const std::exception& e) {
    appendLog(std::string("open failed: ") + e.what());
    manager_.close();
  }
}

void Gui::closeCamera() {
  if (capture_) {
    capture_->stop();
    capture_.reset();
  }
  manager_.close();
  hasFrame_ = false;
}

void Gui::rebuildPreviewTexture(int w, int h) {
  if (w == texW_ && h == texH_ && previewTex_) return;
  if (previewTex_) {
    glDeleteTextures(1, &previewTex_);
    previewTex_ = 0;
  }
  glGenTextures(1, &previewTex_);
  glBindTexture(GL_TEXTURE_2D, previewTex_);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, w, h, 0, GL_RED, GL_UNSIGNED_BYTE, nullptr);
  // Monochrome: replicate R channel to G and B so the texture renders as
  // greyscale instead of red when sampled by ImGui's default RGBA shader.
  const GLint swizzle[4] = {GL_RED, GL_RED, GL_RED, GL_ONE};
  glTexParameteriv(GL_TEXTURE_2D, GL_TEXTURE_SWIZZLE_RGBA, swizzle);
  texW_ = w;
  texH_ = h;
}

void Gui::uploadFrameToTexture(const Frame& f) {
  const int scale = std::clamp(previewScale_, 1, 4);
  const int outW = std::max(1, (f.width + scale - 1) / scale);
  const int outH = std::max(1, (f.height + scale - 1) / scale);
  rebuildPreviewTexture(outW, outH);
  const std::size_t n = static_cast<std::size_t>(outW) * outH;

  // If calibration is loaded and the toggle is on, transform raw → corrected
  // u16 first, then proceed through the existing 16-bit display path.
  std::vector<std::uint16_t> corrected16;
  bool useCorrected = false;
  if (applyCalibPreview_ && calib_ &&
      (calib_->hasDark() || calib_->hasFlat()) &&
      (f.type == ASI_IMG_RAW16 || f.type == ASI_IMG_RAW8 ||
       f.type == ASI_IMG_Y8)) {
    corrected16 = calib_->applyU16(f);
    useCorrected = true;
  }

  std::vector<std::uint8_t> g8(n);

  auto srcIndex = [&](int outX, int outY) {
    const int x = std::min(f.width - 1, outX * scale);
    const int y = std::min(f.height - 1, outY * scale);
    return static_cast<std::size_t>(y) * f.width + x;
  };

  auto stretchU16 = [&](const std::uint16_t* src) {
    int lo = blackPoint_;
    int hi = whitePoint_;
    if (autoStretch_) {
      std::uint16_t mn = 65535, mx = 0;
      for (int y = 0; y < outH; ++y) {
        for (int x = 0; x < outW; ++x) {
          const std::uint16_t v = src[srcIndex(x, y)];
          mn = std::min(mn, v);
          mx = std::max(mx, v);
        }
      }
      lo = mn;
      hi = std::max<int>(mn + 1, mx);
    }
    int range = std::max(1, hi - lo);
    for (int y = 0; y < outH; ++y) {
      for (int x = 0; x < outW; ++x) {
        const std::size_t dst = static_cast<std::size_t>(y) * outW + x;
        int v = (static_cast<int>(src[srcIndex(x, y)]) - lo) * 255 / range;
        g8[dst] = static_cast<std::uint8_t>(std::clamp(v, 0, 255));
      }
    }
  };

  if (useCorrected) {
    stretchU16(corrected16.data());
  } else {
    switch (f.type) {
      case ASI_IMG_RAW8:
      case ASI_IMG_Y8:
        if (autoStretch_) {
          std::uint8_t mn = 255, mx = 0;
          for (int y = 0; y < outH; ++y) {
            for (int x = 0; x < outW; ++x) {
              const std::uint8_t v = f.data[srcIndex(x, y)];
              mn = std::min(mn, v);
              mx = std::max(mx, v);
            }
          }
          int range = std::max<int>(1, mx - mn);
          for (int y = 0; y < outH; ++y) {
            for (int x = 0; x < outW; ++x) {
              const std::size_t dst = static_cast<std::size_t>(y) * outW + x;
              g8[dst] = static_cast<std::uint8_t>(
                  std::clamp(((f.data[srcIndex(x, y)] - mn) * 255) / range, 0, 255));
            }
          }
        } else {
          for (int y = 0; y < outH; ++y) {
            for (int x = 0; x < outW; ++x) {
              g8[static_cast<std::size_t>(y) * outW + x] = f.data[srcIndex(x, y)];
            }
          }
        }
        break;
      case ASI_IMG_RAW16:
        stretchU16(reinterpret_cast<const std::uint16_t*>(f.data.data()));
        break;
      case ASI_IMG_RGB24:
        for (int y = 0; y < outH; ++y) {
          for (int x = 0; x < outW; ++x) {
            const std::size_t src = srcIndex(x, y) * 3;
            const std::size_t dst = static_cast<std::size_t>(y) * outW + x;
            g8[dst] = static_cast<std::uint8_t>(
                (54u * f.data[src + 2] + 183u * f.data[src + 1] +
                 19u * f.data[src + 0]) >>
                8);
          }
        }
        break;
      default:
        break;
    }
  }
  glBindTexture(GL_TEXTURE_2D, previewTex_);
  glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
  glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, outW, outH, GL_RED,
                  GL_UNSIGNED_BYTE, g8.data());
}

void Gui::computeHistogram(const Frame& f) {
  const std::size_t n = static_cast<std::size_t>(f.width) * f.height;
  const bool sixteen = (f.type == ASI_IMG_RAW16);
  const int bins = sixteen ? 1024 : 256;
  const int binShift = sixteen ? 6 : 0;       // map 16-bit -> 1024 bins
  const int valueMax = sixteen ? 65532 : 255;
  const int satHigh = valueMax;
  histogram_.assign(bins, 0.0f);
  histogramAxis_.assign(bins, 0.0f);
  const float binWidth = static_cast<float>(valueMax + 1) / bins;
  for (int i = 0; i < bins; ++i) {
    histogramAxis_[i] = (i + 0.5f) * binWidth;
  }

  // First pass: histogram + min/max/sum + saturation counts.
  std::uint64_t sum = 0;
  std::uint64_t sumSq = 0;
  std::size_t highCount = 0;
  std::size_t lowCount = 0;
  int mn = valueMax;
  int mx = 0;

  auto tally = [&](int v) {
    int b = v >> binShift;
    if (b < 0) b = 0;
    if (b >= bins) b = bins - 1;
    histogram_[b] += 1.0f;
    if (v < mn) mn = v;
    if (v > mx) mx = v;
    sum   += static_cast<std::uint64_t>(v);
    sumSq += static_cast<std::uint64_t>(v) * static_cast<std::uint64_t>(v);
    if (v >= satHigh) ++highCount;
    if (v <= 0)       ++lowCount;
  };

  switch (f.type) {
    case ASI_IMG_RAW8:
    case ASI_IMG_Y8:
      for (std::size_t i = 0; i < n; ++i) tally(f.data[i]);
      break;
    case ASI_IMG_RAW16: {
      const auto* src = reinterpret_cast<const std::uint16_t*>(f.data.data());
      for (std::size_t i = 0; i < n; ++i) tally(src[i]);
      break;
    }
    case ASI_IMG_RGB24:
      for (std::size_t i = 0; i < n; ++i) {
        int y = (54u * f.data[i * 3 + 2] + 183u * f.data[i * 3 + 1] +
                 19u * f.data[i * 3 + 0]) >> 8;
        tally(y);
      }
      break;
    default:
      break;
  }

  histogramMax_ = 1.0f;
  for (float v : histogram_) histogramMax_ = std::max(histogramMax_, v);

  // Stats.
  if (n > 0) {
    double mean = static_cast<double>(sum) / n;
    double var = static_cast<double>(sumSq) / n - mean * mean;
    statMean_ = mean;
    statStd_ = var > 0 ? std::sqrt(var) : 0.0;
    statMin_ = mn;
    statMax_ = mx;
    satFracHigh_ = 100.0 * highCount / n;
    satFracLow_ = 100.0 * lowCount / n;
    // Median via cumulative histogram (approximate, bin-level).
    std::uint64_t half = n / 2;
    std::uint64_t acc = 0;
    int medianBin = 0;
    for (int i = 0; i < bins; ++i) {
      acc += static_cast<std::uint64_t>(histogram_[i]);
      if (acc >= half) { medianBin = i; break; }
    }
    statMedian_ = static_cast<int>((medianBin + 0.5f) * binWidth);
  }
}

CalibKey Gui::currentKey() const {
  CalibKey k;
  const Camera* cur = manager_.current();
  if (!cur) return k;
  try {
    RoiFormat roi = cur->getRoi();
    k.width = roi.width;
    k.height = roi.height;
    k.bin = roi.bin;
    k.imgType = roi.imageType;
  } catch (...) {
    k.width = roiWidth_ / std::max(1, roiBin_);
    k.height = roiHeight_ / std::max(1, roiBin_);
    k.bin = roiBin_;
    k.imgType = supportedImgTypes_.empty()
                    ? ASI_IMG_RAW16
                    : supportedImgTypes_[roiImgType_];
  }
  try { k.exposureUs = cur->getControl(ASI_EXPOSURE).value; } catch (...) {}
  try { k.gain = cur->getControl(ASI_GAIN).value; } catch (...) {}
  try { k.offset = cur->getControl(ASI_OFFSET).value; } catch (...) {}
  return k;
}

void Gui::tryAutoLoadCalibration() {
  if (!calib_ || !manager_.current()) return;
  CalibKey k = currentKey();
  int n = calib_->autoLoadFor(k);
  if (n > 0) appendLog("Auto-loaded " + std::to_string(n) + " calibration master(s)");
}

void Gui::doSnap() {
  Camera* cam = manager_.current();
  if (!cam) return;
  bool resumeLive = capture_ && capture_->running();
  try {
    if (resumeLive) {
      capture_->stop();
    }
    // ASI533 has no mechanical shutter; dark frames require physically
    // covering the optical input, not the SDK dark-exposure flag.
    cam->startExposure(false);
    snapping_ = true;
    // Poll status synchronously (simple; exposure is one-off).
    ASI_EXPOSURE_STATUS st = ASI_EXP_WORKING;
    long expUs = cam->getControl(ASI_EXPOSURE).value;
    int waited = 0;
    int maxWaitMs = static_cast<int>(expUs / 1000) + 5000;
    while (st == ASI_EXP_WORKING && waited < maxWaitMs) {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
      waited += 50;
      st = cam->expStatus();
    }
    if (st != ASI_EXP_SUCCESS) {
      appendLog("Snap failed (status=" + std::to_string(st) + ")");
    } else {
      RoiFormat roi = cam->getRoi();
      Frame f;
      f.width = roi.width;
      f.height = roi.height;
      f.bin = roi.bin;
      f.type = roi.imageType;
      f.data.resize(static_cast<std::size_t>(f.width) * f.height *
                    Camera::bytesPerPixel(f.type));
      cam->getDataAfterExp(f.data.data(), f.data.size());
      f.timestamp = std::chrono::system_clock::now();
      previewFrame_ = f;
      hasFrame_ = true;
      uploadFrameToTexture(f);
      computeHistogram(f);

      CaptureMetadata meta;
      meta.cameraName = cam->info().Name;
      meta.sdkVersion = CameraManager::sdkVersion();
      meta.serialNumber = cam->serialNumber();
      try { meta.gain = cam->getControl(ASI_GAIN).value; } catch (...) {}
      try { meta.exposureUs = cam->getControl(ASI_EXPOSURE).value; } catch (...) {}
      try { meta.offset = cam->getControl(ASI_OFFSET).value; } catch (...) {}
      try { meta.gamma = cam->getControl(ASI_GAMMA).value; } catch (...) {}
      try { meta.usbBandwidth = cam->getControl(ASI_BANDWIDTHOVERLOAD).value; } catch (...) {}
      meta.sensorTempC = cam->temperatureCelsius();
      meta.bitDepth = cam->info().BitDepth;
      meta.pixelSizeUm = static_cast<float>(cam->info().PixelSize);
      meta.bin = f.bin;

      if (saveTiff_) appendLog("Saved: " + ImageWriter::writeTiff(saveDir_, savePrefix_, f, meta));
      if (saveFits_) appendLog("Saved: " + ImageWriter::writeFits(saveDir_, savePrefix_, f, meta));
      if (savePng_)  appendLog("Saved: " + ImageWriter::writePng (saveDir_, savePrefix_, f));
      // Optional: save calibration-corrected float TIFF alongside.
      if (saveCorrectedAlongside_ && calib_ &&
          (calib_->hasDark() || calib_->hasFlat())) {
        try {
          AppliedFrame ap = calib_->apply(f);
          std::string correctedPath =
              (std::filesystem::path(saveDir_) /
               (savePrefix_ + "_corrected_" +
                std::to_string(std::chrono::system_clock::now()
                                   .time_since_epoch()
                                   .count()) +
                ".tif"))
                  .string();
          std::ostringstream desc;
          desc << "calibrated  dark="
               << (calib_->hasDark() ? "y" : "n")
               << "  flat=" << (calib_->hasFlat() ? "y" : "n");
          ImageWriter::writeFloatTiff(correctedPath, ap.width, ap.height,
                                      ap.data.data(), desc.str());
          appendLog("Saved corrected: " + correctedPath);
        } catch (const std::exception& e) {
          appendLog(std::string("corrected save failed: ") + e.what());
        }
      }
    }
    snapping_ = false;
    if (resumeLive) {
      capture_ = std::make_unique<CaptureThread>(*cam);
      capture_->start();
    }
  } catch (const std::exception& e) {
    appendLog(std::string("snap exception: ") + e.what());
    snapping_ = false;
    if (resumeLive && !(capture_ && capture_->running())) {
      try {
        capture_ = std::make_unique<CaptureThread>(*cam);
        capture_->start();
      } catch (...) {
      }
    }
  }
}

// ---------------------------------------------------------------------------
//  RENDERING
// ---------------------------------------------------------------------------

void Gui::draw(GLFWwindow* window) {
  (void)window;

  // Pull a frame from the capture thread, if running.
  if (capture_ && capture_->running()) {
    const auto now = std::chrono::steady_clock::now();
    const int fpsLimit = std::clamp(previewMaxFps_, 1, 60);
    const auto minInterval =
        std::chrono::milliseconds(std::max(1, 1000 / fpsLimit));
    if (lastPreviewPull_.time_since_epoch().count() == 0 ||
        now - lastPreviewPull_ >= minInterval) {
      Frame f;
      if (capture_->latest(f)) {
        previewFrame_ = f;
        hasFrame_ = true;
        uploadFrameToTexture(f);
        if ((histogramFrameCounter_++ % 4) == 0) {
          computeHistogram(f);
        }
      }
      lastPreviewPull_ = now;
    }
  }

  ImGuiViewport* vp = ImGui::GetMainViewport();
  ImGuiID dockspaceId = ImGui::DockSpaceOverViewport(
      vp->ID, vp, ImGuiDockNodeFlags_PassthruCentralNode);

  // ---------------------------------------------------------------------
  // Lay out the dockspace fresh whenever the mode requires it (first time
  // ever, or every time the user flips between Config and Hyperspectral).
  // ImGui saves the layout to imgui.ini so subsequent visits restore the
  // user's tweaks.
  // ---------------------------------------------------------------------
  auto buildConfigLayout = [&]() {
    ImGui::DockBuilderRemoveNode(dockspaceId);
    ImGui::DockBuilderAddNode(dockspaceId,
                              ImGuiDockNodeFlags_PassthruCentralNode |
                                  ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(dockspaceId, vp->WorkSize);

    ImGuiID dockMain = dockspaceId;
    ImGuiID dockLeft = ImGui::DockBuilderSplitNode(
        dockMain, ImGuiDir_Left, 0.22f, nullptr, &dockMain);
    ImGuiID dockRight = ImGui::DockBuilderSplitNode(
        dockMain, ImGuiDir_Right, 0.28f, nullptr, &dockMain);
    ImGuiID dockBottom = ImGui::DockBuilderSplitNode(
        dockMain, ImGuiDir_Down, 0.28f, nullptr, &dockMain);
    ImGuiID dockLeftBottom = ImGui::DockBuilderSplitNode(
        dockLeft, ImGuiDir_Down, 0.45f, nullptr, &dockLeft);
    ImGuiID dockRightBottom = ImGui::DockBuilderSplitNode(
        dockRight, ImGuiDir_Down, 0.45f, nullptr, &dockRight);

    ImGui::DockBuilderDockWindow("Cameras", dockLeft);
    ImGui::DockBuilderDockWindow("Monochromator", dockLeft);
    ImGui::DockBuilderDockWindow("ROI / Format", dockLeftBottom);
    ImGui::DockBuilderDockWindow("Controls", dockRight);
    ImGui::DockBuilderDockWindow("Capture", dockRightBottom);
    ImGui::DockBuilderDockWindow("Calibration", dockRightBottom);
    ImGui::DockBuilderDockWindow("Preview", dockMain);
    ImGui::DockBuilderDockWindow("Histogram", dockBottom);
    ImGui::DockBuilderDockWindow("Log", dockBottom);
    ImGui::DockBuilderFinish(dockspaceId);
  };

  auto buildHyperspectralLayout = [&]() {
    ImGui::DockBuilderRemoveNode(dockspaceId);
    ImGui::DockBuilderAddNode(dockspaceId,
                              ImGuiDockNodeFlags_PassthruCentralNode |
                                  ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(dockspaceId, vp->WorkSize);

    ImGuiID dockMain = dockspaceId;
    ImGuiID dockLeft = ImGui::DockBuilderSplitNode(
        dockMain, ImGuiDir_Left, 0.28f, nullptr, &dockMain);
    ImGuiID dockRight = ImGui::DockBuilderSplitNode(
        dockMain, ImGuiDir_Right, 0.30f, nullptr, &dockMain);
    ImGuiID dockBottom = ImGui::DockBuilderSplitNode(
        dockMain, ImGuiDir_Down, 0.24f, nullptr, &dockMain);
    ImGuiID dockLeftBottom = ImGui::DockBuilderSplitNode(
        dockLeft, ImGuiDir_Down, 0.42f, nullptr, &dockLeft);

    ImGui::DockBuilderDockWindow("Scan Setup", dockLeft);
    ImGui::DockBuilderDockWindow("Scan Progress", dockLeftBottom);
    ImGui::DockBuilderDockWindow("Preview", dockRight);
    ImGui::DockBuilderDockWindow("Cube Viewer", dockMain);
    ImGui::DockBuilderDockWindow("Log", dockBottom);
    ImGui::DockBuilderFinish(dockspaceId);
  };

  // Always rebuild from scratch when the mode changes. The previous layout's
  // dock nodes are torn down by DockBuilderRemoveNode so windows can't end up
  // stranded after a mode switch.
  if (pendingLayoutBuild_) {
    pendingLayoutBuild_ = false;
    if (mode_ == AppMode::Manual) buildConfigLayout();
    else                          buildHyperspectralLayout();
  }

  renderTopMenu();

  if (mode_ == AppMode::Manual) {
    renderDevicePanel();
    renderMonochromatorPanel();
    renderControlsPanel();
    renderRoiPanel();
    renderCapturePanel();
    renderCalibrationPanel();
    renderPreview();
    renderHistogram();
  } else {
    renderScanSetupPanel();
    renderScanProgressPanel();
    renderCubeViewerPanel();
    renderPreview();
  }
  renderStatusBar();
}

void Gui::renderTopMenu() {
  if (ImGui::BeginMainMenuBar()) {
    if (ImGui::BeginMenu("File")) {
      if (ImGui::MenuItem("Refresh device list")) manager_.refresh();
      if (ImGui::MenuItem("Close camera", nullptr, false,
                          manager_.current() != nullptr)) {
        closeCamera();
      }
      ImGui::EndMenu();
    }
    if (ImGui::BeginMenu("Mode")) {
      if (ImGui::MenuItem("Manual Mode", nullptr,
                          mode_ == AppMode::Manual)) {
        if (mode_ != AppMode::Manual) {
          mode_ = AppMode::Manual;
          pendingLayoutBuild_ = true;
        }
      }
      if (ImGui::MenuItem("Hyperspectral Acquisition", nullptr,
                          mode_ == AppMode::Hyperspectral)) {
        if (mode_ != AppMode::Hyperspectral) {
          mode_ = AppMode::Hyperspectral;
          pendingLayoutBuild_ = true;
        }
      }
      ImGui::EndMenu();
    }
    if (ImGui::BeginMenu("View")) {
      ImGui::MenuItem("Auto stretch preview", nullptr, &autoStretch_);
      ImGui::EndMenu();
    }
    const char* modeLabel = (mode_ == AppMode::Manual)
                                ? "[ Manual Mode ]"
                                : "[ Hyperspectral ]";
    ImGui::Text("   %s", modeLabel);

    // ---- Persistent status indicators (right-aligned) ----------------
    Camera* cam = manager_.current();
    const bool monoConn = mono_.connected();
    const bool live = capture_ && capture_->running();
    const bool scanRun = scanThread_ &&
                        (scanThread_->status() == ScanThread::Status::Running ||
                         scanThread_->status() == ScanThread::Status::Exporting);
    const bool calRun = calibJob_ &&
                       calibJob_->status() == MasterAcquisition::Status::Running;

    // Build labels — no dash when disconnected, just the word.
    std::string camLabel = cam ? std::string("Camera: ") + cam->info().Name
                                : std::string("Camera");

    std::string monoLabel;
    if (monoConn) {
      const std::string& p = mono_.portName();
      auto pos = p.find_last_of('/');
      monoLabel = std::string("Monochromator: ") +
                  ((pos != std::string::npos) ? p.substr(pos + 1) : p);
    } else {
      monoLabel = "Monochromator";
    }

    char actLabel[64] = "";
    bool actActive = false;
    ImVec4 actCol(0.55f, 0.85f, 1.0f, 1.0f);
    if (scanRun) {
      actActive = true;
      std::snprintf(actLabel, sizeof(actLabel), "SCAN %d/%d",
                    scanThread_->progress(), scanThread_->total());
      actCol = ImVec4(1.0f, 0.78f, 0.30f, 1.0f);
    } else if (calRun) {
      actActive = true;
      std::snprintf(actLabel, sizeof(actLabel), "CAL %d/%d",
                    calibJob_->progress(), calibJob_->total());
      actCol = ImVec4(1.0f, 0.78f, 0.30f, 1.0f);
    } else if (live) {
      actActive = true;
      std::snprintf(actLabel, sizeof(actLabel), "Live %.1f fps",
                    capture_->fps());
    }

    std::string sdkLabel = "SDK " + CameraManager::sdkVersion();

    // Helper: a compact "dot + label" badge for the menu bar. The dot is
    // drawn AFTER the text using the text's actual screen-space rectangle so
    // it always sits exactly on the text's vertical centre.
    auto barBadge = [&](BadgeKind kind, const char* label,
                        const ImVec4* customCol = nullptr) {
      ImVec4 col;
      if (customCol) {
        col = *customCol;
      } else {
        switch (kind) {
          case BadgeKind::Ok:  col = ImVec4(0.40f, 0.90f, 0.50f, 1.0f); break;
          case BadgeKind::Warn:col = ImVec4(1.00f, 0.78f, 0.20f, 1.0f); break;
          case BadgeKind::Err: col = ImVec4(1.00f, 0.40f, 0.40f, 1.0f); break;
          default:             col = ImVec4(0.55f, 0.55f, 0.58f, 1.0f); break;
        }
      }
      float lineH = ImGui::GetTextLineHeight();
      float r = lineH * 0.30f;
      float dotW = r * 2.0f + 8.0f;
      // Capture where the badge starts on screen, then reserve dot space
      // and render the text.
      ImVec2 startScreen = ImGui::GetCursorScreenPos();
      ImGui::SetCursorPosX(ImGui::GetCursorPosX() + dotW);
      ImGui::TextUnformatted(label);
      // Centre the dot on the menu bar's vertical mid-line — same line that
      // ImGui uses for centring menu text. This guarantees baseline match
      // regardless of font metrics.
      ImVec2 winPos = ImGui::GetWindowPos();
      float winH = ImGui::GetWindowSize().y;
      float yMid = winPos.y + winH * 0.5f;
      float dotX = startScreen.x + r + 2.0f;
      ImGui::GetWindowDrawList()->AddCircleFilled(
          ImVec2(dotX, yMid), r, ImGui::GetColorU32(col), 16);
    };

    // Compute total width of the right-aligned cluster, then jump there.
    const float spacing = 16.0f;
    const float dotPad = ImGui::GetTextLineHeight() * 0.64f + 10.0f;
    float rightW =
        ImGui::CalcTextSize(camLabel.c_str()).x + dotPad + spacing +
        ImGui::CalcTextSize(monoLabel.c_str()).x + dotPad + spacing +
        ImGui::CalcTextSize(sdkLabel.c_str()).x + 16.0f;
    if (actActive) {
      rightW += ImGui::CalcTextSize(actLabel).x + dotPad + spacing;
    }
    float targetX = ImGui::GetWindowWidth() - rightW;
    if (targetX > ImGui::GetCursorPosX()) {
      ImGui::SameLine(targetX);
    } else {
      ImGui::SameLine(0, spacing);
    }

    barBadge(cam ? BadgeKind::Ok : BadgeKind::Err, camLabel.c_str());
    ImGui::SameLine(0, spacing);
    barBadge(monoConn ? BadgeKind::Ok : BadgeKind::Err, monoLabel.c_str());
    if (actActive) {
      ImGui::SameLine(0, spacing);
      barBadge(BadgeKind::Ok, actLabel, &actCol);
    }
    ImGui::SameLine(0, spacing);
    ImGui::TextDisabled("%s", sdkLabel.c_str());

    ImGui::EndMainMenuBar();
  }
}

void Gui::renderDevicePanel() {
  ImGui::Begin("Cameras");

  Camera* cur = manager_.current();
  StatusBadge(cur ? BadgeKind::Ok : BadgeKind::Idle,
              cur ? "Camera connected" : "No camera open");

  ImGui::Spacing();
  SectionHeader("Available devices");
  if (ImGui::Button("Refresh")) manager_.refresh();
  ImGui::SameLine();
  ImGui::TextDisabled("%d found", static_cast<int>(manager_.devices().size()));

  if (manager_.devices().empty()) {
    ImGui::Dummy(ImVec2(0, 4));
    ImGui::TextDisabled("No ZWO ASI cameras detected.");
    ImGui::TextDisabled("Plug in via USB and click Refresh.");
  } else {
    if (ImGui::BeginTable("##devTable", 3,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH)) {
      ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
      ImGui::TableSetupColumn("Size", ImGuiTableColumnFlags_WidthFixed, 110);
      ImGui::TableSetupColumn("Type", ImGuiTableColumnFlags_WidthFixed, 60);
      for (std::size_t i = 0; i < manager_.devices().size(); ++i) {
        const auto& d = manager_.devices()[i];
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        bool sel = static_cast<int>(i) == selectedDevice_;
        if (ImGui::Selectable(d.Name, sel,
                              ImGuiSelectableFlags_SpanAllColumns)) {
          selectedDevice_ = static_cast<int>(i);
        }
        ImGui::TableNextColumn();
        ImGui::Text("%ldx%ld", d.MaxWidth, d.MaxHeight);
        ImGui::TableNextColumn();
        ImGui::TextColored(d.IsColorCam ? ImVec4(1, 0.7f, 0.4f, 1)
                                        : ImVec4(0.7f, 0.85f, 1, 1),
                           "%s", d.IsColorCam ? "color" : "mono");
      }
      ImGui::EndTable();
    }
  }

  ImGui::Spacing();
  if (!cur) {
    ImGui::BeginDisabled(manager_.devices().empty());
    if (ImGui::Button("Open Selected Camera", ImVec2(-1, 32))) {
      openSelectedCamera();
    }
    ImGui::EndDisabled();
  } else {
    SectionHeader("Active camera");
    if (ImGui::BeginTable("##infoTable", 2,
                          ImGuiTableFlags_BordersInnerH |
                          ImGuiTableFlags_SizingFixedFit)) {
      auto row = [&](const char* k, const char* fmt, auto v) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.65f, 1.0f), "%s", k);
        ImGui::TableNextColumn();
        ImGui::Text(fmt, v);
      };
      row("Name",      "%s",     cur->info().Name);
      if (auto sn = cur->serialNumber()) row("Serial", "%s", sn->c_str());
      row("ADC depth", "%d-bit effective", cur->info().BitDepth);
      row("Pixel",     "%.2f um", cur->info().PixelSize);
      ImGui::EndTable();
    }
    std::string fmts;
    for (int i = 0; i < 8; ++i) {
      ASI_IMG_TYPE t = cur->info().SupportedVideoFormat[i];
      if (t == ASI_IMG_END) break;
      if (!fmts.empty()) fmts += ", ";
      fmts += Camera::imageTypeName(t);
    }
    if (!fmts.empty()) {
      ImGui::TextDisabled("SDK pixel formats: %s", fmts.c_str());
    }

    ImGui::Dummy(ImVec2(0, 4));
    SectionHeader("Capabilities");
    auto cap = [](const char* n, bool ok) {
      StatusBadge(ok ? BadgeKind::Ok : BadgeKind::Idle, n);
    };
    cap("USB3 host",      cur->info().IsUSB3Host);
    cap("USB3 camera",    cur->info().IsUSB3Camera);
    cap("Cooler",         cur->info().IsCoolerCam);
    cap("Hardware trigger", cur->info().IsTriggerCam);
    cap("Mech. shutter",  cur->info().MechanicalShutter);

    ImGui::Dummy(ImVec2(0, 6));
    if (ImGui::Button("Close camera", ImVec2(-1, 0))) closeCamera();
  }
  ImGui::End();
}

void Gui::renderControlsPanel() {
  ImGui::Begin("Controls");
  Camera* cur = manager_.current();
  if (!cur) {
    StatusBadge(BadgeKind::Idle, "No camera open");
    ImGui::Dummy(ImVec2(0, 4));
    ImGui::TextDisabled("Open a camera in the Cameras panel to see its");
    ImGui::TextDisabled("exposure / gain / cooling / advanced controls.");
    ImGui::End();
    return;
  }

  auto drawControl = [&](ASI_CONTROL_TYPE t) {
    const ControlDescriptor* desc = nullptr;
    for (const auto& d : cur->controls()) {
      if (d.type == t) { desc = &d; break; }
    }
    if (!desc) return false;
    ControlValue cv;
    try { cv = cur->getControl(t); }
    catch (...) { return false; }

    ImGui::PushID(static_cast<int>(t));
    bool ro = isReadOnly(t) || !desc->isWritable;
    const char* help = controlHelp(t, *desc);

    if (t == ASI_TEMPERATURE) {
      float c = cv.value / 10.0f;
      ImVec4 col = c > 40 ? ImVec4(1, 0.5f, 0.4f, 1)
                          : c < 10 ? ImVec4(0.6f, 0.8f, 1, 1)
                                   : ImVec4(0.9f, 0.9f, 0.9f, 1);
      ImGui::Text("%s", desc->name.c_str());
      ImGui::SameLine();
      HelpMarker(help);
      ImGui::SameLine(ImGui::GetContentRegionAvail().x - 70);
      ImGui::TextColored(col, "%.1f °C", c);
    } else if (desc->minValue == 0 && desc->maxValue == 1) {
      bool b = cv.value != 0;
      ImGui::BeginDisabled(ro);
      if (ImGui::Checkbox(desc->name.c_str(), &b)) {
        try { cur->setControl(t, b ? 1 : 0, cv.isAuto); }
        catch (const std::exception& e) { appendLog(e.what()); }
      }
      ImGui::EndDisabled();
      ImGui::SameLine();
      HelpMarker(help);
    } else {
      // Use 64-bit values throughout — ASI exposure can reach 2e9 us, well
      // beyond what a float-backed SliderInt can represent accurately.
      long long mn = static_cast<long long>(desc->minValue);
      long long mx = static_cast<long long>(desc->maxValue);
      long long val = cv.value;
      if (val < mn) val = mn;
      if (val > mx) val = mx;

      ImGui::TextUnformatted(desc->name.c_str());
      ImGui::SameLine();
      HelpMarker(help);

      ImGuiSliderFlags flags = ImGuiSliderFlags_AlwaysClamp;
      // Exposure spans many decades; a logarithmic slider is much more usable.
      if (t == ASI_EXPOSURE || t == ASI_AUTO_MAX_EXP) {
        flags |= ImGuiSliderFlags_Logarithmic;
      }

      ImGui::BeginDisabled(ro);
      float availW = ImGui::GetContentRegionAvail().x;
      bool wantAuto = desc->isAutoSupported;
      float inputW = std::min(120.0f, availW * 0.34f);
      float sliderW = std::max(80.0f, availW - inputW - (wantAuto ? 68.0f : 8.0f));
      ImGui::PushItemWidth(sliderW);
      bool changed = ImGui::SliderScalar("##slider", ImGuiDataType_S64,
                                         &val, &mn, &mx, "%lld", flags);
      ImGui::PopItemWidth();
      ImGui::SameLine();
      long long step = (t == ASI_EXPOSURE || t == ASI_AUTO_MAX_EXP) ? 1000 : 1;
      long long stepFast = (t == ASI_EXPOSURE || t == ASI_AUTO_MAX_EXP) ? 100000 : 10;
      ImGui::PushItemWidth(inputW);
      changed |= ImGui::InputScalar("##value", ImGuiDataType_S64, &val,
                                    &step, &stepFast, "%lld");
      ImGui::PopItemWidth();
      if (changed) {
        if (val < mn) val = mn;
        if (val > mx) val = mx;
        try { cur->setControl(t, static_cast<long>(val), cv.isAuto); }
        catch (const std::exception& e) { appendLog(e.what()); }
      }
      ImGui::EndDisabled();
      if (wantAuto) {
        ImGui::SameLine();
        bool a = cv.isAuto;
        if (ImGui::Checkbox("Auto", &a)) {
          try { cur->setControl(t, cv.value, a); }
          catch (const std::exception& e) { appendLog(e.what()); }
        }
      }
      if (t == ASI_EXPOSURE || t == ASI_AUTO_MAX_EXP) {
        ImGui::TextDisabled("Current: %s (%lld us)", formatExposure(val).c_str(), val);
      } else {
        ImGui::TextDisabled("Range: %lld ... %lld", mn, mx);
      }
    }
    ImGui::PopID();
    return true;
  };

	  // ---- Exposure & Gain --------------------------------------------------
	  if (ImGui::CollapsingHeader("Exposure & Gain",
	                              ImGuiTreeNodeFlags_DefaultOpen)) {
    auto exp = tryGetControl(*cur, ASI_EXPOSURE);
    auto gain = tryGetControl(*cur, ASI_GAIN);
    if ((exp && exp->isAuto) || (gain && gain->isAuto)) {
      ImGui::TextColored(ImVec4(1.0f, 0.62f, 0.28f, 1.0f),
                         "Auto exposure/gain is on. Turn it off for measurement.");
      ImGui::SameLine();
      HelpMarker("Auto controls are useful for preview, but quantitative capture should use fixed exposure, gain and offset.");
    }
	    drawControl(ASI_EXPOSURE);
	    drawControl(ASI_GAIN);
	    drawControl(ASI_OFFSET);
    drawControl(ASI_GAMMA);
    ImGui::Dummy(ImVec2(0, 2));
    ImGui::TextDisabled("Auto-exposure limits");
    drawControl(ASI_AUTO_MAX_EXP);
    drawControl(ASI_AUTO_MAX_GAIN);
    drawControl(ASI_AUTO_TARGET_BRIGHTNESS);
  }

	  // ---- Image / acquisition ---------------------------------------------
	  if (ImGui::CollapsingHeader("Image & Acquisition",
	                              ImGuiTreeNodeFlags_DefaultOpen)) {
	    drawControl(ASI_BANDWIDTHOVERLOAD);
	  }

  // ---- Cooling / fan ---------------------------------------------------
  bool isCooler = cur->info().IsCoolerCam;
  if (ImGui::CollapsingHeader("Cooling & Fan",
                              isCooler ? ImGuiTreeNodeFlags_DefaultOpen : 0)) {
    SectionHeader("Temperature status");
    auto temp = tryGetControl(*cur, ASI_TEMPERATURE);
    auto target = tryGetControl(*cur, ASI_TARGET_TEMP);
    auto coolerOn = tryGetControl(*cur, ASI_COOLER_ON);
    auto coolerPower = tryGetControl(*cur, ASI_COOLER_POWER_PERC);

    if (temp) {
      float c = temp->value / 10.0f;
      ImVec4 col = c > 40 ? ImVec4(1, 0.5f, 0.4f, 1)
                          : c < 10 ? ImVec4(0.6f, 0.8f, 1, 1)
                                   : ImVec4(0.9f, 0.9f, 0.9f, 1);
      ImGui::TextUnformatted("Sensor");
      ImGui::SameLine(120);
      ImGui::TextColored(col, "%.1f °C", c);
    } else {
      ImGui::TextDisabled("Sensor temperature unavailable");
    }

    if (isCooler) {
      ImGui::TextUnformatted("Cooler");
      ImGui::SameLine(120);
      if (coolerOn) {
        StatusBadge(coolerOn->value ? BadgeKind::Ok : BadgeKind::Idle,
                    coolerOn->value ? "On" : "Off");
      } else {
        ImGui::TextDisabled("Unavailable");
      }

      ImGui::TextUnformatted("Target");
      ImGui::SameLine(120);
      if (target) ImGui::Text("%ld °C", target->value);
      else ImGui::TextDisabled("Unavailable");

      ImGui::TextUnformatted("TEC power");
      ImGui::SameLine(120);
      if (coolerPower) {
        float frac = std::clamp(coolerPower->value / 100.0f, 0.0f, 1.0f);
        ImGui::ProgressBar(frac, ImVec2(-1, 0),
                           (std::to_string(coolerPower->value) + "%").c_str());
      } else {
        ImGui::TextDisabled("Unavailable");
      }
    } else {
      ImGui::TextDisabled("This camera has no TEC cooler.");
    }

    SectionHeader("Controls");
    drawControl(ASI_TEMPERATURE);
    if (isCooler) {
      drawControl(ASI_COOLER_ON);
      drawControl(ASI_TARGET_TEMP);
      drawControl(ASI_COOLER_POWER_PERC);
    } else {
      ImGui::TextDisabled("This camera has no cooler.");
    }
    drawControl(ASI_FAN_ON);
    drawControl(ASI_ANTI_DEW_HEATER);
  }

	  // ---- Advanced --------------------------------------------------------
	  if (ImGui::CollapsingHeader("Advanced")) {
    drawControl(ASI_HIGH_SPEED_MODE);
    drawControl(ASI_HARDWARE_BIN);
    drawControl(ASI_MONO_BIN);
    drawControl(ASI_FLIP);
	    drawControl(ASI_PWRLED_BRIGNT);
	  }

  ImGui::End();
}

void Gui::renderRoiPanel() {
  ImGui::Begin("ROI / Format");
  Camera* cur = manager_.current();
  if (!cur) {
    ImGui::TextDisabled("No camera open.");
    ImGui::End();
    return;
  }
  int maxW = cur->info().MaxWidth;
  int maxH = cur->info().MaxHeight;

  SectionHeader("Sensor");
  ImGui::Text("Max size: %d × %d  (%.1f MP)", maxW, maxH,
              maxW * maxH / 1e6);

  // -- Mini visual of sensor + current ROI rectangle --------------------
  {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 origin = ImGui::GetCursorScreenPos();
    float w = std::min(ImGui::GetContentRegionAvail().x, 240.0f);
    float h = w * static_cast<float>(maxH) / std::max(1, maxW);
    ImVec2 br(origin.x + w, origin.y + h);
    dl->AddRectFilled(origin, br, IM_COL32(35, 35, 40, 255), 4.0f);
    dl->AddRect(origin, br, IM_COL32(80, 80, 90, 255), 4.0f);
    float sx = w / maxW;
    float sy = h / maxH;
    ImVec2 rTl(origin.x + roiX_ * sx, origin.y + roiY_ * sy);
    ImVec2 rBr(origin.x + (roiX_ + roiWidth_) * sx,
               origin.y + (roiY_ + roiHeight_) * sy);
    dl->AddRectFilled(rTl, rBr, IM_COL32(80, 140, 220, 90), 2.0f);
    dl->AddRect(rTl, rBr, IM_COL32(140, 200, 255, 255), 2.0f, 0, 1.5f);
    ImGui::Dummy(ImVec2(w, h + 4));
  }

  SectionHeader("Format");
  const char* fmtNames[8] = {"?", "?", "?", "?", "?", "?", "?", "?"};
  for (std::size_t i = 0; i < supportedImgTypes_.size(); ++i) {
    fmtNames[i] = Camera::imageTypeName(supportedImgTypes_[i]);
  }
  ImGui::PushItemWidth(160);
  ImGui::Combo("Pixel format", &roiImgType_, fmtNames,
               static_cast<int>(supportedImgTypes_.size()));
  ImGui::PopItemWidth();
  ImGui::SameLine();
  HelpMarker("The ASI SDK exposes RAW8, RAW16, RGB24 and/or Y8. There is no RAW14 image type in SDK v1.41. For ASI533, RAW16 is a 16-bit transport container for the camera's effective 14-bit ADC data.");

  // Binning combo.
  int binIdx = 0;
  std::vector<int> bins;
  for (int i = 0; i < 16; ++i) {
    int b = cur->info().SupportedBins[i];
    if (b == 0) break;
    if (b == roiBin_) binIdx = static_cast<int>(bins.size());
    bins.push_back(b);
  }
  std::vector<std::string> binLabels;
  for (int b : bins) binLabels.push_back(std::to_string(b) + "x" + std::to_string(b));
  std::vector<const char*> binCstr;
  for (auto& s : binLabels) binCstr.push_back(s.c_str());
  if (!bins.empty()) {
    ImGui::PushItemWidth(160);
    if (ImGui::Combo("Binning", &binIdx, binCstr.data(),
                     static_cast<int>(bins.size()))) {
      roiBin_ = bins[binIdx];
    }
    ImGui::PopItemWidth();
  }

  SectionHeader("Region of interest");
  // Presets
  auto preset = [&](const char* label, int w, int h) {
    if (ImGui::Button(label)) {
      roiWidth_  = std::min(w, maxW) / 8 * 8;
      roiHeight_ = std::min(h, maxH) / 2 * 2;
      roiX_ = (maxW - roiWidth_) / 2 / 8 * 8;
      roiY_ = (maxH - roiHeight_) / 2 / 2 * 2;
    }
    ImGui::SameLine();
  };
  if (ImGui::Button("Full sensor")) {
    roiWidth_ = maxW; roiHeight_ = maxH; roiX_ = 0; roiY_ = 0;
  }
  ImGui::SameLine();
  preset("1024²", 1024, 1024);
  preset("512²", 512, 512);
  preset("256²", 256, 256);
  ImGui::NewLine();

  ImGui::PushItemWidth(-1);
  ImGui::SliderInt("##W", &roiWidth_,  8, maxW, "Width  %d px");
  ImGui::SliderInt("##H", &roiHeight_, 8, maxH, "Height %d px");
  ImGui::SliderInt("##X", &roiX_, 0, std::max(0, maxW - roiWidth_),
                   "X offset %d");
  ImGui::SliderInt("##Y", &roiY_, 0, std::max(0, maxH - roiHeight_),
                   "Y offset %d");
  ImGui::PopItemWidth();

	  ImGui::Dummy(ImVec2(0, 4));
	  ImGui::TextDisabled(
	      "Note: width is rounded to a multiple of 8, height to 2.");
  ImGui::SameLine();
  HelpMarker("The UI edits sensor-space ROI. The SDK receives binned coordinates; the camera output below shows the actual captured frame.");

  try {
    RoiFormat current = cur->getRoi();
    StartPos pos = cur->getStartPos();
    ImGui::Text("Camera output: %d x %d  bin %dx  %s",
                current.width, current.height, current.bin,
                Camera::imageTypeName(current.imageType));
    ImGui::TextDisabled("SDK start: (%d, %d)   sensor-space: (%d, %d)",
                        pos.x, pos.y, pos.x * current.bin, pos.y * current.bin);
  } catch (...) {
    int effW = roiWidth_ / std::max(1, roiBin_);
    int effH = roiHeight_ / std::max(1, roiBin_);
    ImGui::TextDisabled("Pending output: %d x %d  bin %dx", effW, effH, roiBin_);
  }

	  if (ImGui::Button("Apply ROI", ImVec2(-1, 32))) {
    bool wasRunning = capture_ && capture_->running();
    try {
      if (wasRunning) capture_->stop();
      int bin = std::max(1, roiBin_);
      int binnedMaxW = std::max(1, maxW / bin);
      int binnedMaxH = std::max(1, maxH / bin);
      int sdkW = std::clamp(roiWidth_ / bin, 8, binnedMaxW);
      int sdkH = std::clamp(roiHeight_ / bin, 2, binnedMaxH);
      sdkW = std::max(8, (sdkW / 8) * 8);
      sdkH = std::max(2, (sdkH / 2) * 2);
      if (sdkW > binnedMaxW) sdkW = std::max(8, (binnedMaxW / 8) * 8);
      if (sdkH > binnedMaxH) sdkH = std::max(2, (binnedMaxH / 2) * 2);

      int sdkX = std::clamp(roiX_ / bin, 0, std::max(0, binnedMaxW - sdkW));
      int sdkY = std::clamp(roiY_ / bin, 0, std::max(0, binnedMaxH - sdkH));

      cur->setRoi({sdkW, sdkH, bin, supportedImgTypes_[roiImgType_]});
      cur->setStartPos({sdkX, sdkY});

      RoiFormat applied = cur->getRoi();
      StartPos appliedPos = cur->getStartPos();
      roiBin_ = applied.bin;
      roiWidth_ = applied.width * roiBin_;
      roiHeight_ = applied.height * roiBin_;
      roiX_ = appliedPos.x * roiBin_;
      roiY_ = appliedPos.y * roiBin_;
      tryAutoLoadCalibration();
      if (wasRunning) capture_->start();
      appendLog("ROI applied: " + std::to_string(roiWidth_) + "×" +
                std::to_string(roiHeight_) + " @ (" +
                std::to_string(roiX_) + "," + std::to_string(roiY_) + ")");
    } catch (const std::exception& e) {
      if (wasRunning) {
        try { capture_->start(); } catch (...) {}
      }
      appendLog(std::string("ROI: ") + e.what());
    }
  }

  ImGui::End();
}

void Gui::renderCapturePanel() {
  ImGui::Begin("Capture");
  Camera* cur = manager_.current();
  if (!cur) {
    ImGui::TextDisabled("No camera open.");
    ImGui::End();
    return;
  }

  bool live = capture_ && capture_->running();
  SectionHeader("Live video");
  StatusBadge(live ? BadgeKind::Ok : BadgeKind::Idle,
              live ? "Streaming" : "Stopped");
  if (live) {
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 100);
    ImGui::TextColored(ImVec4(0.55f, 0.85f, 1.0f, 1.0f), "%.1f fps",
                       capture_->fps());
  }
  if (live) {
    ImGui::TextDisabled("Frames: %llu   Timeouts: %llu",
        static_cast<unsigned long long>(capture_->totalFrames()),
        static_cast<unsigned long long>(capture_->timeoutCount()));
  }
  ImGui::Dummy(ImVec2(0, 4));
  if (ImGui::Button(live ? "Stop streaming" : "Start streaming",
                    ImVec2(-1, 28))) {
    try {
      if (!live) {
        if (!capture_) capture_ = std::make_unique<CaptureThread>(*cur);
        lastPreviewPull_ = {};
        histogramFrameCounter_ = 0;
        capture_->start();
        videoMode_ = true;
      } else {
        if (capture_) capture_->stop();
        videoMode_ = false;
      }
    } catch (const std::exception& e) {
      appendLog(e.what());
    }
  }

  ImGui::PushItemWidth(-1);
  ImGui::SliderInt("Preview FPS limit", &previewMaxFps_, 1, 30);
  ImGui::SameLine();
  HelpMarker("Limits how often the UI copies, stretches and uploads live frames. SNAP and scan data are not affected.");
  const char* scaleLabels[] = {"Full", "1:2", "1:4"};
  int scaleIdx = previewScale_ == 4 ? 2 : (previewScale_ == 2 ? 1 : 0);
  if (ImGui::Combo("Preview resolution", &scaleIdx, scaleLabels, 3)) {
    previewScale_ = scaleIdx == 2 ? 4 : (scaleIdx == 1 ? 2 : 1);
    texW_ = 0;
    texH_ = 0;
  }
  ImGui::SameLine();
  HelpMarker("Only the displayed live preview is downscaled. The latest raw frame, SNAP and hyperspectral capture remain full ROI data.");
  ImGui::PopItemWidth();

	  ImGui::Spacing();
	  SectionHeader("Save settings");
  ImGui::TextDisabled("SNAP output");
  ImGui::SameLine();
  HelpMarker("SNAP files are saved as <measurement_name>_<timestamp>.tif/fits/png inside the selected folder.");
	  char buf[256];
	  ImGui::PushItemWidth(-1);
	  std::strncpy(buf, saveDir_.c_str(), sizeof(buf) - 1);
	  buf[sizeof(buf) - 1] = 0;
	  if (ImGui::InputTextWithHint("Output folder", "captures", buf, sizeof(buf)))
	    saveDir_ = buf;
	  std::strncpy(buf, savePrefix_.c_str(), sizeof(buf) - 1);
	  buf[sizeof(buf) - 1] = 0;
	  if (ImGui::InputTextWithHint("Measurement name", "sample_or_condition", buf, sizeof(buf)))
	    savePrefix_ = buf;
	  ImGui::PopItemWidth();
	  ImGui::TextDisabled("Next SNAP: %s/%s_<timestamp>.{tif,fits,png}",
	                      saveDir_.c_str(), savePrefix_.c_str());

  ImGui::Dummy(ImVec2(0, 4));
  ImGui::TextUnformatted("Formats");
  ImGui::SameLine();
  HelpMarker(
      "TIFF — 16-bit greyscale (ImageJ/Fiji)\n"
      "FITS — astronomy format with metadata header\n"
      "PNG  — 8-bit preview");
  ImGui::Checkbox("TIFF (16-bit)", &saveTiff_);
  ImGui::SameLine();
  ImGui::Checkbox("FITS", &saveFits_);
  ImGui::SameLine();
  ImGui::Checkbox("PNG", &savePng_);

  ImGui::Dummy(ImVec2(0, 4));
  ImGui::Checkbox("Next snap is a manual DARK", &saveAsDark_);
  if (saveAsDark_)
    ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1),
                       "Cover lens/input before SNAP; camera has no internal shutter.");

	  ImGui::Spacing();
	  SectionHeader("Acquisition");
  ImGui::TextDisabled("SNAP = controlled single exposure for measurement.");
  ImGui::SameLine();
  HelpMarker("SNAP stops live video if needed, runs single-exposure capture, then saves the selected TIFF/FITS/PNG outputs.");
	  ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.25f, 0.55f, 0.85f, 1.0f));
	  ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0.35f, 0.65f, 0.95f, 1.0f));
	  if (ImGui::Button("SNAP single exposure", ImVec2(-1, 40))) doSnap();
	  ImGui::PopStyleColor(2);

	  ImGui::End();
	}

void Gui::renderPreview() {
  ImGui::Begin("Preview");
  if (!previewTex_ || !hasFrame_) {
    ImGui::TextDisabled("No frame yet. Start live video or take a snap.");
    ImGui::End();
    return;
  }

  // Top bar: frame info + FPS.
  ImGui::Text("%d × %d  ·  %s  ·  bin %dx",
              previewFrame_.width, previewFrame_.height,
              Camera::imageTypeName(previewFrame_.type), previewFrame_.bin);
  if (capture_ && capture_->running()) {
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 80);
    ImGui::TextColored(ImVec4(0.55f, 0.85f, 1.0f, 1.0f), "%.1f fps",
                       capture_->fps());
  }
  ImGui::Separator();

  ImVec2 avail = ImGui::GetContentRegionAvail();
  // Reserve a row at the bottom for the pixel readout.
  float readoutH = 22.0f;
  ImVec2 area(avail.x, std::max(50.0f, avail.y - readoutH));

  float aspect = static_cast<float>(texW_) / std::max(1, texH_);
  float w = area.x;
  float h = w / aspect;
  if (h > area.y) {
    h = area.y;
    w = h * aspect;
  }
  ImVec2 imgPos = ImGui::GetCursorScreenPos();
  ImGui::Image(static_cast<ImTextureID>(previewTex_), ImVec2(w, h));

  // Pixel readout under mouse.
  int hoverX = -1, hoverY = -1;
  long hoverV = -1;
  if (ImGui::IsItemHovered() && previewFrame_.width > 0) {
    ImVec2 mp = ImGui::GetMousePos();
    float u = (mp.x - imgPos.x) / std::max(1.0f, w);
    float vv = (mp.y - imgPos.y) / std::max(1.0f, h);
    if (u >= 0 && u <= 1 && vv >= 0 && vv <= 1) {
      hoverX = std::clamp(static_cast<int>(u * previewFrame_.width), 0,
                          previewFrame_.width - 1);
      hoverY = std::clamp(static_cast<int>(vv * previewFrame_.height), 0,
                          previewFrame_.height - 1);
      std::size_t idx = static_cast<std::size_t>(hoverY) *
                            previewFrame_.width + hoverX;
      if (previewFrame_.type == ASI_IMG_RAW16 &&
          idx * 2 + 1 < previewFrame_.data.size()) {
        const auto* p =
            reinterpret_cast<const std::uint16_t*>(previewFrame_.data.data());
        hoverV = p[idx];
      } else if (idx < previewFrame_.data.size()) {
        hoverV = previewFrame_.data[idx];
      }
      // Draw small crosshair on the image.
      ImDrawList* dl = ImGui::GetWindowDrawList();
      dl->AddCircle(mp, 6.0f, IM_COL32(255, 220, 80, 220), 12, 1.5f);
    }
  }

  // Readout line.
  if (hoverX >= 0) {
    ImGui::Text("Pixel (%d, %d)   value = %ld", hoverX, hoverY, hoverV);
  } else {
    ImGui::TextDisabled("Hover the image to read pixel values…");
  }

  ImGui::End();
}

void Gui::renderHistogram() {
  ImGui::Begin("Histogram");
  if (!hasFrame_) {
    ImGui::TextDisabled("No frame yet.");
    ImGui::End();
    return;
  }

  // Two-column layout: plot left, stats right.
  ImVec2 avail = ImGui::GetContentRegionAvail();
  float statsW = 180.0f;
  if (avail.x < statsW + 200.0f) statsW = avail.x * 0.35f;
  float plotW = avail.x - statsW - 8.0f;

  // Plot
  ImGui::BeginChild("##histPlot", ImVec2(plotW, avail.y));
  if (ImPlot::BeginPlot("##h", ImVec2(-1, -1),
                        ImPlotFlags_NoTitle | ImPlotFlags_NoMouseText |
                            ImPlotFlags_NoLegend)) {
    ImPlot::SetupAxis(ImAxis_X1, "Pixel value");
    ImPlot::SetupAxis(ImAxis_Y1, "Count", ImPlotAxisFlags_AutoFit);
    if (histogramLogY_) {
      ImPlot::SetupAxisScale(ImAxis_Y1, ImPlotScale_Log10);
    }
    const bool sixteen = (previewFrame_.type == ASI_IMG_RAW16);
    int axMax = sixteen ? 65532 : 255;
    ImPlot::SetupAxisLimits(ImAxis_X1, 0, axMax, ImPlotCond_Always);
    if (!histogram_.empty()) {
      float binWidth = static_cast<float>(axMax + 1) / histogram_.size();
      ImPlot::PlotBars("hist", histogramAxis_.data(), histogram_.data(),
                       static_cast<int>(histogram_.size()), binWidth);
    }
    // Black/white point lines (only meaningful for RAW16 manual stretch).
    if (sixteen && !autoStretch_) {
      double bp = blackPoint_;
      double wp = whitePoint_;
      ImPlot::DragLineX(101, &bp, ImVec4(0.2f, 0.6f, 1.0f, 1.0f), 1.0f);
      ImPlot::DragLineX(102, &wp, ImVec4(1.0f, 0.6f, 0.2f, 1.0f), 1.0f);
      blackPoint_ = std::clamp(static_cast<int>(bp), 0, axMax);
      whitePoint_ = std::clamp(static_cast<int>(wp), blackPoint_ + 1, axMax);
    }
    ImPlot::EndPlot();
  }
  ImGui::EndChild();

  ImGui::SameLine();

  // Stats sidebar
  ImGui::BeginChild("##histStats", ImVec2(statsW, avail.y), true);
  ImGui::TextUnformatted("Stats");
  ImGui::Separator();
  ImGui::Text("Min    : %d", statMin_);
  ImGui::Text("Max    : %d", statMax_);
  ImGui::Text("Mean   : %.1f", statMean_);
  ImGui::Text("Median : %d", statMedian_);
  ImGui::Text("StdDev : %.1f", statStd_);
  ImGui::Separator();
  ImVec4 hiCol = satFracHigh_ > 0.5 ? ImVec4(1, 0.4f, 0.4f, 1)
                                    : ImVec4(0.6f, 0.6f, 0.6f, 1);
  ImVec4 loCol = satFracLow_ > 0.5 ? ImVec4(0.4f, 0.7f, 1, 1)
                                   : ImVec4(0.6f, 0.6f, 0.6f, 1);
  ImGui::TextColored(hiCol, "Saturated : %.2f%%", satFracHigh_);
  ImGui::TextColored(loCol, "Zero-clip : %.2f%%", satFracLow_);
  ImGui::Separator();
  ImGui::Checkbox("Log Y", &histogramLogY_);
  ImGui::Checkbox("Auto stretch", &autoStretch_);
  if (previewFrame_.type == ASI_IMG_RAW16 && !autoStretch_) {
    ImGui::PushItemWidth(-1);
    ImGui::Text("Black");
    ImGui::SliderInt("##bp", &blackPoint_, 0, 65532);
    ImGui::Text("White");
    ImGui::SliderInt("##wp", &whitePoint_, 0, 65532);
    ImGui::PopItemWidth();
  }
  ImGui::EndChild();

  ImGui::End();
}

// ---------------------------------------------------------------------------
//  Calibration
// ---------------------------------------------------------------------------
void Gui::renderCalibrationPanel() {
  ImGui::Begin("Calibration");
  Camera* cur = manager_.current();
  if (!cur) {
    ImGui::TextDisabled("No camera open.");
    ImGui::End();
    return;
  }
  if (!calib_) {
    calib_ = std::make_unique<CalibrationManager>(saveDir_ + "/calib");
  }

  CalibKey k = currentKey();
  SectionHeader("Acquisition key");
  ImGui::TextDisabled("Masters are matched 1:1 by these parameters.");
  if (ImGui::BeginTable("##keyTable", 2,
                        ImGuiTableFlags_BordersInnerH |
                        ImGuiTableFlags_SizingStretchProp)) {
    auto kvrow = [](const char* k, const char* fmt, auto v) {
      ImGui::TableNextRow();
      ImGui::TableNextColumn();
      ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.65f, 1.0f), "%s", k);
      ImGui::TableNextColumn();
      ImGui::Text(fmt, v);
    };
    char sz[32]; std::snprintf(sz, sizeof(sz), "%dx%d", k.width, k.height);
    kvrow("Size",     "%s", sz);
    kvrow("Binning",  "%d", k.bin);
    kvrow("Format",   "%s", Camera::imageTypeName(k.imgType));
    kvrow("Exposure", "%ld us", k.exposureUs);
    kvrow("Gain",     "%ld", k.gain);
    kvrow("Offset",   "%ld", k.offset);
    ImGui::EndTable();
  }

  ImGui::Dummy(ImVec2(0, 6));
  SectionHeader("Status");
  auto statusLine = [&](const char* name, bool has, bool match,
                        const MasterFrame* mf) {
    BadgeKind bk;
    const char* state;
    if (!has)        { bk = BadgeKind::Idle; state = "MISSING"; }
    else if (!match) { bk = BadgeKind::Warn; state = "MISMATCH"; }
    else             { bk = BadgeKind::Ok;   state = "READY"; }
    ImGui::Text("%-6s", name);
    ImGui::SameLine();
    StatusBadge(bk, state);
    if (has && mf) {
      ImGui::SameLine(ImGui::GetContentRegionAvail().x - 80);
      ImGui::TextDisabled("%d frames", mf->frameCount);
    }
  };
  statusLine("Dark", calib_->hasDark(), calib_->darkMatches(k), calib_->dark());
  statusLine("Flat", calib_->hasFlat(), calib_->flatMatches(k), calib_->flat());

  ImGui::Dummy(ImVec2(0, 6));
  SectionHeader("Capture new master");
  ImGui::PushItemWidth(120);
  ImGui::InputInt("Frame count", &calibFrameCount_);
  if (calibFrameCount_ < 1)   calibFrameCount_ = 1;
  if (calibFrameCount_ > 500) calibFrameCount_ = 500;
  ImGui::PopItemWidth();
  ImGui::SameLine();
  HelpMarker(
      "More frames → lower noise in the master.\n"
      "Astro: 30–100   Microscopy: 10–20 typical");

  bool busy = calibJob_ && calibJob_->status() == MasterAcquisition::Status::Running;

  if (busy) {
    int p = calibJob_->progress();
    int t = calibJob_->total();
    char label[64];
    std::snprintf(label, sizeof(label), "%d / %d frames", p, t);
    ImGui::ProgressBar(t > 0 ? static_cast<float>(p) / t : 0.0f,
                       ImVec2(-1, 24), label);
    if (ImGui::Button("Cancel", ImVec2(-1, 0))) calibJob_->cancel();
  } else {
    ImGui::TextDisabled("DARK = lens/input physically covered");
    if (ImGui::Button("Capture master DARK", ImVec2(-1, 32))) {
      if (capture_) capture_->stop();
      calibJob_ = std::make_unique<MasterAcquisition>(
          *cur, MasterAcquisition::Kind::Dark, calibFrameCount_, k);
      calibJob_->start();
      appendLog("Capturing " + std::to_string(calibFrameCount_) +
                " dark frames (cover lens/input; no internal shutter).");
    }
    ImGui::Dummy(ImVec2(0, 4));
    ImGui::TextDisabled("FLAT = uniform white reference (Spectralon / paper)");
    if (ImGui::Button("Capture master FLAT", ImVec2(-1, 32))) {
      if (capture_) capture_->stop();
      calibJob_ = std::make_unique<MasterAcquisition>(
          *cur, MasterAcquisition::Kind::Flat, calibFrameCount_, k);
      calibJob_->start();
      appendLog("Capturing " + std::to_string(calibFrameCount_) +
                " flat frames (uniform white reference).");
    }
  }

  // Harvest finished job.
  if (calibJob_ && calibJob_->finished()) {
    if (calibJob_->status() == MasterAcquisition::Status::Done) {
      auto result = calibJob_->takeResult();
      auto kind = calibJob_->kind();
      if (kind == MasterAcquisition::Kind::Dark) {
        calib_->installDark(std::move(result));
        appendLog("Master DARK saved.");
      } else {
        calib_->installFlat(std::move(result));
        appendLog("Master FLAT saved.");
      }
    } else if (calibJob_->status() == MasterAcquisition::Status::Failed) {
      appendLog("Calibration job failed: " + calibJob_->errorMessage());
    }
    calibJob_.reset();
    // Resume live video if it was running before.
    if (videoMode_ && !(capture_ && capture_->running())) {
      capture_ = std::make_unique<CaptureThread>(*cur);
      capture_->start();
    }
  }

  ImGui::Dummy(ImVec2(0, 6));
  SectionHeader("Apply");
  ImGui::Checkbox("Apply calibration on live preview", &applyCalibPreview_);
  ImGui::SameLine();
  HelpMarker("Live calibration is useful for inspection, but it is CPU-heavy at full ROI. Quantitative SNAP/scan calibration is unaffected by this display toggle.");
  ImGui::Checkbox("Also save *_corrected.tif on Snap",
                  &saveCorrectedAlongside_);

  ImGui::Dummy(ImVec2(0, 4));
  if (ImGui::Button("Reload from disk", ImVec2(-1, 0))) {
    calib_->clearDark();
    calib_->clearFlat();
    tryAutoLoadCalibration();
  }
  if (ImGui::Button("Clear loaded masters", ImVec2(-1, 0))) {
    calib_->clearDark();
    calib_->clearFlat();
  }

  ImGui::End();
}

// ===========================================================================
//  HYPERSPECTRAL MODE PANELS
// ===========================================================================

void Gui::renderMonochromatorPanel() {
  ImGui::Begin("Monochromator");
  drainMonoJobLog();

  bool connected = mono_.connected();
  bool scanBusy = scanThread_ &&
                  (scanThread_->status() == ScanThread::Status::Running ||
                   scanThread_->status() == ScanThread::Status::Exporting);
  bool monoBusy = monoJobRunning_.load();
  bool busy = scanBusy || monoBusy;

  // ---- Connection ------------------------------------------------------
  StatusBadge(connected ? BadgeKind::Ok : BadgeKind::Idle,
              connected ? "Connected" : "Disconnected");

  ImGui::Spacing();
  std::vector<const char*> labels;
  labels.reserve(serialPorts_.size());
  for (const auto& s : serialPorts_) labels.push_back(s.c_str());
  ImGui::PushItemWidth(-1);
  if (!serialPorts_.empty()) {
    ImGui::Combo("##port", &selectedSerialPort_, labels.data(),
                 static_cast<int>(labels.size()));
  } else {
    ImGui::TextDisabled("No serial device found");
  }
  ImGui::PopItemWidth();

  ImGui::PushItemWidth(100);
  ImGui::InputInt("Baud", &serialBaud_);
  ImGui::PopItemWidth();
  ImGui::SameLine();
  if (ImGui::SmallButton("Refresh")) refreshSerialPorts();

  if (!connected) {
    if (ImGui::Button("Connect", ImVec2(-1, 28))) {
      try {
        std::string p =
            (selectedSerialPort_ >= 0 &&
             selectedSerialPort_ < static_cast<int>(serialPorts_.size()))
                ? serialPorts_[selectedSerialPort_]
                : serialCustomPath_;
        if (p.empty()) appendLog("No serial port selected.");
        else {
          mono_.connect(p, serialBaud_);
          appendLog("Monochromator connected: " + p);
        }
      } catch (const std::exception& e) {
        appendLog(std::string("Mono connect failed: ") + e.what());
      }
    }
  } else {
    ImGui::BeginDisabled(monoBusy);
    if (ImGui::Button("Disconnect", ImVec2(-1, 0))) {
      mono_.disconnect();
      appendLog("Monochromator disconnected.");
    }
    ImGui::EndDisabled();
  }

  ImGui::Spacing();
  ImGui::Separator();

  // ---- Position --------------------------------------------------------
  ImGui::Text("Position:");
  ImGui::SameLine();
  ImVec4 posCol = connected ? ImVec4(0.55f, 0.85f, 1.0f, 1.0f)
                            : ImVec4(0.55f, 0.55f, 0.58f, 1.0f);
  ImGui::TextColored(posCol, "%.2f nm", mono_.position());
  if (monoBusy) {
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(1.0f, 0.78f, 0.30f, 1.0f), "moving...");
  }

  ImGui::Spacing();
  ImGui::BeginDisabled(!connected || busy);

  // ---- GOTO ------------------------------------------------------------
  ImGui::PushItemWidth(140);
  ImGui::InputDouble("Target (nm)", &manualGotoNm_, 1.0, 10.0, "%.2f");
  ImGui::PopItemWidth();
  if (ImGui::Button("GOTO", ImVec2(-1, 28))) {
    const double target = manualGotoNm_;
    startMonoJob("GOTO " + std::to_string(target) + " nm",
                 [this, target] { return mono_.goTo(target); });
  }

  // ---- HOME ------------------------------------------------------------
  if (ImGui::Button("HOME", ImVec2(-1, 28))) {
    startMonoJob("HOME", [this] { return mono_.home(); });
  }

  // ---- Read status -----------------------------------------------------
  if (ImGui::Button("Read POS", ImVec2(-1, 0))) {
    if (auto p = mono_.queryPosition())
      appendLog("POS: " + std::to_string(*p) + " nm");
    else
      appendLog("POS: no reply");
  }
  if (ImGui::Button("Read STATUS", ImVec2(-1, 0))) {
    appendLog("STATUS: " + mono_.queryStatus());
  }

  ImGui::Spacing();
  ImGui::Separator();

  // ---- RESET (set current position) -----------------------------------
  ImGui::Text("Reset position:");
  ImGui::PushItemWidth(140);
  ImGui::InputDouble("##rstnm", &manualResetNm_, 1.0, 10.0, "%.2f");
  ImGui::PopItemWidth();
  ImGui::SameLine();
  if (ImGui::Button("RESET")) {
    const double target = manualResetNm_;
    startMonoJob("RESET to " + std::to_string(target) + " nm",
                 [this, target] { return mono_.reset(target); });
  }

  ImGui::EndDisabled();
  ImGui::End();
}

void Gui::renderScanSetupPanel() {
  ImGui::Begin("Scan Setup");
  Camera* cam = manager_.current();
  bool monoOk = mono_.connected();
  bool busy = scanThread_ &&
              (scanThread_->status() == ScanThread::Status::Running ||
               scanThread_->status() == ScanThread::Status::Exporting);
  bool camAlive = false;
  if (cam) {
    try {
      (void)cam->getRoi();
      camAlive = true;
    } catch (...) {
      camAlive = false;
    }
  }

  SectionHeader("System");
  StatusBadge(camAlive ? BadgeKind::Ok : BadgeKind::Err,
              cam ? cam->info().Name : "Camera not connected");
  StatusBadge(monoOk ? BadgeKind::Ok : BadgeKind::Err,
              monoOk ? "Monochromator port open" : "Monochromator not connected");
  if (monoOk) {
    ImGui::TextDisabled("Position: %.2f nm", mono_.position());
  }
  if (!cam) {
    ImGui::TextColored(ImVec4(1, 0.55f, 0.35f, 1),
                       "Connect the camera in Manual Mode first.");
    ImGui::End();
    return;
  }
  if (!camAlive) {
    ImGui::TextColored(ImVec4(1, 0.45f, 0.35f, 1),
                       "Camera handle is open but not responding. Reconnect in Manual Mode.");
  }
  ImGui::BeginDisabled(!monoOk || busy);
  if (ImGui::Button("Ping monochromator", ImVec2(-1, 24))) {
    if (auto p = mono_.queryPosition()) {
      lastMonoPingOk_ = true;
      lastMonoPingNm_ = *p;
      appendLog("Monochromator ping OK: " + std::to_string(*p) + " nm");
    } else {
      lastMonoPingOk_ = false;
      appendLog("Monochromator ping failed.");
    }
  }
  ImGui::EndDisabled();
  if (monoOk) {
    StatusBadge(lastMonoPingOk_ ? BadgeKind::Ok : BadgeKind::Warn,
                lastMonoPingOk_ ? "Last ping OK" : "Ping not verified");
    if (lastMonoPingOk_) {
      ImGui::TextDisabled("Last ping position: %.2f nm", lastMonoPingNm_);
    }
  }

  bool live = capture_ && capture_->running();
  ImGui::BeginDisabled(busy);
  if (ImGui::Button(live ? "Stop live preview" : "Start live preview",
                    ImVec2(-1, 28))) {
    try {
      if (live) {
        if (capture_) capture_->stop();
        videoMode_ = false;
      } else {
        if (!capture_) capture_ = std::make_unique<CaptureThread>(*cam);
        capture_->start();
        videoMode_ = true;
      }
    } catch (const std::exception& e) {
      appendLog(std::string("Live preview: ") + e.what());
    }
  }
  ImGui::EndDisabled();
  if (busy) {
    ImGui::TextDisabled("Preview is paused during scan capture.");
  }

  if (!scanCameraSettingsSynced_) {
    try { scanExposureUs_ = cam->getControl(ASI_EXPOSURE).value; } catch (...) {}
    try { scanGain_ = cam->getControl(ASI_GAIN).value; } catch (...) {}
    try { scanOffset_ = cam->getControl(ASI_OFFSET).value; } catch (...) {}
    scanCameraSettingsSynced_ = true;
  }

  SectionHeader("Measurement");
  ImGui::PushItemWidth(-1);
  ImGui::InputDouble("Start (nm)", &scanParams_.startNm, 1.0, 10.0, "%.2f");
  ImGui::InputDouble("End (nm)",   &scanParams_.endNm,   1.0, 10.0, "%.2f");
  ImGui::InputDouble("Step (nm)",  &scanParams_.stepNm,  0.1, 1.0,  "%.3f");
  if (scanParams_.stepNm < 0.001) scanParams_.stepNm = 0.001;
  ImGui::InputDouble("Dwell (s)",  &scanParams_.dwellSec, 0.05, 0.5, "%.2f");
  if (scanParams_.dwellSec < 0.0) scanParams_.dwellSec = 0.0;
  ImGui::PopItemWidth();
  ImGui::SameLine();
  HelpMarker(
      "Dwell is the settle time after each monochromator move before the camera exposure starts.\n"
      "Exposure/gain/offset are configured below and applied to the camera before scan.");

  SectionHeader("Camera settings");
  auto clampControl = [&](ASI_CONTROL_TYPE t, long& value) {
    for (const auto& d : cam->controls()) {
      if (d.type == t) {
        value = std::clamp(value, d.minValue, d.maxValue);
        return;
      }
    }
  };
  clampControl(ASI_EXPOSURE, scanExposureUs_);
  clampControl(ASI_GAIN, scanGain_);
  clampControl(ASI_OFFSET, scanOffset_);

  ImGui::BeginDisabled(busy);
  ImGui::PushItemWidth(-1);
  long long expVal = scanExposureUs_;
  long long expStep = 1000;
  long long expFast = 100000;
  if (ImGui::InputScalar("Exposure (us)", ImGuiDataType_S64, &expVal,
                         &expStep, &expFast, "%lld")) {
    scanExposureUs_ = static_cast<long>(std::max<long long>(1, expVal));
  }
  ImGui::TextDisabled("%s", formatExposure(scanExposureUs_).c_str());

  int gainVal = static_cast<int>(scanGain_);
  if (ImGui::InputInt("Gain", &gainVal)) {
    scanGain_ = gainVal;
  }
  int offsetVal = static_cast<int>(scanOffset_);
  if (ImGui::InputInt("Offset", &offsetVal)) {
    scanOffset_ = offsetVal;
  }
  ImGui::PopItemWidth();
  if (ImGui::Button("Apply camera settings", ImVec2(-1, 28))) {
    try {
      clampControl(ASI_EXPOSURE, scanExposureUs_);
      clampControl(ASI_GAIN, scanGain_);
      clampControl(ASI_OFFSET, scanOffset_);
      cam->setControl(ASI_EXPOSURE, scanExposureUs_, false);
      cam->setControl(ASI_GAIN, scanGain_, false);
      cam->setControl(ASI_OFFSET, scanOffset_, false);
      appendLog("Scan camera settings applied: exp=" +
                std::to_string(scanExposureUs_) + " us gain=" +
                std::to_string(scanGain_) + " offset=" +
                std::to_string(scanOffset_));
      tryAutoLoadCalibration();
    } catch (const std::exception& e) {
      appendLog(std::string("Scan camera settings: ") + e.what());
    }
  }
  ImGui::EndDisabled();
  HelpMarker(
      "These settings are written to the camera and used for every band in the scan.\n"
      "Auto exposure/gain should be off for quantitative hyperspectral measurement.");

  // Step count + estimate.
  int n = 0;
  double s = scanParams_.startNm, e = scanParams_.endNm;
  double st = std::abs(scanParams_.stepNm);
  if (st > 1e-9) {
    if (s <= e) for (double x = s; x <= e + 1e-9; x += st) ++n;
    else        for (double x = s; x >= e - 1e-9; x -= st) ++n;
  }

  double firstDist = std::abs(scanParams_.startNm - mono_.position());
  double totalMove = monoEstimateSeconds(firstDist) +
                     std::max(0, n - 1) * monoEstimateSeconds(st);
  double totalDwell = n * scanParams_.dwellSec;
  long expUs = scanExposureUs_;
  double totalExp = n * expUs / 1e6;
  double totalSec = totalMove + totalDwell + totalExp;

  RoiFormat roi{};
  bool gotRoi = false;
  try { roi = cam->getRoi(); gotRoi = true; } catch (...) {}
  long gain = scanGain_;
  long offset = scanOffset_;
  auto tempC = cam->temperatureCelsius();

  ImGui::Spacing();
  if (ImGui::BeginTable("##scanSummary", 2,
                        ImGuiTableFlags_BordersInnerH |
                        ImGuiTableFlags_SizingStretchProp)) {
    auto row = [](const char* k, const char* fmt, auto v) {
      ImGui::TableNextRow();
      ImGui::TableNextColumn();
      ImGui::TextColored(ImVec4(0.62f, 0.62f, 0.68f, 1), "%s", k);
      ImGui::TableNextColumn();
      ImGui::Text(fmt, v);
    };
    row("Bands", "%d", n);
    row("Exposure", "%ld us", expUs);
    row("Gain", "%ld", gain);
    row("Offset", "%ld", offset);
    if (tempC) row("Sensor temp", "%.1f C", *tempC);
    if (gotRoi) {
      char sz[48];
      std::snprintf(sz, sizeof(sz), "%dx%d  bin %d  %s",
                    roi.width, roi.height, roi.bin,
                    Camera::imageTypeName(roi.imageType));
      row("Frame", "%s", sz);
    }
    row("Move time", "%.1f s", totalMove);
    row("Dwell time", "%.1f s", totalDwell);
    row("Total est.", "%.1f min", totalSec / 60.0);
    ImGui::EndTable();
  }

  SectionHeader("Calibration");
  CalibKey k = currentKey();
  bool darkReady = calib_ && calib_->hasDark() && calib_->darkMatches(k);
  bool flatReady = calib_ && calib_->hasFlat() && calib_->flatMatches(k);
  StatusBadge(darkReady ? BadgeKind::Ok : BadgeKind::Warn,
              darkReady ? "Dark ready" : "Dark missing/mismatch");
  StatusBadge(flatReady ? BadgeKind::Ok : BadgeKind::Warn,
              flatReady ? "Flat ready" : "Flat missing/mismatch");
  ImGui::TextDisabled("Masters must match exposure, gain, offset, ROI, binning and format.");

	  SectionHeader("Output");
  ImGui::TextDisabled("Scan session output");
  ImGui::SameLine();
  HelpMarker("Each scan creates a timestamped session folder under the root. The measurement name is appended to that folder and written into metadata.");
	  char buf[256];
	  std::strncpy(buf, scanRootDir_.c_str(), sizeof(buf) - 1);
	  buf[sizeof(buf) - 1] = 0;
	  if (ImGui::InputText("Session root", buf, sizeof(buf))) {
	    scanRootDir_ = buf;
	    if (cube_) cube_->setSessionDir(scanRootDir_);
	  }
	  std::strncpy(buf, scanParams_.sampleName.c_str(), sizeof(buf) - 1);
	  buf[sizeof(buf) - 1] = 0;
	  if (ImGui::InputText("Measurement name", buf, sizeof(buf))) {
	    scanParams_.sampleName = buf;
	  }
  std::strncpy(buf, scanParams_.notes.c_str(), sizeof(buf) - 1);
  buf[sizeof(buf) - 1] = 0;
	  if (ImGui::InputText("Notes", buf, sizeof(buf))) {
	    scanParams_.notes = buf;
	  }
  ImGui::TextDisabled("Next scan: %s/<timestamp>_%s/",
                      scanRootDir_.c_str(),
                      scanParams_.sampleName.empty() ? "measurement" : scanParams_.sampleName.c_str());

  ImGui::Spacing();
  bool ready = camAlive && monoOk && n > 0 && !busy;
  ImGui::BeginDisabled(!ready);
  ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.18f, 0.50f, 0.82f, 1.0f));
  ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0.28f, 0.62f, 0.95f, 1.0f));
  if (ImGui::Button("START HYPERSPECTRAL SCAN", ImVec2(-1, 38))) {
    if (capture_) capture_->stop();
    videoMode_ = false;
    try {
      clampControl(ASI_EXPOSURE, scanExposureUs_);
      clampControl(ASI_GAIN, scanGain_);
      clampControl(ASI_OFFSET, scanOffset_);
      cam->setControl(ASI_EXPOSURE, scanExposureUs_, false);
      cam->setControl(ASI_GAIN, scanGain_, false);
      cam->setControl(ASI_OFFSET, scanOffset_, false);
    } catch (const std::exception& e) {
      appendLog(std::string("Scan camera settings: ") + e.what());
      ImGui::PopStyleColor(2);
      ImGui::EndDisabled();
      ImGui::End();
      return;
    }
    // Reset cube viewer state.
    cube_->clear();
    cube_->setSessionDir(scanRootDir_);
    viewerBandIdx_ = 0;
    cubeFollowLatest_ = true;
    cubeTextureDirty_ = true;
    lastCubeUploadBandIdx_ = -1;
    lastCubeUploadBandCount_ = 0;
    cubeDisplayBand_ = {};
    pixelX_ = pixelY_ = -1;
    spectrumX_.clear();
    spectrumY_.clear();
    scanThread_ = std::make_unique<ScanThread>(*cam, mono_, cube_,
                                               calib_.get(), scanParams_);
    scanThread_->start();
    appendLog("Scan started: " + std::to_string(scanParams_.startNm) +
	              " → " + std::to_string(scanParams_.endNm) + " step " +
	              std::to_string(scanParams_.stepNm));
	  }
  ImGui::PopStyleColor(2);
  ImGui::EndDisabled();
  if (!monoOk) {
    ImGui::TextColored(ImVec4(1, 0.6f, 0.2f, 1),
                       "Monochromator not connected.");
  } else if (!lastMonoPingOk_) {
    ImGui::TextColored(ImVec4(1, 0.75f, 0.25f, 1),
                       "Recommended: ping monochromator before scan.");
  }

  ImGui::End();
}

void Gui::renderScanProgressPanel() {
  ImGui::Begin("Scan Progress");
  if (!scanThread_) {
    ImGui::TextDisabled("No active scan.");
    ImGui::End();
    return;
  }
  auto st = scanThread_->status();
  int p = scanThread_->progress();
  int t = scanThread_->total();
  double frac = (t > 0) ? static_cast<float>(p) / t : 0.0f;
  char label[64];
  std::snprintf(label, sizeof(label), "%d / %d  (%.1f%%)", p, t, 100.0 * frac);
  ImGui::ProgressBar(static_cast<float>(frac), ImVec2(-1, 0), label);

  ImGui::Text("Current λ: %.2f nm", scanThread_->currentWavelength());
  const char* sname = "?";
  switch (st) {
    case ScanThread::Status::Idle:      sname = "Idle"; break;
    case ScanThread::Status::Running:   sname = "Running"; break;
    case ScanThread::Status::Exporting: sname = "Exporting ENVI"; break;
    case ScanThread::Status::Done:      sname = "Done"; break;
    case ScanThread::Status::Failed:    sname = "Failed"; break;
    case ScanThread::Status::Cancelled: sname = "Cancelled"; break;
  }
  ImGui::Text("Status:   %s", sname);
  if (st == ScanThread::Status::Failed) {
    ImGui::TextColored(ImVec4(1, 0.4f, 0.4f, 1), "%s",
                       scanThread_->errorMessage().c_str());
  }

  ImGui::Separator();
  if (st == ScanThread::Status::Running) {
    if (ImGui::Button("Cancel", ImVec2(-1, 28))) {
      scanThread_->cancel();
      appendLog("Scan cancel requested.");
    }
  } else if (st == ScanThread::Status::Exporting) {
    ImGui::TextDisabled("Writing cube_metadata.json and ENVI cube.dat/cube.hdr...");
  } else {
    if (ImGui::Button("Clear", ImVec2(-1, 0))) {
      scanThread_.reset();
    }
  }

  // If scan ended, log it once (use a static flag-ish trick: check thread_).
  if (st != ScanThread::Status::Running &&
      st != ScanThread::Status::Exporting &&
      st != ScanThread::Status::Idle) {
    static ScanThread::Status lastLogged = ScanThread::Status::Idle;
    if (lastLogged != st) {
      lastLogged = st;
      if (st == ScanThread::Status::Done)
        appendLog("Scan complete: " + std::to_string(p) + " bands.");
      else if (st == ScanThread::Status::Cancelled)
        appendLog("Scan cancelled at band " + std::to_string(p));
      else if (st == ScanThread::Status::Failed)
        appendLog("Scan failed: " + scanThread_->errorMessage());
    }
  }
  ImGui::End();
}

void Gui::rebuildCubeBandTexture() {
  if (cubeTex_) {
    glDeleteTextures(1, &cubeTex_);
    cubeTex_ = 0;
  }
}

void Gui::uploadBandToTexture(const CubeBand& b) {
  if (b.width != cubeTexW_ || b.height != cubeTexH_ || cubeTex_ == 0) {
    if (cubeTex_) glDeleteTextures(1, &cubeTex_);
    glGenTextures(1, &cubeTex_);
    glBindTexture(GL_TEXTURE_2D, cubeTex_);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, b.width, b.height, 0, GL_RED,
                 GL_UNSIGNED_BYTE, nullptr);
    const GLint swizzle[4] = {GL_RED, GL_RED, GL_RED, GL_ONE};
    glTexParameteriv(GL_TEXTURE_2D, GL_TEXTURE_SWIZZLE_RGBA, swizzle);
    cubeTexW_ = b.width;
    cubeTexH_ = b.height;
  }

  std::size_t n = static_cast<std::size_t>(b.width) * b.height;
  float lo = cubeStretchLo_;
  float hi = cubeStretchHi_;
  if (cubeAutoStretch_) {
    lo = std::numeric_limits<float>::max();
    hi = -lo;
    for (float v : b.data) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (hi <= lo) hi = lo + 1.0f;
    cubeStretchLo_ = lo;
    cubeStretchHi_ = hi;
  }
  float range = hi - lo;
  if (range <= 0) range = 1.0f;

  std::vector<std::uint8_t> g8(n);
  for (std::size_t i = 0; i < n; ++i) {
    float v = (b.data[i] - lo) / range * 255.0f;
    if (v < 0) v = 0;
    if (v > 255) v = 255;
    g8[i] = static_cast<std::uint8_t>(v);
  }
  glBindTexture(GL_TEXTURE_2D, cubeTex_);
  glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
  glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, b.width, b.height, GL_RED,
                  GL_UNSIGNED_BYTE, g8.data());
}

void Gui::recomputeSpectrumPlot() {
  spectrumX_.clear();
  spectrumY_.clear();
  if (!cube_ || pixelX_ < 0 || pixelY_ < 0) return;
  cube_->spectrumAt(pixelX_, pixelY_, spectrumX_, spectrumY_);
}

bool Gui::renderWavelengthSlider(const std::vector<double>& wavelengths) {
  if (wavelengths.empty()) return false;
  if (viewerBandIdx_ < 0) viewerBandIdx_ = 0;
  if (viewerBandIdx_ >= static_cast<int>(wavelengths.size())) {
    viewerBandIdx_ = static_cast<int>(wavelengths.size()) - 1;
  }

  ImVec2 pos = ImGui::GetCursorScreenPos();
  float w = ImGui::GetContentRegionAvail().x;
  float h = 30.0f;
  ImDrawList* dl = ImGui::GetWindowDrawList();
  int segments = std::max(1, static_cast<int>(wavelengths.size()) - 1);
  for (int i = 0; i < segments; ++i) {
    float x0 = pos.x + w * i / segments;
    float x1 = pos.x + w * (i + 1) / segments;
    double nm = wavelengths[std::min(i, static_cast<int>(wavelengths.size()) - 1)];
    dl->AddRectFilled(ImVec2(x0, pos.y), ImVec2(x1 + 1.0f, pos.y + h),
                      wavelengthColor(nm));
  }
  dl->AddRect(pos, ImVec2(pos.x + w, pos.y + h), IM_COL32(230, 230, 235, 180));

  float knobX = pos.x;
  if (wavelengths.size() > 1) {
    knobX += w * viewerBandIdx_ / (static_cast<float>(wavelengths.size()) - 1.0f);
  }
  dl->AddLine(ImVec2(knobX, pos.y - 3.0f), ImVec2(knobX, pos.y + h + 3.0f),
              IM_COL32(255, 255, 255, 255), 3.0f);
  dl->AddCircleFilled(ImVec2(knobX, pos.y + h * 0.5f), 5.0f,
                      IM_COL32(20, 20, 24, 255), 16);
  dl->AddCircle(ImVec2(knobX, pos.y + h * 0.5f), 5.0f,
                IM_COL32(255, 255, 255, 255), 16, 1.5f);

  ImGui::InvisibleButton("##wavelength_slider", ImVec2(w, h + 4.0f));
  bool changed = false;
  if (ImGui::IsItemActive() || ImGui::IsItemClicked()) {
    ImVec2 mp = ImGui::GetMousePos();
    float u = std::clamp((mp.x - pos.x) / std::max(1.0f, w), 0.0f, 1.0f);
    int next = static_cast<int>(std::round(u * (wavelengths.size() - 1)));
    next = std::clamp(next, 0, static_cast<int>(wavelengths.size()) - 1);
    if (next != viewerBandIdx_) {
      viewerBandIdx_ = next;
      cubeAutoStretch_ = true;
      changed = true;
    }
  }

  ImGui::Text("%.2f nm", wavelengths[viewerBandIdx_]);
  ImGui::SameLine();
  ImGui::TextDisabled("Band %d / %zu", viewerBandIdx_ + 1, wavelengths.size());
  return changed;
}

void Gui::renderCubeViewerPanel() {
  ImGui::Begin("Cube Viewer");

  // Session selector.
  if (ImGui::Button("Rescan disk")) {
    sessionsList_ = listCubeSessions(scanRootDir_);
    selectedSessionIdx_ = -1;
  }
  ImGui::SameLine();
  ImGui::Text("(%zu sessions in %s)", sessionsList_.size(), scanRootDir_.c_str());

  std::vector<const char*> sessLabels;
  std::vector<std::string> shortNames;
  shortNames.reserve(sessionsList_.size());
  for (const auto& s : sessionsList_) {
    shortNames.push_back(std::filesystem::path(s).filename().string());
  }
  for (auto& s : shortNames) sessLabels.push_back(s.c_str());
  ImGui::PushItemWidth(-1);
  if (!sessLabels.empty()) {
    if (ImGui::Combo("##sess", &selectedSessionIdx_, sessLabels.data(),
                     static_cast<int>(sessLabels.size()))) {
      try {
        cube_->loadFromDir(sessionsList_[selectedSessionIdx_]);
        viewerBandIdx_ = 0;
        pixelX_ = pixelY_ = -1;
        spectrumX_.clear();
        spectrumY_.clear();
        cubeAutoStretch_ = true;
        cubeTextureDirty_ = true;
        lastCubeUploadBandIdx_ = -1;
        lastCubeUploadBandCount_ = 0;
        cubeDisplayBand_ = {};
        appendLog("Loaded cube: " + sessionsList_[selectedSessionIdx_]);
      } catch (const std::exception& e) {
        appendLog(std::string("Cube load failed: ") + e.what());
      }
    }
  }
  ImGui::PopItemWidth();

  std::size_t nb = cube_ ? cube_->bandCount() : 0;
  if (nb == 0) {
    ImGui::TextDisabled("No bands loaded. Start a scan or pick a session.");
    ImGui::End();
    return;
  }

  const bool scanActive =
      scanThread_ && (scanThread_->status() == ScanThread::Status::Running ||
                      scanThread_->status() == ScanThread::Status::Exporting);
  if (scanActive && cubeFollowLatest_ && nb != lastCubeUploadBandCount_) {
    viewerBandIdx_ = static_cast<int>(nb) - 1;
    cubeTextureDirty_ = true;
  }

  if (viewerBandIdx_ >= static_cast<int>(nb)) viewerBandIdx_ = static_cast<int>(nb) - 1;
  if (viewerBandIdx_ < 0) viewerBandIdx_ = 0;

  std::vector<double> wavelengths = cube_->wavelengths();
  SectionHeader("Wavelength");
  if (renderWavelengthSlider(wavelengths)) {
    cubeFollowLatest_ = false;
    cubeTextureDirty_ = true;
  }

  if (cubeTextureDirty_ || lastCubeUploadBandIdx_ != viewerBandIdx_ ||
      lastCubeUploadBandCount_ != nb) {
    if (!cube_->getBand(viewerBandIdx_, cubeDisplayBand_)) {
      ImGui::End();
      return;
    }
    uploadBandToTexture(cubeDisplayBand_);
    cubeTextureDirty_ = false;
    lastCubeUploadBandIdx_ = viewerBandIdx_;
    lastCubeUploadBandCount_ = nb;
  }

  ImGui::Text("λ = %.2f nm", cubeDisplayBand_.wavelength_nm);
  ImGui::SameLine();
  ImGui::TextDisabled("%s", cubeDisplayBand_.filename.empty() ? "live cube" : cubeDisplayBand_.filename.c_str());
  if (ImGui::Checkbox("Auto-stretch", &cubeAutoStretch_)) {
    cubeTextureDirty_ = true;
  }
  ImGui::SameLine();
  ImGui::Text("range = [%.1f, %.1f]", cubeStretchLo_, cubeStretchHi_);
  if (scanActive) {
    if (ImGui::Checkbox("Follow latest band", &cubeFollowLatest_)) {
      if (cubeFollowLatest_) {
        viewerBandIdx_ = static_cast<int>(nb) - 1;
        cubeTextureDirty_ = true;
      }
    }
  }

  // Two-column layout: image left, spectrum right.
  ImVec2 avail = ImGui::GetContentRegionAvail();
  float imgW = avail.x * 0.55f;
  float specW = avail.x - imgW - 8.0f;

  ImGui::BeginChild("##cubeImg", ImVec2(imgW, avail.y));
  if (cubeTex_) {
    float aspect = static_cast<float>(cubeTexW_) / std::max(1, cubeTexH_);
    ImVec2 ca = ImGui::GetContentRegionAvail();
    float w = ca.x;
    float h = w / aspect;
    if (h > ca.y) { h = ca.y; w = h * aspect; }
    ImVec2 imgPos = ImGui::GetCursorScreenPos();
    ImGui::Image(static_cast<ImTextureID>(cubeTex_), ImVec2(w, h));
    if (ImGui::IsItemHovered() && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
      ImVec2 mp = ImGui::GetMousePos();
      float u = (mp.x - imgPos.x) / std::max(1.0f, w);
      float vv = (mp.y - imgPos.y) / std::max(1.0f, h);
      if (u >= 0 && u <= 1 && vv >= 0 && vv <= 1) {
        pixelX_ = std::clamp(static_cast<int>(u * cubeTexW_), 0, cubeTexW_ - 1);
        pixelY_ = std::clamp(static_cast<int>(vv * cubeTexH_), 0, cubeTexH_ - 1);
        recomputeSpectrumPlot();
      }
    }
  }
  if (pixelX_ >= 0)
    ImGui::Text("Pixel: (%d, %d)", pixelX_, pixelY_);
  ImGui::EndChild();

  ImGui::SameLine();

  ImGui::BeginChild("##cubeSpec", ImVec2(specW, avail.y), true);
  ImGui::TextUnformatted("Spectrum at clicked pixel");
  if (pixelX_ < 0) {
    ImGui::TextDisabled("Click a pixel in the image.");
  } else if (!spectrumX_.empty()) {
    if (ImPlot::BeginPlot("##spec", ImVec2(-1, -1), ImPlotFlags_NoTitle)) {
      ImPlot::SetupAxes("Wavelength (nm)", "Intensity",
                        ImPlotAxisFlags_AutoFit, ImPlotAxisFlags_AutoFit);
      ImPlot::PlotLine("I(λ)", spectrumX_.data(), spectrumY_.data(),
                       static_cast<int>(spectrumX_.size()));
      ImPlot::EndPlot();
    }
  }
  ImGui::EndChild();

  ImGui::End();
}

void Gui::renderStatusBar() {
  ImGui::Begin("Log");
  if (ImGui::Button("Clear")) log_.clear();
  ImGui::SameLine();
  ImGui::Checkbox("Auto-scroll", &autoScrollLog_);
  ImGui::Separator();
  ImGui::BeginChild("logscroll");
  for (const auto& line : log_) ImGui::TextUnformatted(line.c_str());
  if (autoScrollLog_ && ImGui::GetScrollY() >= ImGui::GetScrollMaxY()) {
    ImGui::SetScrollHereY(1.0f);
  }
  ImGui::EndChild();
  ImGui::End();
}

}  // namespace mc
