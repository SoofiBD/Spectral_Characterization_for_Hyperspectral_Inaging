"""Read-only review of verified camera scans: common-scale frames, two ROIs, ratio, export.

Calculations live in analysis.roi_analysis. Raw frames are memory-mapped read-only.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout)

from analysis import roi_analysis as ra

COLORS = {"a": "#24b4ff", "b": "#ffb547", "ratio": "#d983d9", "flag": "#ff4d4d"}

class RunPicker(QDialog):
    """List run manifests from the data folder, with a file picker for other locations."""
    COLUMNS = ("Run", "Created (UTC)", "Status", "Kind", "Channels", "Points", "Camera", "Range (nm)",
               "Directions", "Camera dark")

    def __init__(self, data_dir: Path, parent=None, kind: str | None = None, current: Path | None = None,
                 title: str = "Choose run", camera_only: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 460)
        self.data_dir, self.kind, self.selected = Path(data_dir), kind, None
        self.current = Path(current).resolve() if current else None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Runs in {self.data_dir}" + (f" — kind: {kind}" if kind else "")))
        self.camera_only = QCheckBox("Only camera runs with a camera dark", checked=camera_only)
        self.camera_only.toggled.connect(self.refresh)
        layout.addWidget(self.camera_only)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self.accept)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        browse = buttons.addButton("Browse…", QDialogButtonBox.ActionRole)
        browse.clicked.connect(self._browse)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self):
        runs = ra.list_runs(self.data_dir)
        if self.kind:
            runs = [r for r in runs if r["kind"] == self.kind or r["error"]]
        if self.camera_only.isChecked():
            runs = [r for r in runs if r["camera_points"] and r["camera_dark"]]
        self.runs = runs
        self.table.setRowCount(0)
        for r in runs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            span = "" if r["range_nm"] is None else f"{r['range_nm'][0]:g}–{r['range_nm'][1]:g}"
            status = f"unreadable: {r['error']}" if r["error"] else r["status"]
            values = (r["name"], (r["created_utc"] or "")[:19], status, r["kind"] or "", "+".join(r["channels"]),
                      r["points"], r["camera_points"], span, "+".join(r["directions"]),
                      "yes" if r["camera_dark"] else "no")
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        rows = [i for i, r in enumerate(runs) if self.current is not None and Path(r["path"]).resolve() == self.current]
        if rows or runs:
            self.table.selectRow(rows[0] if rows else 0)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose run folder", str(self.data_dir))
        if path:
            self.selected = Path(path)
            super().accept()

    def accept(self):
        row = self.table.currentRow()
        if self.selected is None and 0 <= row < len(self.runs):
            if self.runs[row]["error"]:
                QMessageBox.warning(self, "Unreadable run", self.runs[row]["error"])
                return
            self.selected = Path(self.runs[row]["path"])
        if self.selected is not None:
            super().accept()

    @classmethod
    def choose(cls, data_dir, parent=None, **kw) -> Path | None:
        dialog = cls(data_dir, parent, **kw)
        return dialog.selected if dialog.exec() == QDialog.Accepted else None

class CameraReview(QDialog):
    DISPLAY_PIXELS = 1000  # display decimation target; ROI coordinates map back to full-resolution pixels

    def __init__(self, root: Path, parent=None, temperature_tolerance_c: float = 2.0,
                 white_root: Path | None = None):
        super().__init__(parent)
        self.run = ra.load_camera_run(Path(root), temperature_tolerance_c)
        self.root = self.run.root
        self.SCALE = max(1, math.ceil(max(self.run.shape) / self.DISPLAY_PIXELS))
        self.white, white_note = None, ""
        if white_root:
            try:
                self.white = ra.load_camera_run(Path(white_root), temperature_tolerance_c)
                ra.check_white(self.run, self.white)
                missing = {(p["direction"], p["target_nm"]) for p in self.run.points} - {
                    (p["direction"], p["target_nm"]) for p in self.white.points}
                if missing:
                    raise ValueError(f"it lacks {len(missing)} sample wavelength/direction pair(s)")
                white_note = f"White reference: {self.white.root.name}"
            except Exception as exc:
                self.white, white_note = None, f"White reference not used: {exc}"
        self.setWindowTitle(f"Camera scan review — {self.root.name}")
        self.resize(1180, 920)
        layout = QVBoxLayout(self)
        notes = [f"Dark-subtracted signal of stored RAW16 frames. Display is decimated {self.SCALE}×; "
                 "ROI statistics use every pixel."]
        if self.run.problems:
            notes.append(f"Only verified frames are used; {len(self.run.problems)} recovery warning(s).")
        if self.run.max_intensity is None:
            notes.append("Camera saturation level unknown: saturation is not checked.")
        if white_note:
            notes.append(white_note)
        self.info = QLabel("\n".join(notes))
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        controls = QHBoxLayout()
        self.choice = QComboBox()
        for point in self.run.points:
            self.choice.addItem(f"{point['target_nm']:g} nm  {point['direction']}")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, len(self.run.points) - 1)
        self.mode = QComboBox()
        self.mode.addItem("Dark-subtracted counts", "counts")
        if self.white is not None:
            self.mode.addItem("White-corrected (relative reflectance)", "white")
        load_btn, preset_btn, save_btn = (QPushButton(t) for t in
                                          ("Load ROI…", "Save ROI preset…", "Export analysis…"))
        controls.addWidget(QLabel("Frame:"))
        controls.addWidget(self.choice)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.mode)
        for b in (load_btn, preset_btn, save_btn):
            controls.addWidget(b)
        layout.addLayout(controls)
        self.frame_lbl = QLabel()
        layout.addWidget(self.frame_lbl)

        self.image_plot = pg.PlotWidget()
        self.image_plot.setAspectLocked(True)
        self.image_plot.invertY(True)  # same orientation as the Camera tab
        self.image = pg.ImageItem(axisOrder="row-major")
        self.image_plot.addItem(self.image)
        h, w = self.run.shape
        dh, dw = h / self.SCALE, w / self.SCALE
        self.rois = []
        for name, x in (("a", dw * .3), ("b", dw * .6)):
            roi = pg.RectROI((x, dh * .4), (dw * .1, dh * .1),
                             pen=pg.mkPen(COLORS[name], width=2), movable=True, resizable=True,
                             maxBounds=QRectF(0, 0, dw, dh))
            self.image_plot.addItem(roi)
            pg.TextItem(name.upper(), color=COLORS[name], anchor=(0, 1)).setParentItem(roi)
            roi.sigRegionChangeFinished.connect(self.update_curves)
            self.rois.append(roi)
        layout.addWidget(self.image_plot, 3)
        self.signal_plot = pg.PlotWidget(title="A (blue) and B (orange): mean ± s.e.   ○ forward  △ backward")
        self.signal_plot.setLabel("bottom", "Set wavelength (nm)")
        self.ratio_plot = pg.PlotWidget(title="A / B   (red symbol: flagged point)")
        self.ratio_plot.setLabel("bottom", "Set wavelength (nm)")
        for plot in (self.signal_plot, self.ratio_plot):
            plot.getAxis("left").enableAutoSIPrefix(False)
            layout.addWidget(plot, 1)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.warning)

        self.choice.currentIndexChanged.connect(self.show_frame)
        self.choice.currentIndexChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self.choice.setCurrentIndex)
        self.mode.currentIndexChanged.connect(self.update_curves)
        load_btn.clicked.connect(self.load_roi)
        preset_btn.clicked.connect(self.save_preset)
        save_btn.clicked.connect(self.save)
        # One common display scale for all frames (display only; sampled every 32 px).
        top = [np.percentile(np.asarray(f[::32, ::32], dtype=np.float32) - self.run.dark[::32, ::32], 99.5)
               for f in self.run.frames]
        self.levels = (0.0, max(1.0, float(max(top))))
        self.rows: list[dict] = []
        self.show_frame(0)
        self.update_curves()

    def show_frame(self, index: int):
        if not 0 <= index < len(self.run.frames):
            return
        frame = np.asarray(self.run.frames[index][::self.SCALE, ::self.SCALE], dtype=np.float32)
        self.image.setImage(frame - self.run.dark[::self.SCALE, ::self.SCALE], autoLevels=False, levels=self.levels)
        p = self.run.points[index]
        cam = p["camera"]
        reported = p.get("reported_nm")
        self.frame_lbl.setText(
            f"#{p['index']}  {p['direction']}  set {p['target_nm']:g} nm"
            + (f"  (reported {reported:g} nm)" if reported is not None else "")
            + f"   T {cam.get('temperature_c')} °C   saturated px in frame: {cam.get('saturated_pixels')}"
            + f"   common display scale 0–{self.levels[1]:.0f} counts")

    def roi_box(self, roi) -> ra.Box:
        x, y = roi.pos()
        w, h = roi.size()
        height, width = self.run.shape
        x0, y0 = max(0, int(round(x * self.SCALE))), max(0, int(round(y * self.SCALE)))
        x1 = min(width, int(round((x + w) * self.SCALE)))
        y1 = min(height, int(round((y + h) * self.SCALE)))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("Each selected region must contain pixels inside the image")
        return x0, y0, x1, y1

    def boxes(self):
        return tuple(self.roi_box(roi) for roi in self.rois)

    def set_boxes(self, boxes):
        for roi, (x0, y0, x1, y1) in zip(self.rois, ra.check_boxes(boxes, self.run.shape)):
            roi.setPos((x0 / self.SCALE, y0 / self.SCALE), update=False, finish=False)
            roi.setSize(((x1 - x0) / self.SCALE, (y1 - y0) / self.SCALE), finish=False)
        self.update_curves()

    @property
    def white_mode(self) -> bool:
        return self.mode.currentData() == "white"

    def update_curves(self):
        self.signal_plot.clear()
        self.ratio_plot.clear()
        try:
            self.rows = ra.analyze(self.run, self.boxes(), self.white)
        except ValueError as exc:
            self.rows = []
            self.warning.setText(str(exc))
            return
        white = self.white_mode
        a, b = ("a_reflectance", "b_reflectance") if white else ("a_mean", "b_mean")
        r = "reflectance_ratio_a_b" if white else "ratio_a_b"
        self.signal_plot.setLabel("left", "Relative reflectance" if white else "Counts/px")
        self.ratio_plot.setLabel("left", "A / B" + (" (white-corrected)" if white else ""))
        for direction, symbol in (("forward", "o"), ("backward", "t")):
            subset = sorted((x for x in self.rows if x["direction"] == direction), key=lambda x: x["target_nm"])
            if not subset:
                continue
            nm = np.array([x["target_nm"] for x in subset], dtype=float)
            flagged = [bool(x["flags"]) for x in subset]
            for key, plot, color in ((a, self.signal_plot, COLORS["a"]), (b, self.signal_plot, COLORS["b"]),
                                     (r, self.ratio_plot, COLORS["ratio"])):
                y = np.array([x[key] for x in subset], dtype=float)
                err = np.array([x[key.removesuffix("_mean") + "_sem"] for x in subset], dtype=float)
                brushes = [COLORS["flag"] if f else color for f in flagged]
                plot.plot(nm, y, pen=pg.mkPen(color), symbol=symbol, symbolBrush=brushes, connect="finite")
                ok = np.isfinite(y) & np.isfinite(err)
                if ok.any():
                    plot.addItem(pg.ErrorBarItem(x=nm[ok], y=y[ok], height=2 * err[ok], pen=pg.mkPen(color)))
        pairs = ra.forward_backward(self.rows, r)
        s = ra.summary(self.rows, pairs, r)

        def pct(v):
            return "n/a" if v is None else f"{100 * v:.2f}%"
        z = s["max_abs_ratio_z"]
        text = [f"{s['points']} points; {s['points_with_flags']} flagged ({s['saturated_points']} with "
                f"saturation, {s['invalid_ratio_points']} without a valid ratio)."]
        if pairs:
            text.append(f"Forward/backward at {len(pairs)} wavelengths: max |Δ| A {pct(s['max_abs_rel_diff_a'])}, "
                        f"B {pct(s['max_abs_rel_diff_b'])}; max |z| of the ratio "
                        f"{'n/a' if z is None else f'{z:.1f}'} (|z| > 3: difference exceeds pixel scatter).")
        flagged = [f"{x['target_nm']:g} {x['direction']}: {x['flags']}" for x in self.rows if x["flags"]]
        if flagged:
            text.append("Flags: " + " | ".join(flagged[:6]) + (" …" if len(flagged) > 6 else ""))
        self.warning.setText("\n".join(text))

    def load_roi(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load ROI preset or roi.json", str(self.root.parent),
                                              "ROI (*.json)")
        if not path:
            return
        try:
            self.set_boxes(ra.load_preset(Path(path), self.run))
        except Exception as exc:
            QMessageBox.critical(self, "Cannot use ROI", str(exc))

    def save_preset(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save ROI preset", str(self.root.parent / "roi_preset.json"),
                                              "ROI (*.json)")
        if not path:
            return
        try:
            ra.save_preset(Path(path), self.run, self.boxes())
        except Exception as exc:
            QMessageBox.critical(self, "Cannot save ROI", str(exc))

    def export_dir(self) -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return self.root.parent / f"{self.root.name}_roi_{stamp}"

    def save(self) -> Path | None:
        def plots(tmp: Path):
            for name, widget in (("roi_signals", self.signal_plot), ("roi_ratio", self.ratio_plot),
                                 ("roi_frame", self.image_plot)):
                pg.exporters.ImageExporter(widget.plotItem).export(str(tmp / f"{name}.png"))
        try:
            out = ra.export(self.run, self.boxes(), self.export_dir(), self.white, extra=plots)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return None
        QMessageBox.information(self, "Saved", f"Analysis saved to {out}")
        return out
