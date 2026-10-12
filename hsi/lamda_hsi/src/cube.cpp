#include "cube.h"

#include "image_writer.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <regex>
#include <sstream>
#include <stdexcept>

namespace mc {

namespace fs = std::filesystem;

namespace {

std::string nowIso() {
  auto t = std::chrono::system_clock::to_time_t(
      std::chrono::system_clock::now());
  std::tm tm{};
#if defined(_WIN32)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  std::ostringstream os;
  os << std::put_time(&tm, "%Y-%m-%dT%H:%M:%S");
  return os.str();
}

std::string nowStamp() {
  auto t = std::chrono::system_clock::to_time_t(
      std::chrono::system_clock::now());
  std::tm tm{};
#if defined(_WIN32)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  std::ostringstream os;
  os << std::put_time(&tm, "%Y%m%d_%H%M%S");
  return os.str();
}

std::string jsonEscape(const std::string& s) {
  std::string o;
  o.reserve(s.size() + 2);
  for (char c : s) {
    switch (c) {
      case '"':  o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n";  break;
      case '\r': o += "\\r";  break;
      case '\t': o += "\\t";  break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          o += buf;
        } else {
          o += c;
        }
    }
  }
  return o;
}

std::string bandFilename(int idx, double nm) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), "band_%04d_%07.2fnm.tif", idx, nm);
  return buf;
}

fs::path fullResolutionCachePath(const fs::path& sessionDir,
                                 const std::string& bandFilename) {
  return sessionDir / ".lamda_fullres_cache" / bandFilename;
}

std::optional<double> parseWavelengthFromName(const std::string& name) {
  static const std::regex re(R"(band_\d+_(-?\d+\.\d+)nm\.tif)");
  std::smatch m;
  if (std::regex_search(name, m, re)) {
    try {
      return std::stod(m[1]);
    } catch (...) {}
  }
  return std::nullopt;
}

CubeBand makePreviewBand(const CubeBand& full) {
  constexpr std::size_t kMaxPreviewBytes = 5u * 1024u * 1024u;
  constexpr std::size_t kMaxPreviewPixels =
      kMaxPreviewBytes / sizeof(float);
  const std::size_t pixels =
      static_cast<std::size_t>(full.width) * full.height;
  if (pixels <= kMaxPreviewPixels) return full;

  const double ratio =
      static_cast<double>(pixels) / static_cast<double>(kMaxPreviewPixels);
  const int scale = std::max(2, static_cast<int>(std::ceil(std::sqrt(ratio))));

  CubeBand preview;
  preview.wavelength_nm = full.wavelength_nm;
  preview.width = (full.width + scale - 1) / scale;
  preview.height = (full.height + scale - 1) / scale;
  preview.filename = full.filename;
  preview.data.resize(
      static_cast<std::size_t>(preview.width) * preview.height);

  // Box averaging preserves the visual/spectral trend better than simply
  // dropping pixels and keeps every in-memory band below the preview budget.
  for (int py = 0; py < preview.height; ++py) {
    const int y0 = py * scale;
    const int y1 = std::min(full.height, y0 + scale);
    for (int px = 0; px < preview.width; ++px) {
      const int x0 = px * scale;
      const int x1 = std::min(full.width, x0 + scale);
      double sum = 0.0;
      int count = 0;
      for (int y = y0; y < y1; ++y) {
        const std::size_t row = static_cast<std::size_t>(y) * full.width;
        for (int x = x0; x < x1; ++x) {
          sum += full.data[row + x];
          ++count;
        }
      }
      preview.data[static_cast<std::size_t>(py) * preview.width + px] =
          count > 0 ? static_cast<float>(sum / count) : 0.0f;
    }
  }
  return preview;
}

}  // namespace

// ---------------------------------------------------------------------------
// CubeSession
// ---------------------------------------------------------------------------
CubeSession::CubeSession() = default;

void CubeSession::appendBand(CubeBand&& b) {
  std::lock_guard<std::mutex> lock(mutex_);
  bands_.push_back(std::move(b));
}

std::size_t CubeSession::bandCount() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return bands_.size();
}

bool CubeSession::getBand(std::size_t i, CubeBand& out) const {
  std::lock_guard<std::mutex> lock(mutex_);
  if (i >= bands_.size()) return false;
  out = bands_[i];
  return true;
}

