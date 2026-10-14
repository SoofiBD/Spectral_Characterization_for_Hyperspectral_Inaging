"""Stationary raw recording, partial failure, reference loading and single-point GUI tests."""
from pathlib import Path
import json
import sys
import tempfile
import time
import numpy as np

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from acquisition.hdx_measurement import record_measurement
from acquisition.session import load_verified, file_record
from analysis.session_analysis import load_results
from devices.ocean_hdx import OceanHDXController


def wait(app, condition):
    deadline = time.monotonic() + 30
    while not condition():
        app.processEvents()
        time.sleep(.01)
        assert time.monotonic() < deadline, "GUI timed out"


def main():
    from gui.qt_bootstrap import prepare_qt
    prepare_qt()
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    warnings = []
    QMessageBox.critical = lambda *args, **kw: warnings.append(args[2])
    QMessageBox.information = lambda *args, **kw: None
    recovery = []
    QMessageBox.warning = lambda *args, **kw: recovery.append(args[2])
    with tempfile.TemporaryDirectory(prefix="hdx-recording-test-") as tmp:
        root = Path(tmp)
        win = MainWindow(root, simulate=True)
        hp, cp = win.hdx_panel, win.char_panel
        win.hdx.connect(simulate=True)
        hp._update_state()
        hp.int_spin.setValue(100)
        hp.dark_avg_spin.setValue(20)
        hp.dark_btn.click()
        assert hp._busy and not hp.acquire_btn.isEnabled() and not hp.connect_btn.isEnabled()
        wait(app, lambda: not hp._busy)
        dark_root = hp.run_dir
        manifest, _, problems = load_verified(dark_root)
        assert not problems and len(manifest["dark_exposures"]) == 20
        before = file_record(dark_root, "dark.npz")
        assert win.hdx.dark_valid
        hp.avg_spin.setValue(5)
        hp.acquire_btn.click()
        wait(app, lambda: not hp._busy)
        light_root = hp.run_dir
        manifest, points, problems = load_verified(light_root)
        assert not problems and len(points[0]["hdx_exposures"]) == 5
        assert points[0]["reported_nm"] is None
        with np.load(light_root / points[0]["hdx"]["file"]) as z:
            assert z["raw_scans"].shape == (5, 2068)
            expected = z["raw_scans"].mean(axis=0) - win.hdx.dark
        assert np.allclose(hp._last[1], expected)
        _, rows, _, problems = load_results(light_root)
        assert len(rows) == 1 and not problems
        win.hdx.disconnect()
        win.hdx.connect(simulate=True)
        assert win.hdx.dark is None
        QFileDialog.getExistingDirectory = lambda *args, **kw: str(dark_root)
        hp._load_dark()
        assert win.hdx.dark_valid
        win.hdx.set_integration_time(101)
        old_dark = win.hdx.dark.copy()
        hp._load_dark()
        assert warnings and "integration" in warnings.pop()
        assert np.array_equal(old_dark, win.hdx.dark)
        QFileDialog.getExistingDirectory = lambda *args, **kw: str(light_root)
        hp._open_measurement()
        assert np.allclose(hp._last[1], expected)
        QFileDialog.getExistingDirectory = lambda *args, **kw: str(dark_root)
        hp._open_measurement()
        assert np.array_equal(hp._last[1], old_dark)
        assert before == file_record(dark_root, "dark.npz")

        win.hdx.set_integration_time(100)
        original = win.hdx.acquire_raw
        calls = 0
        def fail_third():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("injected acquisition failure")
            return original()
        win.hdx.acquire_raw = fail_third
        failed_root = root / "failed"
        try:
            record_measurement(win.hdx, failed_root, 100, 5, 550)
            raise AssertionError("Expected acquisition failure")
        except RuntimeError as exc:
            assert "injected" in str(exc)
        finally:
            win.hdx.acquire_raw = original
        manifest, points, problems = load_verified(failed_root)
        assert manifest["status"] == "failed" and not points
        assert len(manifest["points"][0]["hdx_exposures"]) == 2
        assert any("partial point" in p for p in problems)
        assert not any("checksum" in p for p in problems)

        win.mono.connect("SIMULATED")
        cp.start_spin.setValue(550)
        cp.stop_spin.setValue(550)
        cp.avg_spin.setValue(5)
        cp.int_spin.setValue(100)
        cp.settle_spin.setValue(0)
        cp.bwd_chk.setChecked(True)
        cp.dark_chk.setChecked(False)
        hp.subtract_chk.setChecked(False)  # operator choice must survive the scan
        assert win.hdx.dark_valid
        cp.start()
        wait(app, lambda: cp.worker is None and len(cp.rows) == 1)
        manifest, rows, _, problems = load_results(cp.run_dir)
        assert len(rows) == 1 and not problems
        # The run applied the existing dark (recorded), the operator setting is restored afterwards.
        assert manifest["parameters"]["dark"] == "existing dark" and "hdx_dark" in manifest["references"]
        assert "hdx_dark" in manifest["points"][0]["references"]
        assert not win.hdx.subtract_dark and not hp.subtract_chk.isChecked()

        # Open Measurement with every forward point corrupted: the backward point opens with its own spectrum.
        hp.subtract_chk.setChecked(True)
        cp.start_spin.setValue(500)
        cp.stop_spin.setValue(510)
        cp.step_spin.setValue(10)
        cp.dark_chk.setChecked(True)
        cp.dark_avg_spin.setValue(2)
        cp.avg_spin.setValue(2)
        cp.start()
        wait(app, lambda: cp.worker is None and len(cp.rows) == 3)
        run = cp.run_dir
        manifest = json.loads((run / "manifest.json").read_text())
        backward = [p for p in manifest["points"] if p["direction"] == "backward"]
        assert len(backward) == 1 and backward[0]["target_nm"] == 500
        for point in manifest["points"]:
            if point["direction"] == "forward":
                with (run / point["hdx"]["file"]).open("ab") as f:  # test-owned temporary run
                    f.write(b"x")
        _, rows, _, problems = load_results(run)
        assert [r["direction"] for r in rows] == ["backward"] and len(problems) == 2
        QFileDialog.getExistingDirectory = lambda *args, **kw: str(run)
        hp._open_measurement()
        with np.load(run / backward[0]["hdx"]["file"]) as z, np.load(run / "dark.npz") as d:
            expected = (z["raw_scans"] - d["mean_raw"]).mean(axis=0)
        assert np.allclose(hp._last[1], expected) and "backward" in hp.plot.plotItem.titleLabel.text
        assert recovery and "checksum" in recovery[-1]
        assert not warnings, warnings
        win.close()
    print("ALL HDX RECORDING TESTS PASSED")


if __name__ == "__main__":
    main()
