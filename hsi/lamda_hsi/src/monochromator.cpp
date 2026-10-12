#include "monochromator.h"

#include <chrono>
#include <cstring>
#include <thread>

namespace mc {

namespace {
using ms = std::chrono::milliseconds;
constexpr auto kReplyPollInterval = ms(250);

bool startsWith(const std::string& s, const char* prefix) {
  std::size_t n = std::strlen(prefix);
  return s.size() >= n && std::memcmp(s.data(), prefix, n) == 0;
}

std::optional<double> tail_double(const std::string& s) {
  auto pos = s.find_last_of(':');
  if (pos == std::string::npos) return std::nullopt;
  try {
    return std::stod(s.substr(pos + 1));
  } catch (...) {
    return std::nullopt;
  }
}
}  // namespace

void Monochromator::connect(const std::string& path, int baudrate) {
  std::lock_guard<std::mutex> lock(mutex_);
  port_.open(path, baudrate);
  // Arduino resets when the serial port is opened (DTR pulse). Give the
  // bootloader ~2 s and drain any boot banner. The firmware prints OK:READY
  // when it's ready to take commands.
  std::this_thread::sleep_for(std::chrono::milliseconds(2000));
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (std::chrono::steady_clock::now() < deadline) {
    std::string line = port_.readLine(std::chrono::milliseconds(200));
    if (line.empty()) continue;
    if (line.find("OK:READY") != std::string::npos) break;
  }
  port_.flushInput();
}

void Monochromator::disconnect() {
  std::lock_guard<std::mutex> lock(mutex_);
  port_.close();
}

MoveResult Monochromator::sendAndWait(const std::string& cmd) {
  MoveResult r;
  if (!port_.isOpen()) {
    r.status = MoveResult::Status::NotConnected;
    return r;
  }
  port_.flushInput();
  port_.writeLine(cmd);

  while (!cancelRequested_.load()) {
    std::string line = port_.readLine(kReplyPollInterval);
    if (line.empty()) {
      // Silence is expected while the motor is moving. Keep polling without
      // blocking the GUI; the caller runs this operation on its worker thread.
      continue;
    }

    // Acknowledgements: OK:MOVING / OK:HOMING / OK:RESETTING — just continue.
    if (line == "OK:MOVING" || line == "OK:HOMING" ||
        line == "OK:RESETTING") {
      continue;
    }

    if (startsWith(line, "OK:DONE")) {
      r.status = MoveResult::Status::Done;
      if (auto v = tail_double(line)) {
        r.position = *v;
        position_.store(*v);
      }
      return r;
    }
    if (line == "OK:RESET_DONE" || startsWith(line, "OK:RESET_DONE")) {
      r.status = MoveResult::Status::Done;
      if (auto v = tail_double(line)) {
        r.position = *v;
        position_.store(*v);
      } else {
        r.position = position_.load();
      }
      return r;
    }
    if (startsWith(line, "ERROR")) {
      r.status = MoveResult::Status::Error;
      r.message = line;
      return r;
    }
    // Unknown line — keep waiting.
  }
  r.status = MoveResult::Status::Cancelled;
  r.message = "operation wait cancelled";
  return r;
}

MoveResult Monochromator::goTo(double nm) {
  std::lock_guard<std::mutex> lock(mutex_);
  return sendAndWait("GOTO:" + std::to_string(nm));
}

MoveResult Monochromator::home() {
  std::lock_guard<std::mutex> lock(mutex_);
  return sendAndWait("HOME");
}

MoveResult Monochromator::reset(double nm) {
  std::lock_guard<std::mutex> lock(mutex_);
  MoveResult r = sendAndWait("RESET:" + std::to_string(nm));
  if (r.status == MoveResult::Status::Done) {
    r.position = nm;
    position_.store(nm);
  }
  return r;
}

std::optional<double> Monochromator::queryPosition() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!port_.isOpen()) return std::nullopt;
  port_.flushInput();
  port_.writeLine("POS");
  std::string line = port_.readLine(std::chrono::milliseconds(1500));
  if (line.empty()) return std::nullopt;
  if (startsWith(line, "OK:POS")) {
    if (auto v = tail_double(line)) {
      position_.store(*v);
      return *v;
    }
  }
  return std::nullopt;
}

std::string Monochromator::queryStatus() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!port_.isOpen()) return "(not connected)";
  port_.flushInput();
  port_.writeLine("STATUS");
  return port_.readLine(std::chrono::milliseconds(1500));
}

}  // namespace mc
