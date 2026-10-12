#include "image_writer.h"

#include "stb_image_write.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <vector>

namespace mc {

namespace fs = std::filesystem;

namespace {

std::string timestamp() {
  auto now = std::chrono::system_clock::now();
  auto t = std::chrono::system_clock::to_time_t(now);
  auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                now.time_since_epoch())
                .count() %
            1000000;
  std::tm tm{};
#if defined(_WIN32)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  std::ostringstream os;
  os << std::put_time(&tm, "%Y%m%d_%H%M%S") << '_' << std::setw(6)
     << std::setfill('0') << us;
  return os.str();
}

std::string composePath(const std::string& dir, const std::string& prefix,
                        const std::string& ext) {
  fs::path p = fs::path(dir) / (prefix + "_" + timestamp() + "." + ext);
  return p.string();
}

// Promote frame to 16-bit greyscale row-major buffer.
std::vector<std::uint16_t> toGrey16(const Frame& frame) {
  const std::size_t n = static_cast<std::size_t>(frame.width) * frame.height;
  std::vector<std::uint16_t> out(n);
  switch (frame.type) {
    case ASI_IMG_RAW16: {
      std::memcpy(out.data(), frame.data.data(), n * sizeof(std::uint16_t));
      break;
    }
    case ASI_IMG_RAW8:
    case ASI_IMG_Y8: {
      for (std::size_t i = 0; i < n; ++i) {
        out[i] = static_cast<std::uint16_t>(frame.data[i]) << 8;
      }
      break;
    }
    case ASI_IMG_RGB24: {
      for (std::size_t i = 0; i < n; ++i) {
        std::uint16_t b = frame.data[i * 3 + 0];
        std::uint16_t g = frame.data[i * 3 + 1];
        std::uint16_t r = frame.data[i * 3 + 2];
        std::uint16_t y = static_cast<std::uint16_t>(
            (54u * r + 183u * g + 19u * b) >> 8);
        out[i] = y << 8;
      }
      break;
    }
    default:
      throw std::runtime_error("unsupported image type");
  }
  return out;
}

// Quick 16->8 bit downscale for PNG preview.
std::vector<std::uint8_t> toGrey8(const Frame& frame) {
  const std::size_t n = static_cast<std::size_t>(frame.width) * frame.height;
  std::vector<std::uint8_t> out(n);
  switch (frame.type) {
    case ASI_IMG_RAW8:
    case ASI_IMG_Y8:
      std::memcpy(out.data(), frame.data.data(), n);
      break;
    case ASI_IMG_RAW16: {
      const auto* src = reinterpret_cast<const std::uint16_t*>(frame.data.data());
      for (std::size_t i = 0; i < n; ++i) out[i] = src[i] >> 8;
      break;
    }
    case ASI_IMG_RGB24:
      for (std::size_t i = 0; i < n; ++i) {
        out[i] = static_cast<std::uint8_t>(
            (54u * frame.data[i * 3 + 2] + 183u * frame.data[i * 3 + 1] +
             19u * frame.data[i * 3 + 0]) >>
            8);
      }
      break;
    default:
      throw std::runtime_error("unsupported image type");
  }
  return out;
}

// ----------------------------------------------------------------------------
// Minimal baseline TIFF writer (single strip, 16-bit greyscale, little-endian)
// ----------------------------------------------------------------------------

class ByteStream {
public:
  void u16(std::uint16_t v) { buf_.push_back(v & 0xff); buf_.push_back((v >> 8) & 0xff); }
  void u32(std::uint32_t v) {
    buf_.push_back(v & 0xff);
    buf_.push_back((v >> 8) & 0xff);
    buf_.push_back((v >> 16) & 0xff);
    buf_.push_back((v >> 24) & 0xff);
  }
  void bytes(const void* p, std::size_t n) {
    const auto* b = static_cast<const std::uint8_t*>(p);
    buf_.insert(buf_.end(), b, b + n);
  }
  std::size_t size() const { return buf_.size(); }
  const std::vector<std::uint8_t>& data() const { return buf_; }
  void writeU32At(std::size_t offset, std::uint32_t v) {
    buf_[offset + 0] = v & 0xff;
    buf_[offset + 1] = (v >> 8) & 0xff;
    buf_[offset + 2] = (v >> 16) & 0xff;
    buf_[offset + 3] = (v >> 24) & 0xff;
  }

private:
  std::vector<std::uint8_t> buf_;
};

void writeTiffEntry(ByteStream& s, std::uint16_t tag, std::uint16_t type,
                    std::uint32_t count, std::uint32_t valueOrOffset) {
  s.u16(tag);
  s.u16(type);
  s.u32(count);
  s.u32(valueOrOffset);
}

}  // namespace

