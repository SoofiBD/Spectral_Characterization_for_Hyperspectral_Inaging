"""ROI analysis of camera scans (headless) and the read-only review dialog (offscreen Qt).

Synthetic runs are written with the real Session/manifest code; one end-to-end case uses a
simulated camera-only scan from ScanCoordinator. No hardware is touched.
"""
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from acquisition.session import Session  # noqa: E402
from analysis import roi_analysis as ra  # noqa: E402

SETTINGS = {"exposure_us": 1000, "gain": 10, "offset": 5, "bin": 1, "roi": [0, 0, 16, 8], "format": "RAW16",
            "temperature_c": 20.0}
MAX = 4000


def make_run(root: Path, frames: dict, dark_value=100, kind="sample", serial="S1", temperature=20.0,
             dark_temperature=20.0, exposure=1000, legacy=False, max_intensity=MAX):
    """frames: {(direction, nm): 2D uint16 array}; dark is a constant frame."""
    session = Session(root, {"parameters": {"kind": kind}, "channels": ["camera"],
                             "devices": {"camera": {"model": "fake", "serial": serial, "simulated": True,
                                                    "max_intensity": max_intensity}}})
    m = session.manifest
    m["camera_settings"] = dict(SETTINGS)
    shape = next(iter(frames.values())).shape
    np.save(root / "camera_dark.npy", np.full(shape, dark_value, dtype="<u2"))
    session.set_reference("camera_dark", {**session.record("camera_dark.npy"), "exposure_us": 1000,
                                          "temperature_c": dark_temperature, "dtype": "uint16",
                                          "shape": list(shape), "settings": dict(SETTINGS)})
    for i, ((direction, nm), frame) in enumerate(frames.items()):
        name = f"{i:04d}_{direction}_{nm:g}nm.npy"
        np.save(root / name, frame.astype("<u2"))
        cam = {**session.record(name), "exposure_us": exposure, "temperature_c": temperature,
               "dtype": "uint16", "shape": list(frame.shape), "saturated_pixels": int((frame >= MAX).sum()),
               "settings": dict(SETTINGS)}
        point = {"index": i, "status": "complete", "direction": direction, "target_nm": nm,
                 "reported_nm": nm + 0.1, "camera": cam, "references": ["camera_dark"]}
        if legacy:
            del cam["settings"]
            del point["references"]
        session.add_point(point)
    session.finish("complete")
    return root


def two_level(a, b, shape=(8, 16), dark=100):
    """Left half = dark + a, right half = dark + b, with a +-1 checkerboard as pixel noise."""
    frame = np.empty(shape, dtype=np.int64)
    half = shape[1] // 2
    frame[:, :half] = dark + a
    frame[:, half:] = dark + b
    frame += (np.indices(shape).sum(axis=0) % 2) * 2 - 1
    return np.clip(frame, 0, 65535).astype(np.uint16)


BOXES = ((0, 0, 8, 8), (8, 0, 16, 8))


