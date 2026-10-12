"""Scan coordinator tests with simulated monochromator, HDX and camera process (no Qt).

    python tests/test_scan.py        (or: pytest tests/test_scan.py)
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import numpy as np

from acquisition.coordinator import ScanCoordinator, State
from acquisition.session import load_verified
from analysis.cube_export import export_envi
from devices.asi_camera import ASICameraClient, CameraError
from devices.monochromator import MonochromatorController, MonochromatorError, SimulationState
from devices.ocean_hdx import OceanHDXController

CAMERA = {"exposure_us": 2000, "gain": 100, "offset": 50, "bin": 4, "roi": (0, 0, 64, 32)}


def params(**over):
    p = dict(start=500, stop=520, step=10, averages=2, integration_ms=6, settle_s=0,
             backward=False, measure_dark=True, dark_averages=2, camera=dict(CAMERA))
    p.update(over)
    return p


def devices():
    st = SimulationState()
    mono, hdx = MonochromatorController(st), OceanHDXController(st)
    mono.connect("SIMULATED")
    hdx.connect(simulate=True)
    return mono, hdx


def run(tmp, use_camera=True, **over):
    mono, hdx = devices()
    cam = ASICameraClient(simulate=True, log_path=tmp / "camera.log") if use_camera else None
    states, points = [], []
    coord = ScanCoordinator(mono, hdx, params(**over), tmp / "session", cam,
                            on_state=lambda s, d: states.append(s),
                            on_point=lambda pt, wl, raw: points.append(pt))
    return coord, cam, states, points


def test_single_point_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    coord, cam, states, points = run(tmp, stop=500)
    session = coord.run()
    cam.close()
    assert states[0] == State.PREPARING and states[-1] == State.COMPLETE
    for s in (State.MOVING, State.SETTLING, State.ACQUIRING, State.SAVING):
        assert s in states
    manifest, good, problems = load_verified(session.root)
    assert manifest["status"] == "complete" and not problems, problems
    assert len(good) == 1 and set(manifest["references"]) == {"hdx_dark", "camera_dark"}
    pt = good[0]
    assert pt["reported_nm"] == 500 and pt["camera"]["width"] == 64
    frame = np.load(session.root / pt["camera"]["file"])
    assert frame.dtype == np.uint16 and frame.shape == (32, 64)
    with np.load(session.root / pt["hdx"]["file"]) as z:
        assert z["raw_scans"].shape == (2, 2068)
    assert manifest["camera_settings"]["format"] == "RAW16"
    assert manifest["devices"]["camera"]["serial"] == "SIM0000"
    hdr = export_envi(session.root, tmp / "cube")
    assert "bands = 1" in hdr.read_text()
    assert (tmp / "cube" / "cube.raw").stat().st_size == 64 * 32 * 4
    print("single point OK")


def test_move_timeout_fails_and_keeps_points():
    tmp = Path(tempfile.mkdtemp())
    coord, cam, states, points = run(tmp, use_camera=False, stop=900, step=200)
    real_goto = coord.mono.goto
    # second scan point never reports DONE in time
    coord.mono.goto = lambda w, timeout: real_goto(w, 0.01 if w > 700 else timeout)
    try:
        coord.run()
        raise AssertionError("expected timeout")
    except MonochromatorError as exc:
        assert "Timeout" in str(exc)
    manifest, good, _ = load_verified(coord.session.root)
    assert states[-1] == State.FAILED and manifest["status"] == "failed"
    assert [p["target_nm"] for p in good] == [500.0, 700.0]
    print("move timeout OK")


def test_position_mismatch_fails():
    tmp = Path(tempfile.mkdtemp())
    coord, cam, states, _ = run(tmp, use_camera=False)
    real_goto = coord.mono.goto
    coord.mono.goto = lambda w, timeout: real_goto(w, timeout) + (1.0 if w == 510 else 0)
    try:
        coord.run()
        raise AssertionError("expected position error")
    except MonochromatorError as exc:
        assert "differs from target" in str(exc)
    assert len(coord.session.manifest["points"]) == 1
    print("position mismatch OK")


def test_abort_during_exposure_and_partial_recovery():
    tmp = Path(tempfile.mkdtemp())
    camera = dict(CAMERA, exposure_us=3_000_000)  # 3 s exposures: abort lands mid-exposure
    coord, cam, states, points = run(tmp, stop=560, measure_dark=False, camera=camera)
    t = threading.Thread(target=coord.run)
    t.start()
    while not points:
        time.sleep(0.05)
    time.sleep(0.5)
    t0 = time.monotonic()
    coord.abort()
    t.join(10)
    assert not t.is_alive() and time.monotonic() - t0 < 3, "abort did not interrupt exposure"
    cam.close()
    manifest, good, problems = load_verified(coord.session.root)
    assert states[-1] == State.ABORTED and manifest["status"] == "aborted"
    assert len(good) == 1 and not problems, (len(good), problems)
    # an aborted exposure leaves no frame behind
    assert sorted(p.name for p in coord.session.root.glob("*.npy")) == [good[0]["camera"]["file"]]
    try:
        ASICameraClient.expose(cam, coord.session.root / "x.npy")
        raise AssertionError("process should be gone")
    except CameraError:
        pass

    # crash simulation: manifest left "running", one raw file corrupted afterwards
    m = json.loads((coord.session.root / "manifest.json").read_text())
    m["status"] = "running"
    (coord.session.root / "manifest.json").write_text(json.dumps(m))
    raw = coord.session.root / good[0]["hdx"]["file"]
    data = raw.read_bytes()
    raw.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
    _, good, problems = load_verified(coord.session.root)
    assert not good and any("checksum" in p for p in problems)
    assert any("interrupted" in p for p in problems)
    print("abort + recovery OK")


def test_calibration_mismatch_refused():
    tmp = Path(tempfile.mkdtemp())
    coord, cam, _, _ = run(tmp, stop=500)
    session = coord.run()
    cam.close()
    m = json.loads((session.root / "manifest.json").read_text())
    m["references"]["camera_dark"]["settings"]["gain"] = 0
    (session.root / "manifest.json").write_text(json.dumps(m))
    try:
        export_envi(session.root, tmp / "cube")
        raise AssertionError("mismatched dark must not be applied")
    except ValueError as exc:
        assert "do not match" in str(exc)
    print("calibration mismatch OK")


def test_manifest_path_escape_rejected():
    tmp = Path(tempfile.mkdtemp())
    coord, cam, _, _ = run(tmp, use_camera=False, stop=500)
    session = coord.run()
    m = json.loads((session.root / "manifest.json").read_text())
    m["points"][0]["hdx"]["file"] = "../../etc/passwd"
    (session.root / "manifest.json").write_text(json.dumps(m))
    _, good, problems = load_verified(session.root)
    assert not good and any("leaves the session" in p for p in problems)
    print("path escape OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL SCAN TESTS PASSED")
