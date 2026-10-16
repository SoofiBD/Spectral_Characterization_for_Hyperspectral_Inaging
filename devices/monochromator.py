"""Motorized monochromator controller (Arduino serial protocol).

Protocol (9600 baud by default, newline terminated ASCII):

    GOTO:<nm>   -> OK:MOVING[:<nm>]   ... OK:DONE:<position>
    HOME        -> OK:HOMING          ... OK:DONE:<position>   (goes to position 0)
    RESET:<nm>  -> OK:RESETTING[:<nm>] ... OK:RESET_DONE:0
                   MOVES BACK by <nm>, then calls that position 0 (it does not set a value!)
    SET:<nm>    -> OK:SET:<position>   (firmware v2) define the current position, no motion
    POS         -> OK:POS:<position>
    STATUS      -> free status text
    any         -> ERROR:<message>    on failure
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover - pyserial is a hard requirement, but keep import errors readable
    serial = None


BAUDRATE = 9600  # firmware default (verified on the lab Arduino)
BAUDRATES = (9600, 19200, 38400, 57600, 115200)

# Long scans of the grating can take minutes; these are upper limits, not delays.
MOVE_TIMEOUT_S = 600.0   # GOTO / HOME
RESET_TIMEOUT_S = 120.0  # RESET
SIMULATED_PORT = "SIMULATED"

# Real nm per firmware nm: the grating turns only 0.9882x the commanded distance
# (lab fit 2026-10-16, counter vs firmware 0-800 nm, residual <0.2 nm, no measurable backlash).
# shortcut: corrects the firmware's steps/nm in software; set to 1.0 once the firmware is fixed
# (docs/FIRMWARE_CALIBRATION.md).
NM_SCALE = 0.9882


class MonochromatorError(Exception):
    """Raised for communication failures or ERROR responses from the device."""


@dataclass
class SimulationState:
    """State shared between simulated devices so fake spectra follow the fake grating."""

    wavelength_nm: float = 500.0
    # Systematic grating error: small offset + slope + curvature, typical for a cheap monochromator
    offset_nm: float = 0.35
    slope: float = 0.0012
    curvature: float = -4e-6
    backlash_nm: float = 0.25
    last_direction: int = 1
    light_blocked: bool = False  # simulated shutter, used for background (dark) measurements
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng())

    def true_wavelength(self) -> float:
        """Actual wavelength leaving the exit slit, including mechanical errors."""
        x = self.wavelength_nm - 600.0
        error = self.offset_nm + self.slope * x + self.curvature * x * x
        error += self.backlash_nm * (0.5 if self.last_direction > 0 else -0.5)
        return self.wavelength_nm + error


def list_serial_ports() -> list[str]:
    if serial is None:
        return []
    # skip non-USB pseudo ports (macOS debug-console, Bluetooth) that never answer
    return [p.device for p in serial.tools.list_ports.comports() if p.vid is not None]


class _SimulatedSerial:
    """Mimics the subset of serial.Serial used by the controller."""

    MIN_NM, MAX_NM = 200.0, 1100.0
    SPEED_NM_PER_S = 400.0

    def __init__(self, state: SimulationState):
        self.state = state
        self._out: list[str] = []
        self._ready_at: list[float] = []
        self._offset = 0.0  # simulated RESET shifts the firmware's zero (not modelled optically)
        self.is_open = True

    def reset_input_buffer(self):
        self._out.clear()
        self._ready_at.clear()

    def _push(self, line: str, delay: float = 0.0):
        self._out.append(line)
        self._ready_at.append(time.monotonic() + delay)

    def write(self, data: bytes):
        cmd = data.decode(errors="replace").strip().upper()
        s = self.state
        try:
            if cmd.startswith("GOTO:"):
                target = float(cmd.split(":", 1)[1])
                if not self.MIN_NM <= target <= self.MAX_NM:
                    self._push(f"ERROR:OUT_OF_RANGE:{target:g}")
                    return
                travel = abs(target - s.wavelength_nm) / self.SPEED_NM_PER_S + 0.05
                if target != s.wavelength_nm:
                    s.last_direction = 1 if target > s.wavelength_nm else -1
                s.wavelength_nm = target
                self._push("OK:MOVING")
                self._push(f"OK:DONE:{target:.2f}", travel)
            elif cmd == "HOME":
                travel = abs(s.wavelength_nm - self.MIN_NM) / self.SPEED_NM_PER_S + 0.1
                s.wavelength_nm = self.MIN_NM
                s.last_direction = -1
                self._push("OK:HOMING")
                self._push(f"OK:DONE:{self.MIN_NM:.2f}", travel)
            elif cmd.startswith("RESET:"):
                # firmware semantics: move back by <nm>, then that position becomes 0
                back = float(cmd.split(":", 1)[1])
                self._offset += s.wavelength_nm - back
                s.wavelength_nm = 0.0
                self._push(f"OK:RESETTING:{back:g}")
                self._push("OK:RESET_DONE:0", 0.1 + back / self.SPEED_NM_PER_S)
            elif cmd.startswith("SET:"):
                value = float(cmd.split(":", 1)[1])
                s.wavelength_nm = value
                self._push(f"OK:SET:{value:.2f}")
            elif cmd == "POS":
                self._push(f"OK:POS:{s.wavelength_nm:.2f}")
            elif cmd == "STATUS":
                self._push(f"SIMULATED MONOCHROMATOR | READY | POS={s.wavelength_nm:.2f} nm | "
                           f"RANGE={self.MIN_NM:.0f}-{self.MAX_NM:.0f} nm")
            else:
                self._push(f"ERROR:UNKNOWN_COMMAND:{cmd}")
        except ValueError:
            self._push(f"ERROR:BAD_ARGUMENT:{cmd}")

    def readline(self) -> bytes:
        deadline = time.monotonic() + 0.2  # emulate serial read timeout
        while time.monotonic() < deadline:
            if self._out and time.monotonic() >= self._ready_at[0]:
                self._ready_at.pop(0)
                return (self._out.pop(0) + "\n").encode()
            time.sleep(0.005)
        return b""

    def close(self):
        self.is_open = False


class MonochromatorController:
    """Thread-safe controller. All public methods block; call them from a worker thread."""

    def __init__(self, sim_state: SimulationState | None = None):
        self.sim_state = sim_state or SimulationState()
        self._ser = None
        self._lock = threading.RLock()
        self.port: str | None = None
        self.position_nm: float | None = None  # real nm (NM_SCALE applied)
        self.nm_scale = NM_SCALE
        # Optional hook (direction, text) for logging raw traffic: direction is ">" or "<"
        self.traffic_callback: Callable[[str, str], None] | None = None

    # ------------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    @property
    def is_simulated(self) -> bool:
        return isinstance(self._ser, _SimulatedSerial)

    def connect(self, port: str, simulate: bool = False, timeout: float = 0.2,
                baudrate: int = BAUDRATE):
        with self._lock:
            self.disconnect()
            if simulate or port == SIMULATED_PORT:
                self._ser = _SimulatedSerial(self.sim_state)
                self.port = SIMULATED_PORT
                self.nm_scale = 1.0  # the simulator models its own grating errors
            else:
                if serial is None:
                    raise MonochromatorError("pyserial is not installed")
                if not port:
                    raise MonochromatorError("No COM port selected")
                try:
                    self._ser = serial.Serial(port, baudrate, timeout=timeout,
                                              **({"exclusive": True} if os.name == "posix" else {}))
                except serial.SerialException as exc:
                    raise MonochromatorError(f"Cannot open {port}: {exc}") from exc
                self.port = port
                self.nm_scale = NM_SCALE
                self._wake_arduino()
            self._ser.reset_input_buffer()

    def _wake_arduino(self, boot_s: float = 2.5):
        """Opening the port resets the Arduino. Wait for it to boot, then send a bare newline:
        the bootloader leaves a stray byte (0xFF) in the firmware's line buffer that would
        otherwise corrupt the first real command (seen as 'ERROR:UNKNOWN:\\xffPOS')."""
        time.sleep(boot_s)
        try:
            self._ser.reset_input_buffer()
            self._ser.write(b"\n")
            end = time.monotonic() + 1.0
            while time.monotonic() < end:  # discard the firmware's reply to the empty line
                if not self._ser.readline():
                    break
            self._ser.reset_input_buffer()
        except Exception as exc:
            raise MonochromatorError(f"Monochromator did not start: {exc}") from exc

    def disconnect(self):
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
            self.position_nm = None  # a port reopen resets the Arduino: re-read POS before trusting it
            self.port = None

    # ------------------------------------------------------------------ low level
    def _write(self, cmd: str):
        if not self.is_connected:
            raise MonochromatorError("Monochromator not connected")
        self._emit(">", cmd)
        try:
            self._ser.write((cmd + "\n").encode())
        except Exception as exc:
            raise MonochromatorError(f"Write failed: {exc}") from exc

    def _readline(self) -> str:
        try:
            raw = self._ser.readline()
        except Exception as exc:
            raise MonochromatorError(f"Read failed: {exc}") from exc
        line = raw.decode(errors="replace").strip()
        if line:
            self._emit("<", line)
        return line

    def _emit(self, direction: str, text: str):
        if self.traffic_callback:
            try:
                self.traffic_callback(direction, text)
            except Exception:
                pass

    def query(self, cmd: str, terminators: tuple[str, ...] | None = None,
              timeout: float = 30.0) -> list[str]:
        """Send a command and collect response lines.

        If `terminators` is given, read until a line starting with one of them.
        Otherwise return after the first non-empty line. ERROR lines raise.
        """
        with self._lock:
            if not self.is_connected:
                raise MonochromatorError("Monochromator not connected")
            self._ser.reset_input_buffer()
            self._write(cmd)
            lines: list[str] = []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self._readline()
                if not line:
                    continue
                lines.append(line)
                if line.upper().startswith("ERROR"):
                    raise MonochromatorError(f"Device error for '{cmd}': {line}")
                if terminators is None or line.upper().startswith(terminators):
                    return lines
            raise MonochromatorError(f"Timeout waiting for response to '{cmd}' "
                                     f"(received: {lines or 'nothing'})")

    def _fw(self, nm: float) -> str:
        """Real nm -> firmware nm, formatted for a command."""
        return f"{nm / self.nm_scale:.6g}"

    def _parse_position(self, line: str) -> float:
        """Firmware position in a reply -> real nm."""
        try:
            value = float(line.rsplit(":", 1)[1]) * self.nm_scale
            if not math.isfinite(value):
                raise ValueError("non-finite position")
            return value
        except (IndexError, ValueError) as exc:
            raise MonochromatorError(f"Cannot parse position from '{line}'") from exc

    # ------------------------------------------------------------------ commands
    def goto(self, wavelength_nm: float, timeout: float = MOVE_TIMEOUT_S) -> float:
        lines = self.query(f"GOTO:{self._fw(wavelength_nm)}", ("OK:DONE",), timeout)
        self.position_nm = self._parse_position(lines[-1])
        return self.position_nm

    def home(self, timeout: float = MOVE_TIMEOUT_S) -> float:
        lines = self.query("HOME", ("OK:DONE",), timeout)
        self.position_nm = self._parse_position(lines[-1])
        return self.position_nm

    def set_position(self, wavelength_nm: float) -> float:
        """SET:<nm> (firmware v2): declare the current position without moving."""
        lines = self.query(f"SET:{self._fw(wavelength_nm)}", ("OK:SET",), 5.0)
        self.position_nm = self._parse_position(lines[-1])
        return self.position_nm

    def reset(self, back_nm: float, timeout: float = RESET_TIMEOUT_S) -> float:
        """RESET:<nm>: MOVE BACK by back_nm, then the firmware calls that position 0."""
        lines = self.query(f"RESET:{self._fw(back_nm)}", ("OK:RESET_DONE",), timeout)
        self.position_nm = self._parse_position(lines[-1])
        return self.position_nm

    def get_position(self) -> float:
        lines = self.query("POS", ("OK:POS",), 5.0)
        self.position_nm = self._parse_position(lines[-1])
        return self.position_nm

    def status(self) -> str:
        return self.query("STATUS", None, 5.0)[-1]

    def send_raw(self, cmd: str, timeout: float = MOVE_TIMEOUT_S) -> list[str]:
        """Manual command: routes known commands to their proper terminator.
        GOTO/SET/RESET arguments are real nm, like the panel buttons."""
        cmd = cmd.strip()
        upper = cmd.upper()
        name, sep, arg = cmd.partition(":")
        if sep and name.strip().upper() in ("GOTO", "SET", "RESET"):
            try:
                cmd = f"{name.strip().upper()}:{self._fw(float(arg))}"
            except ValueError:
                pass  # let the firmware reply ERROR for a bad argument
        if upper.startswith("GOTO:") or upper == "HOME":
            lines = self.query(cmd, ("OK:DONE",), timeout)
            self.position_nm = self._parse_position(lines[-1])
        elif upper.startswith("RESET:"):
            lines = self.query(cmd, ("OK:RESET_DONE",), max(timeout, RESET_TIMEOUT_S))
            self.position_nm = self._parse_position(lines[-1])
        elif upper.startswith("SET:"):
            lines = self.query(cmd, ("OK:SET",), 5.0)
            self.position_nm = self._parse_position(lines[-1])
        elif upper == "POS":
            lines = self.query(cmd, ("OK:POS",), 5.0)
            self.position_nm = self._parse_position(lines[-1])
        else:
            lines = self.query(cmd, None, 5.0)
        return lines
