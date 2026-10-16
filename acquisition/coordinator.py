"""Scan coordinator: the single owner of the monochromator during a scan.

Per point: GOTO -> OK:DONE (with timeout) -> check the reported position -> settle -> optional HDX
raw exposures -> camera RAW16 frame -> written and hashed -> manifest updated -> point done.
The camera is the primary channel; the HDX is optional (hdx=None). At least one channel is required.
Acquisition is sequential (HDX, then camera); simultaneous exposure is not implemented.
"""

from __future__ import annotations

import platform
import math
import subprocess
import threading
import copy
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Callable

import numpy as np

from acquisition.camera_measurement import check_raw16
from acquisition.session import Session, save_spectra, utc_now
from devices.asi_camera import CameraError
from devices.contracts import Camera, Spectrometer, scan_access
from devices.monochromator import MOVE_TIMEOUT_S, MonochromatorController, MonochromatorError

POSITION_TOLERANCE_NM = 0.5  # reported vs target; lab firmware reports within ~0.01 nm


class State(str, Enum):
    IDLE = "Idle"
    PREPARING = "Preparing"
    MOVING = "Moving"
    SETTLING = "Settling"
    ACQUIRING = "Acquiring"
    SAVING = "Saving"
    COMPLETE = "Complete"
    STOPPING = "Stopping"
    ABORTED = "Aborted"
    FAILED = "Failed"


class ScanAborted(Exception):
    pass


def scan_plan(start: float, stop: float, step: float, backward: bool) -> list[tuple[str, float]]:
    if not all(math.isfinite(x) for x in (start, stop, step)) or step <= 0 or stop < start:
        raise ValueError("Invalid scan range or step")
    n = int(np.floor(round((stop - start) / step, 9))) + 1
    fwd = np.round(start + step * np.arange(n), 6)
    plan = [("forward", float(w)) for w in fwd]
    if backward:  # the turnaround point is measured once: repeating it would not be a backward approach
        plan += [("backward", float(w)) for w in fwd[-2::-1]]
    return plan


