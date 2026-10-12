"""Ocean Insight HDX spectrometer controller (python-seabreeze) with simulation backend."""

from __future__ import annotations

import threading
import time

import numpy as np

from devices.monochromator import SimulationState


class SpectrometerError(Exception):
    pass


class _SimulatedHDX:
    """Generates realistic monochromator line spectra: lamp x grating response, slit-limited
    Gaussian line with wavelength-dependent bandwidth, dark offset, shot + read noise."""

    model = "HDX (simulated)"
    serial_number = "SIM-HDX-0001"

    def __init__(self, state: SimulationState, pixels: int = 2068):
        self.state = state
        self._wl = np.linspace(185.0, 1120.0, pixels)
        self._int_us = 100_000

    def wavelengths(self):
        return self._wl.copy()

    def integration_time_micros(self, us: int):
        self._int_us = int(us)

    def intensities(self, correct_dark_counts=False, correct_nonlinearity=False):
        time.sleep(min(self._int_us / 1e6, 2.0))
        s = self.state
        center = s.true_wavelength()
        # Spectral bandwidth grows slightly with wavelength (fixed slit, angular dispersion)
        fwhm = 2.0 + 1.5e-3 * (center - 400.0)
        sigma = fwhm / 2.3548
        # Halogen lamp (~3000 K blackbody) times grating efficiency
        lam_m = center * 1e-9
        planck = 1.0 / (lam_m ** 5 * (np.exp(1.4388e-2 / (lam_m * 3000.0)) - 1.0))
        planck /= 1.0 / ((900e-9) ** 5 * (np.exp(1.4388e-2 / (900e-9 * 3000.0)) - 1.0))
        grating = np.exp(-0.5 * ((center - 550.0) / 250.0) ** 2)
        amplitude = 45000.0 * planck * grating * (self._int_us / 100_000)
        amplitude = min(amplitude, 60000.0)
        if s.light_blocked:
            amplitude = 0.0
        signal = amplitude * np.exp(-0.5 * ((self._wl - center) / sigma) ** 2)
        # Weak stray light pedestal
        signal += 0.002 * amplitude * np.exp(-0.5 * ((self._wl - center) / 25.0) ** 2)
        rng = s.rng
        shot = rng.normal(0.0, np.sqrt(np.maximum(signal, 0.0)))
        dark = 1500.0 + rng.normal(0.0, 12.0, self._wl.size)
        counts = signal + shot + dark
        if correct_dark_counts:
            counts -= 1500.0
        return np.clip(counts, 0, 65535)

    def close(self):
        pass


_backend_selected: str | None = None


def select_backend(name: str = "pyseabreeze") -> str:
    """Choose the seabreeze backend once per process, before seabreeze.spectrometers is imported.

    pyseabreeze is pure Python (needs pyusb + system libusb). cseabreeze is the compiled
    backend bundled in the seabreeze wheel.
    """
    global _backend_selected
    if _backend_selected is not None:
        return _backend_selected
    import seabreeze
    seabreeze.use(name)
    if name == "pyseabreeze":
        _patch_pyseabreeze()
    _backend_selected = name
    return name