std::vector<double> CubeSession::wavelengths() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::vector<double> out;
  out.reserve(bands_.size());
  for (const auto& b : bands_) out.push_back(b.wavelength_nm);
  return out;
}

std::pair<double, double> CubeSession::wavelengthRange() const {
  std::lock_guard<std::mutex> lock(mutex_);
  if (bands_.empty()) return {0.0, 0.0};
  double lo = bands_.front().wavelength_nm;
  double hi = lo;
  for (const auto& b : bands_) {
    lo = std::min(lo, b.wavelength_nm);
    hi = std::max(hi, b.wavelength_nm);
  }
  return {lo, hi};
}

void CubeSession::spectrumAt(int x, int y, std::vector<double>& xs,
                             std::vector<double>& ys) const {
  std::lock_guard<std::mutex> lock(mutex_);
  xs.clear();
  ys.clear();
  if (bands_.empty()) return;
  int w = bands_.front().width;
  int h = bands_.front().height;
  if (x < 0 || y < 0 || x >= w || y >= h) return;
  xs.reserve(bands_.size());
  ys.reserve(bands_.size());
  std::size_t idx = static_cast<std::size_t>(y) * w + x;
  for (const auto& b : bands_) {
    if (idx < b.data.size()) {
      xs.push_back(b.wavelength_nm);
      ys.push_back(b.data[idx]);
    }
  }
  // Sort by wavelength so the plot reads left-to-right.
  std::vector<std::size_t> order(xs.size());
  for (std::size_t i = 0; i < order.size(); ++i) order[i] = i;
  std::sort(order.begin(), order.end(),
            [&](std::size_t a, std::size_t b) { return xs[a] < xs[b]; });
  std::vector<double> sxs(xs.size()), sys(ys.size());
  for (std::size_t i = 0; i < order.size(); ++i) {
    sxs[i] = xs[order[i]];
    sys[i] = ys[order[i]];
  }
  xs.swap(sxs);
  ys.swap(sys);
}

void CubeSession::setMetadata(const CubeMetadata& m) {
  std::lock_guard<std::mutex> lock(mutex_);
  meta_ = m;
}

CubeMetadata CubeSession::metadata() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return meta_;
}

void CubeSession::saveMetadata() const {
  CubeMetadata m;
  std::vector<double> wavelengths;
  std::vector<std::string> filenames;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    m = meta_;
    for (const auto& b : bands_) {
      wavelengths.push_back(b.wavelength_nm);
      filenames.push_back(b.filename);
    }
  }
  fs::create_directories(m.sessionDir);
  std::string path = (fs::path(m.sessionDir) / "cube_metadata.json").string();
  std::ofstream f(path);
  if (!f) throw std::runtime_error("cannot open " + path);

  auto kv = [&](const char* k, const std::string& v, bool comma = true) {
    f << "  \"" << k << "\": \"" << jsonEscape(v) << "\"" << (comma ? ",\n" : "\n");
  };
  auto kvi = [&](const char* k, long long v, bool comma = true) {
    f << "  \"" << k << "\": " << v << (comma ? ",\n" : "\n");
  };
  auto kvf = [&](const char* k, double v, bool comma = true) {
    f << "  \"" << k << "\": " << v << (comma ? ",\n" : "\n");
  };
  auto kvb = [&](const char* k, bool v, bool comma = true) {
    f << "  \"" << k << "\": " << (v ? "true" : "false") << (comma ? ",\n" : "\n");
  };

  f << "{\n";
  kv("schema", "lamda_cube_v1");
  kv("start_timestamp", m.startTimestamp);
  kv("sample_name", m.sampleName);
  kv("notes", m.notes);
  kv("camera_name", m.cameraName);
  kv("camera_serial", m.cameraSerial);
  kv("sdk_version", m.sdkVersion);
  kvi("width", m.width);
  kvi("height", m.height);
  kvi("bin", m.bin);
  kv("image_type", m.imgType);
  kvi("exposure_us", m.exposureUs);
  kvi("gain", m.gain);
  kvi("offset", m.offset);
  kvi("gamma", m.gamma);
  kvi("usb_bandwidth", m.usbBandwidth);
  kvf("pixel_size_um", m.pixelSizeUm);
  kvi("bit_depth", m.bitDepth);
  if (m.bitDepth == 14 && m.imgType == "RAW16") {
    kvi("raw16_saturation_level", 65532);
    kv("storage_note", "ASI RAW16 stores 14-bit ADC data in a 16-bit container");
  }
  kvb("dark_applied", m.darkApplied);
  kvb("flat_applied", m.flatApplied);
  kvf("dwell_sec", m.dwellSec);

  f << "  \"bands\": [\n";
  for (std::size_t i = 0; i < wavelengths.size(); ++i) {
    f << "    {\"wavelength_nm\": " << wavelengths[i]
      << ", \"file\": \"" << jsonEscape(filenames[i]) << "\"}"
      << (i + 1 < wavelengths.size() ? "," : "") << "\n";
  }
  f << "  ]\n";
  f << "}\n";
}

