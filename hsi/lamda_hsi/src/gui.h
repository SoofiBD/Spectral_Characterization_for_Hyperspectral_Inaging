#pragma once

#include "calibration.h"
#include "camera.h"
#include "capture_thread.h"
#include "cube.h"
#include "monochromator.h"

#include <array>
#include <atomic>
#include <chrono>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

struct GLFWwindow;

namespace mc {

enum class AppMode { Manual, Hyperspectral };

class Gui {
public:
  Gui();
  ~Gui();

  // Render one ImGui frame. `window` is needed only for display-size queries.
  void draw(GLFWwindow* window);

  // True when something is actively producing frames or running in the
  // background (live capture, scan, calibration job). The main loop polls
  // events continuously when this is true, and blocks otherwise to keep
  // idle CPU/memory pressure at zero.
  bool needsContinuousRedraw() const;

private:
  // ---- Hyperspectral mode panels ----------------------------------------
  void renderMonochromatorPanel();
  void renderScanSetupPanel();
  void renderScanProgressPanel();
  void renderCubeViewerPanel();
  bool renderWavelengthSlider(const std::vector<double>& wavelengths);
  void rebuildCubeBandTexture();
  void uploadBandToTexture(const CubeBand& b);
  void recomputeSpectrumPlot();
  void refreshSerialPorts();
  // ---- Camera lifecycle -------------------------------------------------
  void renderTopMenu();
  void renderDevicePanel();
  void renderControlsPanel();
  void renderRoiPanel();
  void renderCapturePanel();
  void renderCalibrationPanel();
  void renderPreview();
  void renderHistogram();
  void renderStatusBar();

  void openSelectedCamera();
  void closeCamera();
  void rebuildPreviewTexture(int w, int h);
  void uploadFrameToTexture(const Frame& f);
  void computeHistogram(const Frame& f);
  void doSnap();
  void appendLog(const std::string& msg);
  CalibKey currentKey() const;
  void tryAutoLoadCalibration();
  void startMonoJob(std::string label, std::function<MoveResult()> job);
  void drainMonoJobLog();

  // ---- State -------------------------------------------------------------
  CameraManager manager_;
  std::unique_ptr<CaptureThread> capture_;

  int selectedDevice_ = 0;
  bool videoMode_ = true;

  // Preview texture (OpenGL).
  unsigned int previewTex_ = 0;
  int texW_ = 0;
  int texH_ = 0;
  Frame previewFrame_;
  bool hasFrame_ = false;
  int previewMaxFps_ = 12;
  int previewScale_ = 2;
  std::chrono::steady_clock::time_point lastPreviewPull_{};
  std::uint64_t histogramFrameCounter_ = 0;

  // ROI editing state.
  int roiWidth_ = 0;
  int roiHeight_ = 0;
  int roiX_ = 0;
  int roiY_ = 0;
  int roiBin_ = 1;
  int roiImgType_ = 0;  // index into supportedImgTypes_
  std::vector<ASI_IMG_TYPE> supportedImgTypes_;

  // Histogram. 1024 bins for RAW16 (6-bit downsample); 256 for 8-bit.
  std::vector<float> histogram_;
  std::vector<float> histogramAxis_;    // bin centre in pixel-value units
  float histogramMax_ = 1.0f;
  bool histogramLogY_ = true;
  bool autoStretch_ = true;
  // Frame statistics for the stats sidebar.
  double statMean_ = 0;
  double statStd_ = 0;
  int statMin_ = 0;
  int statMax_ = 0;
  int statMedian_ = 0;
  double satFracHigh_ = 0;  // % of pixels at max value
  double satFracLow_ = 0;   // % of pixels at 0

  // Display contrast (16-bit).
  int blackPoint_ = 0;
  int whitePoint_ = 65535;

  // Save settings.
  std::string saveDir_ = "captures";
  std::string savePrefix_ = "img";
  bool saveTiff_ = true;
  bool saveFits_ = false;
  bool savePng_ = false;
  bool saveAsDark_ = false;

  // Calibration.
  std::unique_ptr<CalibrationManager> calib_;
  std::unique_ptr<MasterAcquisition> calibJob_;
  int calibFrameCount_ = 20;
  bool applyCalibPreview_ = false;
  bool saveCorrectedAlongside_ = false;

  // Snap mode state.
  bool snapping_ = false;

  // Log buffer.
  std::vector<std::string> log_;
  bool autoScrollLog_ = true;

  // Pending layout rebuild after mode switch (always rebuild from scratch).
  bool pendingLayoutBuild_ = true;

  // ---- Mode + Hyperspectral state ---------------------------------------
  AppMode mode_ = AppMode::Manual;
  Monochromator mono_;
  std::vector<std::string> serialPorts_;
  int selectedSerialPort_ = -1;
  int serialBaud_ = 9600;
  std::string serialCustomPath_;     // optional manual override
  double manualGotoNm_ = 500.0;
  double manualResetNm_ = 0.0;
  std::thread monoJob_;
  std::atomic<bool> monoJobRunning_{false};
  std::mutex monoJobMutex_;
  std::vector<std::string> monoJobLog_;

	  // Scan configuration.
	  ScanParams scanParams_;
	  std::string scanRootDir_ = "captures/cubes";
	  std::string scanSampleName_ = "sample";
	  long scanExposureUs_ = 50000;
	  long scanGain_ = 100;
	  long scanOffset_ = 0;
	  bool scanCameraSettingsSynced_ = false;

  // Live cube.
  std::shared_ptr<CubeSession> cube_;
	  std::unique_ptr<ScanThread> scanThread_;
	  bool lastMonoPingOk_ = false;
	  double lastMonoPingNm_ = 0.0;

	  // Cube viewer state.
  int viewerBandIdx_ = 0;
  unsigned int cubeTex_ = 0;
  int cubeTexW_ = 0;
  int cubeTexH_ = 0;
  bool cubeAutoStretch_ = true;
  bool cubeFollowLatest_ = true;
  bool cubeTextureDirty_ = true;
  int lastCubeUploadBandIdx_ = -1;
  std::size_t lastCubeUploadBandCount_ = 0;
  CubeBand cubeDisplayBand_;
  float cubeStretchLo_ = 0.0f;
  float cubeStretchHi_ = 65535.0f;
  int pixelX_ = -1;
  int pixelY_ = -1;
  std::vector<double> spectrumX_;
  std::vector<double> spectrumY_;
  std::vector<std::string> sessionsList_;
  int selectedSessionIdx_ = -1;
};

}  // namespace mc