void ImageWriter::ensureDir(const std::string& dir) {
  fs::create_directories(dir);
}

std::string ImageWriter::writeTiff(const std::string& dir,
                                   const std::string& prefix,
                                   const Frame& frame,
                                   const CaptureMetadata& meta) {
  ensureDir(dir);
  std::string path = composePath(dir, prefix, "tif");
  std::vector<std::uint16_t> pixels = toGrey16(frame);
  const std::uint32_t width = frame.width;
  const std::uint32_t height = frame.height;
  const std::uint32_t bitsPerSample = 16;
  const std::uint32_t stripBytes = width * height * 2;

  // Build description text with metadata for ImageJ etc.
  std::ostringstream desc;
  desc << "Camera=" << meta.cameraName;
  if (meta.serialNumber)  desc << "\nSerial=" << *meta.serialNumber;
  if (meta.exposureUs)    desc << "\nExposureUs=" << *meta.exposureUs;
  if (meta.gain)          desc << "\nGain=" << *meta.gain;
  if (meta.offset)        desc << "\nOffset=" << *meta.offset;
  if (meta.gamma)         desc << "\nGamma=" << *meta.gamma;
  if (meta.usbBandwidth)  desc << "\nUSBBandwidth=" << *meta.usbBandwidth;
  if (meta.sensorTempC)   desc << "\nSensorTempC=" << *meta.sensorTempC;
  if (meta.bitDepth)      desc << "\nBitDepth=" << *meta.bitDepth;
  if (meta.pixelSizeUm)   desc << "\nPixelSizeUm=" << *meta.pixelSizeUm;
  desc << "\nBin=" << meta.bin << "\nSDK=" << meta.sdkVersion << '\0';
  std::string descStr = desc.str();
  // Pad description to even length for IFD alignment.
  if (descStr.size() & 1u) descStr.push_back('\0');

  // Layout:
  //   [0..7]    header
  //   [8..]     IFD (2 + 12*N + 4 bytes)
  //   ...       description string
  //   ...       image strip
  const std::uint16_t entries = 11;
  const std::uint32_t ifdOffset = 8;
  const std::uint32_t ifdSize = 2 + 12u * entries + 4;
  const std::uint32_t descOffset = ifdOffset + ifdSize;
  const std::uint32_t descSize = static_cast<std::uint32_t>(descStr.size());
  const std::uint32_t stripOffset = descOffset + descSize;

  ByteStream s;
  s.u16(0x4949);  // "II"  (little-endian)
  s.u16(42);
  s.u32(ifdOffset);
  s.u16(entries);
  writeTiffEntry(s, 0x00FE, 4, 1, 0);              // NewSubfileType (LONG)
  writeTiffEntry(s, 0x0100, 4, 1, width);          // ImageWidth (LONG)
  writeTiffEntry(s, 0x0101, 4, 1, height);         // ImageLength (LONG)
  writeTiffEntry(s, 0x0102, 3, 1, bitsPerSample);  // BitsPerSample (SHORT)
  writeTiffEntry(s, 0x0103, 3, 1, 1);              // Compression: none
  writeTiffEntry(s, 0x0106, 3, 1, 1);              // PhotometricInterpretation: BlackIsZero
  writeTiffEntry(s, 0x0111, 4, 1, stripOffset);    // StripOffsets
  writeTiffEntry(s, 0x0115, 3, 1, 1);              // SamplesPerPixel
  writeTiffEntry(s, 0x0116, 4, 1, height);         // RowsPerStrip
  writeTiffEntry(s, 0x0117, 4, 1, stripBytes);     // StripByteCounts
  writeTiffEntry(s, 0x010E, 2, descSize, descOffset); // ImageDescription
  s.u32(0);                                         // next IFD = none

  s.bytes(descStr.data(), descStr.size());
  s.bytes(pixels.data(), pixels.size() * sizeof(std::uint16_t));

  std::ofstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  f.write(reinterpret_cast<const char*>(s.data().data()),
          static_cast<std::streamsize>(s.size()));
  return path;
}

