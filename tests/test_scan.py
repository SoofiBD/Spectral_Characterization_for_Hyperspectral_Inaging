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
    deadline = time.monotonic() + 10
    while not points and t.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert points, "first point did not complete"
    time.sleep(0.5)
    t0 = time.monotonic()
    coord.abort()
    t.join(10)
    assert not t.is_alive() and time.monotonic() - t0 < 3, "abort did not interrupt exposure"
    cam.close()
    manifest, good, problems = load_verified(coord.session.root)
    assert states[-1] == State.ABORTED and manifest["status"] == "aborted"
    assert len(good) == 1 and all("partial point" in p for p in problems), (len(good), problems)
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


def test_abort_preserves_each_hdx_exposure():
    tmp = Path(tempfile.mkdtemp())
    coord, _, _, _ = run(tmp, use_camera=False, stop=500, measure_dark=False, averages=4)
    acquire = coord.hdx.acquire_raw
    captured = []
    def stop_after_one():
        wl, values = acquire()
        captured.append(values.copy())
        coord.abort()
        return wl, values
    coord.hdx.acquire_raw = stop_after_one
    session = coord.run()
    assert session.manifest['status'] == 'aborted' and len(captured) == 1
    point = session.manifest['points'][0]
    assert point['status'] == 'partial' and len(point['hdx_exposures']) == 1
    with np.load(session.root / point['hdx_exposures'][0]['file']) as z:
        assert np.array_equal(z['raw_scans'][0], captured[0])
    assert not load_verified(session.root)[1]
    print('HDX cancellation persistence OK')


def test_camera_failure_keeps_hdx_and_dark_cancel_keeps_reference():
    tmp = Path(tempfile.mkdtemp())
    coord, cam, _, _ = run(tmp, stop=500, measure_dark=False)
    def fail(path):
        raise CameraError('injected camera readout failure')
    cam.expose = fail
    try:
        coord.run()
        raise AssertionError('camera failure ignored')
    except CameraError:
        pass
    finally:
        cam.close()
    point = coord.session.manifest['points'][0]
    assert point['hdx']['n_scans'] == 2 and point['status'] == 'partial'
    assert (coord.session.root / point['hdx']['file']).is_file()
    tmp = Path(tempfile.mkdtemp())
    coord, _, _, _ = run(tmp, use_camera=False, stop=500)
    # Real-mode operator flow, but the underlying serial and spectrum objects stay simulated.
    class OperatorMono(type(coord.mono)):
        @property
        def is_simulated(self): return False
    class OperatorHDX(type(coord.hdx)):
        @property
        def is_simulated(self): return False
    coord.mono.__class__ = OperatorMono
    coord.hdx.__class__ = OperatorHDX
    answers = iter([True, False])
    coord.prompt = lambda _: next(answers)
    session = coord.run()
    assert session.manifest['status'] == 'aborted'
    assert 'hdx_dark' in session.manifest['references']
    assert (session.root / 'dark.npz').exists()
    print('camera failure and dark cancellation persistence OK')


def test_nonfinite_position_and_mixed_modes_rejected():
    for value in (float('nan'), float('inf'), 123):
        tmp = Path(tempfile.mkdtemp())
        coord, _, _, _ = run(tmp, use_camera=False, stop=500, measure_dark=False)
        coord.mono.goto = lambda *args: value
        try:
            coord.run()
            raise AssertionError('bad preposition accepted')
        except MonochromatorError:
            pass
        assert not coord.session.manifest['points']
    tmp = Path(tempfile.mkdtemp())
    coord, cam, _, _ = run(tmp, stop=500)
    cam.simulated = False  # still the simulated executable; only metadata is changed
    try:
        coord.run()
        raise AssertionError('mixed devices accepted')
    except ValueError as exc:
        assert 'Mixed' in str(exc)
    finally:
        cam.close()
    assert coord.session is None
    print('invalid positions and mixed modes rejected OK')


