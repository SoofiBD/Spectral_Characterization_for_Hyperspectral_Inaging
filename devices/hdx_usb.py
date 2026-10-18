"""Minimal native USB driver for the Ocean Insight HDX (Ocean Binary Protocol over pyusb).

seabreeze's pyseabreeze transport re-configures and resets the device on open/close, which
leaves the HDX unresponsive on Linux. This driver only claims the interface, so sessions can
be opened and closed repeatedly. It exposes the subset of the seabreeze Spectrometer API that
OceanHDXController uses.
"""

from __future__ import annotations

import struct
import threading

import numpy as np

VENDOR_ID = 0x2457
HDX_PRODUCT_ID = 0x2003
EP_OUT, EP_IN = 0x01, 0x81
HDX_PIXELS = 2068
INT_TIME_MIN_US, INT_TIME_MAX_US = 6_000, 10_000_000

# OBP message types
GET_SERIAL = 0x00000100
GET_RAW_SPECTRUM_HDX = 0x00101000
SET_ITIME_USEC = 0x00110010
GET_WL_COEFF_COUNT = 0x00180100
GET_WL_COEFF = 0x00180101

FLAG_ACK, FLAG_REQUEST_ACK, FLAG_NACK = 0x0002, 0x0004, 0x0008
HEADER = struct.Struct("<HHHHII6sBB16sI")   # 44 bytes
FOOTER = struct.Struct("<16sI")             # 20 bytes
START, VERSION, END = 0xC0C1, 0x1100, 0xC2C3C4C5

OBP_ERRORS = {1: "unsupported protocol", 2: "unknown message type", 3: "bad checksum",
              4: "message too large", 5: "payload length mismatch", 6: "invalid payload",
              7: "device not ready", 8: "unknown checksum type", 9: "device reset",
              10: "too many buses", 11: "out of memory", 12: "command not found"}


class HDXError(Exception):
    pass


