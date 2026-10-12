// asi_camd: headless ZWO ASI camera process driven by the Python scan coordinator.
//
// Line protocol on stdin/stdout (one request, one reply):
//   startup                                   -> READY {json camera info} | ERROR <msg> (then exit)
//   CONFIG <exp_us> <gain> <offset> <bin> <x> <y> <w> <h>
//                                             -> OK {json applied settings}   (w/h = 0: full frame)
//   EXPOSE <path>                             -> OK {json frame info}         (RAW16 written as .npy)
//   QUIT                                      -> exits
// Any failure replies "ERROR <msg>". SIGTERM/SIGINT aborts a running exposure, replies
// "ERROR aborted" and exits. This process never touches the monochromator serial port.

#include "camera.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
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
  FILE* f = std::fopen(tmp.c_str(), "wb");
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
  if (!ok || std::rename(tmp.c_str(), path.c_str()) != 0) {
    std::remove(tmp.c_str());
    throw std::runtime_error("write failed: " + path);
  }
}

std::string configure(mc::Camera& cam, std::istringstream& args) {
  long expUs, gain, offset;
  int bin, x, y, w, h;
  if (!(args >> expUs >> gain >> offset >> bin >> x >> y >> w >> h))
    throw std::runtime_error("CONFIG needs: exp_us gain offset bin x y w h");
  if (bin < 1 || expUs < 1 || x < 0 || y < 0 || w < 0 || h < 0)
    throw std::runtime_error("CONFIG values out of range");
  const auto& info = cam.info();
  if (w == 0) w = (static_cast<int>(info.MaxWidth) / bin) & ~7;   // ASI: width % 8 == 0
  if (h == 0) h = (static_cast<int>(info.MaxHeight) / bin) & ~1;  // ASI: height % 2 == 0
  cam.setRoi({w, h, bin, ASI_IMG_RAW16});
  cam.setStartPos({x, y});
  cam.setControl(ASI_EXPOSURE, expUs);
  cam.setControl(ASI_GAIN, gain);
  cam.setControl(ASI_OFFSET, offset);
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
  try {
    mgr = std::make_unique<mc::CameraManager>();
    if (mgr->devices().empty()) throw std::runtime_error("no ASI camera found");
    cam = mgr->open(index);
    cam->stopVideoCapture();
  } catch (const std::exception& e) {
    reply(std::string("ERROR ") + e.what());
    return 1;
  }
  const auto& info = cam->info();
  std::ostringstream ready;
  ready << "{\"model\": \"" << jsonEscape(info.Name) << "\", \"serial\": \""
        << jsonEscape(cam->serialNumber().value_or("")) << "\", \"sdk\": \""
        << jsonEscape(mc::CameraManager::sdkVersion()) << "\", \"max_width\": " << info.MaxWidth
        << ", \"max_height\": " << info.MaxHeight << ", \"bit_depth\": " << info.BitDepth
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
      reply(std::string("ERROR ") + e.what());
      if (g_abort) break;
    }
  }
  mgr->close();
  return 0;
}