void CubeSession::saveEnvi() const {
  CubeMetadata m;
  std::vector<std::pair<double, std::string>> sources;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    m = meta_;
    sources.reserve(bands_.size());
    for (const auto& band : bands_) {
      sources.emplace_back(band.wavelength_nm, band.filename);
    }
  }
  const std::size_t bandCount = sources.size();
  if (bandCount == 0) return;
  fs::create_directories(m.sessionDir);

  fs::path datPath = fs::path(m.sessionDir) / "cube.dat";
  fs::path hdrPath = fs::path(m.sessionDir) / "cube.hdr";
  std::vector<double> wavelengths;
  wavelengths.reserve(bandCount);

  const bool writeFloat = m.darkApplied || m.flatApplied;
  int width = 0;
  int height = 0;
  {
    std::ofstream dat(datPath, std::ios::binary);
    if (!dat) throw std::runtime_error("cannot open " + datPath.string());
    for (std::size_t i = 0; i < bandCount; ++i) {
      int bandWidth = 0;
      int bandHeight = 0;
      std::vector<float> pixels;
      fs::path sourcePath =
          fullResolutionCachePath(m.sessionDir, sources[i].second);
      if (!fs::exists(sourcePath)) {
        sourcePath = fs::path(m.sessionDir) / sources[i].second;
      }
      ImageWriter::readFloatTiff(sourcePath.string(), bandWidth, bandHeight,
                                 pixels);
      if (i == 0) {
        width = bandWidth;
        height = bandHeight;
      }
      if (bandWidth != width || bandHeight != height ||
          pixels.size() != static_cast<std::size_t>(width) * height) {
        throw std::runtime_error("cannot export ENVI cube with inconsistent band sizes");
      }
      wavelengths.push_back(sources[i].first);
      if (writeFloat) {
        dat.write(reinterpret_cast<const char*>(pixels.data()),
                  static_cast<std::streamsize>(pixels.size() * sizeof(float)));
      } else {
        std::vector<std::uint16_t> u16(pixels.size());
        for (std::size_t k = 0; k < pixels.size(); ++k) {
          const float v = std::clamp(pixels[k], 0.0f, 65535.0f);
          u16[k] = static_cast<std::uint16_t>(std::lround(v));
        }
        dat.write(reinterpret_cast<const char*>(u16.data()),
                  static_cast<std::streamsize>(u16.size() * sizeof(std::uint16_t)));
      }
      if (!dat) throw std::runtime_error("failed writing " + datPath.string());
    }
  }

  std::ofstream hdr(hdrPath);
  if (!hdr) throw std::runtime_error("cannot open " + hdrPath.string());
  hdr << "ENVI\n";
  hdr << "description = {LAMDA HyperSpectral Imaging System cube}\n";
  hdr << "samples = " << width << "\n";
  hdr << "lines = " << height << "\n";
  hdr << "bands = " << bandCount << "\n";
  hdr << "header offset = 0\n";
  hdr << "file type = ENVI Standard\n";
  hdr << "data type = " << (writeFloat ? 4 : 12) << "\n";
  hdr << "interleave = bsq\n";
  hdr << "byte order = 0\n";      // little endian
  hdr << "wavelength units = Nanometers\n";
  hdr << "band names = {";
  for (std::size_t i = 0; i < wavelengths.size(); ++i) {
    if (i) hdr << ", ";
    hdr << "band " << (i + 1) << " " << std::fixed << std::setprecision(2)
        << wavelengths[i] << " nm";
  }
  hdr << "}\n";
  hdr << "wavelength = {";
  for (std::size_t i = 0; i < wavelengths.size(); ++i) {
    if (i) hdr << ", ";
    hdr << std::fixed << std::setprecision(4) << wavelengths[i];
  }
  hdr << "}\n";
  hdr << "lamda sample name = {" << m.sampleName << "}\n";
  hdr << "lamda camera = {" << m.cameraName << "}\n";
  hdr << "lamda image type = {" << m.imgType << "}\n";
  hdr << "lamda storage = {" << (writeFloat ? "float32 calibrated" : "uint16 raw sdk scale") << "}\n";
  hdr << "lamda effective adc bits = " << m.bitDepth << "\n";
  if (m.bitDepth == 14 && m.imgType == "RAW16") {
    hdr << "lamda raw16 saturation level = 65532\n";
    hdr << "lamda note = {ASI RAW16 stores 14-bit ADC data in a 16-bit container}\n";
  }
  hdr << "lamda exposure us = " << m.exposureUs << "\n";
  hdr << "lamda gain = " << m.gain << "\n";
  hdr << "lamda offset = " << m.offset << "\n";
  hdr << "lamda bin = " << m.bin << "\n";
  hdr << "lamda dark applied = " << (m.darkApplied ? 1 : 0) << "\n";
  hdr << "lamda flat applied = " << (m.flatApplied ? 1 : 0) << "\n";

  std::error_code ec;
  fs::remove_all(fs::path(m.sessionDir) / ".lamda_fullres_cache", ec);
}

