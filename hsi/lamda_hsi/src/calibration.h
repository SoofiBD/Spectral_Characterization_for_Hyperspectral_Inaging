#pragma once

#include "camera.h"
#include "capture_thread.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace mc {

// Identifies a set of acquisition parameters that a calibration master must
// match exactly. Two masters are interchangeable iff their keys are equal.
struct CalibKey {
  int width = 0;       // ROI width  (post-binning, i.e. the data width)
  int height = 0;      // ROI height (post-binning)
  int bin = 1;
  ASI_IMG_TYPE imgType = ASI_IMG_RAW16;
  long exposureUs = 0;
  long gain = 0;
  long offset = 0;

  bool operator==(const CalibKey& o) const {
    return width == o.width && height == o.height && bin == o.bin &&
           imgType == o.imgType && exposureUs == o.exposureUs &&
           gain == o.gain && offset == o.offset;
  }
  bool operator!=(const CalibKey& o) const { return !(*this == o); }

  // Used as a filename component, e.g. "exp50000_gain100_off10_bin1_2048x2048_RAW16".
  std::string toFilenameStem() const;
  static std::optional<CalibKey> fromFilename(const std::string& filename);
};

// A master frame is a per-pixel float32 image obtained by averaging N raw
// frames. Stored on disk as 32-bit float TIFF.
struct MasterFrame {
  CalibKey key;
  std::vector<float> data;  // size = key.width * key.height
  int frameCount = 0;
  std::string sourcePath;    // where it lives on disk, if loaded/saved
};

// Result of applying calibration to a raw frame.
struct AppliedFrame {
  std::vector<float> data;  // post-calibration float values in original units
  int width = 0;
  int height = 0;
};

// Long-running async acquisition (capture N frames + average). The capture
// thread is paused while this runs; we use ASIStartExposure for each shot so
// the timing is precise even for long exposures.
class MasterAcquisition {
public:
  enum class Kind { Dark, Flat };
  enum class Status { Idle, Running, Done, Failed };

  MasterAcquisition(Camera& camera, Kind kind, int frameCount,
                    const CalibKey& key);
  ~MasterAcquisition();

  void start();        // launches the worker thread
  void cancel();       // signals stop; join in dtor
  bool finished() const noexcept { return status_.load() != Status::Running &&
                                          status_.load() != Status::Idle; }
  Status status() const noexcept { return status_.load(); }
  int progress() const noexcept { return progress_.load(); }
  int total() const noexcept { return total_; }
  std::string errorMessage() const;
  std::unique_ptr<MasterFrame> takeResult();   // move out after finished()
  Kind kind() const noexcept { return kind_; }

private:
  void run();

  Camera& camera_;
  Kind kind_;
  int total_;
  CalibKey key_;
  std::thread thread_;
  std::atomic<Status> status_{Status::Idle};
  std::atomic<int> progress_{0};
  std::atomic<bool> shouldStop_{false};
  std::unique_ptr<MasterFrame> result_;
  mutable std::mutex errorMutex_;
  std::string error_;
};

// Holds the currently-loaded master dark and master flat, applies them on
// demand and persists to/from disk.
class CalibrationManager {
public:
  explicit CalibrationManager(std::string calibDir);

  void setCalibDir(std::string dir) { dir_ = std::move(dir); }
  const std::string& calibDir() const noexcept { return dir_; }

  // Whether the named master is currently loaded.
  bool hasDark() const noexcept { return dark_ != nullptr; }
  bool hasFlat() const noexcept { return flat_ != nullptr; }
  const MasterFrame* dark() const noexcept { return dark_.get(); }
  const MasterFrame* flat() const noexcept { return flat_.get(); }

  // True if loaded masters match the given key. UI uses this to colour-code
  // the calibration status indicator.
  bool darkMatches(const CalibKey& k) const;
  bool flatMatches(const CalibKey& k) const;

  // Try to auto-load matching masters from disk for the given key.
  // Returns the number of masters loaded (0, 1, or 2).
  int autoLoadFor(const CalibKey& k);

  // Replace the current dark/flat (taken from a finished MasterAcquisition).
  // The master is saved to calibDir() as float32 TIFF.
  void installDark(std::unique_ptr<MasterFrame> m);
  void installFlat(std::unique_ptr<MasterFrame> m);

  void clearDark() { dark_.reset(); }
  void clearFlat() { flat_.reset(); }

  // Apply the loaded masters to a raw frame.
  // Result floats are in the same scale as the raw frame:
  //   corrected = (raw - dark) / (flat - dark) * mean(flat - dark)
  // If dark missing: skip subtraction.
  // If flat missing: just dark subtraction.
  AppliedFrame apply(const Frame& raw) const;

  // Same, but write the result back into a uint16 buffer suitable for the
  // existing preview/save path (clipped to 0..65535).
  std::vector<std::uint16_t> applyU16(const Frame& raw) const;

  static CalibKey keyFromFrame(const Frame& f, long expUs, long gain, long off);

private:
  std::string buildFilename(const std::string& kindPrefix,
                            const CalibKey& k) const;
  std::optional<std::string> findMatchingMaster(const std::string& kindPrefix,
                                                const CalibKey& k) const;
  std::unique_ptr<MasterFrame> loadMaster(const std::string& path) const;
  void saveMaster(const MasterFrame& m, const std::string& path) const;

  std::string dir_;
  std::unique_ptr<MasterFrame> dark_;
  std::unique_ptr<MasterFrame> flat_;
};

}  // namespace mc