void ImageWriter::writeGrey16Tiff(const std::string& path, int width,
                                  int height, const std::uint16_t* pixels,
                                  const std::string& desc) {
  fs::create_directories(fs::path(path).parent_path());
  const std::uint32_t W = static_cast<std::uint32_t>(width);
  const std::uint32_t H = static_cast<std::uint32_t>(height);
  const std::uint32_t stripBytes = W * H * sizeof(std::uint16_t);

  std::string description = desc.empty()
                                ? "LAMDA HyperSpectral Imaging System 16-bit band"
                                : desc;
  description.push_back('\0');
  if (description.size() & 1u) description.push_back('\0');

  const std::uint16_t entries = 12;
  const std::uint32_t ifdOffset = 8;
  const std::uint32_t ifdSize = 2 + 12u * entries + 4;
  const std::uint32_t descOffset = ifdOffset + ifdSize;
  const std::uint32_t descSize = static_cast<std::uint32_t>(description.size());
  const std::uint32_t stripOffset = descOffset + descSize;

  ByteStream s;
  s.u16(0x4949);
  s.u16(42);
  s.u32(ifdOffset);
  s.u16(entries);
  writeTiffEntry(s, 0x00FE, 4, 1, 0);              // NewSubfileType
  writeTiffEntry(s, 0x0100, 4, 1, W);              // ImageWidth
  writeTiffEntry(s, 0x0101, 4, 1, H);              // ImageLength
  writeTiffEntry(s, 0x0102, 3, 1, 16);             // BitsPerSample
  writeTiffEntry(s, 0x0103, 3, 1, 1);              // Compression: none
  writeTiffEntry(s, 0x0106, 3, 1, 1);              // BlackIsZero
  writeTiffEntry(s, 0x0111, 4, 1, stripOffset);    // StripOffsets
  writeTiffEntry(s, 0x0115, 3, 1, 1);              // SamplesPerPixel
  writeTiffEntry(s, 0x0116, 4, 1, H);              // RowsPerStrip
  writeTiffEntry(s, 0x0117, 4, 1, stripBytes);     // StripByteCounts
  writeTiffEntry(s, 0x010E, 2, descSize, descOffset); // ImageDescription
  writeTiffEntry(s, 0x0153, 3, 1, 1);              // SampleFormat = unsigned int
  s.u32(0);

  s.bytes(description.data(), description.size());
  s.bytes(pixels, static_cast<std::size_t>(W) * H * sizeof(std::uint16_t));

  std::ofstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  f.write(reinterpret_cast<const char*>(s.data().data()),
          static_cast<std::streamsize>(s.size()));
}

// ----------------------------------------------------------------------------
// Minimal FITS writer: SIMPLE = T, BITPIX 8/16, 2D image, BIG-ENDIAN data.
// Header in 80-char records, padded to a multiple of 2880 bytes.
// ----------------------------------------------------------------------------

