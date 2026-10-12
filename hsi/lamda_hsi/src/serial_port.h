#pragma once

#include <chrono>
#include <string>
#include <vector>

namespace mc {

// Minimal POSIX serial port wrapper. Line-oriented protocol assumed.
class SerialPort {
public:
  SerialPort() = default;
  ~SerialPort();
  SerialPort(const SerialPort&) = delete;
  SerialPort& operator=(const SerialPort&) = delete;

  // Open the device file at @baudrate. Throws std::runtime_error on failure.
  // Common Arduino baud rates: 9600, 115200.
  void open(const std::string& path, int baudrate);
  void close();
  bool isOpen() const noexcept { return fd_ >= 0; }
  const std::string& path() const noexcept { return path_; }

  // Write the string, followed by '\n'. Throws on I/O error.
  void writeLine(const std::string& s);

  // Return the next '\n'-terminated line (without the newline), or empty
  // string if no full line arrived before the timeout. Trailing '\r' is
  // stripped.
  std::string readLine(std::chrono::milliseconds timeout);

  // Discard pending input bytes.
  void flushInput();

  // List likely serial devices on this machine (best-effort, cross-platform):
  //   macOS  -> /dev/cu.usbmodem*, /dev/cu.usbserial*, /dev/cu.SLAB*
  //   Linux  -> /dev/ttyACM*, /dev/ttyUSB*
  static std::vector<std::string> listPorts();

private:
  int fd_ = -1;
  std::string path_;
  std::string rxBuf_;
};

}  // namespace mc
