"""Scan coordinator: the single owner of the monochromator during a scan.

Per point: GOTO -> OK:DONE (with timeout) -> check the reported position -> settle -> HDX raw
exposures -> camera RAW16 frame -> both written and hashed -> manifest updated -> point done.
Acquisition is sequential (HDX, then camera); simultaneous exposure is not implemented.
"""

from __future__ import annotations

import platform
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Callable

import numpy as np

from acquisition.session import Session, save_spectra, utc_now
from devices.asi_camera import ASICameraClient, CameraError
from devices.monochromator import MOVE_TIMEOUT_S, MonochromatorController, MonochromatorError
from devices.ocean_hdx import OceanHDXController

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
    n = int(np.floor(round((stop - start) / step, 9))) + 1
    fwd = np.round(start + step * np.arange(n), 6)
    plan = [("forward", float(w)) for w in fwd]
    if backward:  # the turnaround point is measured once: repeating it would not be a backward approach
        plan += [("backward", float(w)) for w in fwd[-2::-1]]
    return plan


class ScanCoordinator:
    def __init__(self, mono: MonochromatorController, hdx: OceanHDXController, params: dict,
                 session_dir: Path, camera: ASICameraClient | None = None,
                 prompt: Callable[[str], bool] | None = None,
                 on_state: Callable[[State, str], None] | None = None,
                 on_point: Callable[[dict, np.ndarray, list], None] | None = None):
        self.mono, self.hdx, self.camera, self.p = mono, hdx, camera, params
        self.session_dir = session_dir
        self.prompt = prompt or (lambda text: True)
        self.on_state = on_state or (lambda state, detail: None)
        self.on_point = on_point or (lambda point, wl, raw: None)
        self.state = State.IDLE
        self.session: Session | None = None
        self._camera_dark: dict | None = None
        self._abort = False

    def abort(self):
        """Stop before the next move/exposure; a running camera exposure is cancelled."""
        self._abort = True
        if self.camera is not None:
            self.camera.abort()

    def _set(self, state: State, detail: str = ""):
        self.state = state
        self.on_state(state, detail)

    def _check_abort(self):
        if self._abort:
            raise ScanAborted

    # ------------------------------------------------------------------ run
    def run(self) -> Session:
        """Run the scan. Returns the session; raises on failure (manifest says "failed")."""
        self._set(State.PREPARING)
        self.session = Session(self.session_dir, self._session_info())
        try:
            self._run()
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
        self.hdx.set_integration_time(p["integration_ms"])
        if self.camera is not None:
            c = p["camera"]
            s.manifest["camera_settings"] = self.camera.configure(
                c["exposure_us"], c["gain"], c["offset"], c["bin"], tuple(c.get("roi", (0, 0, 0, 0))))
        if p["measure_dark"]:
            self._measure_dark()
        self._record_references()
        plan = scan_plan(p["start"], p["stop"], p["step"], p["backward"])
        # Approach the start from below so the forward scan has a consistent backlash direction
        self._check_abort()
        self._set(State.MOVING, "pre-positioning")
        self.mono.goto(max(plan[0][1] - 2 * p["step"], 200.0), p.get("move_timeout_s", MOVE_TIMEOUT_S))
        for i, (direction, w) in enumerate(plan):
            self._point(i, direction, w)

    def _point(self, i: int, direction: str, w: float):
        p, s = self.p, self.session
        self._check_abort()
        self._set(State.MOVING, f"{direction}: GOTO {w:g} nm")
        reported = self.mono.goto(w, p.get("move_timeout_s", MOVE_TIMEOUT_S))
        tol = p.get("position_tolerance_nm", POSITION_TOLERANCE_NM)
        if abs(reported - w) > tol:
            raise MonochromatorError(f"Reported position {reported:g} nm differs from target "
                                     f"{w:g} nm by more than {tol:g} nm")
        self._set(State.SETTLING, f"{direction}: {w:g} nm")
        time.sleep(p["settle_s"])
        self._check_abort()
        self._set(State.ACQUIRING, f"{direction}: {p['averages']}× at {w:g} nm")
        started = utc_now()
        raw = [self.hdx.acquire_raw()[1] for _ in range(p["averages"])]
        wl = self.hdx.wavelengths
        stem = f"{i:04d}_{direction}_{w:g}nm"
        frame = None
        if self.camera is not None:
            self._check_abort()
            frame = self.camera.expose(s.root / f"{stem}.npy")
        self._set(State.SAVING, f"{direction}: {w:g} nm")
        save_spectra(s.root / f"{stem}.npz", wavelength_nm=wl, raw_scans=np.asarray(raw),
                     set_wavelength_nm=w, direction=direction, reported_position_nm=reported,
                     integration_ms=p["integration_ms"], captured_at_utc=started)
        point = {"index": i, "direction": direction, "target_nm": w, "reported_nm": reported,
                 "started_utc": started, "finished_utc": utc_now(), "settle_s": p["settle_s"],
                 "hdx": {**s.record(f"{stem}.npz"), "integration_ms": p["integration_ms"],
                         "n_scans": len(raw)},
                 "references": [r for r in ("hdx_dark", "camera_dark") if r in s.manifest["references"]]}
        if frame is not None:
            point["camera"] = {**s.record(f"{stem}.npy"), **frame}
        s.add_point(point)
        self.on_point(point, wl, raw)

    # ------------------------------------------------------------------ references
    def _measure_dark(self):
        p = self.p
        sim = self.hdx.sim_state if self.hdx.is_simulated else None
        if sim is None:
            self._confirm("Background measurement: block the light (close the shutter or turn the "
                          "lamp off), then press OK.")
        self._set(State.ACQUIRING, f"background {p['dark_averages']}× at {p['integration_ms']:g} ms")
        if sim:
            sim.light_blocked = True
        try:
            self.hdx.acquire_dark(p["dark_averages"])
            if self.camera is not None:
                self._check_abort()
                self._camera_dark = self.camera.expose(self.session.root / "camera_dark.npy")
        finally:
            if sim:
                sim.light_blocked = False
        if sim is None:
            self._confirm("Background stored. Restore the light, then press OK to start the scan.")

    def _confirm(self, text: str):
        """Operator step; Cancel ends the scan as aborted (nothing measured is lost)."""
        if not self.prompt(text):
            self._abort = True
        self._check_abort()

    def _record_references(self):
        s = self.session
        if self.hdx.dark_active:
            save_spectra(s.root / "dark.npz", wavelength_nm=self.hdx.wavelengths,
                         raw_scans=np.asarray(self.hdx.dark_scans), mean_raw=self.hdx.dark)
            s.set_reference("hdx_dark", {**s.record("dark.npz"),
                                         "integration_ms": self.hdx.dark_integration_ms,
                                         "n_scans": len(self.hdx.dark_scans),
                                         "measured_in_this_session": bool(self.p["measure_dark"])})
        if self._camera_dark is not None:
            # Settings travel with the reference so analysis can refuse a mismatched dark
            s.set_reference("camera_dark", {**s.record("camera_dark.npy"), **self._camera_dark,
                                            "settings": s.manifest["camera_settings"]})
        # White (flat) references are separate measurements and are not taken by this scan.

    def _session_info(self) -> dict:
        try:
            mono_status = self.mono.status()
        except MonochromatorError as exc:
            mono_status = f"unavailable: {exc}"
        devices = {"monochromator": {"port": self.mono.port, "simulated": self.mono.is_simulated,
                                     "status": mono_status},
                   "spectrometer": {"info": self.hdx.device_info, "backend": self.hdx.backend,
                                    "simulated": self.hdx.is_simulated}}
        if self.camera is not None:
            devices["camera"] = {**self.camera.info, "simulated": self.camera.simulated}
        return {"parameters": self.p, "devices": devices,
                "software": {"python": sys.version.split()[0], "numpy": np.__version__,
                             "platform": platform.platform()}}