void CubeSession::loadFromDir(const std::string& dir) {
  clear();
  sessionDir_ = dir;
  // Walk all band_*.tif files, parse wavelength from filename, load as float.
  std::vector<std::pair<double, std::string>> entries;
  for (const auto& e : fs::directory_iterator(dir)) {
    if (!e.is_regular_file()) continue;
    std::string name = e.path().filename().string();
    if (name.rfind("band_", 0) != 0) continue;
    if (auto wl = parseWavelengthFromName(name)) {
      entries.emplace_back(*wl, e.path().string());
    }
  }
  std::sort(entries.begin(), entries.end());
  for (const auto& [wl, path] : entries) {
    CubeBand full;
    full.wavelength_nm = wl;
    ImageWriter::readFloatTiff(path, full.width, full.height, full.data);
    full.filename = fs::path(path).filename().string();
    CubeBand preview = makePreviewBand(full);
    {
      std::lock_guard<std::mutex> lock(mutex_);
      bands_.push_back(std::move(preview));
    }
  }
  // Try to read metadata.json for context (parsing is best-effort: we accept
  // missing keys silently — the file is for traceability, not consumed here).
  CubeMetadata m;
  m.sessionDir = dir;
  setMetadata(m);
}

void CubeSession::clear() {
  std::lock_guard<std::mutex> lock(mutex_);
  bands_.clear();
}

// ---------------------------------------------------------------------------
// ScanThread
// ---------------------------------------------------------------------------
ScanThread::ScanThread(Camera& camera, Monochromator& mono,
                       std::shared_ptr<CubeSession> session,
                       CalibrationManager* calib, ScanParams params)
    : camera_(camera), mono_(mono), session_(std::move(session)),
      calib_(calib), params_(std::move(params)) {
  // Compute total step count (inclusive of start and end).
  double s = params_.startNm;
  double e = params_.endNm;
  double step = std::abs(params_.stepNm);
  if (step <= 1e-9) step = 1.0;
  int n = 0;
  if (s <= e) {
    for (double x = s; x <= e + 1e-9; x += step) ++n;
  } else {
    for (double x = s; x >= e - 1e-9; x -= step) ++n;
  }
  total_ = n;
}

ScanThread::~ScanThread() {
  cancel();
  if (thread_.joinable()) thread_.join();
}

void ScanThread::start() {
  if (status_.load() != Status::Idle) return;
  mono_.prepareOperation();
  status_.store(Status::Running);
  thread_ = std::thread(&ScanThread::run, this);
}

void ScanThread::cancel() {
  shouldStop_.store(true);
  mono_.cancelPendingOperation();
}

std::string ScanThread::errorMessage() const {
  std::lock_guard<std::mutex> lock(errorMutex_);
  return error_;
}