static void fitsRecord(std::string& hdr, const std::string& key,
                       const std::string& value, const std::string& comment) {
  std::string line(80, ' ');
  std::string k = key;
  if (k.size() > 8) k.resize(8);
  while (k.size() < 8) k.push_back(' ');
  std::copy(k.begin(), k.end(), line.begin());
  line[8] = '=';
  line[9] = ' ';
  std::string v = value;
  // Right-justify to col 30 (1-indexed col 30 = index 29)
  if (v.size() < 20) {
    std::string pad(20 - v.size(), ' ');
    v = pad + v;
  }
  std::copy(v.begin(), v.begin() + std::min<std::size_t>(v.size(), 70),
            line.begin() + 10);
  if (!comment.empty()) {
    std::size_t pos = 10 + std::min<std::size_t>(v.size(), 70);
    if (pos < 78) {
      line[pos++] = ' ';
      line[pos++] = '/';
      line[pos++] = ' ';
      for (char c : comment) {
        if (pos >= 80) break;
        line[pos++] = c;
      }
    }
  }
  hdr.append(line);
}

static void fitsString(std::string& hdr, const std::string& key,
                       const std::string& s, const std::string& comment) {
  std::string v = "'" + s + "'";
  if (v.size() < 20) v.append(20 - v.size(), ' ');
  fitsRecord(hdr, key, v, comment);
}

static void fitsEnd(std::string& hdr) {
  std::string line(80, ' ');
  line.replace(0, 3, "END");
  hdr.append(line);
  while (hdr.size() % 2880 != 0) hdr.push_back(' ');
}

std::string ImageWriter::writeFits(const std::string& dir,
                                   const std::string& prefix,
                                   const Frame& frame,
                                   const CaptureMetadata& meta) {
  ensureDir(dir);
  std::string path = composePath(dir, prefix, "fits");

  const bool sixteen = (frame.type == ASI_IMG_RAW16);
  const int bitpix = sixteen ? 16 : 8;

  std::string hdr;
  fitsRecord(hdr, "SIMPLE", "T", "FITS standard");
  fitsRecord(hdr, "BITPIX", std::to_string(bitpix), "bits per pixel");
  fitsRecord(hdr, "NAXIS", "2", "number of axes");
  fitsRecord(hdr, "NAXIS1", std::to_string(frame.width), "image width");
  fitsRecord(hdr, "NAXIS2", std::to_string(frame.height), "image height");
  if (sixteen) fitsRecord(hdr, "BZERO", "32768", "");
  if (sixteen) fitsRecord(hdr, "BSCALE", "1", "");
  fitsString(hdr, "INSTRUME", meta.cameraName, "camera model");
  if (meta.serialNumber) fitsString(hdr, "SERIAL", *meta.serialNumber, "");
  if (meta.exposureUs)
    fitsRecord(hdr, "EXPTIME", std::to_string(*meta.exposureUs / 1e6),
               "exposure time (s)");
  if (meta.gain)        fitsRecord(hdr, "GAIN",    std::to_string(*meta.gain),    "");
  if (meta.offset)      fitsRecord(hdr, "OFFSET",  std::to_string(*meta.offset),  "");
  if (meta.gamma)       fitsRecord(hdr, "GAMMA",   std::to_string(*meta.gamma),   "");
  if (meta.usbBandwidth)fitsRecord(hdr, "USBBW",   std::to_string(*meta.usbBandwidth), "USB bandwidth %");
  if (meta.sensorTempC) fitsRecord(hdr, "CCD-TEMP",std::to_string(*meta.sensorTempC), "sensor temp C");
  if (meta.bitDepth)    fitsRecord(hdr, "BITDEPTH",std::to_string(*meta.bitDepth),"sensor ADC bit depth");
  if (meta.pixelSizeUm) fitsRecord(hdr, "XPIXSZ",  std::to_string(*meta.pixelSizeUm), "pixel size um");
  fitsRecord(hdr, "XBINNING", std::to_string(meta.bin), "");
  fitsRecord(hdr, "YBINNING", std::to_string(meta.bin), "");
  fitsString(hdr, "SDKVERS", meta.sdkVersion, "");
  fitsEnd(hdr);

  std::ofstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  f.write(hdr.data(), static_cast<std::streamsize>(hdr.size()));

  const std::size_t n = static_cast<std::size_t>(frame.width) * frame.height;
  if (sixteen) {
    // FITS BITPIX=16 expects signed big-endian with BZERO=32768 for unsigned data.
    const auto* src = reinterpret_cast<const std::uint16_t*>(frame.data.data());
    std::vector<std::uint8_t> be(n * 2);
    for (std::size_t i = 0; i < n; ++i) {
      std::int32_t signedV = static_cast<std::int32_t>(src[i]) - 32768;
      std::uint16_t u = static_cast<std::uint16_t>(signedV & 0xffff);
      be[i * 2 + 0] = (u >> 8) & 0xff;
      be[i * 2 + 1] = u & 0xff;
    }
    f.write(reinterpret_cast<const char*>(be.data()),
            static_cast<std::streamsize>(be.size()));
    // Pad data block to 2880.
    std::size_t pad = (2880 - (be.size() % 2880)) % 2880;
    if (pad) {
      std::vector<char> zeros(pad, 0);
      f.write(zeros.data(), static_cast<std::streamsize>(pad));
    }
  } else {
    std::vector<std::uint8_t> g8 = toGrey8(frame);
    f.write(reinterpret_cast<const char*>(g8.data()),
            static_cast<std::streamsize>(g8.size()));
    std::size_t pad = (2880 - (g8.size() % 2880)) % 2880;
    if (pad) {
      std::vector<char> zeros(pad, 0);
      f.write(zeros.data(), static_cast<std::streamsize>(pad));
    }
  }
  return path;
}