def test_missing_reference_and_immutable_raw():
    from acquisition.session import save_spectra
    tmp = Path(tempfile.mkdtemp())
    coord, cam, _, _ = run(tmp, stop=500)
    s = coord.run(); cam.close()
    before = {p.name: p.read_bytes() for p in s.root.glob('*.np*')}
    export_envi(s.root, tmp/'cube')
    assert before == {p.name: p.read_bytes() for p in s.root.glob('*.np*')}
    try:
        save_spectra(s.root/'dark.npz', raw_scans=np.zeros((1,1)))
        raise AssertionError('raw overwrite accepted')
    except FileExistsError:
        pass
    m = json.loads((s.root/'manifest.json').read_text())
    del m['references']['camera_dark']
    (s.root/'manifest.json').write_text(json.dumps(m))
    _, good, problems = load_verified(s.root)
    assert not good and any('camera_dark' in p for p in problems)
    print('missing reference and raw immutability OK')


def test_white_reference_and_temperature():
    tmp = Path(tempfile.mkdtemp())
    roots = []
    for kind in ('sample','white'):
        parent=tmp/kind;parent.mkdir()
        coord, cam, _, _ = run(parent, stop=500, kind=kind,
                              camera=dict(CAMERA, exposure_us=100000))
        roots.append(coord.run().root);cam.close()
    sample, white = roots
    export_envi(sample,tmp/'cube',white_session=white)
    values=np.fromfile(tmp/'cube'/'cube.raw',dtype='<f4')
    assert .37 < np.mean(values) < .43
    info=json.loads((tmp/'cube'/'cube.json').read_text())
    assert info['units']=='relative reflectance' and info['white_manifest_sha256']
    m=json.loads((sample/'manifest.json').read_text())
    m['points'][0]['camera']['temperature_c']=30
    (sample/'manifest.json').write_text(json.dumps(m))
    try:
        export_envi(sample,tmp/'cube')
        raise AssertionError('temperature mismatch ignored')
    except ValueError as exc:
        assert 'temperature' in str(exc)
    print('white correction and temperature matching OK')


def test_scan_excludes_concurrent_device_work():
    from devices.contracts import scan_access
    mono, hdx = devices()
    entered=threading.Event();done=threading.Event()
    def manual():
        entered.set();mono.get_position();done.set()
    with scan_access(mono,hdx):
        t=threading.Thread(target=manual);t.start();assert entered.wait(1)
        assert not done.wait(.1)
    t.join(2);assert done.is_set()
    held=threading.Event();release=threading.Event()
    def live():
        with hdx._lock:
            held.set();release.wait(2)
    t=threading.Thread(target=live);t.start();assert held.wait(1)
    try:
        try:
            with scan_access(mono,hdx):
                raise AssertionError('busy controller accepted')
        except RuntimeError:
            pass
    finally:
        release.set();t.join(2)
    print('scan ownership OK')


def test_alternate_spectrometer_and_recovery():
    from analysis.session_analysis import load_results
    from devices.contracts import driver_factory
    assert driver_factory('devices.ocean_hdx:OceanHDXController') is OceanHDXController
    tmp=Path(tempfile.mkdtemp())
    coord, _, _, _=run(tmp,use_camera=False,stop=500,measure_dark=False)
    class OtherSensor:
        model='Different spectrometer';serial_number='OTHER-17'
        def integration_time_micros(self,us): self.us=us
        def intensities(self,**kwargs):
            assert kwargs==dict(correct_dark_counts=False,correct_nonlinearity=False)
            return np.arange(17,dtype=float)
        def close(self): pass
    # Replace just the sensor, retaining the public controller contract and simulated mode.
    from devices.ocean_hdx import _SimulatedHDX
    class OtherSimSensor(OtherSensor, _SimulatedHDX):
        def __init__(self): pass
    coord.hdx._spec=OtherSimSensor()
    coord.hdx._wavelengths=np.linspace(480,520,17)
    coord.hdx.MIN_INT_MS=.1;coord.hdx.MAX_INT_MS=500;coord.hdx.max_intensity=4095
    coord.p['integration_ms']=.25
    s=coord.run()
    with np.load(s.root/s.manifest['points'][0]['hdx']['file']) as z:
        assert z['raw_scans'].shape==(2,17)
        assert float(z['integration_ms'])==.25
    m, rows, _, problems=load_results(s.root)
    assert len(rows)==1 and not problems
    assert m['devices']['spectrometer']['max_intensity']==4095
    print('alternate sensor dimensions/limits and recovery OK')