class HDXUSB:
    model = "HDX"

    def __init__(self, dev):
        import usb.core
        import usb.util
        self._usb, self._util = usb, usb.util
        self._dev = dev
        self._lock = threading.RLock()
        self._int_us = 100_000
        self._stale_wait_ms = 0  # set after a timeout: wait this long for a late reply to flush
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as exc:
            if exc.errno == 13:
                raise HDXError("USB access denied - install the udev rules "
                               "(sudo .venv/bin/seabreeze_os_setup) and replug the HDX") from exc
            if exc.errno == 16:
                raise HDXError("HDX is in use by another program") from exc
            raise HDXError(f"Cannot claim HDX interface: {exc}") from exc
        self._drain()
        self.serial_number = self._query(GET_SERIAL).split(b"\0")[0].decode("ascii", "replace")

    @classmethod
    def open(cls, serial_number: str | None = None) -> "HDXUSB":
        try:
            import usb.core
        except ImportError as exc:
            raise HDXError("pyusb is not installed") from exc
        try:
            devices = list(usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=HDX_PRODUCT_ID))
        except usb.core.NoBackendError as exc:
            raise HDXError("libusb not found - install libusb-1.0 (apt install libusb-1.0-0)") from exc
        if not devices:
            raise HDXError("No HDX found on USB (vendor 2457, product 2003)")
        errors = []
        for dev in devices:
            try:
                spec = cls(dev)
            except HDXError as exc:
                if "timed out" not in str(exc):
                    errors.append(str(exc))
                    cls._release(dev)
                    continue
                # The HDX can stay unresponsive after an interrupted session (it is not
                # power-cycled by replugging USB when externally powered). A USB reset
                # followed by a few seconds of settling brings it back.
                dev = cls._reset(dev)
                try:
                    spec = cls(dev)
                except Exception as exc2:
                    errors.append(str(exc2))
                    cls._release(dev)
                    continue
            except Exception as exc:
                errors.append(str(exc))
                cls._release(dev)
                continue
            if serial_number is None or spec.serial_number == serial_number:
                return spec
            spec.close()
        raise HDXError("; ".join(errors) or f"HDX with serial {serial_number} not found")

    def _drain(self, timeout_ms: int = 300):
        while True:
            try:
                self._dev.read(EP_IN, 16384, timeout_ms)
            except self._usb.core.USBTimeoutError:
                return
            except self._usb.core.USBError:
                return

    def _send(self, msg_type: int, payload: bytes = b"", request_ack: bool = False):
        if len(payload) <= 16:
            immediate, imm_len, body = payload.ljust(16, b"\0"), len(payload), b""
        else:
            immediate, imm_len, body = b"\0" * 16, 0, payload
        header = HEADER.pack(START, VERSION, FLAG_REQUEST_ACK if request_ack else 0, 0, msg_type,
                             0, b"\0" * 6, 0, imm_len, immediate, len(body) + FOOTER.size)
        self._dev.write(EP_OUT, header + body + FOOTER.pack(b"\0" * 16, END), 2000)

    def _receive_message(self, timeout_ms: int) -> tuple[int, bytes] | None:
        """Read one OBP message. Returns (msg_type, payload), or None for a corrupt frame."""
        buf = bytes(self._dev.read(EP_IN, 16384, timeout_ms))
        while len(buf) < HEADER.size:
            buf += bytes(self._dev.read(EP_IN, 16384, timeout_ms))
        (start, _version, flags, errno, msg_type, _reg, _res, _ck, imm_len, immediate,
         remaining) = HEADER.unpack(buf[:HEADER.size])
        if start != START:
            return None
        total = HEADER.size + remaining
        while len(buf) < total:
            buf += bytes(self._dev.read(EP_IN, 16384, timeout_ms))
        if len(buf) != total or FOOTER.unpack(buf[total - FOOTER.size:total])[1] != END:
            return None  # lost or duplicated USB packet inside a multi-packet reply
        if flags & FLAG_NACK or errno:
            raise HDXError(f"HDX error {errno}: {OBP_ERRORS.get(errno, 'unknown')}")
        payload = immediate[:imm_len] if imm_len else buf[HEADER.size:total - FOOTER.size]
        return msg_type, payload

    def _transact(self, msg_type: int, payload: bytes = b"", request_ack: bool = False,
                  timeout_ms: int = 1000, retries: int = 4) -> bytes:
        """Send a request and return the payload of the matching reply.

        Over some USB paths (seen through a hub) the HDX occasionally loses or repeats a reply
        packet. All messages used here are idempotent, so stale/duplicate replies are discarded
        by message type and a missing reply simply triggers a resend.
        """
        usb_err = self._usb.core.USBError
        with self._lock:
            last_exc = None
            for _ in range(retries):
                if self._stale_wait_ms:
                    self._drain(self._stale_wait_ms)
                    self._stale_wait_ms = 0
                try:
                    self._send(msg_type, payload, request_ack)
                except usb_err as exc:
                    last_exc = exc
                    continue
                deadline = 3
                while deadline:
                    deadline -= 1
                    try:
                        msg = self._receive_message(timeout_ms)
                    except self._usb.core.USBTimeoutError as exc:
                        last_exc = exc
                        # No reply in time. It may still arrive late; it must not be taken as
                        # the answer to a later request (a scan would then record a spectrum
                        # from the previous wavelength). Flush it before the next send.
                        self._stale_wait_ms = timeout_ms
                        break
                    except usb_err as exc:
                        last_exc = exc
                        self._drain()
                        break
                    if msg is None:
                        self._drain()
                        last_exc = HDXError("corrupt reply")
                        break
                    if msg[0] == msg_type:
                        return msg[1]
                    # reply to an earlier request: ignore and keep reading
            raise HDXError(f"No reply to message 0x{msg_type:08x} after {retries} attempts: {last_exc}")

    def _query(self, msg_type: int, payload: bytes = b"", timeout_ms: int = 1000) -> bytes:
        return self._transact(msg_type, payload, timeout_ms=timeout_ms)

    def _command(self, msg_type: int, payload: bytes = b""):
        self._transact(msg_type, payload, request_ack=True)

    def wavelengths(self) -> np.ndarray:
        n = struct.unpack("<B", self._query(GET_WL_COEFF_COUNT)[:1])[0]
        coeffs = [struct.unpack("<f", self._query(GET_WL_COEFF, struct.pack("<B", i))[:4])[0]
                  for i in range(n)]
        idx = np.arange(HDX_PIXELS, dtype=float)
        return sum(c * idx ** i for i, c in enumerate(coeffs))

    def integration_time_micros(self, us: int):
        us = int(us)
        if not INT_TIME_MIN_US <= us <= INT_TIME_MAX_US:
            raise HDXError(f"Integration time {us} us outside {INT_TIME_MIN_US}-{INT_TIME_MAX_US} us")
        self._command(SET_ITIME_USEC, struct.pack("<L", us))
        self._int_us = us

    def intensities(self, correct_dark_counts: bool = False, correct_nonlinearity: bool = False) -> np.ndarray:
        # A lost reply is resent after this timeout, so keep the margin short: with a 3 s margin
        # each dropped USB packet froze live view for >3 s. Measured replies arrive within
        # the exposure time + a few ms.
        raw = self._query(GET_RAW_SPECTRUM_HDX, timeout_ms=self._int_us // 1000 + 300)
        need = HDX_PIXELS * 2
        if len(raw) < need:
            raise HDXError(f"Short spectrum: {len(raw)} bytes, expected {need}")
        # Any metadata precedes the pixel data
        return np.frombuffer(raw[-need:], dtype="<u2").astype(float)

    def close(self):
        self._release(self._dev)

    @classmethod
    def _reset(cls, dev, settle_s: float = 3.0):
        """USB-reset the HDX, wait for it to boot and return a fresh device handle."""
        import time
        import usb.core
        bus, address = dev.bus, dev.address
        cls._release(dev)
        try:
            dev.reset()
        except usb.core.USBError:
            pass
        cls._release(dev)
        time.sleep(settle_s)
        for new in usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=HDX_PRODUCT_ID):
            if (new.bus, new.address) == (bus, address):
                return new
        return usb.core.find(idVendor=VENDOR_ID, idProduct=HDX_PRODUCT_ID) or dev

    @staticmethod
    def _release(dev):
        import usb.util
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass
        usb.util.dispose_resources(dev)