def _patch_pyseabreeze():
    """Work around HDX quirks in pyseabreeze, observed with HDX firmware on USB."""
    from seabreeze.pyseabreeze import devices, transport
    from seabreeze.pyseabreeze.exceptions import SeaBreezeError

    # The HDX does not answer OBP 0x101 (GET_SERIAL_LENGTH); read the serial directly.
    def hdx_serial(self):
        try:
            protocol = self._transport.protocol
        except RuntimeError:
            raise SeaBreezeError("device not open")
        return protocol.query(0x00000100).split(b"\0")[0].decode("utf8", "replace")

    devices.HDX.get_serial_number = hdx_serial

    # Ethernet discovery raises OSError (e.g. "No route to host") when no network path
    # exists; this must not break USB discovery.
    original = transport.IPv4Transport.list_devices.__func__

    def safe_ipv4_list(cls, **kwargs):
        try:
            yield from original(cls, **kwargs)
        except OSError:
            return

    transport.IPv4Transport.list_devices = classmethod(safe_ipv4_list)

    # USB open/close: the stock code re-runs set_configuration() on every open and calls
    # reset() on close. Both leave the HDX bulk endpoints out of sync, so every later query
    # times out. A raw session that only claims the interface works reliably.
    import usb.core
    import usb.util

    def open_device(self, device):
        if not isinstance(device, transport.USBTransportHandle):
            raise TypeError("device needs to be a USBTransportHandle")
        self._device = device
        pyusb_device = device.pyusb_device
        try:
            if pyusb_device.is_kernel_driver_active(0):
                pyusb_device.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            try:
                configured = pyusb_device.get_active_configuration() is not None
            except usb.core.USBError:
                configured = False
            if not configured:
                pyusb_device.set_configuration()
            usb.util.claim_interface(pyusb_device, 0)
        except usb.core.USBError as err:
            if err.errno == 16:
                self._opened = True
                raise transport.USBTransportDeviceInUse("device probably used by another thread/process")
            raise transport.USBTransportError.from_usberror(err)
        # Drop replies left over from an earlier, interrupted session
        ep_in = self._endpoint_map.lowspeed_in
        while True:
            try:
                pyusb_device.read(ep_in, 512, 50)
            except usb.core.USBError:
                break
        sizes = {ep.bEndpointAddress: ep.wMaxPacketSize
                 for intf in pyusb_device.get_active_configuration() for ep in intf.endpoints()}
        # This HDX exposes only 0x01/0x81; route the "high speed" reads to 0x81 as well
        for mode, ep_name in list(self._read_endpoints.items()):
            if getattr(self._endpoint_map, ep_name, None) not in sizes:
                self._read_endpoints[mode] = "lowspeed_in"
                self._default_read_size[mode] = 512
        # Reads must be at least one full USB packet, otherwise libusb raises "Overflow"
        # when the device sends a 512-byte packet (e.g. a spectrum) into a 64-byte buffer.
        for mode, ep_name in self._read_endpoints.items():
            ep = getattr(self._endpoint_map, ep_name, None)
            if ep in sizes:
                self._default_read_size[mode] = max(self._default_read_size[mode], sizes[ep])
        self._opened = True
        self._protocol = self._protocol_cls(self)

    def close_handle(self):
        try:
            usb.util.release_interface(self.pyusb_device, 0)
        except Exception:
            pass
        usb.util.dispose_resources(self.pyusb_device)

    transport.USBTransport.open_device = open_device
    transport.USBTransportHandle.close = close_handle


