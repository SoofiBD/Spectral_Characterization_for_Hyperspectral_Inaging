#pragma once

#include "calibration.h"
#include "camera.h"
#include "capture_thread.h"
#include "monochromator.h"

#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace mc {

// In-memory viewer representation of one hyperspectral band. This copy is
// box-averaged to a bounded size for interactive browsing and spectra; saved
// TIFF/ENVI outputs retain their full acquisition resolution.
struct CubeBand {
  double wavelength_nm = 0.0;
  int width = 0;
  int height = 0;
  std::vector<float> data;
  std::string filename;   // relative to the session directory
};

struct CubeMetadata {
  std::string sessionDir;
  std::string startTimestamp;     // ISO-ish "YYYY-MM-DDTHH:MM:SS"
  std::string cameraName;
  std::string cameraSerial;
  std::string sdkVersion;
  int width = 0;
  int height = 0;
  int bin = 1;
  std::string imgType;             // "RAW16" / "RAW8" / ...
  long exposureUs = 0;
  long gain = 0;
  long offset = 0;
  long gamma = 0;
  long usbBandwidth = 0;
  double pixelSizeUm = 0.0;
  int bitDepth = 0;
  bool darkApplied = false;
  bool flatApplied = false;
  double dwellSec = 0.0;
  std::string sampleName;
  std::string notes;
};

struct ScanParams {
  double startNm = 400.0;
  double endNm = 800.0;
  double stepNm = 5.0;
  double dwellSec = 0.2;            // settle after move, before capture
  std::string sampleName = "sample";
  std::string notes;
};

// In-memory representation of one acquisition. Bands are added one at a time
// by the scan thread; the GUI reads through a shared pointer.
class CubeSession {
public:
  CubeSession();

  void setSessionDir(const std::string& dir) { sessionDir_ = dir; }
  const std::string& sessionDir() const noexcept { return sessionDir_; }

  // Thread-safe append from scan thread.
  void appendBand(CubeBand&& b);
  // Snapshot the band count.
  std::size_t bandCount() const;
  // Copy band[i] out (returns false if out of range).
  bool getBand(std::size_t i, CubeBand& out) const;
  // Lightweight wavelength snapshot for UI sliders.
  std::vector<double> wavelengths() const;
  // Min/max wavelength currently loaded.
  std::pair<double, double> wavelengthRange() const;
  // Get spectrum at pixel (x, y) — fills xs (nm) and ys (intensity).
  void spectrumAt(int x, int y, std::vector<double>& xs,
                  std::vector<double>& ys) const;

  void setMetadata(const CubeMetadata& m);
  CubeMetadata metadata() const;

	  // Persist metadata.json into sessionDir_.
	  void saveMetadata() const;
	  // Persist standard ENVI cube export: cube.dat + cube.hdr (float32 BSQ).
	  void saveEnvi() const;

	  // Load a session from a folder (reads metadata.json + all band TIFFs).
  void loadFromDir(const std::string& dir);

  // Discard all loaded bands.
  void clear();

private:
  mutable std::mutex mutex_;
  std::vector<CubeBand> bands_;
  CubeMetadata meta_;
  std::string sessionDir_;
};

// Background scan worker. Drives both monochromator and camera.
class ScanThread {
public:
  enum class Status { Idle, Running, Exporting, Done, Failed, Cancelled };

  ScanThread(Camera& camera, Monochromator& mono,
             std::shared_ptr<CubeSession> session,
             CalibrationManager* calib, ScanParams params);
  ~ScanThread();

  void start();
  void cancel();
  Status status() const noexcept { return status_.load(); }
  int progress() const noexcept { return progress_.load(); }
  int total() const noexcept { return total_; }
  double currentWavelength() const noexcept { return currentWavelength_.load(); }
  std::string errorMessage() const;

private:
  void run();

  Camera& camera_;
  Monochromator& mono_;
  std::shared_ptr<CubeSession> session_;
  CalibrationManager* calib_;
  ScanParams params_;
  std::thread thread_;
  int total_ = 0;
  std::atomic<int> progress_{0};
  std::atomic<Status> status_{Status::Idle};
  std::atomic<bool> shouldStop_{false};
  std::atomic<double> currentWavelength_{0.0};
  mutable std::mutex errorMutex_;
  std::string error_;
};

// Helper: list cube session directories under a parent directory.
std::vector<std::string> listCubeSessions(const std::string& parentDir);

}  // namespace mc