// ----------------------------------------------------------------------------
// Float TIFF — 32-bit IEEE float greyscale, single strip. Used for calibration
// masters and corrected science frames. Round-trippable via readFloatTiff.
// ----------------------------------------------------------------------------
void ImageWriter::writeFloatTiff(const std::string& path, int width, int height,
                                 const float* pixels, const std::string& desc) {
  ensureDir(fs::path(path).parent_path().string());

  std::string description = desc;
  if (description.empty()) description = "LAMDA HyperSpectral Imaging System float master";
  if (description.size() & 1u) description.push_back('\0');

  const std::uint32_t W = width;
  const std::uint32_t H = height;
  const std::uint32_t stripBytes = W * H * sizeof(float);
  const std::uint16_t entries = 12;
  const std::uint32_t ifdOffset = 8;
  const std::uint32_t ifdSize = 2 + 12u * entries + 4;
  const std::uint32_t descOffset = ifdOffset + ifdSize;
  const std::uint32_t descSize = static_cast<std::uint32_t>(description.size());
  const std::uint32_t stripOffset = descOffset + descSize;

  ByteStream s;
  s.u16(0x4949);
  s.u16(42);
  s.u32(ifdOffset);
  s.u16(entries);
  writeTiffEntry(s, 0x00FE, 4, 1, 0);              // NewSubfileType
  writeTiffEntry(s, 0x0100, 4, 1, W);              // ImageWidth
  writeTiffEntry(s, 0x0101, 4, 1, H);              // ImageLength
  writeTiffEntry(s, 0x0102, 3, 1, 32);             // BitsPerSample = 32
  writeTiffEntry(s, 0x0103, 3, 1, 1);              // Compression: none
  writeTiffEntry(s, 0x0106, 3, 1, 1);              // PhotometricInterpretation
  writeTiffEntry(s, 0x0111, 4, 1, stripOffset);    // StripOffsets
  writeTiffEntry(s, 0x0115, 3, 1, 1);              // SamplesPerPixel
  writeTiffEntry(s, 0x0116, 4, 1, H);              // RowsPerStrip
  writeTiffEntry(s, 0x0117, 4, 1, stripBytes);     // StripByteCounts
  writeTiffEntry(s, 0x010E, 2, descSize, descOffset); // ImageDescription
  writeTiffEntry(s, 0x0153, 3, 1, 3);              // SampleFormat = IEEE float
  s.u32(0);                                         // next IFD = none
  s.bytes(description.data(), description.size());
  s.bytes(pixels, static_cast<std::size_t>(W) * H * sizeof(float));

  std::ofstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  f.write(reinterpret_cast<const char*>(s.data().data()),
          static_cast<std::streamsize>(s.size()));
}