class ScanCoordinator:
    def __init__(self, mono: MonochromatorController, hdx: Spectrometer | None, params: dict,
                 session_dir: Path, camera: Camera | None = None,
                 prompt: Callable[[str], bool] | None = None,
                 on_state: Callable[[State, str], None] | None = None,
                 on_point: Callable[[dict, np.ndarray, list], None] | None = None):
        self.mono, self.hdx, self.camera, self.p = mono, hdx, camera, params
        self.session_dir = session_dir
        self.prompt = prompt
        self.on_state = on_state or (lambda state, detail: None)
        self.on_point = on_point or (lambda point, wl, raw: None)
        self.state = State.IDLE
        self.session: Session | None = None
        self._camera_dark: dict | None = None
        self._abort = False
        self._stop = threading.Event()

    def abort(self):
        """Stop before the next move/exposure; a running camera exposure is cancelled."""
        self._abort = True
        self._stop.set()
        self._set(State.STOPPING, "Waiting for active device I/O; motor motion is not physically stopped")
        if self.camera is not None:
            self.camera.abort()

    def _set(self, state: State, detail: str = ""):
        self.state = state
        self.on_state(state, detail)

    def _check_abort(self):
        if self._abort:
            raise ScanAborted

    # ------------------------------------------------------------------ run
    @property
    def channels(self) -> list[str]:
        return [name for name, dev in (("camera", self.camera), ("hdx", self.hdx)) if dev is not None]

    @property
    def _simulated(self) -> bool:
        return self.mono.is_simulated  # all devices share one mode (checked before the session starts)

    def run(self) -> Session:
        with scan_access(self.mono, *([self.hdx] if self.hdx is not None else [])):
            return self._run_owned()

    def _run_owned(self) -> Session:
        """Run the scan. Returns the session; raises on failure (manifest says "failed")."""
        if not self.channels:
            raise ValueError("Select at least one acquisition channel (camera or HDX)")
        modes = {self.mono.is_simulated}
        if self.hdx is not None:
            modes.add(self.hdx.is_simulated)
        if self.camera is not None:
            modes.add(self.camera.simulated)
        if len(modes) != 1:
            raise ValueError("Mixed real/simulated devices are not allowed")
        scan_plan(self.p["start"], self.p["stop"], self.p["step"], self.p["backward"])
        if not math.isfinite(self.p["settle_s"]) or self.p["settle_s"] < 0:
            raise ValueError("Invalid settling time")
        if self.hdx is not None and any(not isinstance(self.p[k], int) or self.p[k] < 1
                                        for k in ("averages", "dark_averages")):
            raise ValueError("Exposure counts must be positive")
        for key, default in (("position_tolerance_nm", POSITION_TOLERANCE_NM), ("move_timeout_s", MOVE_TIMEOUT_S)):
            if not math.isfinite(self.p.get(key, default)) or self.p.get(key, default) < 0:
                raise ValueError(f"Invalid {key}")
        if self.p.get("kind") == "white" and not self.p["measure_dark"]:
            raise ValueError("A white-reference run requires a fresh dark measurement")
        self._set(State.PREPARING)
        self.session = Session(self.session_dir, self._session_info())
        try:
            self._check_abort()
            self._run()
            self._check_abort()
        except Exception as exc:
            if self._abort and isinstance(exc, (ScanAborted, CameraError)):
                self._set(State.STOPPING)
                self.session.finish("aborted")
                self._set(State.ABORTED, f"{len(self.session.manifest['points'])} points kept")
                return self.session
            self.session.finish("failed", str(exc))
            self._set(State.FAILED, str(exc))
            raise
        self.session.finish("complete")
        self._set(State.COMPLETE)
        return self.session

    def _run(self):
        p, s = self.p, self.session
        if self.hdx is not None:
            self.hdx.set_integration_time(p["integration_ms"])
        if self.camera is not None:
            c = p["camera"]
            s.manifest["camera_settings"] = self.camera.configure(
                c["exposure_us"], c["gain"], c["offset"], c["bin"], tuple(c.get("roi", (0, 0, 0, 0))))
        if self.hdx is not None:  # HDX dark state; the camera dark is recorded as references.camera_dark
            s.manifest["parameters"]["dark"] = ("measured before scan" if p["measure_dark"] else
                                                "existing dark" if self.hdx.dark_active else "none (raw counts)")
        else:
            s.manifest["parameters"]["dark"] = "camera dark measured before scan" if p["measure_dark"] else "none (raw counts)"
        s.checkpoint()
        if p["measure_dark"]:
            self._measure_dark()
        else:
            self._record_references()
        if p.get("kind") == "white" and not self._simulated:
            self._confirm("Place the uniform white reference in the measurement plane, then press OK.")
        plan = scan_plan(p["start"], p["stop"], p["step"], p["backward"])
        # Approach the start from below so the forward scan has a consistent backlash direction
        self._check_abort()
        self._set(State.MOVING, "pre-positioning")
        target = max(plan[0][1] - 2 * p["step"], 200.0)
        reported, timing = self._move(target)
        s.manifest["preposition"] = {"target_nm": target, "reported_nm": reported, **timing}
        s.checkpoint()
        for i, (direction, w) in enumerate(plan):
            self._point(i, direction, w)

    def _move(self, target):
        started, clock = utc_now(), time.monotonic()
        reported = self.mono.goto(target, self.p.get("move_timeout_s", MOVE_TIMEOUT_S))
        tolerance = self.p.get("position_tolerance_nm", POSITION_TOLERANCE_NM)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Invalid position tolerance")
        if not math.isfinite(reported) or abs(reported - target) > tolerance:
            raise MonochromatorError(f"Reported position {reported:g} nm differs from target {target:g} nm")
        return reported, {"started_utc": started, "finished_utc": utc_now(),
                          "duration_s": time.monotonic() - clock}

    def _point(self, i: int, direction: str, w: float):
        p, s = self.p, self.session
        self._check_abort()
        self._set(State.MOVING, f"{direction}: GOTO {w:g} nm")
        reported, move = self._move(w)
        self._check_abort()
        self._set(State.SETTLING, f"{direction}: {w:g} nm")
        self._stop.wait(p["settle_s"])
        self._check_abort()
        self._set(State.ACQUIRING, f"{direction}: {w:g} nm ({' + '.join(self.channels)})")
        stem = f"{i:04d}_{direction}_{w:g}nm"
        point = {"index": i, "status": "partial", "direction": direction, "target_nm": w,
                 "reported_nm": reported, "move": move, "started_utc": utc_now(),
                 "settle_s": p["settle_s"], "references": list(s.manifest["references"])}
        if self.hdx is not None:
            point["hdx_exposures"] = []
        s.add_point(point)
        raw, wl = [], None
        if self.hdx is not None:
            self._hdx_point(point, stem, w, reported, raw)
            wl = self.hdx.wavelengths
        self._check_abort()
        if self.camera is not None:
            if self.camera.simulated and hasattr(self.camera, "set_scene"):
                self.camera.set_scene(w, False, p.get("kind") == "white")
            started = utc_now()
            frame = self.camera.expose(s.root / f"{stem}.npy")
            meta = check_raw16(s.root / f"{stem}.npy", frame, self.camera.info.get("max_intensity"))
            point["camera"] = {**frame, **meta, **s.record(f"{stem}.npy"),
                               "started_utc": started, "finished_utc": utc_now()}
            s.checkpoint()
        self._check_abort()
        self._set(State.SAVING, f"{direction}: {w:g} nm")
        point.update(status="complete", finished_utc=utc_now())
        s.checkpoint()
        self.on_point(point, wl, raw)

    def _hdx_point(self, point: dict, stem: str, w: float, reported: float, raw: list):
        p, s = self.p, self.session
        wl = self.hdx.wavelengths
        for j in range(p["averages"]):
            self._check_abort()
            started = utc_now()
            axis, counts = self.hdx.acquire_raw()
            if not np.array_equal(axis, wl) or counts.shape != wl.shape:
                raise ValueError("Spectrometer wavelength axis or shape changed during scan")
            finished = utc_now()
            name = f"{stem}_exposure{j:04d}.npz"
            save_spectra(s.root / name, wavelength_nm=wl, raw_scans=counts[None, :],
                         integration_ms=self.hdx.integration_time_ms, captured_at_utc=started)
            point["hdx_exposures"].append({**s.record(name), "started_utc": started,
                                            "finished_utc": finished})
            s.checkpoint()
            raw.append(counts)
        save_spectra(s.root / f"{stem}.npz", wavelength_nm=wl, raw_scans=np.asarray(raw),
                     set_wavelength_nm=w, direction=point["direction"], reported_position_nm=reported,
                     integration_ms=self.hdx.integration_time_ms, captured_at_utc=point["started_utc"])
        point["hdx"] = {**s.record(f"{stem}.npz"), "integration_ms": self.hdx.integration_time_ms,
                        "n_scans": len(raw)}
        s.checkpoint()

    # ------------------------------------------------------------------ references
    def _measure_dark(self):
        p = self.p
        sim = (self.hdx if self.hdx is not None else self.mono).sim_state if self._simulated else None
        if sim is None:
            self._confirm("Background measurement: block the light (close the shutter or turn the "
                          "lamp off), then press OK.")
        self._set(State.ACQUIRING, "background: " + " + ".join(self.channels))
        if sim:
            sim.light_blocked = True
        try:
            if self.hdx is not None:
                self._measure_hdx_dark()
            if self.camera is not None:
                self._check_abort()
                if self.camera.simulated and hasattr(self.camera, "set_scene"):
                    self.camera.set_scene(p["start"], True, False)
                self._camera_dark = self.camera.expose(self.session.root / "camera_dark.npy")
                self._camera_dark.update(check_raw16(self.session.root / "camera_dark.npy", self._camera_dark,
                                                     self.camera.info.get("max_intensity")))
                self._record_references()
        finally:
            if sim:
                sim.light_blocked = False
        if sim is None:
            self._confirm("Background stored. Restore the light, then press OK to start the scan.")

    def _measure_hdx_dark(self):
        p = self.p
        scans = []
        s = self.session
        s.manifest["dark_exposures"] = []
        for i in range(p["dark_averages"]):
            self._check_abort()
            started = utc_now()
            wl, counts = self.hdx.acquire_raw()
            name = f"dark_exposure{i:04d}.npz"
            save_spectra(s.root / name, wavelength_nm=wl, raw_scans=counts[None, :],
                         integration_ms=self.hdx.integration_time_ms, captured_at_utc=started)
            s.manifest["dark_exposures"].append(s.record(name))
            s.checkpoint()
            scans.append(counts)
        self.hdx.dark_scans = scans
        self.hdx.dark = np.mean(scans, axis=0)
        self.hdx.dark_integration_ms = self.hdx.integration_time_ms
        self.hdx.dark_captured_utc = utc_now()
        self._record_references()

    def _confirm(self, text: str):
        """Operator step; Cancel ends the scan as aborted (nothing measured is lost)."""
        if self.prompt is None:
            raise RuntimeError("A real reference measurement requires operator confirmation")
        self._check_abort()
        if not self.prompt(text):
            self._abort = True
        self._check_abort()

    def _record_references(self):
        s = self.session
        if self.hdx is not None and self.hdx.dark_active and "hdx_dark" not in s.manifest["references"]:
            save_spectra(s.root / "dark.npz", wavelength_nm=self.hdx.wavelengths,
                         raw_scans=np.asarray(self.hdx.dark_scans), mean_raw=self.hdx.dark)
            s.set_reference("hdx_dark", {**s.record("dark.npz"),
                                         "captured_utc": getattr(self.hdx, "dark_captured_utc", None),
                                         "integration_ms": self.hdx.dark_integration_ms,
                                         "n_scans": len(self.hdx.dark_scans),
                                         "measured_in_this_session": bool(self.p["measure_dark"])})
        if self._camera_dark is not None and "camera_dark" not in s.manifest["references"]:
            # Settings travel with the reference so analysis can refuse a mismatched dark
            s.set_reference("camera_dark", {**self._camera_dark, **s.record("camera_dark.npy"),
                                            "settings": s.manifest["camera_settings"]})
        # White (flat) references are separate measurements and are not taken by this scan.

    def _session_info(self) -> dict:
        try:
            mono_status = self.mono.status()
        except MonochromatorError as exc:
            mono_status = f"unavailable: {exc}"
        devices = {"monochromator": {"port": self.mono.port, "simulated": self.mono.is_simulated,
                                     "nm_scale": self.mono.nm_scale, "status": mono_status}}
        if self.hdx is not None:
            devices["spectrometer"] = {"info": self.hdx.device_info, "backend": self.hdx.backend,
                                       "simulated": self.hdx.is_simulated, "max_intensity": self.hdx.max_intensity}
        if self.camera is not None:
            devices["camera"] = {**self.camera.info, "simulated": self.camera.simulated}
        try:
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[1], text=True, stderr=subprocess.DEVNULL).strip()
            dirty = bool(subprocess.check_output(["git", "diff", "HEAD", "--"],
                cwd=Path(__file__).resolve().parents[1], stderr=subprocess.DEVNULL))
        except (OSError, subprocess.CalledProcessError):
            revision, dirty = None, None
        return {"parameters": copy.deepcopy(self.p), "channels": self.channels, "devices": devices,
                "software": {"revision": revision, "dirty": dirty, "python": sys.version.split()[0], "numpy": np.__version__,
                             "platform": platform.platform()}}
