"""Headless end-to-end test of simulation mode: GUI build, device commands, full scan, CSV, PDF.

    QT_QPA_PLATFORM=offscreen python tests/test_simulation.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
from PySide6.QtWidgets import QApplication, QMessageBox

from analysis.spectral_analysis import CSV_COLUMNS, analyze_spectrum, estimate_baseline
from devices.monochromator import MonochromatorController, MonochromatorError, SimulationState
from devices.ocean_hdx import OceanHDXController
from gui.main_window import MainWindow
from gui.qt_bootstrap import prepare_qt


def wait_until(app, cond, timeout=60.0):
    t0 = time.monotonic()
    while not cond():
        app.processEvents()
        time.sleep(0.01)
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("condition not met")


def test_devices():
    rng = np.random.default_rng(0)
    baseline, noise = estimate_baseline(np.r_[100 + rng.normal(size=10000), [600]])
    assert abs(baseline - 100) < 0.1 and 0.9 < noise < 1.1
    st = SimulationState()
    m = MonochromatorController(st)
    m.connect("SIMULATED")
    assert m.goto(550) == 550.0
    assert m.home() == 200.0
    assert m.reset(100) == 0.0          # RESET moves back 100 nm, that point becomes 0
    assert m.set_position(500) == 500.0  # SET only declares the position
    assert m.get_position() == 500.0
    assert "READY" in m.status()
    for bad in ("GOTO:5000", "FOO"):
        try:
            m.send_raw(bad)
            raise AssertionError("expected error")
        except MonochromatorError:
            pass
    h = OceanHDXController(st)
    h.connect(simulate=True)
    h.set_integration_time(10)
    m.goto(600)
    wl, i = h.acquire_spectrum()
    r = analyze_spectrum(wl, i, 600)
    assert abs(r.error_nm) < 2 and 1 < r.fwhm_nm < 5, r
    print("devices OK:", r)


def test_gui():
    QMessageBox.information = lambda *a, **k: None
    QMessageBox.critical = lambda *a, **k: print("CRITICAL", a[1:])
    prepare_qt()
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    win = MainWindow(tmp, simulate=True)
    win.show()
    mp, hp, cp = win.mono_panel, win.hdx_panel, win.char_panel

    mp.connect_btn.click()
    wait_until(app, lambda: win.mono.is_connected and mp.goto_btn.isEnabled())
    mp.cmd_edit.setText("GOTO:550")
    mp.send_btn.click()
    wait_until(app, lambda: mp.pos_lbl.text() == "550.00 nm")
    hp.connect_btn.click()
    wait_until(app, lambda: win.hdx.is_connected)
    hp.int_spin.setValue(5)
    hp.live_btn.setChecked(True)
    wait_until(app, lambda: hp._last is not None)
    hp.live_btn.setChecked(False)
    wait_until(app, lambda: not hp._stopping and hp.dark_btn.isEnabled())
    assert "550" in hp.peak_lbl.text(), hp.peak_lbl.text()

    # Background (dark): take, check it is subtracted, then invalidate by changing integration
    hp.dark_btn.click()
    wait_until(app, lambda: win.hdx.dark is not None and hp.dark_btn.isEnabled())
    assert win.hdx.dark_active and abs(win.hdx.dark.mean() - 1500) < 20, win.hdx.dark.mean()
    _, corrected = win.hdx.acquire_spectrum()
    assert abs(np.median(corrected)) < 20, np.median(corrected)
    assert "Subtracting" in hp.dark_lbl.text(), hp.dark_lbl.text()
    win.hdx.set_integration_time(7)
    hp._update_state()
    assert not win.hdx.dark_active and "Invalid" in hp.dark_lbl.text()
    print("dark OK:", hp.dark_lbl.text())

    cp.int_spin.setValue(5)
    cp.avg_spin.setValue(5)
    cp.settle_spin.setValue(0)
    cp.start_btn.click()
    wait_until(app, lambda: cp.worker is None and cp.rows, timeout=300)
    df = cp.dataframe
    assert len(df) == 81, len(df)
    assert cp.params["dark"] == "measured before scan" and win.hdx.dark_active, cp.params.get("dark")
    assert cp.last_csv and set(CSV_COLUMNS) <= set(pd.read_csv(cp.last_csv).columns)
    spectra = sorted(cp.run_dir.glob("[0-9]*nm.npz"))
    assert len(spectra) == 81
    with np.load(cp.run_dir / "dark.npz") as dark, np.load(spectra[0]) as first:
        assert dark["raw_scans"].shape == (cp.dark_avg_spin.value(), 2068)
        assert first["raw_scans"].shape == (cp.avg_spin.value(), 2068)
        corrected = first["raw_scans"].mean(axis=0) - dark["mean_raw"]
        peak = analyze_spectrum(first["wavelength_nm"], corrected,
                                float(first["set_wavelength_nm"])).measured_peak
        assert abs(peak - df.iloc[0]["measured_peak"]) < 1e-6
    pdf = cp.generate_pdf(interactive=False)
    assert pdf and pdf.stat().st_size > 50_000
    manifest = json.loads((cp.run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete" and len(manifest["points"]) == 81
    assert not cp.envi_btn.isEnabled()  # no camera in this run

    # Short scan with the simulated camera process, then the ENVI cube from its RAW16 frames
    cp.cam_box.setChecked(True)
    cp.cam_exp_spin.setValue(1)
    cp.cam_bin_spin.setValue(4)
    cp.start_spin.setValue(500)
    cp.stop_spin.setValue(520)
    cp.bwd_chk.setChecked(False)
    cp.start_btn.click()
    wait_until(app, lambda: cp.worker is None and len(cp.rows) == 3, timeout=120)
    manifest = json.loads((cp.run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete" and "camera_dark" in manifest["references"]
    assert all(pt["camera"]["width"] == 752 for pt in manifest["points"])
    hdr = cp.export_cube()
    assert hdr and "bands = 3" in hdr.read_text(), hdr
    print("camera scan + ENVI OK:", hdr)
    print("GUI OK:", cp.summary_lbl.text().replace("\n", " | "))
    print("CSV:", cp.last_csv, "\nPDF:", pdf)
    win.grab().save(str(tmp / "screenshot.png"))
    print("screenshot:", tmp / "screenshot.png")
    win.close()


if __name__ == "__main__":
    test_devices()
    test_gui()
    print("ALL TESTS PASSED")
