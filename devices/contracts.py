"""Driver contracts. Acquisition uses these interfaces, not vendor-specific SDKs."""
from contextlib import contextmanager
from importlib import import_module
from typing import Protocol
from pathlib import Path
import numpy as np


class Camera(Protocol):
    simulated: bool
    info: dict
    settings: dict | None
    is_running: bool  # False once the device session ended (closed, aborted, crashed, unplugged)

    def configure(self, exposure_us: int, gain: int, offset: int, binning: int,
                  roi: tuple[int, int, int, int]) -> dict: ...
    def expose(self, path: Path) -> dict: ...
    def abort(self): ...
    def close(self): ...


class Spectrometer(Protocol):
    is_simulated: bool
    is_connected: bool
    device_info: str
    backend: str
    wavelengths: np.ndarray
    integration_time_ms: float
    MIN_INT_MS: float
    MAX_INT_MS: float
    max_intensity: float
    _lock: object  # reentrant lock; all hardware methods must use it
    sim_state: object
    dark: np.ndarray | None
    dark_scans: list[np.ndarray]
    dark_integration_ms: float | None
    dark_captured_utc: str | None
    dark_active: bool
    dark_valid: bool
    subtract_dark: bool

    def connect(self, simulate: bool = False, serial_number: str | None = None): ...
    def disconnect(self): ...
    def clear_dark(self): ...
    def acquire_dark(self, n: int) -> np.ndarray: ...
    def acquire_spectrum(self) -> tuple[np.ndarray, np.ndarray]: ...

    def set_integration_time(self, ms: float): ...
    def acquire_raw(self) -> tuple[np.ndarray, np.ndarray]: ...


@contextmanager
def scan_access(*devices):
    """Hold controller locks for the whole scan; fail instead of queueing behind manual I/O."""
    held = []
    try:
        for device in devices:
            if not device._lock.acquire(blocking=False):
                raise RuntimeError("Device is busy; stop live/manual acquisition before scanning")
            held.append(device._lock)
        yield
    finally:
        for lock in reversed(held):
            lock.release()


def driver_factory(spec: str):
    """Load explicitly configured local driver code (module:factory). Never read this from a manifest."""
    module, separator, name = spec.partition(":")
    if not separator or not module or not name:
        raise ValueError("Driver must be module:factory")
    factory = getattr(import_module(module), name)
    if not callable(factory):
        raise ValueError("Driver factory is not callable")
    return factory