def sha(path: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir())}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="roi-test-"))
        self.frames = {("forward", 500.0): two_level(400, 200), ("forward", 550.0): two_level(600, 300),
                       ("forward", 600.0): two_level(800, 400), ("backward", 550.0): two_level(630, 300),
                       ("backward", 500.0): two_level(400, 200)}
        self.root = make_run(self.tmp / "data" / "run_a", self.frames)

    def test_dark_subtraction_uncertainty_and_ratio(self):
        run = ra.load_camera_run(self.root)
        rows = ra.analyze(run, BOXES)
        self.assertEqual(len(rows), 5)
        r = rows[0]
        self.assertAlmostEqual(r["a_mean"], 400, places=9)
        self.assertAlmostEqual(r["b_mean"], 200, places=9)
        self.assertEqual((r["a_pixels"], r["a_saturated"], r["flags"]), (64, 0, ""))
        expected_sem = np.std(np.where(np.indices((8, 8)).sum(0) % 2, 1., -1.), ddof=1) / 8
        self.assertAlmostEqual(r["a_sem"], expected_sem)
        self.assertAlmostEqual(r["ratio_a_b"], 2.0)
        self.assertAlmostEqual(r["ratio_a_b_sem"], math.hypot(expected_sem / 200, 400 * expected_sem / 200 ** 2))
        self.assertAlmostEqual(r["reported_nm"], 500.1)

    def test_low_denominator_and_saturation_are_flagged(self):
        self.assertTrue(math.isnan(ra.ratio(10, 1, 0, 1)[0]))
        value, err, why = ra.ratio(10, 1, 2, 1)
        self.assertTrue(math.isnan(value) and "sigma" in why)
        frames = {("forward", 500.0): two_level(MAX, 0)}  # A saturated, B ~ dark
        run = ra.load_camera_run(make_run(self.tmp / "sat", frames))
        row = ra.analyze(run, BOXES)[0]
        self.assertEqual(row["a_saturated"], 64)
        self.assertIn("a_saturated", row["flags"])
        self.assertIn("ratio:", row["flags"])
        self.assertTrue(math.isnan(row["ratio_a_b"]))
        unknown = ra.load_camera_run(make_run(self.tmp / "nomax", frames, max_intensity=None))
        self.assertIn("saturation_unknown", ra.analyze(unknown, BOXES)[0]["flags"])

    def test_forward_backward_pairs_by_set_wavelength(self):
        rows = ra.analyze(ra.load_camera_run(self.root), BOXES)
        pairs = ra.forward_backward(rows)
        self.assertEqual([p["target_nm"] for p in pairs], [500.0, 550.0])  # 600 only forward
        p550 = pairs[1]
        self.assertAlmostEqual(p550["a_mean_rel_diff"], 0.05)
        self.assertAlmostEqual(p550["ratio_a_b_diff"], 2.1 - 2.0)
        self.assertGreater(p550["ratio_a_b_z"], 3)
        self.assertAlmostEqual(pairs[0]["ratio_a_b_diff"], 0)
        s = ra.summary(rows, pairs)
        self.assertEqual((s["forward_backward_pairs"], s["points_with_flags"]), (2, 0))

    def test_dark_validation(self):
        with self.assertRaisesRegex(ValueError, "exposure"):
            ra.load_camera_run(make_run(self.tmp / "exp", self.frames, exposure=2000))
        hot = make_run(self.tmp / "hot", self.frames, temperature=25.0)
        with self.assertRaisesRegex(ValueError, "temperature"):
            ra.load_camera_run(hot)
        self.assertEqual(len(ra.load_camera_run(hot, temperature_tolerance_c=6).points), 5)
        tampered = make_run(self.tmp / "tampered", self.frames)
        np.save(tampered / "camera_dark.npy", np.zeros((8, 16), dtype="<u2"))
        with self.assertRaisesRegex(ValueError, "dark failed verification"):
            ra.load_camera_run(tampered)
        with self.assertRaisesRegex(ValueError, "outside"):
            ra.analyze(ra.load_camera_run(self.root), ((0, 0, 17, 8), BOXES[1]))

    def test_legacy_points_without_settings_or_references(self):
        run = ra.load_camera_run(make_run(self.tmp / "legacy", self.frames, legacy=True))
        self.assertEqual(len(ra.analyze(run, BOXES)), 5)

    def test_export_is_separate_atomic_and_traceable(self):
        before = sha(self.root)
        run = ra.load_camera_run(self.root)
        with self.assertRaisesRegex(ValueError, "outside"):
            ra.export(run, BOXES, self.root / "roi")
        out = ra.export(run, BOXES, self.tmp / "data" / "run_a_roi_1")
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["roi.json", "roi_forward_backward.csv", "roi_signals.csv"])
        self.assertFalse(list(out.parent.glob(".*partial")))
        info = json.loads((out / "roi.json").read_text())
        manifest_sha = hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(info["sample"]["manifest"]["sha256"], manifest_sha)
        self.assertEqual(len(info["sample"]["frames"]), 5)
        self.assertEqual(info["preset"]["boxes"], [list(b) for b in BOXES])
        self.assertEqual(info["summary"]["forward_backward_pairs"], 2)
        self.assertEqual(sha(self.root), before)  # raw data untouched
        with self.assertRaises(FileExistsError):
            ra.export(run, BOXES, out)

        def broken(tmp):
            raise RuntimeError("plot failed")
        with self.assertRaises(RuntimeError):
            ra.export(run, BOXES, self.tmp / "data" / "run_a_roi_2", extra=broken)
        self.assertFalse((self.tmp / "data" / "run_a_roi_2").exists())
        self.assertFalse(list((self.tmp / "data").glob(".*partial")))

    def test_preset_round_trip_and_geometry_check(self):
        run = ra.load_camera_run(self.root)
        preset = ra.save_preset(self.tmp / "preset.json", run, BOXES)
        self.assertEqual(ra.load_preset(preset, run), BOXES)
        out = ra.export(run, BOXES, self.tmp / "exported")
        self.assertEqual(ra.load_preset(out / "roi.json", run), BOXES)  # exported roi.json is reusable
        other = ra.load_camera_run(make_run(self.tmp / "big", {("forward", 500.0): two_level(1, 1, (16, 16))}))
        with self.assertRaisesRegex(ValueError, "geometry"):
            ra.load_preset(preset, other)
        with self.assertRaisesRegex(ValueError, "acquisition"):
            ra.save_preset(self.root / "p.json", run, BOXES)

    def test_white_reference_reuses_boxes(self):
        run = ra.load_camera_run(self.root)
        white_frames = {k: two_level(800, 800) for k in self.frames}
        white = ra.load_camera_run(make_run(self.tmp / "white", white_frames, kind="white"))
        rows = ra.analyze(run, BOXES, white)
        r = rows[0]
        self.assertAlmostEqual(r["a_reflectance"], 0.5)
        self.assertAlmostEqual(r["b_reflectance"], 0.25)
        self.assertAlmostEqual(r["reflectance_ratio_a_b"], 2.0)
        self.assertTrue(np.isfinite(r["reflectance_ratio_a_b_sem"]))
        out = ra.export(run, BOXES, self.tmp / "white_export", white)
        info = json.loads((out / "roi.json").read_text())
        self.assertEqual(info["white"]["kind"], "white")
        self.assertIn("reflectance", info["units"])
        with self.assertRaisesRegex(ValueError, "white-reference run"):
            ra.analyze(run, BOXES, ra.load_camera_run(make_run(self.tmp / "notwhite", white_frames)))
        with self.assertRaisesRegex(ValueError, "identity"):
            ra.analyze(run, BOXES, ra.load_camera_run(make_run(self.tmp / "w2", white_frames, kind="white",
                                                               serial="OTHER")))
        partial = ra.load_camera_run(make_run(self.tmp / "w3", {("forward", 500.0): two_level(800, 800)},
                                              kind="white"))
        with self.assertRaisesRegex(ValueError, "no (forward|backward) frame"):
            ra.analyze(run, BOXES, partial)
        saturated = {k: two_level(800, MAX) for k in self.frames}
        rows = ra.analyze(run, BOXES, ra.load_camera_run(make_run(self.tmp / "w4", saturated, kind="white")))
        self.assertTrue(math.isnan(rows[0]["b_reflectance"]))
        self.assertIn("b_white:white saturated", rows[0]["flags"])

    def test_list_runs(self):
        make_run(self.tmp / "data" / "run_white", self.frames, kind="white")
        (self.tmp / "data" / "not_a_run").mkdir()
        (self.tmp / "data" / "broken").mkdir()
        (self.tmp / "data" / "broken" / "manifest.json").write_text("{")
        runs = {r["name"]: r for r in ra.list_runs(self.tmp / "data")}
        self.assertEqual(set(runs), {"run_a", "run_white", "broken"})
        self.assertEqual((runs["run_a"]["camera_points"], runs["run_a"]["range_nm"]), (5, (500.0, 600.0)))
        self.assertEqual(runs["run_white"]["kind"], "white")
        self.assertTrue(runs["broken"]["error"])


class ReviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from gui.qt_bootstrap import prepare_qt
        prepare_qt()
        cls.app = QApplication.instance() or QApplication([])
        cls.messages = []
        QMessageBox.critical = lambda *a, **k: cls.messages.append(a[2])
        QMessageBox.information = lambda *a, **k: None
        QMessageBox.warning = lambda *a, **k: cls.messages.append(a[2])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="roi-gui-"))

    def test_dialog_coordinates_white_mode_and_export(self):
        from gui.camera_review import CameraReview
        shape = (64, 128)
        frames = {(d, nm): two_level(500 + nm / 10, 250, shape)
                  for d, nm in (("forward", 500.0), ("forward", 600.0), ("backward", 500.0))}
        root = make_run(self.tmp / "data" / "run", frames)
        white = make_run(self.tmp / "data" / "white", {k: two_level(900, 900, shape) for k in frames}, kind="white")
        before = sha(root)
        dialog = CameraReview(root, temperature_tolerance_c=2.0, white_root=white)
        self.assertTrue(dialog.image_plot.getViewBox().yInverted())
        boxes = ((8, 16, 40, 48), (72, 8, 128, 64))
        dialog.set_boxes(boxes)
        self.assertEqual(dialog.boxes(), boxes)  # sensor <-> display conversion is exact
        self.assertEqual(len(dialog.rows), 3)
        expected = ra.analyze(dialog.run, boxes, dialog.white)
        self.assertEqual([r["ratio_a_b"] for r in dialog.rows], [r["ratio_a_b"] for r in expected])
        self.assertEqual(dialog.mode.count(), 2)
        dialog.mode.setCurrentIndex(1)
        self.assertIn("Forward/backward", dialog.warning.text())
        dialog.slider.setValue(2)
        self.assertEqual(dialog.choice.currentIndex(), 2)
        self.assertIn("backward", dialog.frame_lbl.text())
        out = dialog.save()
        self.assertIsNotNone(out, self.messages)
        self.assertTrue({"roi.json", "roi_signals.csv", "roi_signals.png", "roi_ratio.png",
                         "roi_frame.png"} <= {p.name for p in out.iterdir()})
        self.assertEqual(sha(root), before)
        dialog.close()

    def test_invalid_white_is_reported_not_used(self):
        from gui.camera_review import CameraReview
        frames = {("forward", 500.0): two_level(400, 200)}
        root = make_run(self.tmp / "run", frames)
        sample_as_white = make_run(self.tmp / "other", frames)  # kind "sample"
        dialog = CameraReview(root, white_root=sample_as_white)
        self.assertIsNone(dialog.white)
        self.assertIn("White reference not used", dialog.info.text())
        self.assertEqual(dialog.mode.count(), 1)
        dialog.close()

    def test_run_picker_lists_and_preselects(self):
        from gui.camera_review import RunPicker
        data = self.tmp / "data"
        make_run(data / "run_1", {("forward", 500.0): two_level(1, 1)})
        make_run(data / "run_2", {("forward", 500.0): two_level(1, 1)}, kind="white")
        picker = RunPicker(data, current=data / "run_1")
        self.assertEqual(picker.table.rowCount(), 2)
        picker.accept()
        self.assertEqual(picker.selected, data / "run_1")
        white = RunPicker(data, kind="white", camera_only=False)
        self.assertEqual([r["name"] for r in white.runs], ["run_2"])

    def test_panel_opens_picked_run_with_white_reference(self):
        import gui.characterization_panel as panel_module
        from gui.camera_review import CameraReview, RunPicker
        from gui.main_window import MainWindow
        shape = (8, 16)
        root = make_run(self.tmp / "data" / "run", {("forward", 500.0): two_level(400, 200, shape)})
        white = make_run(self.tmp / "data" / "white", {("forward", 500.0): two_level(800, 800, shape)}, kind="white")
        win = MainWindow(self.tmp, simulate=True)
        cp = win.char_panel
        picks, opened = [], []
        original_choose, original_exec = RunPicker.choose, CameraReview.exec
        try:
            RunPicker.choose = classmethod(lambda cls, data_dir, parent=None, **kw:
                                           picks.append(kw) or (white if kw.get("kind") == "white" else root))
            CameraReview.exec = lambda dialog: opened.append(dialog)
            self.assertTrue(cp.camera_review_btn.isEnabled())
            cp._choose_white()
            self.assertEqual(cp.white_path.text(), str(white))
            cp.review_camera()
            self.assertEqual(len(opened), 1, self.messages)
            self.assertIsNotNone(opened[0].white)
            self.assertEqual(picks[0]["kind"], "white")
            self.assertIs(panel_module.RunPicker, RunPicker)
        finally:
            RunPicker.choose, CameraReview.exec = original_choose, original_exec
            for dialog in opened:
                dialog.close()
            win.close()

    def test_simulated_scan_end_to_end(self):
        """Real coordinator manifest (simulated camera + monochromator) through the dialog."""
        from acquisition.coordinator import ScanCoordinator
        from devices.asi_camera import ASICameraClient
        from devices.monochromator import MonochromatorController, SimulationState
        from gui.camera_review import CameraReview
        sys.path.insert(0, str(BASE / "tests"))
        from test_scan import camera_params
        mono = MonochromatorController(SimulationState())
        mono.connect("SIMULATED")
        cam = ASICameraClient(simulate=True, log_path=self.tmp / "camera.log")
        try:
            session = ScanCoordinator(mono, None, camera_params(start=500, stop=520, backward=True),
                                      self.tmp / "data" / "session", cam).run()
        finally:
            cam.close()
        dialog = CameraReview(session.root)
        self.assertEqual(len(dialog.rows), len(session.manifest["points"]))
        self.assertTrue(all(np.isfinite(r["a_mean"]) for r in dialog.rows))
        self.assertGreater(len(ra.forward_backward(dialog.rows)), 0)
        dialog.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