void ImageWriter::readFloatTiff(const std::string& path, int& width,
                                int& height, std::vector<float>& out_pixels) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  std::vector<char> buf((std::istreambuf_iterator<char>(f)),
                        std::istreambuf_iterator<char>());
  if (buf.size() < 16) throw std::runtime_error("truncated TIFF: " + path);

  auto rd16 = [&](std::size_t off) -> std::uint16_t {
    return static_cast<std::uint16_t>(static_cast<std::uint8_t>(buf[off])) |
           (static_cast<std::uint16_t>(static_cast<std::uint8_t>(buf[off + 1])) << 8);
  };
  auto rd32 = [&](std::size_t off) -> std::uint32_t {
    return static_cast<std::uint32_t>(static_cast<std::uint8_t>(buf[off])) |
           (static_cast<std::uint32_t>(static_cast<std::uint8_t>(buf[off + 1])) << 8) |
           (static_cast<std::uint32_t>(static_cast<std::uint8_t>(buf[off + 2])) << 16) |
           (static_cast<std::uint32_t>(static_cast<std::uint8_t>(buf[off + 3])) << 24);
  };

  if (rd16(0) != 0x4949 || rd16(2) != 42)
    throw std::runtime_error("not a little-endian baseline TIFF: " + path);
  std::uint32_t ifd = rd32(4);
  std::uint16_t n = rd16(ifd);
  std::uint32_t W = 0, H = 0, bits = 0, fmt = 1, stripOff = 0, stripBytes = 0;
  for (std::uint16_t i = 0; i < n; ++i) {
    std::size_t e = ifd + 2 + 12u * i;
    std::uint16_t tag = rd16(e);
    std::uint32_t val = rd32(e + 8);
    switch (tag) {
      case 0x0100: W = val; break;
      case 0x0101: H = val; break;
      case 0x0102: bits = val & 0xffff; break;
      case 0x0111: stripOff = val; break;
      case 0x0117: stripBytes = val; break;
      case 0x0153: fmt = val & 0xffff; break;
      default: break;
    }
  }
  width = static_cast<int>(W);
  height = static_cast<int>(H);
  out_pixels.resize(static_cast<std::size_t>(W) * H);
  if (bits == 32 && fmt == 3) {
    if (W * H * sizeof(float) != stripBytes)
      throw std::runtime_error("strip size mismatch: " + path);
    std::memcpy(out_pixels.data(), buf.data() + stripOff, stripBytes);
  } else if (bits == 16 && (fmt == 1 || fmt == 0)) {
    if (W * H * sizeof(std::uint16_t) != stripBytes)
      throw std::runtime_error("strip size mismatch: " + path);
    const auto* p = reinterpret_cast<const std::uint8_t*>(buf.data() + stripOff);
    for (std::size_t i = 0; i < out_pixels.size(); ++i) {
      std::uint16_t v = static_cast<std::uint16_t>(p[2 * i]) |
                        (static_cast<std::uint16_t>(p[2 * i + 1]) << 8);
      out_pixels[i] = static_cast<float>(v);
    }
  } else {
    throw std::runtime_error("unsupported TIFF sample format: " + path);
  }
}

std::string ImageWriter::writePng(const std::string& dir,
                                  const std::string& prefix,
                                  const Frame& frame) {
  ensureDir(dir);
  std::string path = composePath(dir, prefix, "png");
  std::vector<std::uint8_t> g8 = toGrey8(frame);
  if (!stbi_write_png(path.c_str(), frame.width, frame.height, 1, g8.data(),
                      frame.width)) {
    throw std::runtime_error("PNG write failed: " + path);
  }
  return path;
}

}  // namespace mc
