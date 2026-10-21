// asi_camd: headless ZWO ASI camera process driven by the Python scan coordinator.
//
// Line protocol on stdin/stdout (one request, one reply):
//   startup                                   -> READY {json camera info} | ERROR <msg> (then exit)
//   CONFIG <exp_us> <gain> <offset> <bin> <x> <y> <w> <h>
//                                             -> OK {json applied settings}   (w/h = 0: full frame)
//   EXPOSE <path>                             -> OK {json frame info}         (RAW16 written as .npy)
//   QUIT                                      -> exits
// Any failure replies "ERROR <msg>"; a failed CONFIG/EXPOSE after which the SDK enumerates no
// camera adds "USB device disappeared" and the process exits (code 2); one after which the SDK
// reports the camera handle closed/removed adds "camera handle lost" and exits (code 3). SIGTERM/SIGINT aborts a running exposure, replies
// "ERROR aborted" and exits. This process never touches the monochromator serial port.

#include "camera.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <filesystem>
#include <fcntl.h>
#include <cstdlib>
#include <iostream>
#include <pthread.h>
#include <sstream>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

std::atomic<bool> g_abort{false};
FILE* g_proto = nullptr;  // protocol channel: the original stdout; fd 1 is redirected to stderr

void reply(const std::string& line) {
  std::fprintf(g_proto, "%s\n", line.c_str());
  std::fflush(g_proto);
}

// SIGTERM/SIGINT are blocked in every thread (SDK/libusb threads inherit the mask) and taken
// here, so the abort reliably sets the flag. close(0) is best effort for an idle read; the
// client also sends QUIT and kills the process after 5 s.
void* signalThread(void* arg) {
  sigset_t* set = static_cast<sigset_t*>(arg);
  int sig = 0;
  sigwait(set, &sig);
  g_abort = true;
  close(0);
  return nullptr;
}

std::string jsonEscape(const std::string& s) {
  std::string out;
  for (char c : s) {
    if (c == '"' || c == '\\') out += '\\';
    if (static_cast<unsigned char>(c) >= 0x20) out += c;
  }
  return out;
}

std::string tempJson(const mc::Camera& cam) {
  auto t = cam.temperatureCelsius();
  return t ? std::to_string(*t) : "null";
}

// Minimal NPY v1.0 writer (little-endian uint16, C order), written to <path>.tmp then renamed
// so a reader never sees a partial frame.
void writeNpy(const std::string& path, const std::vector<std::uint8_t>& data, int w, int h) {
  std::string header = "{'descr': '<u2', 'fortran_order': False, 'shape': (" +
                       std::to_string(h) + ", " + std::to_string(w) + "), }";
  std::size_t total = 10 + header.size() + 1;
  header.append((64 - total % 64) % 64, ' ');
  header += '\n';
  const std::string tmp = path + ".tmp";
  FILE* f = std::fopen(tmp.c_str(), "wbx");
  if (!f) throw std::runtime_error("cannot open " + tmp);
  const unsigned short hlen = static_cast<unsigned short>(header.size());
  const unsigned char preamble[10] = {0x93, 'N', 'U', 'M', 'P', 'Y', 1, 0,
                                      static_cast<unsigned char>(hlen & 0xff),
                                      static_cast<unsigned char>(hlen >> 8)};
  bool ok = std::fwrite(preamble, 1, 10, f) == 10 &&
            std::fwrite(header.data(), 1, header.size(), f) == header.size() &&
            std::fwrite(data.data(), 1, data.size(), f) == data.size() &&
            std::fflush(f) == 0 && fsync(fileno(f)) == 0;
  ok = (std::fclose(f) == 0) && ok;
  if (!ok || ::link(tmp.c_str(), path.c_str()) != 0) {
    std::remove(tmp.c_str());
    throw std::runtime_error("write failed: " + path);
  }
  std::remove(tmp.c_str());
  const auto parent = std::filesystem::path(path).parent_path();
  int dir = ::open(parent.empty() ? "." : parent.c_str(), O_RDONLY);
  if (dir < 0) throw std::runtime_error("cannot sync frame directory");
  const int synced = fsync(dir);
  ::close(dir);
  if (synced != 0) throw std::runtime_error("cannot sync frame directory");
}