def test_process_crash_recovery_and_settle_abort():
    import subprocess
    tmp=Path(tempfile.mkdtemp())
    script='''import os,sys
from pathlib import Path
sys.path.insert(0,'tests')
from test_scan import run
c,_,_,_=run(Path(sys.argv[1]),use_camera=False,measure_dark=False)
c.on_point=lambda *args:os._exit(23)
c.run()
'''
    process=subprocess.run([sys.executable,'-c',script,str(tmp)],cwd=BASE)
    assert process.returncode==23
    m,good,issues=load_verified(tmp/'session')
    assert m['status']=='running' and len(good)==1 and any('interrupted' in p for p in issues)
    tmp=Path(tempfile.mkdtemp())
    coord,_,_,_=run(tmp,use_camera=False,measure_dark=False,settle_s=30)
    t=threading.Thread(target=coord.run);t.start()
    end=time.monotonic()+5
    while coord.state!=State.SETTLING and time.monotonic()<end:time.sleep(.01)
    assert coord.state==State.SETTLING
    start=time.monotonic();coord.abort();t.join(2)
    assert not t.is_alive() and time.monotonic()-start<2
    assert coord.session.manifest['status']=='aborted'
    print('actual process-crash recovery and settle cancellation OK')


def test_white_mismatch_and_missing_wavelength_rejected():
    tmp=Path(tempfile.mkdtemp());roots=[]
    for kind in ('sample','white'):
        parent=tmp/kind;parent.mkdir()
        c,cam,_,_=run(parent,stop=500,kind=kind)
        roots.append(c.run().root);cam.close()
    sample,white=roots
    path=white/'manifest.json';original=json.loads(path.read_text())
    import copy
    for change in ('gain','wavelength','identity'):
        m=copy.deepcopy(original)
        if change=='gain':m['camera_settings']['gain']+=1
        if change=='wavelength':m['points'][0]['target_nm']=700
        if change=='identity':m['devices']['camera']['serial']='another-device'
        path.write_text(json.dumps(m))
        try:
            export_envi(sample,tmp/'cube',white_session=white)
            raise AssertionError('white mismatch accepted: '+change)
        except ValueError:
            pass
    from acquisition.session import file_record
    m=original
    frame=white/m['points'][0]['camera']['file']
    pixels=np.load(frame);pixels[0,0]=65532
    np.save(frame,pixels)  # deliberately saturated temporary test fixture
    m['points'][0]['camera'].update(file_record(white,frame.name))
    path.write_text(json.dumps(m))
    try:
        export_envi(sample,tmp/'saturated',white_session=white)
        raise AssertionError('14-bit RAW16 saturation was missed')
    except ValueError as exc:
        assert 'saturated' in str(exc)
    print('white settings, identity, wavelength and saturation rejection OK')


def test_spectrometer_white_export():
    from analysis.white_export import export_white_spectra
    tmp=Path(tempfile.mkdtemp()); roots=[]
    for kind in ('sample','white'):
        parent=tmp/kind;parent.mkdir()
        c,_,_,_=run(parent,use_camera=False,stop=500,kind=kind,integration_ms=100)
        roots.append(c.run().root)
    out=export_white_spectra(roots[0],roots[1],tmp/'normalized')
    result=json.loads((out/'normalization.json').read_text())
    with np.load(out/result['spectra'][0]['file']) as z:
        peak=np.argmin(abs(z['wavelength_nm']-500))
        assert .8 < z['relative_signal'][peak] < 1.2
    mpath=roots[1]/'manifest.json'
    m=json.loads(mpath.read_text());m['devices']['spectrometer']['info']='wrong instrument'
    mpath.write_text(json.dumps(m))
    try:
        export_white_spectra(roots[0],roots[1],tmp/'bad')
        raise AssertionError('wrong white spectrometer accepted')
    except ValueError as exc:
        assert 'identity' in str(exc)
    print('spectrometer white normalization OK')


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL SCAN TESTS PASSED")
