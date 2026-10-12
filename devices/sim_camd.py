"""Simulated asi_camd: same stdin/stdout protocol, synthetic RAW16 frames. Used in simulation mode
and tests so the real process boundary (pipes, signals, files) is exercised without a camera."""

import json
import os
import signal
import sys
import time

import numpy as np

MAX_W, MAX_H = 3008, 3008  # ASI533MM Pro sensor
aborted = False
exposing = False


def on_signal(*_):
    global aborted
    aborted = True
    if not exposing:
        sys.exit(0)


def reply(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main():
    global exposing
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    rng = np.random.default_rng()
    cfg = None
    reply("READY " + json.dumps({"model": "ZWO ASI533MM Pro (simulated)", "serial": "SIM0000",
                                 "sdk": "sim", "max_width": MAX_W, "max_height": MAX_H,
                                 "bit_depth": 14, "pixel_um": 3.76}))
    for line in sys.stdin:
        cmd, _, rest = line.strip().partition(" ")
        try:
            if cmd == "QUIT":
                break
            if cmd == "CONFIG":
                exp, gain, offset, binning, x, y, w, h = map(int, rest.split())
                w = w or (MAX_W // binning) & ~7
                h = h or (MAX_H // binning) & ~1
                cfg = {"exposure_us": exp, "gain": gain, "offset": offset, "bin": binning,
                       "roi": [x, y, w, h], "format": "RAW16", "temperature_c": 21.5}
                reply("OK " + json.dumps(cfg))
            elif cmd == "EXPOSE":
                if cfg is None:
                    raise RuntimeError("CONFIG first (RAW16 required)")
                end = time.monotonic() + cfg["exposure_us"] / 1e6
                exposing = True
                while time.monotonic() < end and not aborted:
                    time.sleep(0.005)
                exposing = False
                if aborted:
                    raise RuntimeError("aborted")
                _, _, w, h = cfg["roi"]
                frame = rng.poisson(200 + cfg["offset"], size=(h, w)).astype("<u2")
                tmp = rest + ".tmp"
                with open(tmp, "wb") as f:
                    np.save(f, frame)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, rest)
                reply("OK " + json.dumps({"width": w, "height": h,
                                          "exposure_us": cfg["exposure_us"],
                                          "temperature_c": 21.5, "dropped_frames": 0}))
            else:
                raise RuntimeError(f"unknown command: {cmd}")
        except Exception as exc:  # protocol: every failure becomes one ERROR line
            reply(f"ERROR {exc}")
            if aborted:
                break


if __name__ == "__main__":
    main()