std::string configure(mc::Camera& cam, std::istringstream& args) {
  long expUs, gain, offset;
  int bin, x, y, w, h;
  if (!(args >> expUs >> gain >> offset >> bin >> x >> y >> w >> h))
    throw std::runtime_error("CONFIG needs: exp_us gain offset bin x y w h");
  if (bin < 1 || expUs < 1 || x < 0 || y < 0 || w < 0 || h < 0)
    throw std::runtime_error("CONFIG values out of range");
  const auto& info = cam.info();
  if (info.IsColorCam == ASI_TRUE)
    throw std::runtime_error("Quantitative acquisition requires a monochrome RAW16 camera");
  if (w == 0) w = (static_cast<int>(info.MaxWidth) / bin) & ~7;   // ASI: width % 8 == 0
  if (h == 0) h = (static_cast<int>(info.MaxHeight) / bin) & ~1;  // ASI: height % 2 == 0
  cam.setRoi({w, h, bin, ASI_IMG_RAW16});
  cam.setStartPos({x, y});
  const auto setControl = [&cam](ASI_CONTROL_TYPE type, long value) {
    try {
      cam.setControl(type, value);
    } catch (const mc::AsiError& e) {
      throw std::runtime_error(std::string(mc::Camera::controlName(type)) + " value " +
                               std::to_string(value) + " rejected: " + e.what());
    }
  };
  setControl(ASI_EXPOSURE, expUs);
  setControl(ASI_GAIN, gain);
  setControl(ASI_OFFSET, offset);
  // Report what the camera actually applied, not what was requested
  const mc::RoiFormat r = cam.getRoi();
  const mc::StartPos p = cam.getStartPos();
  std::ostringstream o;
  o << "{\"exposure_us\": " << cam.getControl(ASI_EXPOSURE).value
    << ", \"gain\": " << cam.getControl(ASI_GAIN).value
    << ", \"offset\": " << cam.getControl(ASI_OFFSET).value << ", \"bin\": " << r.bin
    << ", \"roi\": [" << p.x << ", " << p.y << ", " << r.width << ", " << r.height << "]"
    << ", \"format\": \"" << mc::Camera::imageTypeName(r.imageType) << "\""
    << ", \"temperature_c\": " << tempJson(cam) << "}";
  return o.str();
}

std::string expose(mc::Camera& cam, const std::string& path) {
  const mc::RoiFormat r = cam.getRoi();
  if (r.imageType != ASI_IMG_RAW16) throw std::runtime_error("CONFIG first (RAW16 required)");
  std::vector<std::uint8_t> buf(cam.frameBytes());
  if (g_abort) throw std::runtime_error("aborted");
  cam.startExposure(false);
  for (;;) {
    if (g_abort) {
      cam.stopExposure();
      throw std::runtime_error("aborted");
    }
    const ASI_EXPOSURE_STATUS s = cam.expStatus();
    if (s == ASI_EXP_SUCCESS) break;
    if (s == ASI_EXP_FAILED) throw std::runtime_error("exposure failed");
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  cam.getDataAfterExp(buf.data(), buf.size());
  writeNpy(path, buf, r.width, r.height);
  std::ostringstream o;
  o << "{\"width\": " << r.width << ", \"height\": " << r.height
    << ", \"exposure_us\": " << cam.getControl(ASI_EXPOSURE).value
    << ", \"temperature_c\": " << tempJson(cam) << ", \"dropped_frames\": " << cam.droppedFrames()
    << "}";
  return o.str();
}

// After a failed command: ASI error code if the SDK says this camera handle is closed or removed, else 0.
int handleLost(const mc::Camera& cam) {
  try {
    cam.getRoi();
  } catch (const mc::AsiError& e) {
    if (e.code() == ASI_ERROR_CAMERA_CLOSED || e.code() == ASI_ERROR_CAMERA_REMOVED) return e.code();
  }
  return 0;
}

// Startup diagnostics go to stderr (camera.log): what the SDK enumerated before any open.
void logEnumeration(const mc::CameraManager& mgr) {
  std::fprintf(stderr, "asi_camd: SDK %s, ASIGetNumOfConnectedCameras = %d\n",
               mc::CameraManager::sdkVersion().c_str(), mgr.reportedCount());
  const auto& devices = mgr.devices();
  for (std::size_t i = 0; i < devices.size(); ++i) {
    const auto& d = devices[i];
    std::fprintf(stderr, "asi_camd:   [%zu] %s ID %d, %ldx%ld, %d bit, USB3 camera %s, USB3 host %s\n", i,
                 d.Name, d.CameraID, d.MaxWidth, d.MaxHeight, d.BitDepth,
                 d.IsUSB3Camera == ASI_TRUE ? "yes" : "no", d.IsUSB3Host == ASI_TRUE ? "yes" : "no");
  }
  std::fflush(stderr);
}

}  // namespace