void ScanThread::run() {
  try {
    if (!mono_.connected()) {
      std::lock_guard<std::mutex> lock(errorMutex_);
      error_ = "monochromator not connected";
      status_.store(Status::Failed);
      return;
    }

    // Setup session directory.
    std::string sessionName = nowStamp();
    if (!params_.sampleName.empty()) sessionName += "_" + params_.sampleName;
    fs::path sessionPath = fs::path(session_->sessionDir()) / sessionName;
    fs::create_directories(sessionPath);
    session_->setSessionDir(sessionPath.string());

    // Snapshot capture settings.
    long expUs = camera_.getControl(ASI_EXPOSURE).value;
    long gain  = camera_.getControl(ASI_GAIN).value;
    long off   = 0;
    try { off = camera_.getControl(ASI_OFFSET).value; } catch (...) {}
    long gamma = 0;
    try { gamma = camera_.getControl(ASI_GAMMA).value; } catch (...) {}
    long usb   = 0;
    try { usb = camera_.getControl(ASI_BANDWIDTHOVERLOAD).value; } catch (...) {}

    RoiFormat roi = camera_.getRoi();

    CubeMetadata meta;
    meta.sessionDir = sessionPath.string();
    meta.startTimestamp = nowIso();
    meta.cameraName = camera_.info().Name;
    if (auto sn = camera_.serialNumber()) meta.cameraSerial = *sn;
    meta.sdkVersion = CameraManager::sdkVersion();
    meta.width = roi.width;
    meta.height = roi.height;
    meta.bin = roi.bin;
    meta.imgType = Camera::imageTypeName(roi.imageType);
    meta.exposureUs = expUs;
    meta.gain = gain;
    meta.offset = off;
    meta.gamma = gamma;
    meta.usbBandwidth = usb;
    meta.pixelSizeUm = camera_.info().PixelSize;
    meta.bitDepth = camera_.info().BitDepth;
    meta.darkApplied = (calib_ && calib_->hasDark());
    meta.flatApplied = (calib_ && calib_->hasFlat());
    meta.dwellSec = params_.dwellSec;
    meta.sampleName = params_.sampleName;
    meta.notes = params_.notes;
    session_->setMetadata(meta);

    // Build wavelength list.
    std::vector<double> wavelengths;
    {
      double s = params_.startNm;
      double e = params_.endNm;
      double step = std::abs(params_.stepNm);
      if (step <= 1e-9) step = 1.0;
      if (s <= e) {
        for (double x = s; x <= e + 1e-9; x += step)
          wavelengths.push_back(x);
      } else {
        for (double x = s; x >= e - 1e-9; x -= step)
          wavelengths.push_back(x);
      }
    }

    // Make sure live video is off while we run synchronous exposures.
    // (caller is expected to stop it before starting us, but be safe).

    int bandIdx = 0;
    for (double wl : wavelengths) {
      if (shouldStop_.load()) {
        status_.store(Status::Cancelled);
        session_->saveMetadata();
        return;
      }
      currentWavelength_.store(wl);

      // 1) Move monochromator.
      MoveResult mr = mono_.goTo(wl);
      if (shouldStop_.load() ||
          mr.status == MoveResult::Status::Cancelled) {
        status_.store(Status::Cancelled);
        session_->saveMetadata();
        return;
      }
      if (mr.status != MoveResult::Status::Done) {
        std::lock_guard<std::mutex> lock(errorMutex_);
        error_ = "monochromator move failed: " + mr.message +
                 " (status=" + std::to_string(static_cast<int>(mr.status)) + ")";
        status_.store(Status::Failed);
        session_->saveMetadata();
        return;
      }

      // 2) Dwell to let optics + sample settle.
      if (params_.dwellSec > 0.0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(
            static_cast<long long>(params_.dwellSec * 1000.0)));
      }
      if (shouldStop_.load()) {
        status_.store(Status::Cancelled);
        session_->saveMetadata();
        return;
      }

      // 3) Single exposure (not video) for precise timing.
      camera_.startExposure(false);
      ASI_EXPOSURE_STATUS st = ASI_EXP_WORKING;
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
        status_.store(Status::Cancelled);
        session_->saveMetadata();
        return;
      }
      if (st != ASI_EXP_SUCCESS) {
        std::lock_guard<std::mutex> lock(errorMutex_);
        error_ = "exposure failed at " + std::to_string(wl) + " nm";
        status_.store(Status::Failed);
        session_->saveMetadata();
        return;
      }

      // 4) Read pixels.
      Frame raw;
      raw.width = roi.width;
      raw.height = roi.height;
      raw.bin = roi.bin;
      raw.type = roi.imageType;
      raw.data.resize(static_cast<std::size_t>(raw.width) * raw.height *
                      Camera::bytesPerPixel(raw.type));
      camera_.getDataAfterExp(raw.data.data(), raw.data.size());

      // 5) Apply calibration to float32.
      CubeBand band;
      band.wavelength_nm = wl;
      band.width = raw.width;
      band.height = raw.height;
      if (calib_ && (calib_->hasDark() || calib_->hasFlat())) {
        AppliedFrame ap = calib_->apply(raw);
        band.data = std::move(ap.data);
      } else {
        const std::size_t n =
            static_cast<std::size_t>(raw.width) * raw.height;
        band.data.resize(n);
        if (raw.type == ASI_IMG_RAW16) {
          const auto* p =
              reinterpret_cast<const std::uint16_t*>(raw.data.data());
          for (std::size_t k = 0; k < n; ++k) band.data[k] = p[k];
        } else {
          for (std::size_t k = 0; k < n; ++k) band.data[k] = raw.data[k];
        }
      }

      // 6) Keep the existing display TIFF output unchanged. A temporary
      // full-resolution float cache feeds ENVI export without retaining the
      // full cube in RAM.
      band.filename = bandFilename(bandIdx + 1, wl);
      std::string path = (sessionPath / band.filename).string();
      float mn = std::numeric_limits<float>::max();
      float mx = -mn;
      double sum = 0.0;
      for (float v : band.data) {
        mn = std::min(mn, v);
        mx = std::max(mx, v);
        sum += v;
      }
      const double mean = band.data.empty() ? 0.0 : sum / band.data.size();
      std::ostringstream desc;
      desc << "wavelength_nm=" << wl
           << "  exposure_us=" << expUs
           << "  gain=" << gain
           << "  dark=" << (meta.darkApplied ? "y" : "n")
           << "  flat=" << (meta.flatApplied ? "y" : "n")
           << "  min=" << mn
           << "  max=" << mx
           << "  mean=" << mean;
      std::vector<std::uint16_t> display16(band.data.size());
      for (std::size_t k = 0; k < band.data.size(); ++k) {
        const float v = std::clamp(band.data[k], 0.0f, 65535.0f);
        display16[k] = static_cast<std::uint16_t>(std::lround(v));
      }
      ImageWriter::writeGrey16Tiff(path, band.width, band.height,
                                   display16.data(), desc.str());

      const fs::path cachePath =
          fullResolutionCachePath(sessionPath, band.filename);
      ImageWriter::writeFloatTiff(cachePath.string(), band.width, band.height,
                                  band.data.data(), desc.str());

      // 7) Hand only a box-averaged, size-bounded copy to the live viewer.
      session_->appendBand(makePreviewBand(band));
      ++bandIdx;
      progress_.store(bandIdx);
    }

    status_.store(Status::Exporting);
    session_->saveMetadata();
    session_->saveEnvi();
    status_.store(Status::Done);
  } catch (const std::exception& e) {
    std::lock_guard<std::mutex> lock(errorMutex_);
    error_ = e.what();
    status_.store(Status::Failed);
    try { session_->saveMetadata(); } catch (...) {}
  }
}

std::vector<std::string> listCubeSessions(const std::string& parentDir) {
  std::vector<std::string> out;
  std::error_code ec;
  if (!fs::exists(parentDir, ec)) return out;
  for (const auto& e : fs::directory_iterator(parentDir, ec)) {
    if (ec) break;
    if (!e.is_directory()) continue;
    // Sessions contain at least one band_*.tif.
    bool has = false;
    for (const auto& f : fs::directory_iterator(e.path(), ec)) {
      if (ec) break;
      if (f.is_regular_file() &&
          f.path().filename().string().rfind("band_", 0) == 0) {
        has = true;
        break;
      }
    }
    if (has) out.push_back(e.path().string());
  }
  std::sort(out.begin(), out.end());
  return out;
}

}  // namespace mc
