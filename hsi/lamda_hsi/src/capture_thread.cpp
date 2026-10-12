#include "capture_thread.h"

#include <algorithm>

namespace mc {

CaptureThread::CaptureThread(Camera& camera) : camera_(camera) {}

CaptureThread::~CaptureThread() { stop(); }

void CaptureThread::start() {
  if (running_.load()) return;
  shouldStop_.store(false);
  running_.store(true);
  thread_ = std::thread(&CaptureThread::run, this);
}

void CaptureThread::stop() {
  if (!running_.load()) return;
  shouldStop_.store(true);
  if (thread_.joinable()) thread_.join();
  running_.store(false);
}

bool CaptureThread::latest(Frame& out) {
  std::lock_guard<std::mutex> lock(frameMutex_);
  if (frame_.sequence == 0 || frame_.sequence == consumedSequence_) return false;
  out = frame_;
  consumedSequence_ = frame_.sequence;
  return true;
}

std::string CaptureThread::lastError() const {
  std::lock_guard<std::mutex> lock(errorMutex_);
  return lastError_;
}

void CaptureThread::run() {
  try {
    camera_.startVideoCapture();
  } catch (const std::exception& e) {
    std::lock_guard<std::mutex> lock(errorMutex_);
    lastError_ = std::string("startVideoCapture: ") + e.what();
    running_.store(false);
    return;
  }

  std::vector<std::uint8_t> buffer;
  auto fpsAnchor = std::chrono::steady_clock::now();
  auto nextFrameTime = std::chrono::steady_clock::now();
  std::uint64_t fpsFrames = 0;

  while (!shouldStop_.load()) {
    RoiFormat roi;
    std::size_t needed;
    try {
      roi = camera_.getRoi();
      needed = static_cast<std::size_t>(roi.width) * roi.height *
               Camera::bytesPerPixel(roi.imageType);
    } catch (const std::exception& e) {
      std::lock_guard<std::mutex> lock(errorMutex_);
      lastError_ = std::string("getRoi: ") + e.what();
      break;
    }
    if (buffer.size() != needed) buffer.resize(needed);

    // Pick a timeout that accommodates the current exposure setting.
    long expUs = 100000;
    try {
      expUs = camera_.getControl(ASI_EXPOSURE).value;
    } catch (...) {
    }
    int timeoutMs = static_cast<int>((expUs * 2) / 1000) + 500;

    bool got = false;
    try {
      got = camera_.getVideoData(buffer.data(), buffer.size(), timeoutMs);
    } catch (const std::exception& e) {
      std::lock_guard<std::mutex> lock(errorMutex_);
      lastError_ = std::string("getVideoData: ") + e.what();
      break;
    }

    if (!got) {
      timeouts_.fetch_add(1);
      continue;
    }

    {
      std::lock_guard<std::mutex> lock(frameMutex_);
      frame_.data = buffer;
      frame_.width = roi.width;
      frame_.height = roi.height;
      frame_.bin = roi.bin;
      frame_.type = roi.imageType;
      frame_.sequence = ++sequence_;
      frame_.timestamp = std::chrono::system_clock::now();
    }
    totalFrames_.fetch_add(1);
    ++fpsFrames;

    auto now = std::chrono::steady_clock::now();
    auto dt = std::chrono::duration<double>(now - fpsAnchor).count();
    if (dt >= 1.0) {
      fpsEstimate_.store(fpsFrames / dt);
      fpsFrames = 0;
      fpsAnchor = now;
    }

    // Live preview is a display stream, not the quantitative acquisition path.
    // Cap worker throughput so short exposures do not flood the UI with full
    // RAW frame copies. Quantitative SNAP/scan acquisition uses single
    // exposures and is not affected by this preview-only limit.
    nextFrameTime = std::max(nextFrameTime + std::chrono::milliseconds(83),
                             std::chrono::steady_clock::now());
    std::this_thread::sleep_until(nextFrameTime);
  }

  try {
    camera_.stopVideoCapture();
  } catch (...) {
  }
  running_.store(false);
}

}  // namespace mc
