#pragma once

#include "camera.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <thread>
#include <vector>

namespace mc {

// Stores one captured frame plus the metadata needed to render and save it.
struct Frame {
  std::vector<std::uint8_t> data;
  int width = 0;
  int height = 0;
  int bin = 1;
  ASI_IMG_TYPE type = ASI_IMG_RAW8;
  std::uint64_t sequence = 0;
  std::chrono::system_clock::time_point timestamp;
};

// Background video-capture worker. Owns a single output Frame slot that the
// GUI thread can copy under a mutex.
class CaptureThread {
public:
  explicit CaptureThread(Camera& camera);
  ~CaptureThread();

  CaptureThread(const CaptureThread&) = delete;
  CaptureThread& operator=(const CaptureThread&) = delete;

  void start();
  void stop();
  bool running() const noexcept { return running_.load(); }

  // Copy the most recent frame into out, returning true if a new frame was
  // delivered since the last call.
  bool latest(Frame& out);

  // Stats — atomic, safe to read from GUI thread.
  std::uint64_t totalFrames() const noexcept { return totalFrames_.load(); }
  std::uint64_t timeoutCount() const noexcept { return timeouts_.load(); }
  double fps() const noexcept { return fpsEstimate_.load(); }
  std::string lastError() const;

private:
  void run();

  Camera& camera_;
  std::thread thread_;
  std::atomic<bool> running_{false};
  std::atomic<bool> shouldStop_{false};

  std::mutex frameMutex_;
  Frame frame_;
  std::uint64_t sequence_ = 0;
  std::uint64_t consumedSequence_ = 0;

  std::atomic<std::uint64_t> totalFrames_{0};
  std::atomic<std::uint64_t> timeouts_{0};
  std::atomic<double> fpsEstimate_{0.0};

  mutable std::mutex errorMutex_;
  std::string lastError_;
};

}  // namespace mc
