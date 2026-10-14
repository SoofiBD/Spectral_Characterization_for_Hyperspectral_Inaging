"""Scan plan display and per-scan position confirmation; fake controllers only, no hardware."""
from pathlib import Path
import sys
import tempfile
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from PySide6.QtWidgets import QApplication, QMessageBox
from gui.qt_bootstrap import prepare_qt
import gui.characterization_panel as panel_module


class Fake:
    """Device stand-in: any attribute beyond the listed state (a command) fails the test."""
    def __init__(self, simulated, **state):
        self.__dict__.update({"is_connected": True, "is_simulated": simulated, "calls": [], **state})

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"unexpected device access: {name}")


class Sig:
    def connect(self, *_):
        pass


class FakeWorker:
    created = []

    def __init__(self, mono, hdx, params, run_dir, parent=None, camera_factory=None, camera_handover=None):
        self.params, self.run_dir, self.hdx, self.camera_handover = params, run_dir, hdx, camera_handover
        for name in ("point_done", "status", "progress", "failed", "ask_user", "finished"):
            setattr(self, name, Sig())
        FakeWorker.created.append(self)

    def start(self):
        pass


def main():
    prepare_qt()
    QApplication.instance() or QApplication([])
    real_worker = panel_module.CharacterizationWorker
    panel_module.CharacterizationWorker = FakeWorker
    warnings, prompts, answer = [], [], [QMessageBox.No]
    QMessageBox.warning = lambda *a, **k: warnings.append(a[2])
    QMessageBox.question = lambda *a, **k: (prompts.append(a[2]), answer[0])[1]
    with tempfile.TemporaryDirectory(prefix="scan-preflight-") as tmp:
        data = Path(tmp)
        mono = Fake(False, port="FAKE", position_nm=None)
        hdx = Fake(False, device_info={"model": "fake"})
        cp = panel_module.CharacterizationPanel(mono, hdx, data, data / "reports", lambda: False)
        cp.cam_box.setChecked(False)  # HDX-only plan and preflight; camera plans are in test_camera.py
        cp.hdx_box.setChecked(True)
        cp.start_spin.setValue(400); cp.stop_spin.setValue(800); cp.step_spin.setValue(10); cp.avg_spin.setValue(5)
        cp.bwd_chk.setChecked(False)
        assert cp.scan_summary() == (41, 205, 380) and "41 positions, 205 HDX" in cp.plan_lbl.text()
        cp.bwd_chk.setChecked(True)
        assert cp.scan_summary()[:2] == (81, 405) and "GOTO 380 nm" in cp.plan_lbl.text()
        cp.stop_spin.setValue(400)
        assert cp.scan_summary()[0] == 1 and "1 position," in cp.plan_lbl.text()
        assert str(data) in cp.folder_lbl.text()

        def nothing_created():
            assert cp.worker is None and not FakeWorker.created and cp.run_dir is None
            assert not any(data.iterdir()) and not mono.calls and not hdx.calls and cp.params == {}

        cp.start()  # unknown position: redirected to POS, never asked, nothing created
        assert len(warnings) == 1 and "POS" in warnings[0] and not prompts
        nothing_created()
        mono.position_nm = 395.0
        cp.start()  # operator answers No
        assert len(prompts) == 1 and "395 nm" in prompts[0]
        nothing_created()
        answer[0] = QMessageBox.Yes
        cp.start()
        check = FakeWorker.created[-1].params["position_check"]
        assert check["confirmed"] and check["mode"] == "hardware" and check["reported_nm"] == 395.0
        assert cp.run_dir.is_dir() and str(cp.run_dir) in cp.folder_lbl.text() and not mono.calls
        cp.worker = None; cp._set_running(False)
        answer[0] = QMessageBox.No
        cp.start()  # an earlier confirmation is never reused
        assert len(prompts) == 3 and len(FakeWorker.created) == 1

        sim = panel_module.CharacterizationPanel(Fake(True, port="SIM", position_nm=None),
                                                 Fake(True, device_info={}), data / "sim", data, lambda: True)
        sim.cam_box.setChecked(False)
        sim.hdx_box.setChecked(True)
        sim.start()
        assert len(prompts) == 3 and FakeWorker.created[-1].params["position_check"]["mode"] == "simulation"

        # A reconnect (Arduino reset) never offers the previous connection's position for confirmation.
        from devices.monochromator import MonochromatorController
        ctrl = MonochromatorController()
        ctrl.connect("SIMULATED")
        assert ctrl.get_position() is not None
        ctrl.disconnect()
        assert ctrl.position_nm is None
        ctrl.connect("SIMULATED")  # the post-connect POS has not succeeded
        assert ctrl.position_nm is None
        stale = panel_module.CharacterizationPanel(ctrl, hdx, data / "stale", data, lambda: False)
        assert stale._confirm_position() is None and "POS" in warnings[-1] and len(prompts) == 3
        ctrl.disconnect()

        # The worker restores the operator's subtract setting even when the scan fails.
        hdx_state, mono_state = Fake(False, device_info={}, subtract_dark=False), Fake(False, port="FAKE")
        worker = real_worker(mono_state, hdx_state, {"start": 500, "stop": 500, "step": 10, "backward": False,
                                                     "measure_dark": True}, data / "w")
        failures = []
        worker.failed.connect(failures.append)
        worker.run()  # fails at the coordinator's first device access (lock), after subtract was forced on
        assert failures and "_lock" in failures[0] and mono_state.calls == ["_lock"] and not hdx_state.calls
        assert hdx_state.subtract_dark is False

        # Camera-only hardware scan (no HDX connected): the position check comes before the live
        # preview is stopped/handed over; cancelling leaves the Camera tab untouched.
        class FakeCameraPanel:
            handovers = 0
            def scan_settings(self):
                return dict(index=0, exposure_us=1000, gain=0, offset=0, bin=1, roi=[0, 0, 0, 0], simulate=False)
            def begin_scan_handover(self):
                FakeCameraPanel.handovers += 1
                return "handover"
        cam_dir = data / "camera"
        mono_cam = Fake(False, port="FAKE", position_nm=480.0)
        cam_panel = panel_module.CharacterizationPanel(mono_cam, Fake(False, is_connected=False), cam_dir, data,
                                                       lambda: False)
        cam_panel.camera_panel = FakeCameraPanel()
        created = len(FakeWorker.created)
        answer[0] = QMessageBox.No
        cam_panel.start()
        assert FakeCameraPanel.handovers == 0 and len(FakeWorker.created) == created and not cam_dir.exists()
        answer[0] = QMessageBox.Yes
        cam_panel.start()
        worker = FakeWorker.created[-1]
        assert FakeCameraPanel.handovers == 1 and worker.camera_handover == "handover" and worker.hdx is None
        assert worker.params["hdx"] is False and worker.params["averages"] is None
        assert worker.params["position_check"]["reported_nm"] == 480.0 and not mono_cam.calls
    assert len(warnings) == 2, warnings
    print("ALL SCAN PREFLIGHT TESTS PASSED")


if __name__ == "__main__":
    main()
