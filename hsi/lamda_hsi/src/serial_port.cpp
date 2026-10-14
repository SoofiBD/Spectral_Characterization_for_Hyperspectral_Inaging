#include "serial_port.h"

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <system_error>

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/file.h>
#include <termios.h>
#include <unistd.h>

namespace mc {

namespace fs = std::filesystem;

namespace {
speed_t baudConst(int b) {
  switch (b) {
    case 9600:   return B9600;
    case 19200:  return B19200;
    case 38400:  return B38400;
    case 57600:  return B57600;
    case 115200: return B115200;
    case 230400: return B230400;
    default:     return B9600;
  }
}
}  // namespace

SerialPort::~SerialPort() { close(); }

void SerialPort::open(const std::string& path, int baudrate) {
  close();
  int fd = ::open(path.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
  if (fd < 0) {
    throw std::runtime_error("serial open failed: " + path + ": " +
                             std::strerror(errno));
  }

  if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
    ::close(fd);
    throw std::runtime_error("serial port already owned: " + path);
  }

  // Some USB-CDC adapters (Arduino) report EIO until DTR settles; we clear
  // the non-blocking flag for write but keep the descriptor available.
  int flags = fcntl(fd, F_GETFL, 0);
  fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);

  termios tio{};
  if (tcgetattr(fd, &tio) < 0) {
    ::close(fd);
    throw std::runtime_error("tcgetattr failed: " + path);
  }

  cfmakeraw(&tio);
  tio.c_cflag |= (CLOCAL | CREAD);
  tio.c_cflag &= ~CRTSCTS;
  tio.c_cflag &= ~CSIZE;
  tio.c_cflag |= CS8;          // 8 data bits
  tio.c_cflag &= ~PARENB;      // no parity
  tio.c_cflag &= ~CSTOPB;      // 1 stop bit
  tio.c_iflag &= ~(IXON | IXOFF | IXANY);
  tio.c_cc[VMIN] = 0;          // non-blocking read
  tio.c_cc[VTIME] = 0;

  speed_t b = baudConst(baudrate);
  cfsetispeed(&tio, b);
  cfsetospeed(&tio, b);

  if (tcsetattr(fd, TCSANOW, &tio) < 0) {
    ::close(fd);
    throw std::runtime_error("tcsetattr failed: " + path);
  }
  tcflush(fd, TCIOFLUSH);

  fd_ = fd;
  path_ = path;
  rxBuf_.clear();
}

void SerialPort::close() {
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
    path_.clear();
    rxBuf_.clear();
  }
}

void SerialPort::writeLine(const std::string& s) {
  if (fd_ < 0) throw std::runtime_error("serial not open");
  std::string out = s;
  out.push_back('\n');
  const char* p = out.data();
  std::size_t left = out.size();
  while (left > 0) {
    ssize_t n = ::write(fd_, p, left);
    if (n < 0) {
      if (errno == EINTR) continue;
      throw std::runtime_error(std::string("serial write failed: ") +
                               std::strerror(errno));
    }
    p += n;
    left -= n;
  }
}

std::string SerialPort::readLine(std::chrono::milliseconds timeout) {
  if (fd_ < 0) throw std::runtime_error("serial not open");
  using clock = std::chrono::steady_clock;
  auto deadline = clock::now() + timeout;

  for (;;) {
    // Look for a newline in the existing buffer.
    auto pos = rxBuf_.find('\n');
    if (pos != std::string::npos) {
      std::string line = rxBuf_.substr(0, pos);
      rxBuf_.erase(0, pos + 1);
      if (!line.empty() && line.back() == '\r') line.pop_back();
      return line;
    }

    auto now = clock::now();
    if (now >= deadline) return {};

    auto remainMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                        deadline - now)
                        .count();
    pollfd pfd{fd_, POLLIN, 0};
    int prc = ::poll(&pfd, 1, static_cast<int>(remainMs));
    if (prc < 0) {
      if (errno == EINTR) continue;
      throw std::runtime_error("serial poll failed");
    }
    if (prc == 0) return {};

    char buf[256];
    ssize_t n = ::read(fd_, buf, sizeof(buf));
    if (n < 0) {
      if (errno == EINTR || errno == EAGAIN) continue;
      throw std::runtime_error(std::string("serial read failed: ") +
                               std::strerror(errno));
    }
    if (n == 0) return {};
    rxBuf_.append(buf, buf + n);
  }
}

void SerialPort::flushInput() {
  if (fd_ < 0) return;
  tcflush(fd_, TCIFLUSH);
  rxBuf_.clear();
}

std::vector<std::string> SerialPort::listPorts() {
  std::vector<std::string> out;
  std::error_code ec;
  if (!fs::exists("/dev", ec)) return out;
  static const char* prefixes[] = {
#ifdef __APPLE__
      "cu.usbmodem", "cu.usbserial", "cu.SLAB", "cu.wchusb",
#else
      "ttyACM", "ttyUSB",
#endif
  };
  for (const auto& e : fs::directory_iterator("/dev", ec)) {
    if (ec) break;
    std::string name = e.path().filename().string();
    for (const char* pre : prefixes) {
      if (name.rfind(pre, 0) == 0) {
        out.push_back(e.path().string());
        break;
      }
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

}  // namespace mc
