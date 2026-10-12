"""Client for the asi_camd camera process (ZWO ASI via the C++ SDK wrapper).

The camera runs in its own process so the ASI SDK and full-resolution frames stay out of the GUI
process. Frames are written by that process straight into the session directory as RAW16 .npy;
only short status lines travel over the pipe. Protocol: see hsi/lamda_hsi/src/asi_camd.cpp.
"""

from __future__ import annotations

import json
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CAMD_EXE = BASE_DIR / "hsi" / "lamda_hsi" / "build" / "asi_camd"
SIM_CAMD = [sys.executable, str(Path(__file__).with_name("sim_camd.py"))]

STARTUP_TIMEOUT_S = 15.0
COMMAND_TIMEOUT_S = 10.0
READOUT_MARGIN_S = 30.0  # added to the exposure time before an EXPOSE counts as hung


class CameraError(Exception):
    """Camera process failure, ERROR reply, or timeout."""


class ASICameraClient:
    """One camera process per scan. Methods block; call them from a worker thread."""

    def __init__(self, simulate: bool = False, log_path: Path | None = None, index: int = 0):
        cmd = SIM_CAMD if simulate else [str(CAMD_EXE)]
        if not simulate and not CAMD_EXE.exists():
            raise CameraError(f"{CAMD_EXE} not found. Build it: cmake -S hsi/lamda_hsi -B "
                              "hsi/lamda_hsi/build -DLAMDA_HSI_BUILD_GUI=OFF && "
                              "cmake --build hsi/lamda_hsi/build --target asi_camd")
        self.simulated = simulate
        self._log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self.settings: dict | None = None
        try:
            self._proc = subprocess.Popen(cmd + [str(index)], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=self._log, text=True,
                                          errors="replace", bufsize=1)
        except OSError as exc:
            self._close_log()
            raise CameraError(f"Cannot start camera process: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        try:
            self.info = self._reply("READY", STARTUP_TIMEOUT_S)
        except CameraError:
            self.close()
            raise

    def _read_stdout(self):
        try:
            for line in self._proc.stdout:
                self._lines.put(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)  # EOF: process exited

    def _close_log(self):
        if self._log is not subprocess.DEVNULL:
            self._log.close()

    def _reply(self, expect: str, timeout: float) -> dict:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise CameraError(f"Camera did not answer within {timeout:g} s") from None
        if line is None:
            raise CameraError(f"Camera process exited (code {self._proc.wait()})")
        head, _, body = line.partition(" ")
        if head == "ERROR":
            raise CameraError(body or "unknown camera error")
        try:
            if head != expect:
                raise ValueError
            return json.loads(body)
        except ValueError:  # protocol out of step: later replies could pair with the wrong command
            self.close()
            raise CameraError(f"Unexpected camera reply: {line!r}") from None

    def _command(self, line: str, timeout: float) -> dict:
        with self._lock:
            if self._proc.poll() is not None:
                raise CameraError("Camera process is not running")
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                raise CameraError(f"Camera pipe closed: {exc}") from exc
            return self._reply("OK", timeout)

    def configure(self, exposure_us: int, gain: int, offset: int, binning: int = 1,
                  roi: tuple[int, int, int, int] = (0, 0, 0, 0)) -> dict:
        """Apply settings; returns what the camera actually applied (RAW16 always)."""
        x, y, w, h = roi
        self.settings = self._command(
            f"CONFIG {int(exposure_us)} {int(gain)} {int(offset)} {int(binning)} {x} {y} {w} {h}",
            COMMAND_TIMEOUT_S)
        return self.settings

    def expose(self, path: Path) -> dict:
        """Take one exposure and write it to `path` (.npy, uint16). Returns frame metadata."""
        if self.settings is None:
            raise CameraError("Camera not configured")
        if "\n" in str(path):
            raise CameraError("Invalid frame path")
        timeout = self.settings["exposure_us"] / 1e6 + READOUT_MARGIN_S
        return self._command(f"EXPOSE {path}", timeout)

    def abort(self):
        """Stop a running exposure and end the process; open a new client for the next scan."""
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)

    def close(self):
        if self._proc.poll() is None:
            try:
                self._proc.stdin.write("QUIT\n")
                self._proc.stdin.flush()
            except OSError:
                pass
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        self._close_log()