class OceanHDXController:
    """Thread-safe wrapper around seabreeze.spectrometers.Spectrometer."""

    MIN_INT_MS = 6.0  # HDX hardware minimum (seabreeze: 6000 us)
    MAX_INT_MS = 10_000.0

    def __init__(self, sim_state: SimulationState | None = None, backend: str = "native"):
        self.backend = backend
        self.sim_state = sim_state or SimulationState()
        self._spec = None
        self._lock = threading.RLock()
        self._wavelengths: np.ndarray | None = None
        self.integration_time_ms = 100.0
        self.correct_dark = False
        # Background (dark) spectrum, valid only for the integration time it was taken at
        self.dark: np.ndarray | None = None
        self.dark_scans: list[np.ndarray] = []
        self.dark_integration_ms: float | None = None
        self.subtract_dark = True

    @property
    def is_connected(self) -> bool:
        return self._spec is not None

    @property
    def is_simulated(self) -> bool:
        return isinstance(self._spec, _SimulatedHDX)

    @property
    def device_info(self) -> str:
        if not self._spec:
            return "Not connected"
        return f"{getattr(self._spec, 'model', 'Unknown')} S/N {getattr(self._spec, 'serial_number', '?')}"

    @property
    def wavelengths(self) -> np.ndarray:
        return self._wavelengths.copy()

    def connect(self, simulate: bool = False, serial_number: str | None = None):
        with self._lock:
            self.disconnect()
            self.clear_dark()  # a background belongs to one device session
            if simulate:
                self._spec = _SimulatedHDX(self.sim_state)
            elif self.backend == "native":
                from devices.hdx_usb import HDXUSB
                try:
                    self._spec = HDXUSB.open(serial_number)
                except Exception as exc:
                    raise SpectrometerError(f"HDX connection failed: {exc}") from exc
                self.correct_dark = False
            else:
                try:
                    select_backend(self.backend)
                    from seabreeze.spectrometers import Spectrometer
                except ImportError as exc:
                    raise SpectrometerError(
                        f"seabreeze backend '{self.backend}' unavailable: {exc}. For pyseabreeze "
                        "install pyusb and the system libusb-1.0 package.") from exc
                try:
                    if serial_number:
                        self._spec = Spectrometer.from_serial_number(serial_number)
                    else:
                        self._spec = Spectrometer.from_first_available()
                except Exception as exc:
                    raise SpectrometerError(f"No Ocean spectrometer found: {exc}") from exc
                # Seabreeze wraps the HDX in a different class; dark correction support varies
                self.correct_dark = False
            self._wavelengths = np.asarray(self._spec.wavelengths(), dtype=float)
            self.set_integration_time(self.integration_time_ms)

    def disconnect(self):
        with self._lock:
            if self._spec is not None:
                try:
                    self._spec.close()
                except Exception:
                    pass
            self._spec = None

    def set_integration_time(self, ms: float):
        if not self.MIN_INT_MS <= ms <= self.MAX_INT_MS:
            raise SpectrometerError(f"Integration time {ms} ms out of range "
                                    f"({self.MIN_INT_MS}-{self.MAX_INT_MS} ms)")
        with self._lock:
            self.integration_time_ms = float(ms)
            if self._spec is not None:
                try:
                    self._spec.integration_time_micros(int(round(ms * 1000)))
                except Exception as exc:
                    raise SpectrometerError(f"Failed to set integration time: {exc}") from exc

    @property
    def dark_valid(self) -> bool:
        return (self.dark is not None and self.dark_integration_ms is not None
                and abs(self.dark_integration_ms - self.integration_time_ms) < 1e-6)

    @property
    def dark_active(self) -> bool:
        """True when acquired spectra are background-corrected."""
        return self.subtract_dark and self.dark_valid

    def acquire_raw(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (wavelength_nm, intensity_counts) without background correction."""
        with self._lock:
            if self._spec is None:
                raise SpectrometerError("Spectrometer not connected")
            try:
                intensities = np.asarray(
                    self._spec.intensities(correct_dark_counts=self.correct_dark), dtype=float)
            except Exception as exc:
                raise SpectrometerError(f"Acquisition failed: {exc}") from exc
            return self._wavelengths.copy(), intensities

    def acquire_spectrum(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (wavelength_nm, intensity), background-subtracted when a valid dark exists."""
        with self._lock:
            wl, inten = self.acquire_raw()
            if self.dark_active:
                inten = inten - self.dark
            return wl, inten

    def acquire_dark(self, n: int = 10) -> np.ndarray:
        """Average n raw spectra with the light blocked and store them as the background."""
        with self._lock:
            scans = [self.acquire_raw()[1] for _ in range(max(1, int(n)))]
            self.dark_scans = scans
            self.dark = np.mean(scans, axis=0)
            self.dark_integration_ms = self.integration_time_ms
            return self.dark.copy()

    def clear_dark(self):
        with self._lock:
            self.dark = None
            self.dark_scans = []
            self.dark_integration_ms = None

    def acquire_averaged(self, n: int) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
        """Average n scans. Also returns individual scans for repeatability analysis."""
        scans = []
        wl = None
        for _ in range(max(1, int(n))):
            wl, inten = self.acquire_spectrum()
            scans.append(inten)
        return wl, np.mean(scans, axis=0), scans