int main(int argc, char** argv) {
  // Keep SDK/libusb chatter off the protocol channel: it goes to stderr (camera.log).
  g_proto = fdopen(dup(1), "w");
  dup2(2, 1);
  if (!g_proto) return 1;

  static sigset_t sigs;
  sigemptyset(&sigs);
  sigaddset(&sigs, SIGTERM);
  sigaddset(&sigs, SIGINT);
  pthread_sigmask(SIG_BLOCK, &sigs, nullptr);  // before the SDK starts any thread
  pthread_t sigThread;
  pthread_create(&sigThread, nullptr, signalThread, &sigs);
  pthread_detach(sigThread);

  const int index = argc > 1 ? std::atoi(argv[1]) : 0;
  std::unique_ptr<mc::CameraManager> mgr;
  mc::Camera* cam = nullptr;
  std::string where;  // camera being opened, for the error reply
  try {
    mgr = std::make_unique<mc::CameraManager>();
    logEnumeration(*mgr);
    if (mgr->devices().empty())
      throw std::runtime_error("no ASI camera found (SDK reported " + std::to_string(mgr->reportedCount()) +
                               " connected)");
    if (index >= 0 && index < static_cast<int>(mgr->devices().size())) {
      const auto& d = mgr->devices()[index];
      where = std::string(" [") + d.Name + ", index " + std::to_string(index) + ", ID " +
              std::to_string(d.CameraID) + ", USB3 host " + (d.IsUSB3Host == ASI_TRUE ? "yes" : "no") + "]";
    }
    cam = mgr->open(index);
    cam->stopVideoCapture();
  } catch (const std::exception& e) {
    std::fprintf(stderr, "asi_camd: startup failed: %s%s\n", e.what(), where.c_str());
    std::fflush(stderr);
    reply(std::string("ERROR ") + e.what() + where);
    return 1;
  }
  const auto& info = cam->info();
  if (info.BitDepth < 1 || info.BitDepth > 16) {
    reply("ERROR unsupported ADC bit depth");
    return 1;
  }
  const unsigned rawMaximum = ((1u << info.BitDepth) - 1u) << (16 - info.BitDepth);
  std::ostringstream ready;
  ready << "{\"protocol_version\": 2, \"model\": \"" << jsonEscape(info.Name) << "\", \"serial\": \""
        << jsonEscape(cam->serialNumber().value_or("")) << "\", \"sdk\": \""
        << jsonEscape(mc::CameraManager::sdkVersion()) << "\", \"max_width\": " << info.MaxWidth
        << ", \"max_height\": " << info.MaxHeight << ", \"bit_depth\": " << info.BitDepth
        << ", \"max_intensity\": " << rawMaximum
        << ", \"raw16_alignment\": \"msb\""
        << ", \"pixel_um\": " << info.PixelSize << "}";
  reply("READY " + ready.str());

  std::string line;
  while (!g_abort && std::getline(std::cin, line)) {
    std::istringstream args(line);
    std::string cmd;
    args >> cmd;
    if (cmd == "QUIT") break;
    try {
      if (cmd == "CONFIG") {
        reply("OK " + configure(*cam, args));
      } else if (cmd == "EXPOSE") {
        std::string path;
        std::getline(args >> std::ws, path);
        if (path.empty()) throw std::runtime_error("EXPOSE needs a path");
        reply("OK " + expose(*cam, path));
      } else {
        throw std::runtime_error("unknown command: " + cmd);
      }
    } catch (const std::exception& e) {
      if (g_abort) {
        reply(std::string("ERROR ") + e.what());
        break;
      }
      // Device check after a failed camera command. The SDK no longer enumerating a camera means the
      // USB device is gone; a handle the SDK reports as closed/removed can never work again. Either
      // is reported once and the process exits; the client releases it (no retry, no new process).
      std::string fatal;
      int code = 0;
      if (cmd == "CONFIG" || cmd == "EXPOSE") {
        if (ASIGetNumOfConnectedCameras() == 0) {
          fatal = "USB device disappeared (SDK reports 0 cameras connected)";
          code = 2;
        } else if (const int lost = handleLost(*cam)) {
          fatal = "camera handle lost: SDK reports the camera " +
                  std::string(lost == ASI_ERROR_CAMERA_CLOSED ? "closed" : "removed") + " (ASI error " +
                  std::to_string(lost) + "); reconnect";
          code = 3;
        }
      }
      std::fprintf(stderr, "asi_camd: %s failed: %s%s\n", cmd.c_str(), e.what(),
                   fatal.empty() ? "" : ("; " + fatal + ", exiting").c_str());
      std::fflush(stderr);
      reply(std::string("ERROR ") + e.what() + (fatal.empty() ? "" : "; " + fatal));
      if (code) {
        mgr->close();
        return code;
      }
    }
  }
  mgr->close();
  return 0;
}
