"""HDX USB diagnostic. Run with the app closed:  .venv/bin/python tests/hdx_diag.py

1) cseabreeze backend (compiled, bundled in the seabreeze wheel)
2) raw OBP 'get serial number' over pyusb with a long timeout, timing each reply
"""

import struct
import subprocess
import sys
import time

print("=== 1) cseabreeze backend ===")
code = ("import seabreeze; seabreeze.use('cseabreeze');"
        "from seabreeze.spectrometers import list_devices, Spectrometer;"
        "d=list_devices(); print('devices:', d);"
        "s=Spectrometer(d[0]) if d else None;"
        "s and s.integration_time_micros(20000);"
        "s and print('spectrum OK, max counts', s.intensities().max())")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
print((r.stdout + r.stderr).strip()[-800:])

print("\n=== 2) raw pyusb, 10 s timeout ===")
import usb.core
import usb.util

dev = usb.core.find(idVendor=0x2457, idProduct=0x2003)
if dev is None:
    sys.exit("HDX (2457:2003) not found on USB")
print("found HDX; interfaces:",
      [(i.bInterfaceNumber, [hex(e.bEndpointAddress) for e in i]) for i in dev.get_active_configuration()])
try:
    if dev.is_kernel_driver_active(0):
        print("kernel driver active -> detaching")
        dev.detach_kernel_driver(0)
except Exception as exc:
    print("kernel driver check:", exc)
usb.util.claim_interface(dev, 0)

# Drain anything left over from earlier failed attempts
while True:
    try:
        stale = dev.read(0x81, 512, 300)
        print("drained stale packet:", len(stale), "bytes")
    except usb.core.USBTimeoutError:
        break

msg = struct.pack("<HHHHI I 6s B B 16s I 16s I", 0xC0C1, 0x1100, 0, 0, 0x100, 0, b"\0" * 6,
                  0, 0, b"\0" * 16, 20, b"\0" * 16, 0xC2C3C4C5)
for n in range(4):
    t0 = time.monotonic()
    dev.write(0x01, msg, 1000)
    try:
        reply = dev.read(0x81, 512, 10_000)
        print(f"query {n}: reply {len(reply)} B after {time.monotonic() - t0:.2f} s ->",
              bytes(reply[24:40]).split(b"\0")[0])
    except usb.core.USBTimeoutError:
        print(f"query {n}: NO reply within 10 s")
usb.util.release_interface(dev, 0)
