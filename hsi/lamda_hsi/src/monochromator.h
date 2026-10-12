#pragma once

#include "serial_port.h"

#include <atomic>
#include <chrono>
#include <mutex>
#include <optional>
#include <string>

namespace mc {

// Timing model from the Python controller — same Arduino firmware.
//   t (s) = a + b * |distance_nm|
constexpr double kMonoModelA = 0.8219;
constexpr double kMonoModelB = 0.201113;

inline double monoEstimateSeconds(double distanceNm) {
  return kMonoModelA + kMonoModelB * (distanceNm < 0 ? -distanceNm : distanceNm);
}

struct MoveResult {
  enum class Status { Done, Error, Cancelled, NoResponse, NotConnected };
  Status status = Status::NotConnected;
  double position = 0.0;
  std::string message;
};

// Wraps the line-based protocol of the LAMDA monochromator firmware:
//   GOTO:<nm>   -> OK:MOVING -> ... -> OK:DONE:<position>
//   HOME        -> OK:HOMING -> OK:DONE:<position>
//   RESET:<nm>  -> OK:RESETTING -> OK:RESET_DONE
//   POS         -> OK:POS:<position>
//   STATUS      -> OK:STATUS:...
//   <anything>  -> ERROR:...
//
// All public methods are thread-safe via an internal mutex; only one command
// can be in flight at a time.
class Monochromator {
public:
  Monochromator() = default;
  ~Monochromator() {
    cancelPendingOperation();
    disconnect();
  }

  void connect(const std::string& port, int baudrate = 9600);
  void disconnect();
  bool connected() const { return port_.isOpen(); }
  const std::string& portName() const noexcept { return port_.path(); }

  // Last known position (updated by every successful operation).
  double position() const noexcept { return position_.load(); }

  MoveResult goTo(double nm);
  MoveResult home();
  MoveResult reset(double nm);
  void prepareOperation() noexcept { cancelRequested_.store(false); }
  void cancelPendingOperation() noexcept { cancelRequested_.store(true); }

  // Returns the position read from the device, or nullopt if no/error reply.
  std::optional<double> queryPosition();

  // Free-form STATUS query (returns the raw response line).
  std::string queryStatus();

private:
  MoveResult sendAndWait(const std::string& cmd);

  mutable std::mutex mutex_;
  SerialPort port_;
  std::atomic<double> position_{0.0};
  std::atomic<bool> cancelRequested_{false};
};

}  // namespace mc
