#include "calibration.h"

#include "image_writer.h"

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <numeric>
#include <regex>
#include <sstream>
#include <stdexcept>

namespace mc {

namespace fs = std::filesystem;

namespace {
const char* imgTypeTag(ASI_IMG_TYPE t) {
  switch (t) {
    case ASI_IMG_RAW8:  return "RAW8";
    case ASI_IMG_RAW16: return "RAW16";
    case ASI_IMG_Y8:    return "Y8";
    case ASI_IMG_RGB24: return "RGB24";
    default:            return "?";
  }
}
ASI_IMG_TYPE imgTypeFromTag(const std::string& s) {
  if (s == "RAW16") return ASI_IMG_RAW16;
  if (s == "RAW8")  return ASI_IMG_RAW8;
  if (s == "Y8")    return ASI_IMG_Y8;
  if (s == "RGB24") return ASI_IMG_RGB24;
  return ASI_IMG_RAW16;
}
}  // namespace

// ---------------------------------------------------------------------------
// CalibKey
// ---------------------------------------------------------------------------
std::string CalibKey::toFilenameStem() const {
  std::ostringstream os;
  os << "exp" << exposureUs
     << "_gain" << gain
     << "_off" << offset
     << "_bin" << bin
     << "_" << width << "x" << height
     << "_" << imgTypeTag(imgType);
  return os.str();
}

std::optional<CalibKey> CalibKey::fromFilename(const std::string& filename) {
  static const std::regex re(
      R"(exp(\d+)_gain(\d+)_off(-?\d+)_bin(\d+)_(\d+)x(\d+)_(RAW8|RAW16|Y8|RGB24))");
  std::smatch m;
  if (!std::regex_search(filename, m, re)) return std::nullopt;
  CalibKey k;
  k.exposureUs = std::stol(m[1]);
  k.gain       = std::stol(m[2]);
  k.offset     = std::stol(m[3]);
  k.bin        = std::stoi(m[4]);
  k.width      = std::stoi(m[5]);
  k.height     = std::stoi(m[6]);
  k.imgType    = imgTypeFromTag(m[7]);
  return k;
}

// ---------------------------------------------------------------------------
// MasterAcquisition
// ---------------------------------------------------------------------------
MasterAcquisition::MasterAcquisition(Camera& camera, Kind kind, int frameCount,
                                     const CalibKey& key)
    : camera_(camera), kind_(kind), total_(frameCount), key_(key) {}

MasterAcquisition::~MasterAcquisition() {
  cancel();
  if (thread_.joinable()) thread_.join();
}

void MasterAcquisition::start() {
  if (status_.load() != Status::Idle) return;
  status_.store(Status::Running);
  thread_ = std::thread(&MasterAcquisition::run, this);
}

void MasterAcquisition::cancel() { shouldStop_.store(true); }

std::string MasterAcquisition::errorMessage() const {
  std::lock_guard<std::mutex> lock(errorMutex_);
  return error_;
}

std::unique_ptr<MasterFrame> MasterAcquisition::takeResult() {
  return std::move(result_);
}

void MasterAcquisition::run() {
  try {
    const std::size_t n = static_cast<std::size_t>(key_.width) * key_.height;
    std::vector<double> accum(n, 0.0);
    std::vector<std::uint8_t> rawBuf;
    int bpp = Camera::bytesPerPixel(key_.imgType);
    rawBuf.resize(n * bpp);

    for (int i = 0; i < total_ && !shouldStop_.load(); ++i) {
      // ASI533-class cameras have no mechanical shutter. A dark master is made
      // by physically covering the optical input; the SDK dark flag cannot do
      // that for this hardware.
      camera_.startExposure(false);
      ASI_EXPOSURE_STATUS st = ASI_EXP_WORKING;
      long expUs = key_.exposureUs > 0 ? key_.exposureUs : 100000;
      int maxWaitMs = static_cast<int>(expUs / 1000) + 10000;
      int waited = 0;
      while (st == ASI_EXP_WORKING && waited < maxWaitMs &&
             !shouldStop_.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        waited += 20;
        st = camera_.expStatus();
      }
      if (shouldStop_.load()) {
        try { camera_.stopExposure(); } catch (...) {}
        status_.store(Status::Failed);
        return;
      }
      if (st != ASI_EXP_SUCCESS) {
        std::lock_guard<std::mutex> lock(errorMutex_);
        error_ = "exposure failed at frame " + std::to_string(i + 1);
        status_.store(Status::Failed);
        return;
      }
      camera_.getDataAfterExp(rawBuf.data(), rawBuf.size());

      if (key_.imgType == ASI_IMG_RAW16) {
        const auto* p = reinterpret_cast<const std::uint16_t*>(rawBuf.data());
        for (std::size_t k = 0; k < n; ++k) accum[k] += p[k];
      } else {
        for (std::size_t k = 0; k < n; ++k) accum[k] += rawBuf[k];
      }
      progress_.store(i + 1);
    }

    auto out = std::make_unique<MasterFrame>();
    out->key = key_;
    out->frameCount = progress_.load();
    out->data.resize(n);
    double invN = 1.0 / std::max(1, out->frameCount);
    for (std::size_t k = 0; k < n; ++k) {
      out->data[k] = static_cast<float>(accum[k] * invN);
    }
    result_ = std::move(out);
    status_.store(Status::Done);
  } catch (const std::exception& e) {
    std::lock_guard<std::mutex> lock(errorMutex_);
    error_ = e.what();
    status_.store(Status::Failed);
  }
}

// ---------------------------------------------------------------------------
// CalibrationManager
// ---------------------------------------------------------------------------
CalibrationManager::CalibrationManager(std::string dir) : dir_(std::move(dir)) {}

CalibKey CalibrationManager::keyFromFrame(const Frame& f, long expUs, long gain,
                                         long off) {
  CalibKey k;
  k.width = f.width;
  k.height = f.height;
  k.bin = f.bin;
  k.imgType = f.type;
  k.exposureUs = expUs;
  k.gain = gain;
  k.offset = off;
  return k;
}

std::string CalibrationManager::buildFilename(const std::string& kindPrefix,
                                              const CalibKey& k) const {
  fs::path p = fs::path(dir_) / (kindPrefix + "_" + k.toFilenameStem() + ".tif");
  return p.string();
}

std::optional<std::string> CalibrationManager::findMatchingMaster(
    const std::string& kindPrefix, const CalibKey& k) const {
  if (!fs::exists(dir_)) return std::nullopt;
  for (const auto& e : fs::directory_iterator(dir_)) {
    if (!e.is_regular_file()) continue;
    std::string name = e.path().filename().string();
    if (name.rfind(kindPrefix + "_", 0) != 0) continue;
    auto pk = CalibKey::fromFilename(name);
    if (pk && *pk == k) return e.path().string();
  }
  return std::nullopt;
}

std::unique_ptr<MasterFrame> CalibrationManager::loadMaster(
    const std::string& path) const {
  auto out = std::make_unique<MasterFrame>();
  int w = 0, h = 0;
  ImageWriter::readFloatTiff(path, w, h, out->data);
  std::string filename = fs::path(path).filename().string();
  auto key = CalibKey::fromFilename(filename);
  if (key) {
    out->key = *key;
  } else {
    out->key.width = w;
    out->key.height = h;
  }
  out->sourcePath = path;
  return out;
}

void CalibrationManager::saveMaster(const MasterFrame& m,
                                    const std::string& path) const {
  std::ostringstream desc;
  desc << "kind=master  frames=" << m.frameCount
       << " exposure_us=" << m.key.exposureUs
       << " gain=" << m.key.gain
       << " offset=" << m.key.offset
       << " bin=" << m.key.bin
       << " size=" << m.key.width << "x" << m.key.height
       << " img=" << imgTypeTag(m.key.imgType);
  ImageWriter::writeFloatTiff(path, m.key.width, m.key.height,
                              m.data.data(), desc.str());
}

void CalibrationManager::installDark(std::unique_ptr<MasterFrame> m) {
  if (!m) return;
  std::string path = buildFilename("master_dark", m->key);
  saveMaster(*m, path);
  m->sourcePath = path;
  dark_ = std::move(m);
}

void CalibrationManager::installFlat(std::unique_ptr<MasterFrame> m) {
  if (!m) return;
  std::string path = buildFilename("master_flat", m->key);
  saveMaster(*m, path);
  m->sourcePath = path;
  flat_ = std::move(m);
}

bool CalibrationManager::darkMatches(const CalibKey& k) const {
  return dark_ && dark_->key == k;
}
bool CalibrationManager::flatMatches(const CalibKey& k) const {
  return flat_ && flat_->key == k;
}

int CalibrationManager::autoLoadFor(const CalibKey& k) {
  int n = 0;
  if (!dark_ || dark_->key != k) {
    if (auto p = findMatchingMaster("master_dark", k)) {
      try { dark_ = loadMaster(*p); ++n; }
      catch (...) { dark_.reset(); }
    }
  }
  if (!flat_ || flat_->key != k) {
    if (auto p = findMatchingMaster("master_flat", k)) {
      try { flat_ = loadMaster(*p); ++n; }
      catch (...) { flat_.reset(); }
    }
  }
  return n;
}

AppliedFrame CalibrationManager::apply(const Frame& raw) const {
  AppliedFrame out;
  out.width = raw.width;
  out.height = raw.height;
  const std::size_t n = static_cast<std::size_t>(raw.width) * raw.height;
  out.data.resize(n);

  // Lift raw pixels into floats first.
  std::vector<float> rawf(n);
  if (raw.type == ASI_IMG_RAW16) {
    const auto* p = reinterpret_cast<const std::uint16_t*>(raw.data.data());
    for (std::size_t i = 0; i < n; ++i) rawf[i] = p[i];
  } else {
    for (std::size_t i = 0; i < n; ++i) rawf[i] = raw.data[i];
  }

  const bool dark = (dark_ && dark_->data.size() == n);
  const bool flat = (flat_ && flat_->data.size() == n);

  if (dark && flat) {
    // mean( flat - dark )
    double sum = 0;
    for (std::size_t i = 0; i < n; ++i) {
      sum += static_cast<double>(flat_->data[i]) - dark_->data[i];
    }
    double meanFlatMinusDark = sum / std::max<std::size_t>(1, n);
    for (std::size_t i = 0; i < n; ++i) {
      float fmd = flat_->data[i] - dark_->data[i];
      if (fmd <= 1e-6f) fmd = 1e-6f;
      out.data[i] = (rawf[i] - dark_->data[i]) / fmd *
                    static_cast<float>(meanFlatMinusDark);
    }
  } else if (dark) {
    for (std::size_t i = 0; i < n; ++i) {
      out.data[i] = rawf[i] - dark_->data[i];
    }
  } else {
    out.data = std::move(rawf);
  }
  return out;
}

std::vector<std::uint16_t> CalibrationManager::applyU16(const Frame& raw) const {
  AppliedFrame f = apply(raw);
  std::vector<std::uint16_t> out(f.data.size());
  for (std::size_t i = 0; i < f.data.size(); ++i) {
    float v = f.data[i];
    if (v < 0.0f) v = 0.0f;
    if (v > 65535.0f) v = 65535.0f;
    out[i] = static_cast<std::uint16_t>(v);
  }
  return out;
}

}  // namespace mc
