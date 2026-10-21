"""Camera-first workflow with the simulated camera process (sim_camd) and simulated Arduino; no hardware.

    QT_QPA_PLATFORM=offscreen python tests/test_camera.py
"""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import numpy as np
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from acquisition.session import file_record, load_verified
from gui.qt_bootstrap import prepare_qt


def wait(app, condition, seconds=60, what="condition"):
    end = time.monotonic() + seconds
    while not condition():
        app.processEvents()
        time.sleep(0.01)
        if time.monotonic() > end:
            raise AssertionError(f"timed out: {what}")


def guard_single_owner(client, log, handed_over):
    """Fail if two camera commands overlap, or if the scan configures the camera while the
    preview thread it took over is still running (handed_over[0], set just before the scan)."""
    active = [0]
    lock = threading.Lock()
    expose, configure = client.expose, client.configure

    def guarded(fn, *args):
        with lock:
            active[0] += 1
            assert active[0] == 1, "concurrent camera commands"
        try:
            return fn(*args)
        finally:
            with lock:
                active[0] -= 1

    def scan_configure(*args):
        if handed_over[0] is not None:  # only the scan configures after the handover point
            assert not handed_over[0].isRunning(), "scan used the camera before the preview stopped"
            log.append(("configure", "scan"))
        return guarded(configure, *args)

    def logged_expose(path):
        log.append(("expose", Path(path).name))
        return guarded(expose, path)
    client.configure, client.expose = scan_configure, logged_expose


