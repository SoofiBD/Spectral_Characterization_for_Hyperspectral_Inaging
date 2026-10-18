"""UI workflow helpers: remembered settings, scan readiness, progress text and the camera scan views.

    QT_QPA_PLATFORM=offscreen python tests/test_ui_workflow.py
"""
from pathlib import Path
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from gui.qt_bootstrap import prepare_qt


def wait(app, condition, seconds=60, what="condition"):
    end = time.monotonic() + seconds
    while not condition():
        app.processEvents()
        time.sleep(0.01)
        if time.monotonic() > end:
            raise AssertionError(f"timed out: {what}")


def main():
    prepare_qt()
    from gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
    QMessageBox.warning = lambda *a, **k: (_ for _ in ()).throw(AssertionError(a[2]))
    with tempfile.TemporaryDirectory(prefix="ui-workflow-test-") as tmp:
        base = Path(tmp)

        # 1) Settings are remembered, except the white-reference flag and SET value.
        settings = QSettings(str(base / "settings.ini"), QSettings.IniFormat)
        win = MainWindow(base, simulate=True, settings=settings)
        cp, cam = win.char_panel, win.camera_panel
        cp.start_spin.setValue(420)
        cp.hdx_box.setChecked(True)
        cp.white_chk.setChecked(True)
        cam.exp_spin.setValue(12.5)
        win.mono_panel.set_spin.setValue(700)
        win._save_settings()
        win.deleteLater()
        win = MainWindow(base, simulate=True, settings=settings)
        cp, cam = win.char_panel, win.camera_panel
        assert cp.start_spin.value() == 420 and cp.hdx_box.isChecked() and cam.exp_spin.value() == 12.5
        assert not cp.white_chk.isChecked() and win.mono_panel.set_spin.value() == 550
        print("settings persistence OK")

        # 2) Readiness lists what is missing and links to the tab that fixes it.
        assert "Monochromator — required" in cp.ready_lbl.text() and 'href="mono"' in cp.ready_lbl.text()
        assert "HDX — required" in cp.ready_lbl.text()
        cp.hdx_box.setChecked(False)
        assert "HDX" not in cp.ready_lbl.text() and "12.5 ms" in cp.cam_settings_lbl.text()

        # 3) Camera-only scan: frame preview, camera signal plot, status-bar progress, no preview path in rows.
        win.mono_panel.connect_btn.click()
        wait(app, lambda: win.mono.is_connected and win.mono_panel.goto_btn.isEnabled(), what="mono connect")
        assert "Monochromator at" in cp.ready_lbl.text()
        assert win._page_state["mono"][0] == "sim" and win._page_state["camera"][0] == "off"  # rail + header badge
        progress = []
        cp.progress_text.connect(progress.append)
        cp.start_spin.setValue(400)
        cp.stop_spin.setValue(420)
        cp.step_spin.setValue(10)
        cp.settle_spin.setValue(0)
        cp.bwd_chk.setChecked(False)
        cp.start()
        assert not cp.start_spin.isEnabled()  # parameters are locked while the scan runs
        wait(app, lambda: cp.worker is None, 120, what="scan")
        assert len(cp.rows) == 3 and all("camera_file" not in row for row in cp.rows)
        assert cp.cam_image.image is not None and max(cp.cam_image.image.shape) <= 1000
        assert cp.cam_plot.plotItem.listDataItems() and cp.tabs.currentWidget() is cp.cam_plot
        assert progress[-1] == "" and any(p.startswith("Scan 1/3") for p in progress)
        assert cp.start_spin.isEnabled()
        assert win._page_state["scan"] == ("off", "")  # scan badge cleared after the run
        assert "camera_file" not in (cp.run_dir / "summary.csv").read_text()
        print("camera scan views OK")
        win._closing = True
        win.close()
    print("ALL UI WORKFLOW TESTS PASSED")


if __name__ == "__main__":
    main()