def main():
    prepare_qt()
    from gui.main_window import MainWindow
    from gui.camera_panel import PREVIEW_PREFIX
    app = QApplication.instance() or QApplication([])
    warnings = []
    QMessageBox.warning = lambda *a, **k: warnings.append(a[2])
    QMessageBox.critical = lambda *a, **k: warnings.append(a[2])
    QMessageBox.information = lambda *a, **k: None
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
    with tempfile.TemporaryDirectory(prefix="camera-primary-test-") as tmp:
        base = Path(tmp)
        win = MainWindow(base, simulate=True)
        cam, cp = win.camera_panel, win.char_panel
        logs = []
        win.camera_panel.log_message.connect(logs.append)

        # 1) Camera alone: no HDX, no Arduino connection.
        cam.exp_spin.setValue(5)
        cam.bin_spin.setValue(4)
        cam.connect_btn.click()
        wait(app, lambda: cam.client is not None and cam.live_btn.isEnabled(), what="camera connect")
        assert not win.mono.is_connected and not win.hdx.is_connected
        assert cam.client.simulated and "SIM0000" in cam.info_lbl.text()
        preview = cam.preview_dir
        assert preview.name.startswith(PREVIEW_PREFIX)
        cam.live_btn.setChecked(True)
        most_files = [0]

        def frames_and_files():
            most_files[0] = max(most_files[0], len(list(preview.iterdir())))
            return cam._live is not None and cam._live.frames >= 5 and cam.image.image is not None
        wait(app, frames_and_files, what="live frames")
        assert cam.image.image.shape == (752, 752) and cam.image.image.dtype == np.uint16
        assert most_files[0] <= 2  # frame + its .tmp at most: preview disk use cannot grow
        assert "752×752" in cam.stats_lbl.text()
        cam.bin_spin.setValue(2)  # settings change while live: applied between frames, SDK reports values
        cam.apply_btn.click()
        wait(app, lambda: "bin 2" in cam.applied_lbl.text() and cam.image.image is not None
             and cam.image.image.shape == (1504, 1504), what="applied settings")
        displayed = cam.image.image.copy()
        cam.auto_chk.setChecked(False)  # contrast is display-only
        cam.save_btn.click()  # recording during live view: the live thread records its next frame
        wait(app, lambda: cam.run_dir is not None, what="saved frame")
        manifest, good, problems = load_verified(cam.run_dir)
        rec = good[0]["camera"]
        assert manifest["kind"] == "camera_frame" and manifest["status"] == "complete" and not problems
        assert rec["dtype"] == "uint16" and rec["shape"] == [1504, 1504] and rec["settings"]["bin"] == 2
        assert file_record(cam.run_dir, rec["file"])["sha256"] == rec["sha256"]
        assert manifest["devices"]["camera"]["serial"] == "SIM0000" and "spectrometer" not in manifest["devices"]
        assert np.array_equal(displayed, displayed.astype(np.uint16))  # displayed array stays raw uint16
        # Stop Live never blocks the GUI, even during a long exposure.
        cam.exp_spin.setValue(1500)
        cam.apply_btn.click()
        wait(app, lambda: "1500 ms" in cam.applied_lbl.text(), what="long exposure applied")
        time.sleep(0.2)
        started = time.monotonic()
        cam.live_btn.setChecked(False)
        assert time.monotonic() - started < 0.3 and cam._stopping and not cam.connect_btn.isEnabled()
        wait(app, lambda: not cam._stopping, what="preview stop")
        assert not list(preview.iterdir())
        cam.exp_spin.setValue(5)
        cam.save_btn.click()  # not live: one background task records one frame
        first = cam.run_dir
        wait(app, lambda: cam.run_dir != first and cam.save_btn.isEnabled(), what="saved frame (not live)")
        assert load_verified(cam.run_dir)[0]["status"] == "complete"
        assert len(list((base / "data").glob("camera_frame_*"))) == 2  # nothing else written to data/
        print("camera alone: live view, settings, raw frame recording OK")

        # 2) Scan handover: live preview running, camera + simulated Arduino, HDX NOT connected.
        exposures, handed_over = [], [None]
        guard_single_owner(cam.client, exposures, handed_over)
        client_before = cam.client
        cam.exp_spin.setValue(400)  # the preview is mid-exposure when the scan starts
        cam.live_btn.setChecked(True)
        wait(app, lambda: cam._live is not None and cam._live.frames >= 2, what="live before scan")
        time.sleep(0.1)
        win.mono.connect("SIMULATED")
        assert cp.cam_box.isChecked() and not cp.hdx_box.isChecked()  # camera is the default channel
        cp.start_spin.setValue(550)
        cp.stop_spin.setValue(550)
        cp.settle_spin.setValue(0)
        handed_over[0] = cam._live
        cp.start()
        assert cam._live is None and not cam.live_btn.isEnabled() and not cam.connect_btn.isEnabled()
        wait(app, lambda: cp.worker is None, what="single-point camera scan")
        run = cp.run_dir
        m = json.loads((run / "manifest.json").read_text())
        assert m["status"] == "complete" and m["channels"] == ["camera"], (m["status"], m.get("error"))
        assert "spectrometer" not in m["devices"] and set(m["references"]) == {"camera_dark"}
        assert len(m["points"]) == 1 and "hdx" not in m["points"][0]
        assert not (run / "camera.log").exists()  # lent client: the scan did not open a second process
        assert cam.client is client_before and cam.client.is_running and cam.live_btn.isEnabled()
        names = [n for _, n in exposures]
        assert names[-3:] == ["scan", "camera_dark.npy", "0000_forward_550nm.npy"], names
        assert names[0].startswith("preview_") and handed_over[0].isFinished()
        handed_over[0] = None
        assert len(cp.rows) == 1 and "measured_peak" not in cp.rows[0] and cp.rows[0]["camera_mean_counts"] > 0
        assert not cp.pdf_btn.isEnabled() and cp.envi_btn.isEnabled() and not cp.white_spectra_btn.isEnabled()
        assert "Camera frames: 1" in cp.summary_lbl.text()
        hdr = cp.export_cube()
        assert hdr and "bands = 1" in hdr.read_text()

        cam.exp_spin.setValue(5)
        cp.start_spin.setValue(500)
        cp.stop_spin.setValue(520)
        cp.bwd_chk.setChecked(True)
        assert "5 positions, 5 camera frames + 1 dark" in cp.plan_lbl.text()
        cp.start()
        wait(app, lambda: cp.worker is None, what="short camera scan")
        run = cp.run_dir
        assert json.loads((run / "manifest.json").read_text())["status"] == "complete"
        assert len(cp.rows) == 5 and cp.last_csv.is_file()
        hdr = cp.export_cube()
        assert "bands = 3" in hdr.read_text()
        QFileDialog.getExistingDirectory = lambda *a, **k: str(run)
        cp.rows.clear()
        cp.load_run()  # reopen manifest: camera rows come back without spectral metrics
        assert len(cp.rows) == 5 and all("camera_mean_counts" in r for r in cp.rows)
        assert cp.envi_btn.isEnabled() and not cp.pdf_btn.isEnabled()
        print("camera + simulated Arduino without HDX: handover, 1 point, 500-520 nm, ENVI, reopen OK")

        # 3) Camera + optional HDX together (lent camera). Selecting the HDX requires it to be connected.
        cp.hdx_box.setChecked(True)
        previous_run = cp.run_dir
        cp.start()
        assert warnings and "HDX" in warnings.pop() and cp.worker is None and cp.run_dir == previous_run
        win.hdx.connect(simulate=True)
        cp.avg_spin.setValue(2)
        cp.dark_avg_spin.setValue(2)
        cp.int_spin.setValue(6)
        cp.stop_spin.setValue(500)
        cp.bwd_chk.setChecked(False)
        cp.start()
        wait(app, lambda: cp.worker is None, what="camera + HDX scan")
        m = json.loads((cp.run_dir / "manifest.json").read_text())
        assert m["status"] == "complete" and m["channels"] == ["camera", "hdx"]
        assert set(m["references"]) == {"camera_dark", "hdx_dark"} and "spectrometer" in m["devices"]
        assert "measured_peak" in cp.rows[0] and "camera_mean_counts" in cp.rows[0] and cp.pdf_btn.isEnabled()
        cp.hdx_box.setChecked(False)
        print("camera + HDX together OK")

        # 4) Abort during a camera-only scan keeps every completed frame; the aborted camera is released.
        cam.exp_spin.setValue(1000)
        cp.start_spin.setValue(500)
        cp.stop_spin.setValue(560)
        cp.dark_chk.setChecked(False)
        cp.start()
        wait(app, lambda: len(cp.rows) >= 1, what="first point before abort")
        cp.abort()
        wait(app, lambda: cp.worker is None, what="abort")
        m, good, problems = load_verified(cp.run_dir)
        assert m["status"] == "aborted" and len(good) >= 1
        assert not any("checksum" in p or "missing" in p for p in problems), problems
        assert not list(cp.run_dir.glob("*.tmp"))
        wait(app, lambda: cam.client is None, what="aborted camera released")
        assert any("reconnect" in line for line in logs)
        print("abort keeps verified frames; aborted camera process released OK")

        # 5) Camera tab not connected: the scan owns its own process (and closes it).
        cam.exp_spin.setValue(5)
        cp.stop_spin.setValue(500)
        cp.start()
        wait(app, lambda: cp.worker is None, what="scan with own camera")
        assert (cp.run_dir / "camera.log").exists()
        assert json.loads((cp.run_dir / "manifest.json").read_text())["status"] == "complete"

        # 6) Device loss during live view: the process dies, the GUI stays responsive and releases it.
        cam.connect_btn.click()
        wait(app, lambda: cam.client is not None and cam.live_btn.isEnabled(), what="reconnect")
        lost_preview = cam.preview_dir
        lost_log = cam.client.log_path
        cam.live_btn.setChecked(True)
        wait(app, lambda: cam._live is not None and cam._live.frames >= 1, what="live before loss")
        cam.client._proc.kill()  # stands in for a USB disconnect / driver crash
        wait(app, lambda: cam.client is None and not cam.busy, what="lost camera released")
        assert not lost_preview.exists() and not cam.live_btn.isChecked()
        stopped, released = logs[-2:]  # one reason line and one release line, both naming the log
        assert stopped.startswith("Camera live view stopped:") and released.startswith("Camera process ended"), logs
        log_hint = f"(camera log: {lost_log})"
        assert stopped.endswith(log_hint) and released.endswith(log_hint), logs
        print("camera loss during live view handled OK")

        # 7) Close while live: exposure aborted, preview folder removed, process ended.
        cam.connect_btn.click()
        wait(app, lambda: cam.client is not None and cam.live_btn.isEnabled(), what="connect before close")
        client, folder = cam.client, cam.preview_dir
        cam.exp_spin.setValue(3000)
        cam.live_btn.setChecked(True)
        time.sleep(0.3)
        started = time.monotonic()
        win.close()
        wait(app, lambda: not client.is_running and cam.client is None, seconds=10, what="close")
        assert time.monotonic() - started < 8 and not folder.exists()
        assert not warnings, warnings
    print("ALL CAMERA TESTS PASSED")


if __name__ == "__main__":
    main()
